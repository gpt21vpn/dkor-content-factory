#!/usr/bin/env python3
"""Telegram publisher (Bot API sendPhoto) for the private DKORWORLD2 TEST channel.

Safe by default:
  * `dry-run` (default) never reads .env and never touches the network.
  * The only allowed recipient is hard-coded (ALLOWED_CHAT_ID); TG_CHANNEL_ID from .env may
    only confirm it. Only getMe / getChat / sendPhoto can be called (no getUpdates, no webhook).
  * A real send needs ALL of: subcommand `send`, TG_ALLOW_LIVE_SEND=1 in the local environment,
    and --confirm <fingerprint> printed by dry-run for this exact caption + image.
  * A local ledger (temp/tg_sent.json) blocks duplicates, also after an ambiguous failure.
Secrets are never printed or logged; errors are redacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import struct
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

import validate_post
from cf_images import detect_image, parse_dotenv, urlsplit_scheme
from redact import proxy_password, redact, safe_proxy_label

log = logging.getLogger("dkor.tg_publisher")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_CHAT_ID = -1001234567890  # DKORWORLD2 TEST (private). Never change without a new owner decision.
API_BASE = "https://api.telegram.org"
ALLOWED_METHODS = frozenset({"getMe", "getChat", "sendPhoto"})
CAPTION_LIMIT = 1024  # Telegram hard limit for sendPhoto
CAPTION_TARGET_MIN, CAPTION_TARGET_MAX = 600, 1000  # project voice rules (CLAUDE.md)
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_DIMENSION_SUM = 10000
TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 256 * 1024
LEDGER_PATH = PROJECT_ROOT / "temp" / "tg_sent.json"
_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
_HASHTAG_RE = re.compile(r"(?<!\w)#\w")
_PUBLISHABLE_STATUSES = {"draft", "approved"}


class TelegramPublishError(Exception):
    """Expected, user-facing failure. The message must already be free of secrets.

    definite=True means Telegram answered with an explicit refusal, so nothing was posted.
    """

    def __init__(self, message: str, definite: bool = False):
        super().__init__(message)
        self.definite = definite


# ---------- settings ----------

def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> dict[str, str]:
    """Real environment wins over .env. Returns raw strings; nothing is logged."""
    merged = {**parse_dotenv(env_file), **dict(os.environ if environ is None else environ)}
    return {key: (merged.get(key) or "").strip() for key in ("TG_BOT_TOKEN", "TG_CHANNEL_ID", "TG_PROXY_URL", "TG_ALLOW_LIVE_SEND")}


def check_target(settings: Mapping[str, str]) -> None:
    """TG_CHANNEL_ID may only confirm the hard-coded recipient, never replace it."""
    configured = settings.get("TG_CHANNEL_ID", "")
    if configured and configured != str(ALLOWED_CHAT_ID):
        raise TelegramPublishError("TG_CHANNEL_ID не совпадает с разрешённым тестовым каналом; отправка запрещена.")


def require_token(settings: Mapping[str, str]) -> str:
    token = settings.get("TG_BOT_TOKEN", "")
    if not token:
        raise TelegramPublishError("Не задан TG_BOT_TOKEN в локальном окружении.")
    if not _TOKEN_RE.fullmatch(token):
        raise TelegramPublishError("TG_BOT_TOKEN имеет недопустимый формат.")
    return token


def proxy_of(settings: Mapping[str, str]) -> str:
    proxy = settings.get("TG_PROXY_URL", "")
    if proxy and urlsplit_scheme(proxy) not in ("http", "https"):
        raise TelegramPublishError("TG_PROXY_URL: поддерживаются только схемы http:// и https://.")
    return proxy


# ---------- post / image preparation (offline) ----------

def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) from the first SOFn marker, or None if the structure is broken."""
    if data[:2] != b"\xff\xd8":
        return None
    pos, end = 2, len(data)
    while pos + 4 <= end:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if pos + 9 > end:
                return None
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return (width, height) if width and height else None
        pos += 2 + length
    return None


