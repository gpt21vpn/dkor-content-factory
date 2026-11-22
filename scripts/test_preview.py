#!/usr/bin/env python3
"""Self-tests for preview.py; no network, no external packages."""
import copy
import json
import re
import tempfile
from pathlib import Path

import preview
import validate_post

REAL_POSTS = Path(__file__).resolve().parent.parent / "posts"


def doc(**over) -> dict:
    base = {
        "schema_version": "dkor-social/v1",
        "status": "draft",
        "source": {"type": "own_case", "title": "Учебный кейс", "url": None, "author": None, "published_at": "2026-10-31"},
        "image_prompt": "dark studio, no text",
        "image_path": None,
        "platforms": {
            "telegram": {"content": "Демонстрационный текст для предпросмотра. " * 16},
            "x": {"content": "Preview demo https://example.org/sample"},
            "youtube_shorts": {"title": "Пример", "script": "Сценарий", "description": "Описание", "keywords": ["a", "b"]},
            "dkor_space": {"title": "Пример", "slug": "demo-example", "body_md": "Черновик"},
        },
        "published": {},
    }
    base.update(copy.deepcopy(over))
    return base


def write_posts(root: Path, **docs) -> Path:
    posts = root / "posts"
    posts.mkdir(exist_ok=True)
    for name, d in docs.items():
        (posts / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return posts


def render(root: Path, posts: Path) -> str:
    return preview.build_html(posts, root, root / "temp")


def test_real_posts_four_platforms():
    page = preview.build_html(REAL_POSTS)
    assert "Telegram" in page and "YouTube Shorts" in page and "dkor.space" in page and ">X<" in page
    assert page.count('class="post"') == len(list(REAL_POSTS.glob("*.json")))
    assert "валидатор: OK" in page


def test_limits_come_from_validator():
    assert preview.TELEGRAM_MAX_CHARS is validate_post.TELEGRAM_MAX_CHARS
    assert preview.X_MAX_CHARS is validate_post.X_MAX_CHARS
    src = Path(preview.__file__).read_text(encoding="utf-8")
    code = re.sub(r'CSS = """.*?"""', "", src, flags=re.S)
    assert not re.search(r"\b(280|600|1000)\b", code),"лимиты не должны дублироваться в preview.py"
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        long_tg = doc()
        long_tg["platforms"]["telegram"]["content"] = "я" * (validate_post.TELEGRAM_MAX_CHARS + 1)
        page = render(root, write_posts(root, a=long_tg))
        assert 'badge bad">1001 симв.' in page


def test_escaping():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        d = doc()
        d["source"]["title"] = '<script>alert(1)</script>'
        d["platforms"]["x"]["content"] = '"><img src=x onerror=alert(1)>'
        page = render(root, write_posts(root, evil=d))
        assert "<script>alert" not in page and "<img src=x" not in page
        assert "&lt;script&gt;" in page and "&quot;&gt;&lt;img" in page


def test_missing_and_broken_data():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        assert "Нет файлов" in render(root, root / "no-such-dir")
        posts = write_posts(root)
        assert "Нет файлов" in render(root, posts)
        (posts / "broken.json").write_text("{not json", encoding="utf-8")
        (posts / "list.json").write_text("[1]", encoding="utf-8")
        (posts / "empty.json").write_text("{}", encoding="utf-8")
        page = render(root, posts)
        assert "не удалось прочитать JSON" in page and "должен быть объектом" in page
        assert "ошибок" in page  # пустой объект проходит через валидатор, страница не падает


def test_image_path_safety():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        (root / "assets").mkdir()
        (root / "assets" / "pic one.jpg").write_bytes(b"\xff\xd8\xff")
        (root.parent / "outside.jpg").write_bytes(b"x")
        out_dir = root / "temp"
        ok = preview.safe_image_href("assets/pic one.jpg", root, out_dir)
        assert ok == "../assets/pic%20one.jpg", ok
        bad = [
            "../outside.jpg", "assets/../../outside.jpg", "/etc/passwd.jpg", "C:/x/y.jpg",
            "https://evil.example/x.jpg", "javascript:alert(1).jpg", "data:image/png;base64,AAAA.png",
            "assets/missing.jpg", "assets/pic one.txt", "other/pic.jpg", "", None, 5,
        ]
        for value in bad:
            assert preview.safe_image_href(value, root, out_dir) is None, value
        page = render(root, write_posts(root, a=doc(image_path="assets/pic one.jpg"), b=doc(image_path="../outside.jpg")))
        assert 'src="../assets/pic%20one.jpg"' in page
        assert page.count("<img") == 1 and "Изображение отсутствует" in page
        (root.parent / "outside.jpg").unlink()


def test_cli_writes_file():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        out = root / "temp" / "preview.html"
        code = preview.main(["--posts-dir", str(write_posts(root, a=doc())), "--out", str(out)])
        assert code == 0 and out.read_text(encoding="utf-8").startswith("<!doctype html>")


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"OK: {len(tests)} тестовых сценариев предпросмотра пройдены.")


if __name__ == "__main__":
    run()
