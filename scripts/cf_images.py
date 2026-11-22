#!/usr/bin/env python3
"""Cloudflare Workers AI image generation (FLUX.2 klein 4B) for DKORWORLD2.

Safe by default: dry-run does no network and writes no files. A live request needs
dry_run=False AND CF_ALLOW_LIVE_GENERATION=1 in the local environment / .env.
Logging goes to stderr only; secrets are never logged or put into error messages.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from redact import proxy_password, redact, safe_proxy_label

log = logging.getLogger("dkor.cf_images")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
API_BASE = "https://api.cloudflare.com/client/v4/accounts"
WIDTH, HEIGHT = 1024, 576
TIMEOUT_SECONDS = 120.0
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # base64 JSON is ~4/3 of the image
MAX_ERROR_BODY_BYTES = 64 * 1024
MIN_IMAGE_BYTES = 64
MAX_PROMPT_CHARS = 2000

ALLOWED_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_EXT_TO_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}(\.[A-Za-z0-9]{2,4})?$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MODEL_RE = re.compile(r"^@cf/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class CloudflareImageError(Exception):
    """Expected, user-facing failure. The message must already be free of secrets."""


@dataclass(frozen=True)
class Settings:
    account_id: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    proxy_url: str = field(default="", repr=False)
    allow_live: bool = False

    def secrets(self) -> list[str]:
        return [s for s in (self.api_token, self.proxy_url, proxy_password(self.proxy_url)) if s]


def parse_dotenv(path: Path | None) -> dict[str, str]:
    """Minimal KEY=VALUE parser. Returns {} if the file is absent. Never logs values."""
    values: dict[str, str] = {}
    if path is None or not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        values[key.strip()] = raw
    return values


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
    """Real process environment wins over .env. SDK 2.x does not read .env itself."""
    merged = {**parse_dotenv(env_file), **dict(os.environ if environ is None else environ)}
    proxy = (merged.get("PROXY_URL") or "").strip()
    if proxy and urlsplit_scheme(proxy) not in ("http", "https"):
        raise CloudflareImageError("PROXY_URL: поддерживаются только схемы http:// и https://.")
    model = (merged.get("CF_IMAGE_MODEL") or DEFAULT_MODEL).strip()
    if not _MODEL_RE.fullmatch(model):
        raise CloudflareImageError("CF_IMAGE_MODEL имеет недопустимый формат (ожидается @cf/<автор>/<модель>).")
    return Settings(
        account_id=(merged.get("CF_ACCOUNT_ID") or "").strip(),
        api_token=(merged.get("CF_API_TOKEN") or "").strip(),
        model=model,
        proxy_url=proxy,
        allow_live=(merged.get("CF_ALLOW_LIVE_GENERATION") or "").strip() == "1",
    )


def urlsplit_scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


# ---------- input validation ----------

def validate_prompt(prompt: object) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise CloudflareImageError("prompt должен быть непустой строкой.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise CloudflareImageError(f"prompt длиннее {MAX_PROMPT_CHARS} символов.")
    return prompt.strip()


def validate_seed(seed: object) -> int | None:
    if seed is None:
        return None
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise CloudflareImageError("seed должен быть неотрицательным целым числом.")
    return seed


def validate_filename(filename: object) -> tuple[str, str | None]:
    """Return (stem, extension-or-None). Only a bare file name is accepted."""
    if not isinstance(filename, str) or not _FILENAME_RE.fullmatch(filename):
        raise CloudflareImageError(
            "filename: только имя файла (латиница, цифры, _ и -), без путей, пробелов и «..»; "
            "расширение необязательно (.png, .jpg, .jpeg, .webp)."
        )
    stem, dot, ext = filename.partition(".")
    if stem.lower() in _RESERVED:
        raise CloudflareImageError("filename: зарезервированное имя Windows.")
    if dot:
        if ("." + ext.lower()) not in _EXT_TO_MIME:
            raise CloudflareImageError("filename: допустимые расширения — .png, .jpg, .jpeg, .webp.")
        return stem, "." + ext.lower()
    return stem, None


def resolve_assets_dir(assets_dir: Path | None = None) -> Path:
    """The assets directory must exist and must not be a symlink / junction."""
    base = assets_dir or (PROJECT_ROOT / "assets")
    if base.is_symlink() or base.is_junction():
        raise CloudflareImageError("Папка assets/ не должна быть символьной ссылкой или junction.")
    if not base.is_dir():
        raise CloudflareImageError("Папка assets/ не найдена в проекте.")
    return base.resolve()


def target_path(assets: Path, stem: str, ext: str) -> Path:
    path = assets / (stem + ext)
    if os.path.lexists(path):  # also catches symlinks, even dangling ones
        raise CloudflareImageError(f"Файл assets/{path.name} уже существует; перезапись запрещена.")
    if path.resolve().parent != assets:
        raise CloudflareImageError("Путь выходит за пределы assets/.")
    return path


def _planned_name(stem: str, ext: str | None) -> str:
    return stem + (ext or ".<png|jpg|webp по ответу>")


# ---------- response handling ----------

def detect_image(data: bytes) -> str | None:
    """Detect PNG / JPEG / WebP by content, with light structural checks."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 33:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png" if width > 0 and height > 0 else None
    if data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _mime_of(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _api_error_text(body: bytes, secrets: list[str]) -> str:
    try:
        errors = json.loads(body.decode("utf-8")).get("errors") or []
        parts = [f"{e.get('code', '?')}: {str(e.get('message', ''))[:200]}" for e in errors[:3] if isinstance(e, dict)]
        return redact("; ".join(parts), secrets)
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ""


def parse_image_response(status: int, content_type: str | None, body: bytes, secrets: list[str] | None = None) -> tuple[bytes, str]:
    """Return (image_bytes, mime) or raise CloudflareImageError. Nothing is written here."""
    secrets = secrets or []
    if status in (401, 403):
        raise CloudflareImageError(f"Cloudflare отклонил доступ (HTTP {status}): проверьте CF_API_TOKEN и права Workers AI.")
    if status >= 400:
        detail = _api_error_text(body, secrets)
        raise CloudflareImageError(f"Cloudflare вернул ошибку HTTP {status}" + (f" ({detail})." if detail else "."))
    if status != 200:
        raise CloudflareImageError(f"Неожиданный HTTP-статус {status}.")

    mime = _mime_of(content_type)
    declared: str | None = None
    if mime == "application/json":
        try:
            doc = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CloudflareImageError("Ответ Cloudflare не является корректным JSON.") from None
        if isinstance(doc, dict) and doc.get("success") is False:
            detail = _api_error_text(body, secrets)
            raise CloudflareImageError("Cloudflare сообщил об ошибке" + (f" ({detail})." if detail else "."))
        payload = doc.get("result", doc) if isinstance(doc, dict) else None  # API envelope is optional
        encoded = payload.get("image") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise CloudflareImageError("В JSON-ответе нет строки с изображением (поле image).")
        if encoded.startswith("data:"):
            head, _, encoded = encoded.partition(",")
            declared = _mime_of(head[5:].split(";")[0])
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise CloudflareImageError("Изображение в ответе не является корректным Base64.") from None
    elif mime.startswith("image/"):
        declared, data = mime, body
    else:
        raise CloudflareImageError(f"Неожиданный Content-Type ответа: {mime or 'не указан'}.")

    if declared is not None and declared not in ALLOWED_MIME:
        raise CloudflareImageError(f"Недопустимый MIME-тип изображения: {declared}.")
    if len(data) < MIN_IMAGE_BYTES:
        raise CloudflareImageError("Изображение слишком маленькое или пустое.")
    if len(data) > MAX_IMAGE_BYTES:
        raise CloudflareImageError(f"Изображение больше {MAX_IMAGE_BYTES // (1024 * 1024)} МБ.")
    detected = detect_image(data)
    if detected is None:
        raise CloudflareImageError("Содержимое ответа не похоже на PNG, JPEG или WebP.")
    if declared is not None and declared != detected:
        raise CloudflareImageError("Заявленный MIME-тип не совпадает с содержимым изображения.")
    return data, detected


def _read_limited(resp: httpx.Response, limit: int, truncate: bool = False) -> bytes:
    declared = resp.headers.get("content-length")
    if not truncate and declared and declared.isdigit() and int(declared) > limit:
        raise CloudflareImageError("Ответ Cloudflare слишком большой.")
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > limit:
            if truncate:
                break
            raise CloudflareImageError("Ответ Cloudflare слишком большой.")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------- HTTP ----------

def make_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(TIMEOUT_SECONDS), "follow_redirects": False}
    if settings.proxy_url:
        kwargs["proxy"] = settings.proxy_url
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def request_image(settings: Settings, prompt: str, seed: int | None, transport: httpx.BaseTransport | None = None) -> tuple[bytes, str]:
    if not settings.account_id or not settings.api_token:
        raise CloudflareImageError("Не заданы CF_ACCOUNT_ID и/или CF_API_TOKEN в локальном окружении.")
    if not _ACCOUNT_RE.fullmatch(settings.account_id):
        raise CloudflareImageError("CF_ACCOUNT_ID имеет недопустимый формат.")
    url = f"{API_BASE}/{settings.account_id}/ai/run/{settings.model}"
    # (None, value) makes httpx send plain multipart/form-data fields.
    fields: dict[str, tuple[None, str]] = {
        "prompt": (None, prompt),
        "width": (None, str(WIDTH)),
        "height": (None, str(HEIGHT)),
    }
    if seed is not None:
        fields["seed"] = (None, str(seed))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    secrets = settings.secrets()
    log.info("Cloudflare request: model=%s size=%dx%d proxy=%s", settings.model, WIDTH, HEIGHT, safe_proxy_label(settings.proxy_url))
    try:
        with make_client(settings, transport) as client:
            with client.stream("POST", url, headers=headers, files=fields) as resp:
                status, ctype = resp.status_code, resp.headers.get("content-type")
                body = _read_limited(resp, MAX_ERROR_BODY_BYTES if status >= 400 else MAX_RESPONSE_BYTES, truncate=status >= 400)
    except httpx.TimeoutException:
        raise CloudflareImageError(f"Тайм-аут запроса к Cloudflare ({int(TIMEOUT_SECONDS)} с).") from None
    except httpx.ProxyError:
        raise CloudflareImageError("Ошибка прокси при подключении к Cloudflare.") from None
    except httpx.HTTPError as exc:  # message may contain hosts/credentials: report the class only
        raise CloudflareImageError(f"Сетевая ошибка при обращении к Cloudflare ({type(exc).__name__}).") from None
    return parse_image_response(status, ctype, body, secrets)


