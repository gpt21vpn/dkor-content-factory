#!/usr/bin/env python3
"""Generate a local HTML preview of posts/*.json -> temp/preview.html.

Standard library only, no network. Character limits are imported from
validate_post.py so the preview and the validator always agree.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from validate_post import (
    TELEGRAM_MAX_CHARS,
    TELEGRAM_MIN_CHARS,
    X_MAX_CHARS,
    validate,
    x_weighted_length,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "temp" / "preview.html"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
IMAGE_DIRS = ("assets", "posts")

CSS = """
:root{--bg:#f5f6f8;--card:#fff;--text:#1c1f26;--muted:#667085;--line:#e3e6ec;--ok:#157f3d;--bad:#b42318;--okbg:#e8f6ee;--badbg:#fdecea;--accent:#2f5bea}
@media(prefers-color-scheme:dark){:root{--bg:#14161b;--card:#1d2027;--text:#e8eaf0;--muted:#98a2b3;--line:#2e323c;--ok:#4ade80;--bad:#f87171;--okbg:#14301f;--badbg:#3a1a18;--accent:#8aa4ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:1.5rem;margin:0 0 4px}.muted{color:var(--muted)}
.post{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:20px 0}
.post h2{font-size:1.15rem;margin:0 0 4px}.meta{font-size:.9rem;color:var(--muted);margin-bottom:12px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}.card h3{font-size:1rem;margin:0 0 8px}
.text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.95rem;margin:8px 0}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:.8rem;font-weight:600}
.ok{color:var(--ok);background:var(--okbg)}.bad{color:var(--bad);background:var(--badbg)}.draft{color:var(--accent);background:var(--line)}
.img{width:100%;max-height:260px;object-fit:cover;border-radius:8px;display:block;margin-bottom:8px}
.ph{display:flex;align-items:center;justify-content:center;height:120px;border:1px dashed var(--line);border-radius:8px;color:var(--muted);margin-bottom:8px;font-size:.9rem}
.errors{background:var(--badbg);color:var(--bad);border-radius:8px;padding:8px 12px;margin:12px 0 0;font-size:.9rem}
.errors ul{margin:4px 0 0;padding-left:20px}pre{white-space:pre-wrap;margin:0}
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def badge(ok: bool, text: str) -> str:
    return f'<span class="badge {"ok" if ok else "bad"}">{e(text)}</span>'


def safe_image_href(image_path: Any, root: Path, out_dir: Path) -> str | None:
    """Relative URL from out_dir to an existing project image, or None if unsafe."""
    if not isinstance(image_path, str) or not image_path.strip():
        return None
    normalized = image_path.replace("\\", "/")
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or ":" in normalized
        or ".." in parts
        or "\x00" in normalized
        or parts[0] not in IMAGE_DIRS
        or not normalized.lower().endswith(IMAGE_EXTENSIONS)
    ):
        return None
    try:
        target = (root / normalized).resolve()
        target.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not target.is_file():
        return None
    rel = os.path.relpath(target, out_dir.resolve()).replace("\\", "/")
    return quote(rel)


def image_block(href: str | None) -> str:
    if href:
        return f'<img class="img" src="{e(href)}" alt="Изображение к публикации" loading="lazy">'
    return '<div class="ph">Изображение отсутствует</div>'


def tg_card(platform: dict, href: str | None) -> str:
    text = platform.get("content") if isinstance(platform.get("content"), str) else ""
    n = len(text)
    ok = TELEGRAM_MIN_CHARS <= n <= TELEGRAM_MAX_CHARS
    return (
        '<section class="card"><h3>Telegram</h3>' + image_block(href)
        + f'<div>{badge(ok, f"{n} симв. (нужно {TELEGRAM_MIN_CHARS}–{TELEGRAM_MAX_CHARS})")}</div>'
        + f'<div class="text">{e(text)}</div></section>'
    )


def x_card(platform: dict) -> str:
    text = platform.get("content") if isinstance(platform.get("content"), str) else ""
    n = x_weighted_length(text)
    return (
        '<section class="card"><h3>X</h3>'
        + f'<div>{badge(n <= X_MAX_CHARS, f"≈{n} симв. (максимум {X_MAX_CHARS})")}</div>'
        + f'<div class="text">{e(text)}</div></section>'
    )