def resolve_image(image_path: object, root: Path) -> Path:
    """Only a regular, non-symlink file directly inside <root>/assets is accepted."""
    if not isinstance(image_path, str) or not image_path.strip():
        raise TelegramPublishError("В посте нет image_path.")
    rel = Path(image_path)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2 or rel.parts[0] != "assets":
        raise TelegramPublishError("image_path должен иметь вид assets/<имя>: пути вне assets/ запрещены.")
    assets = root / "assets"
    if assets.is_symlink() or assets.is_junction():
        raise TelegramPublishError("Папка assets/ не должна быть символьной ссылкой или junction.")
    path = assets / rel.parts[1]
    if path.is_symlink() or not path.is_file():
        raise TelegramPublishError(f"Файл {image_path} не найден или является ссылкой.")
    if path.resolve().parent != assets.resolve():
        raise TelegramPublishError("Путь изображения выходит за пределы assets/.")
    return path


def prepare(post_file: Path, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Validate the post and the JPEG; return everything needed for dry-run / send. No network."""
    try:
        doc = json.loads(post_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TelegramPublishError("Не удалось прочитать JSON-пост.") from None
    errors = validate_post.validate(doc)
    if errors:
        raise TelegramPublishError("Пост не проходит валидатор: " + "; ".join(errors[:3]))
    if doc.get("status") not in _PUBLISHABLE_STATUSES:
        raise TelegramPublishError(f"Статус поста {doc.get('status')!r} не допускает отправку (нужен draft или approved).")
    caption = doc["platforms"]["telegram"]["content"]
    if len(caption) > CAPTION_LIMIT:
        raise TelegramPublishError(f"Подпись {len(caption)} символов; лимит Telegram {CAPTION_LIMIT}.")
    if not CAPTION_TARGET_MIN <= len(caption) <= CAPTION_TARGET_MAX:
        raise TelegramPublishError(f"Подпись {len(caption)} символов; по правилам проекта нужно {CAPTION_TARGET_MIN}–{CAPTION_TARGET_MAX}.")
    if _HASHTAG_RE.search(caption):
        raise TelegramPublishError("В подписи не должно быть хештегов.")

    image = resolve_image(doc.get("image_path"), root)
    data = image.read_bytes()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise TelegramPublishError("Изображение пустое или больше 10 МБ (лимит sendPhoto).")
    if detect_image(data) != "image/jpeg":
        raise TelegramPublishError("Изображение не является корректным JPEG.")
    size = jpeg_size(data)
    if size is None:
        raise TelegramPublishError("Не удалось прочитать размеры JPEG.")
    if sum(size) > MAX_PHOTO_DIMENSION_SUM:
        raise TelegramPublishError("Сумма ширины и высоты изображения больше 10000 (лимит sendPhoto).")

    image_sha = hashlib.sha256(data).hexdigest()
    content_hash = hashlib.sha256(f"{ALLOWED_CHAT_ID}\n{caption}\n{image_sha}".encode("utf-8")).hexdigest()
    return {
        "post_file": post_file.name,
        "chat_id": ALLOWED_CHAT_ID,
        "caption": caption,
        "caption_chars": len(caption),
        "image_path": str(doc["image_path"]),
        "image_bytes": len(data),
        "image_size": f"{size[0]}x{size[1]}",
        "content_hash": content_hash,
        "fingerprint": content_hash[:12],
        "_image_file": image,
        "_image_data": data,
    }


def dry_run_report(prepared: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": "dry_run",
        "network_request": False,
        "env_read": False,
        "chat_id": prepared["chat_id"],
        "post_file": prepared["post_file"],
        "caption_chars": prepared["caption_chars"],
        "caption": prepared["caption"],
        "image_path": prepared["image_path"],
        "image_bytes": prepared["image_bytes"],
        "image_size": prepared["image_size"],
        "fingerprint": prepared["fingerprint"],
        "api_method": "sendPhoto",
        "note": "Для отправки нужны: TG_ALLOW_LIVE_SEND=1, команда send и --confirm с этим fingerprint.",
    }


# ---------- ledger (duplicate protection) ----------

def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TelegramPublishError("Журнал отправок повреждён; отправка остановлена до ручной проверки.") from None
    if not isinstance(data, dict):
        raise TelegramPublishError("Журнал отправок повреждён; отправка остановлена до ручной проверки.")
    return data


def _write_ledger(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check_not_sent(content_hash: str, ledger_path: Path) -> None:
    entry = _read_ledger(ledger_path).get(content_hash)
    if entry:
        if entry.get("state") == "sent":
            raise TelegramPublishError(f"Этот пост уже отправлен (message_id={entry.get('message_id')}); повторная отправка запрещена.")
        raise TelegramPublishError("Предыдущая отправка завершилась неопределённо: проверьте канал вручную и удалите запись из temp/tg_sent.json.")


# ---------- Telegram HTTP ----------

def make_client(proxy: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(TIMEOUT_SECONDS), "follow_redirects": False}
    if proxy:
        kwargs["proxy"] = proxy
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def call_api(token: str, method: str, proxy: str = "", *, data: Mapping[str, str] | None = None,
             files: Mapping[str, Any] | None = None, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """One request, no retries. Returns `result` of an ok response or raises TelegramPublishError."""
    if method not in ALLOWED_METHODS:
        raise TelegramPublishError(f"Метод Bot API {method!r} не разрешён в этом модуле.")
    url = f"{API_BASE}/bot{token}/{method}"
    secrets = [s for s in (token, proxy, proxy_password(proxy)) if s]
    log.info("Telegram request: method=%s proxy=%s", method, safe_proxy_label(proxy))
    try:
        with make_client(proxy, transport) as client:
            with client.stream("POST", url, data=dict(data or {}), files=dict(files or {})) as resp:
                status = resp.status_code
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.TimeoutException:
        raise TelegramPublishError(f"Тайм-аут запроса к Telegram ({int(TIMEOUT_SECONDS)} с); результат неизвестен.") from None
    except httpx.ProxyError:
        raise TelegramPublishError("Ошибка прокси при подключении к Telegram.") from None
    except httpx.HTTPError as exc:  # message may contain the URL with the token: report the class only
        raise TelegramPublishError(f"Сетевая ошибка при обращении к Telegram ({type(exc).__name__}); результат неизвестен.") from None
    return _parse_response(status, body, secrets)


def _parse_response(status: int, body: bytes, secrets: list[str]) -> dict[str, Any]:
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        doc = None
    if not isinstance(doc, dict):
        raise TelegramPublishError(f"Ответ Telegram не является JSON (HTTP {status}).")
    if doc.get("ok") is True and status == 200 and isinstance(doc.get("result"), dict):
        return doc["result"]
    description = redact(str(doc.get("description", ""))[:200], secrets)
    params = doc.get("parameters") if isinstance(doc.get("parameters"), dict) else {}
    if status == 401:
        raise TelegramPublishError("Telegram отклонил токен (HTTP 401): проверьте TG_BOT_TOKEN.", definite=True)
    if status == 403:
        raise TelegramPublishError(f"Бот не имеет доступа к каналу (HTTP 403): {description}", definite=True)
    if status == 429:
        raise TelegramPublishError(f"Слишком много запросов (HTTP 429), повторить через {params.get('retry_after', '?')} с; автоповтор отключён.", definite=True)
    if status == 400:
        raise TelegramPublishError(f"Telegram отклонил запрос (HTTP 400): {description}", definite=True)
    raise TelegramPublishError(f"Telegram вернул ошибку HTTP {status}: {description}".rstrip(": "), definite=status < 500)


def check_access(settings: Mapping[str, str], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Read-only: getMe + getChat for the allowed chat only. Needs explicit owner approval to run."""
    check_target(settings)
    token, proxy = require_token(settings), proxy_of(settings)
    me = call_api(token, "getMe", proxy, transport=transport)
    chat = call_api(token, "getChat", proxy, data={"chat_id": str(ALLOWED_CHAT_ID)}, transport=transport)
    if chat.get("id") != ALLOWED_CHAT_ID:
        raise TelegramPublishError("getChat вернул другой chat_id; работа остановлена.")
    return {"bot_username": me.get("username"), "chat_id": chat.get("id"), "chat_type": chat.get("type"), "chat_title": chat.get("title")}


def send_post(prepared: Mapping[str, Any], settings: Mapping[str, str], confirm: str | None, *,
              ledger_path: Path = LEDGER_PATH, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Real send. All guards run BEFORE any network use; one request, no retries."""
    check_target(settings)
    if settings.get("TG_ALLOW_LIVE_SEND") != "1":
        raise TelegramPublishError("Живая отправка выключена: установите TG_ALLOW_LIVE_SEND=1 в локальном окружении.")
    if not confirm or confirm != prepared["fingerprint"]:
        raise TelegramPublishError("--confirm не совпадает с fingerprint этого поста (см. dry-run); отправка остановлена.")
    token, proxy = require_token(settings), proxy_of(settings)
    check_not_sent(prepared["content_hash"], ledger_path)

    ledger = _read_ledger(ledger_path)
    ledger[prepared["content_hash"]] = {"state": "pending", "chat_id": ALLOWED_CHAT_ID, "post_file": prepared["post_file"], "started_at": int(time.time())}
    _write_ledger(ledger_path, ledger)  # pending marker: any crash/timeout below blocks blind re-sends

    try:
        result = call_api(
            token, "sendPhoto", proxy,
            data={"chat_id": str(ALLOWED_CHAT_ID), "caption": prepared["caption"]},
            files={"photo": (Path(prepared["_image_file"]).name, prepared["_image_data"], "image/jpeg")},
            transport=transport,
        )
    except TelegramPublishError as exc:
        if exc.definite:  # explicit refusal: nothing was posted, so a retry is safe
            ledger.pop(prepared["content_hash"], None)
            _write_ledger(ledger_path, ledger)
        raise
    message_id = result.get("message_id")
    chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
    if not isinstance(message_id, int) or chat.get("id") != ALLOWED_CHAT_ID:
        raise TelegramPublishError("Ответ Telegram не подтверждает отправку в разрешённый канал; проверьте канал вручную.")
    ledger[prepared["content_hash"]] = {"state": "sent", "chat_id": ALLOWED_CHAT_ID, "post_file": prepared["post_file"], "message_id": message_id, "sent_at": int(time.time())}
    _write_ledger(ledger_path, ledger)
    return {"mode": "live", "network_request": True, "chat_id": ALLOWED_CHAT_ID, "message_id": message_id}


# ---------- CLI ----------

def _print(obj: Mapping[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Telegram publisher for the DKORWORLD2 TEST channel (safe by default).")
    sub = parser.add_subparsers(dest="command")
    p_dry = sub.add_parser("dry-run", help="показать подпись, путь JPG и chat_id; без сети и без .env")
    p_dry.add_argument("file")
    p_check = sub.add_parser("check-access", help="getMe + getChat (только чтение; нужен --yes)")
    p_check.add_argument("--yes", action="store_true", help="явное разрешение владельца на сетевой запрос")
    p_send = sub.add_parser("send", help="настоящая отправка sendPhoto")
    p_send.add_argument("file")
    p_send.add_argument("--confirm", help="fingerprint из dry-run")
    args = parser.parse_args(argv)
    command = args.command or "help"

    try:
        if command == "dry-run":
            _print(dry_run_report(prepare(Path(args.file))))
        elif command == "check-access":
            if not args.yes:
                raise TelegramPublishError("Проверка доступа делает сетевой запрос: добавьте --yes после разрешения владельца.")
            settings = load_settings(env_file=PROJECT_ROOT / ".env")
            _print(check_access(settings))
        elif command == "send":
            prepared = prepare(Path(args.file))
            settings = load_settings(env_file=PROJECT_ROOT / ".env")
            _print(send_post(prepared, settings, args.confirm))
        else:
            parser.print_help()
            return 2
    except TelegramPublishError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
