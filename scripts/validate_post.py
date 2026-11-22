#!/usr/bin/env python3
"""Validate DKORWORLD2 content JSON, using only Python standard library.

This tool validates a *draft*, it does NOT approve or publish content.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Project-side approximation of X's t.co URL weighting. Matches http(s) and bare
# domains (dkor.space), but avoids treating email addresses as post URLs.
URL_RE = re.compile(
    r"(?<![\w@])(?:https?://[^\s]+|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?:/[^\s]*)?)",
    re.IGNORECASE,
)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_TYPES = {"news", "own_case", "article", "video"}
ALLOWED_STATUS = {"draft", "approved", "published"}
TELEGRAM_MIN_CHARS = 600
TELEGRAM_MAX_CHARS = 1000
X_MAX_CHARS = 280


def x_weighted_length(text: str) -> int:
    """Approximate X length: each HTTP(S) or bare-domain URL counts as 23.

    This is a project-side guard, not a full implementation of X's text-parser.
    """
    return len(URL_RE.sub("x" * 23, text))


def validate(doc: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["Корень JSON должен быть объектом."]

    def required_map(parent: dict, field: str, label: str) -> dict:
        val = parent.get(field)
        if not isinstance(val, dict):
            errors.append(f"{label} должен быть объектом.")
            return {}
        return val

    def required_text(parent: dict, field: str, label: str, allow_empty: bool = False) -> str:
        val = parent.get(field)
        if not isinstance(val, str):
            errors.append(f"{label} должен быть строкой.")
            return ""
        if not allow_empty and not val.strip():
            errors.append(f"{label} не должен быть пустым.")
        return val

    if doc.get("schema_version") != "dkor-social/v1":
        errors.append("schema_version должен быть 'dkor-social/v1'.")
    status = doc.get("status")
    if status not in ALLOWED_STATUS:
        errors.append("status должен быть draft, approved или published.")

    source = required_map(doc, "source", "source")
    source_type = source.get("type")
    if source_type not in ALLOWED_TYPES:
        errors.append("source.type должен быть news, own_case, article или video.")
    required_text(source, "title", "source.title")
    for key in ("url", "author", "published_at"):
        if key not in source:
            errors.append(f"source.{key} обязателен (разрешено null).")
        elif source[key] is not None and not isinstance(source[key], str):
            errors.append(f"source.{key} должен быть строкой либо null.")
    if isinstance(source.get("published_at"), str):
        try:
            date.fromisoformat(source["published_at"])
        except ValueError:
            errors.append("source.published_at должен быть YYYY-MM-DD или null.")

    required_text(doc, "image_prompt", "image_prompt")
    if "image_path" not in doc:
        errors.append("image_path обязателен (используй null, пока нет картинки).")
    elif doc["image_path"] is not None:
        image_path = doc["image_path"]
        if not isinstance(image_path, str):
            errors.append("image_path должен быть null или строкой относительного пути.")
        else:
            normalized = image_path.replace("\\", "/")
            parts = normalized.split("/")
            if (
                not normalized.startswith(("assets/", "posts/"))
                or ".." in parts
                or normalized.startswith("/")
                or ":" in normalized
                or not normalized.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ):
                errors.append("image_path должен быть безопасным относительным путём к assets/ или posts/ и файлом изображения.")

    platforms = required_map(doc, "platforms", "platforms")
    telegram = required_map(platforms, "telegram", "platforms.telegram")
    tg_content = required_text(telegram, "content", "platforms.telegram.content")
    if tg_content and not TELEGRAM_MIN_CHARS <= len(tg_content) <= TELEGRAM_MAX_CHARS:
        errors.append(f"Telegram: длина {len(tg_content)}; нужно {TELEGRAM_MIN_CHARS}–{TELEGRAM_MAX_CHARS} символов.")
    x = required_map(platforms, "x", "platforms.x")
    x_content = required_text(x, "content", "platforms.x.content")
    if x_content and x_weighted_length(x_content) > X_MAX_CHARS:
        errors.append(f"X: длина ~{x_weighted_length(x_content)}; максимум {X_MAX_CHARS} с приблизительным учётом ссылок.")

    youtube = required_map(platforms, "youtube_shorts", "platforms.youtube_shorts")
    for key in ("title", "script", "description"):
        required_text(youtube, key, f"platforms.youtube_shorts.{key}")
    keywords = youtube.get("keywords")
    if not isinstance(keywords, list) or not keywords or not all(isinstance(k, str) and k.strip() for k in keywords):
        errors.append("platforms.youtube_shorts.keywords должен быть непустым списком непустых строк.")

    site = required_map(platforms, "dkor_space", "platforms.dkor_space")
    for key in ("title", "slug", "body_md"):
        required_text(site, key, f"platforms.dkor_space.{key}")
    slug = site.get("slug")
    if isinstance(slug, str) and not SLUG_RE.fullmatch(slug):
        errors.append("platforms.dkor_space.slug: разрешены только строчные латинские буквы, цифры и дефисы.")

    published = doc.get("published")
    if not isinstance(published, dict):
        errors.append("published должен быть объектом (пустым для черновика).")
    elif status == "draft" and published:
        errors.append("Для status=draft published должен быть пустым объектом.")
    elif status == "published" and not published:
        errors.append("status=published требует непустую запись подтверждённой публикации.")

    # Intentionally does NOT validate truth, copyright or human approval.
    return errors


def main() -> int:
    # When a subprocess inherits Windows cp1251 (e.g., Claude Code capture),
    # non-cp1251 characters in our status messages must not crash validation.
    # Explicit UTF-8 keeps automated callers and Russian text consistent.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Проверка JSON DKORWORLD2 Content Factory без публикации")
    parser.add_argument("file", type=Path, help="путь к JSON")
    args = parser.parse_args()
    try:
        raw = args.file.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            print("ERROR: файл содержит UTF-8 BOM; требуется UTF-8 без BOM.")
            return 1
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: не удалось прочитать валидный UTF-8 JSON: {exc}")
        return 1
    errors = validate(doc)
    if errors:
        print(f"FAIL: {len(errors)} ошибок в {args.file}")
        for n, error in enumerate(errors, 1):
            print(f" {n}. {error}")
        return 1
    print(f"OK: {args.file} | status={doc['status']} | Telegram={len(doc['platforms']['telegram']['content'])} символов | X≈{x_weighted_length(doc['platforms']['x']['content'])} символов")
    print("ВНИМАНИЕ: проверена структура, но не факты и не разрешение на публикацию.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