# ---------- public entry point used by the MCP tool ----------

def run_generate_image(
    prompt: str,
    filename: str,
    seed: int | None = None,
    dry_run: bool = True,
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    assets_dir: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    prompt = validate_prompt(prompt)
    seed = validate_seed(seed)
    stem, ext = validate_filename(filename)
    settings = load_settings(environ, env_file)
    assets = resolve_assets_dir(assets_dir)
    if ext is not None:
        target_path(assets, stem, ext)  # fail early, before any network use

    if dry_run:
        return {
            "mode": "dry_run",
            "network_request": False,
            "file_written": False,
            "model": settings.model,
            "endpoint": f"{API_BASE}/<CF_ACCOUNT_ID>/ai/run/{settings.model}",
            "request_format": "multipart/form-data",
            "fields": {"prompt_chars": len(prompt), "width": WIDTH, "height": HEIGHT, "seed": seed},
            "timeout_seconds": int(TIMEOUT_SECONDS),
            "connection": safe_proxy_label(settings.proxy_url),
            "planned_image_path": f"assets/{_planned_name(stem, ext)}",
            "live_generation_enabled": settings.allow_live,
            "credentials_configured": bool(settings.account_id and settings.api_token),
            "note": "Для настоящей генерации нужны dry_run=false и CF_ALLOW_LIVE_GENERATION=1 в локальном окружении.",
        }

    if not settings.allow_live:
        raise CloudflareImageError("Живая генерация выключена: установите CF_ALLOW_LIVE_GENERATION=1 в локальном окружении.")
    data, mime = request_image(settings, prompt, seed, transport)
    final_ext = ALLOWED_MIME[mime]
    if ext is not None and _EXT_TO_MIME[ext] != mime:
        raise CloudflareImageError(f"Расширение {ext} не соответствует полученному типу {mime}; файл не сохранён.")
    path = target_path(assets, stem, ext or final_ext)
    try:
        with open(path, "xb") as fh:  # exclusive create: never overwrites
            fh.write(data)
    except FileExistsError:
        raise CloudflareImageError(f"Файл assets/{path.name} уже существует; перезапись запрещена.") from None
    except OSError as exc:
        raise CloudflareImageError(f"Не удалось сохранить файл ({type(exc).__name__}).") from None
    log.info("Saved image: assets/%s (%d bytes, %s)", path.name, len(data), mime)
    return {
        "mode": "live",
        "network_request": True,
        "file_written": True,
        "image_path": f"assets/{path.name}",
        "mime_type": mime,
        "size_bytes": len(data),
        "model": settings.model,
        "seed": seed,
    }