def yt_card(platform: dict) -> str:
    kw = platform.get("keywords")
    keywords = ", ".join(str(k) for k in kw) if isinstance(kw, list) else ""
    return (
        '<section class="card"><h3>YouTube Shorts</h3>'
        + f'<strong>{e(platform.get("title"))}</strong>'
        + f'<div class="text"><b>Сценарий:</b> {e(platform.get("script"))}</div>'
        + f'<div class="text"><b>Описание:</b> {e(platform.get("description"))}</div>'
        + f'<div class="meta">Ключевые слова: {e(keywords)}</div></section>'
    )


def site_card(platform: dict) -> str:
    body = platform.get("body_md") if isinstance(platform.get("body_md"), str) else ""
    return (
        '<section class="card"><h3>dkor.space</h3>'
        + f'<strong>{e(platform.get("title"))}</strong>'
        + f'<div class="meta">slug: {e(platform.get("slug"))} · {len(body)} симв.</div>'
        + f'<div class="text">{e(body)}</div></section>'
    )


def post_section(name: str, doc: Any, root: Path, out_dir: Path) -> str:
    if not isinstance(doc, dict):
        return f'<article class="post"><h2>{e(name)}</h2><div class="errors">Корень JSON должен быть объектом.</div></article>'
    errors = validate(doc)
    source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
    platforms = doc.get("platforms") if isinstance(doc.get("platforms"), dict) else {}
    href = safe_image_href(doc.get("image_path"), root, out_dir)

    def plat(key: str) -> dict:
        v = platforms.get(key)
        return v if isinstance(v, dict) else {}

    cards = [tg_card(plat("telegram"), href), x_card(plat("x")), yt_card(plat("youtube_shorts")), site_card(plat("dkor_space"))]
    verdict = badge(not errors, "валидатор: OK" if not errors else f"валидатор: {len(errors)} ошибок")
    err_html = ""
    if errors:
        err_html = '<div class="errors">Ошибки валидации:<ul>' + "".join(f"<li>{e(x)}</li>" for x in errors) + "</ul></div>"
    return (
        '<article class="post">'
        f'<h2>{e(source.get("title") or name)}</h2>'
        f'<div class="meta">{e(name)} · тип: {e(source.get("type"))} · '
        f'<span class="badge draft">{e(doc.get("status"))}</span> {verdict}</div>'
        f'<div class="grid">{"".join(cards)}</div>{err_html}</article>'
    )


def load_posts(posts_dir: Path) -> list[tuple[str, Any, str | None]]:
    """Return (filename, document, load_error). Broken files do not stop the run."""
    result: list[tuple[str, Any, str | None]] = []
    for path in sorted(posts_dir.glob("*.json")):
        try:
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raise ValueError("файл содержит UTF-8 BOM")
            result.append((path.name, json.loads(raw.decode("utf-8")), None))
        except (OSError, UnicodeError, ValueError) as exc:
            result.append((path.name, None, f"не удалось прочитать JSON: {exc}"))
    return result


def build_html(posts_dir: Path, root: Path = PROJECT_ROOT, out_dir: Path | None = None) -> str:
    out_dir = out_dir or DEFAULT_OUT.parent
    entries = load_posts(posts_dir) if posts_dir.is_dir() else []
    if not entries:
        body = '<p class="muted">Нет файлов posts/*.json для предпросмотра. Создайте черновик через скилл /dkor-content-pipeline.</p>'
    else:
        parts = []
        for name, doc, err in entries:
            if err:
                parts.append(f'<article class="post"><h2>{e(name)}</h2><div class="errors">{e(err)}</div></article>')
            else:
                parts.append(post_section(name, doc, root, out_dir))
        body = "".join(parts)
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self'; style-src 'unsafe-inline'\">"
        f"<title>Предпросмотр публикаций</title><style>{CSS}</style></head><body><main>"
        '<h1>DKORWORLD2 Content Factory — предпросмотр</h1>'
        '<p class="muted">Локальный предпросмотр черновиков. Ничего не публикуется.</p>'
        f"{body}</main></body></html>"
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Локальный HTML-предпросмотр posts/*.json")
    parser.add_argument("--posts-dir", type=Path, default=PROJECT_ROOT / "posts")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(args.posts_dir, PROJECT_ROOT, out.parent), encoding="utf-8", newline="\n")
    print(f"OK: предпросмотр записан в {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
