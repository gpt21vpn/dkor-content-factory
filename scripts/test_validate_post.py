#!/usr/bin/env python3
"""Small self-tests; no external packages, network or real publication."""
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from validate_post import validate, x_weighted_length


def example() -> dict:
    return {
        "schema_version": "dkor-social/v1",
        "status": "draft",
        "source": {"type": "own_case", "title": "Учебный кейс", "url": None, "author": None, "published_at": "2026-10-31"},
        "image_prompt": "A contemporary dark studio with yellow accents, no text, no logos, no people, cinematic, 16:9.",
        "image_path": None,
        "platforms": {
            "telegram": {"content": "Это демонстрационный текст для проверки валидатора. " * 14},
            "x": {"content": "A demo of a project-side validator. Source: https://example.org/sample"},
            "youtube_shorts": {"title": "Пример", "script": "Рассказ о проверке", "description": "Описание", "keywords": ["automation"]},
            "dkor_space": {"title": "Пример", "slug": "demo-example", "body_md": "Черновик"},
        },
        "published": {},
    }


def run() -> None:
    good = example()
    assert not validate(good), validate(good)
    assert x_weighted_length("hi https://example.com/test") == 3 + 23
    assert x_weighted_length("More: dkor.space") == 6 + 23
    assert x_weighted_length("user@example.com") == len("user@example.com")

    too_long = copy.deepcopy(good)
    too_long["platforms"]["telegram"]["content"] = "а" * 1001
    assert any("Telegram" in e for e in validate(too_long))

    bad_status = copy.deepcopy(good)
    bad_status["status"] = "autopublish"
    assert any("status" in e for e in validate(bad_status))

    fake_image = copy.deepcopy(good)
    fake_image["image_path"] = "../secret.env"
    assert any("image_path" in e for e in validate(fake_image))

    published_without_receipt = copy.deepcopy(good)
    published_without_receipt["status"] = "published"
    assert any("status=published" in e for e in validate(published_without_receipt))

    # Verify JSON round-trip preserves Cyrillic.
    with tempfile.TemporaryDirectory() as folder:
        filename = Path(folder) / "demo.json"
        filename.write_text(json.dumps(good, ensure_ascii=False, indent=2), encoding="utf-8")
        assert not validate(json.loads(filename.read_text(encoding="utf-8")))

        # Regression: launching the real CLI through a subprocess that requests
        # Windows cp1251 must not fail on Cyrillic text or the ~= symbol.
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1251"
        env["PYTHONUTF8"] = "0"
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("validate_post.py")), str(filename)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr, proc.stdout)
        cli_output = proc.stdout.decode("utf-8")
        assert "OK:" in cli_output and "Telegram=" in cli_output

    print("OK: 9 тестовых сценариев валидатора пройдены.")


if __name__ == "__main__":
    run()
