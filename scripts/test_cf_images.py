#!/usr/bin/env python3
"""Offline tests for cf_images/redact. Fake tokens + httpx.MockTransport only: no real network,
no real .env, no real Cloudflare account. Run: python scripts/test_cf_images.py"""
import base64
import io
import json
import logging
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import httpx

import cf_images
from cf_images import CloudflareImageError, run_generate_image
from redact import redact, safe_proxy_label

FAKE_TOKEN = "cf-FAKE-TOKEN-0123456789abcdefXYZ"
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
PROXY_PASS = "p4ss-SECRET-word"
PROXY_URL = f"http://proxyuser:{PROXY_PASS}@127.0.0.1:3128"
ROOT = Path(__file__).resolve().parent.parent


def make_png(width: int = 1024, height: int = 576) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


PNG = make_png()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"
ENV_LIVE = {"CF_ACCOUNT_ID": FAKE_ACCOUNT, "CF_API_TOKEN": FAKE_TOKEN, "CF_ALLOW_LIVE_GENERATION": "1"}


def json_response(image: bytes, wrapped: bool = True) -> httpx.Response:
    b64 = base64.b64encode(image).decode()
    body = {"result": {"image": b64}, "success": True, "errors": [], "messages": []} if wrapped else {"image": b64}
    return httpx.Response(200, json=body)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.assets = Path(self._tmp.name) / "assets"
        self.assets.mkdir()
        self.requests: list[httpx.Request] = []

    def transport(self, handler):
        def wrapped(request):
            self.requests.append(request)
            return handler(request)
        return httpx.MockTransport(wrapped)

    def live(self, handler, filename="shot", environ=None, **kw):
        return run_generate_image("a dark desk scene", filename, dry_run=False,
                                  environ=environ or ENV_LIVE, assets_dir=self.assets,
                                  transport=self.transport(handler), **kw)


class DryRun(Base):
    def test_default_is_safe_plan_without_network_or_files(self):
        def boom(request):
            raise AssertionError("network must not be used in dry-run")
        result = run_generate_image("a dark desk scene", "shot", environ=ENV_LIVE, assets_dir=self.assets, transport=self.transport(boom))
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["network_request"])
        self.assertFalse(result["file_written"])
        self.assertEqual(self.requests, [])
        self.assertEqual(list(self.assets.iterdir()), [])
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))
        self.assertNotIn(FAKE_ACCOUNT, json.dumps(result))
        self.assertTrue(result["credentials_configured"])

    def test_dry_run_hides_proxy_credentials(self):
        result = run_generate_image("p", "shot", environ={**ENV_LIVE, "PROXY_URL": PROXY_URL}, assets_dir=self.assets)
        text = json.dumps(result)
        self.assertNotIn(PROXY_PASS, text)
        self.assertNotIn("proxyuser", text)
        self.assertEqual(result["connection"], "http://127.0.0.1:3128")

    def test_live_requires_both_switches(self):
        env = {"CF_ACCOUNT_ID": FAKE_ACCOUNT, "CF_API_TOKEN": FAKE_TOKEN}  # no CF_ALLOW_LIVE_GENERATION
        for value in (None, "0", "true", ""):
            e = dict(env)
            if value is not None:
                e["CF_ALLOW_LIVE_GENERATION"] = value
            with self.assertRaises(CloudflareImageError) as ctx:
                self.live(lambda r: json_response(PNG), environ=e)
            self.assertIn("выключена", str(ctx.exception))
        self.assertEqual(self.requests, [])
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_env_flag_alone_does_not_allow_live_in_dry_run(self):
        result = run_generate_image("p", "shot", dry_run=True, environ=ENV_LIVE, assets_dir=self.assets)
        self.assertFalse(result["network_request"])


class LiveSuccess(Base):
    def test_wrapped_json_base64_saved_and_request_shape(self):
        result = self.live(lambda r: json_response(PNG), seed=42)
        self.assertEqual(result["image_path"], "assets/shot.png")
        self.assertEqual((self.assets / "shot.png").read_bytes(), PNG)
        req = self.requests[0]
        self.assertTrue(str(req.url).endswith(f"/accounts/{FAKE_ACCOUNT}/ai/run/@cf/black-forest-labs/flux-2-klein-4b"))
        self.assertTrue(req.headers["content-type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(req.headers["authorization"], f"Bearer {FAKE_TOKEN}")
        body = req.read()
        for name, value in (("prompt", b"a dark desk scene"), ("width", b"1024"), ("height", b"576"), ("seed", b"42")):
            self.assertIn(f'name="{name}"'.encode(), body)
            self.assertIn(value, body)
        self.assertNotIn(b"steps", body)
        self.assertNotIn(b"guidance", body)

    def test_seed_omitted_when_none(self):
        self.live(lambda r: json_response(PNG))
        self.assertNotIn(b'name="seed"', self.requests[0].read())

    def test_unwrapped_json_and_binary_image(self):
        self.live(lambda r: json_response(PNG, wrapped=False), filename="a")
        self.live(lambda r: httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}), filename="b")
        self.assertTrue((self.assets / "a.png").exists())
        self.assertTrue((self.assets / "b.jpg").exists())


class LiveErrors(Base):
    def assertFails(self, handler, fragment, **kw):
        with self.assertRaises(CloudflareImageError) as ctx:
            self.live(handler, **kw)
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(list(self.assets.iterdir()), [], "nothing may be saved on failure")
        return str(ctx.exception)

    def test_auth_errors(self):
        for code in (401, 403):
            msg = self.assertFails(lambda r, c=code: httpx.Response(c, json={"success": False, "errors": [{"code": 10000, "message": f"bad {FAKE_TOKEN}"}]}), f"HTTP {code}")
            self.assertNotIn(FAKE_TOKEN, msg)

    def test_other_http_error_redacts_body(self):
        msg = self.assertFails(lambda r: httpx.Response(500, json={"errors": [{"code": 1, "message": f"oops Bearer {FAKE_TOKEN}"}]}), "HTTP 500")
        self.assertNotIn(FAKE_TOKEN, msg)

    def test_envelope_success_false(self):
        self.assertFails(lambda r: httpx.Response(200, json={"success": False, "errors": [{"code": 7, "message": "limit"}]}), "ошибк")

    def test_corrupted_base64(self):
        self.assertFails(lambda r: httpx.Response(200, json={"result": {"image": "!!!not*base64!!!"}}), "Base64")

    def test_missing_image_field_and_invalid_json(self):
        self.assertFails(lambda r: httpx.Response(200, json={"result": {}}), "нет строки")
        self.assertFails(lambda r: httpx.Response(200, content=b"{oops", headers={"content-type": "application/json"}), "JSON")

    def test_wrong_mime_types(self):
        self.assertFails(lambda r: httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"}), "Content-Type")
        self.assertFails(lambda r: httpx.Response(200, content=b"GIF89a" + b"\x00" * 100, headers={"content-type": "image/gif"}), "MIME")

    def test_content_not_an_image(self):
        self.assertFails(lambda r: httpx.Response(200, content=b"A" * 500, headers={"content-type": "image/png"}), "не похоже")
        self.assertFails(lambda r: json_response(b"B" * 500), "не похоже")

    def test_declared_mime_must_match_content(self):
        self.assertFails(lambda r: httpx.Response(200, content=JPEG, headers={"content-type": "image/png"}), "не совпадает")

    def test_empty_and_oversized(self):
        self.assertFails(lambda r: httpx.Response(200, content=b"", headers={"content-type": "image/png"}), "маленькое")
        self.assertGreaterEqual(len(PNG), cf_images.MIN_IMAGE_BYTES)
        with mock.patch.object(cf_images, "MAX_IMAGE_BYTES", len(PNG) - 1):
            self.assertFails(lambda r: json_response(PNG), "больше")
        with mock.patch.object(cf_images, "MAX_RESPONSE_BYTES", 40):
            self.assertFails(lambda r: json_response(PNG), "слишком большой")

    def test_timeout_and_network_errors(self):
        def timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)
        self.assertFails(timeout, "Тайм-аут")
        def refused(request):
            raise httpx.ConnectError(f"cannot reach via {PROXY_URL}", request=request)
        msg = self.assertFails(refused, "ConnectError", environ={**ENV_LIVE, "PROXY_URL": PROXY_URL})
        self.assertNotIn(PROXY_PASS, msg)

    def test_missing_credentials(self):
        self.assertFails(lambda r: json_response(PNG), "CF_ACCOUNT_ID", environ={"CF_ALLOW_LIVE_GENERATION": "1"})
        self.assertEqual(self.requests, [])

    def test_extension_mismatch_not_saved(self):
        self.assertFails(lambda r: json_response(JPEG), "не соответствует", filename="x.png")


class FilesystemSafety(Base):
    def test_no_overwrite(self):
        self.live(lambda r: json_response(PNG), filename="dup")
        before = (self.assets / "dup.png").read_bytes()
        n = len(self.requests)
        with self.assertRaises(CloudflareImageError) as ctx:
            self.live(lambda r: json_response(make_png(8, 8)), filename="dup.png")
        self.assertIn("уже существует", str(ctx.exception))
        self.assertEqual(len(self.requests), n, "must fail before spending API quota")
        self.assertEqual((self.assets / "dup.png").read_bytes(), before)

    def test_no_overwrite_when_name_given_without_extension(self):
        (self.assets / "taken.png").write_bytes(b"old")
        with self.assertRaises(CloudflareImageError):
            self.live(lambda r: json_response(PNG), filename="taken")
        self.assertEqual((self.assets / "taken.png").read_bytes(), b"old")

    def test_path_traversal_and_absolute_names_rejected(self):
        bad = ["../evil.png", "..\\evil.png", "sub/evil.png", "sub\\evil.png", "/abs.png", "C:\\x.png", "C:x.png",
               "..", ".", "", " ", "a b.png", "name.exe", "name.png.exe", ".hidden.png", "nul.png", "CON", "a" * 80 + ".png",
               "evil.png:stream", "x\x00.png"]
        for name in bad:
            with self.assertRaises(CloudflareImageError, msg=repr(name)):
                run_generate_image("p", name, assets_dir=self.assets)
        self.assertEqual(list(self.assets.iterdir()), [])
        self.assertFalse((self.assets.parent / "evil.png").exists())

    def test_symlink_target_not_followed(self):
        outside = Path(self._tmp.name) / "outside.png"
        link = self.assets / "link.png"
        try:
            os.symlink(outside, link)  # dangling symlink pointing outside assets/
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted on this system")
        with self.assertRaises(CloudflareImageError):
            self.live(lambda r: json_response(PNG), filename="link.png")
        self.assertFalse(outside.exists())

    def test_assets_dir_symlink_or_missing_rejected(self):
        with self.assertRaises(CloudflareImageError):
            run_generate_image("p", "x", assets_dir=Path(self._tmp.name) / "missing")
        link = Path(self._tmp.name) / "assets_link"
        try:
            os.symlink(self.assets, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted on this system")
        with self.assertRaises(CloudflareImageError):
            run_generate_image("p", "x", assets_dir=link)

    def test_input_validation(self):
        for kwargs in ({"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 2001}, {"prompt": "p", "seed": -1}, {"prompt": "p", "seed": True}, {"prompt": "p", "seed": 1.5}):
            kwargs.setdefault("filename", "ok")
            with self.assertRaises(CloudflareImageError, msg=str(kwargs)):
                run_generate_image(assets_dir=self.assets, **kwargs)


class ProxyAndSettings(Base):
    def _captured_client_kwargs(self, environ):
        captured = {}
        real = httpx.Client
        def fake(**kwargs):
            captured.update(kwargs)
            kwargs.pop("proxy", None)  # never connect through the fake proxy
            return real(**kwargs)
        with mock.patch.object(cf_images.httpx, "Client", side_effect=fake):
            self.live(lambda r: json_response(PNG), environ=environ)
        return captured

    def test_proxy_passed_via_proxy_param(self):
        kwargs = self._captured_client_kwargs({**ENV_LIVE, "PROXY_URL": PROXY_URL})
        self.assertEqual(kwargs["proxy"], PROXY_URL)
        self.assertEqual(kwargs["timeout"], httpx.Timeout(120.0))

    def test_empty_proxy_means_direct(self):
        for value in ("", "   "):
            self.tearDown_assets()
            kwargs = self._captured_client_kwargs({**ENV_LIVE, "PROXY_URL": value})
            self.assertNotIn("proxy", kwargs)

    def tearDown_assets(self):
        for p in self.assets.iterdir():
            p.unlink()

    def test_unsupported_proxy_scheme_rejected_without_leak(self):
        with self.assertRaises(CloudflareImageError) as ctx:
            run_generate_image("p", "x", environ={"PROXY_URL": f"socks5://u:{PROXY_PASS}@h:1080"}, assets_dir=self.assets)
        self.assertNotIn(PROXY_PASS, str(ctx.exception))

    def test_dotenv_parsing_and_environment_priority(self):
        env_file = Path(self._tmp.name) / "fake.env"
        env_file.write_text(f'CF_ACCOUNT_ID={FAKE_ACCOUNT}\nCF_API_TOKEN="{FAKE_TOKEN}"\n# c\nCF_ALLOW_LIVE_GENERATION=0\n', encoding="utf-8")
        s = cf_images.load_settings({"CF_ALLOW_LIVE_GENERATION": "1"}, env_file)
        self.assertEqual((s.account_id, s.api_token, s.allow_live), (FAKE_ACCOUNT, FAKE_TOKEN, True))
        self.assertEqual(s.model, cf_images.DEFAULT_MODEL)

    def test_invalid_model_rejected(self):
        with self.assertRaises(CloudflareImageError):
            cf_images.load_settings({"CF_IMAGE_MODEL": "../../evil"})

    def test_settings_repr_hides_secrets(self):
        s = cf_images.load_settings({**ENV_LIVE, "PROXY_URL": PROXY_URL})
        text = repr(s) + str(s)
        for secret in (FAKE_TOKEN, PROXY_PASS, "proxyuser", FAKE_ACCOUNT):
            self.assertNotIn(secret, text)


class NoLeaks(Base):
    def test_redact_helpers(self):
        text = f"token {FAKE_TOKEN} via {PROXY_URL} Authorization: Bearer abc.def-123 https://u:pw@host/x"
        out = redact(text, [FAKE_TOKEN])
        for secret in (FAKE_TOKEN, PROXY_PASS, "proxyuser", "abc.def-123", "u:pw"):
            self.assertNotIn(secret, out)
        self.assertEqual(safe_proxy_label(PROXY_URL), "http://127.0.0.1:3128")
        self.assertEqual(safe_proxy_label(""), "direct")

    def test_logs_and_exceptions_clean_across_scenarios(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger("dkor").addHandler(handler)
        logging.getLogger("dkor").setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger("dkor").removeHandler, handler)
        env = {**ENV_LIVE, "PROXY_URL": PROXY_URL}
        collected = []
        scenarios = [
            lambda r: json_response(PNG),
            lambda r: httpx.Response(401, json={"errors": [{"code": 1, "message": FAKE_TOKEN}]}),
            lambda r: httpx.Response(200, json={"result": {"image": "###"}}),
        ]
        for i, handler_fn in enumerate(scenarios):
            def with_proxy_removed(request, fn=handler_fn):
                return fn(request)
            real = httpx.Client
            def fake(**kwargs):
                kwargs.pop("proxy", None)
                return real(**kwargs)
            try:
                with mock.patch.object(cf_images.httpx, "Client", side_effect=fake):
                    collected.append(json.dumps(self.live(with_proxy_removed, filename=f"n{i}", environ=env)))
            except CloudflareImageError as exc:
                collected.append(str(exc))
                collected.append("".join(__import__("traceback").format_exception(exc)))
        blob = stream.getvalue() + "\n".join(collected)
        for secret in (FAKE_TOKEN, PROXY_PASS, "proxyuser"):
            self.assertNotIn(secret, blob)
        self.assertIn("Saved image", blob)  # logging really happened


class GitHygiene(unittest.TestCase):
    def _git(self, *args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git unavailable")

    def test_env_is_ignored_and_not_tracked(self):
        self.assertEqual(self._git("check-ignore", "-q", ".env").returncode, 0, ".env must be git-ignored")
        self.assertEqual(self._git("ls-files", ".env").stdout.strip(), "")

    def test_env_example_has_only_blank_secrets(self):
        values = dict(l.split("=", 1) for l in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines() if "=" in l and not l.startswith("#"))
        for key in ("CF_ACCOUNT_ID", "CF_API_TOKEN", "PROXY_URL"):
            self.assertEqual(values.get(key), "", key)
        self.assertEqual(values.get("CF_ALLOW_LIVE_GENERATION"), "0")

    def test_mcp_json_has_no_secrets(self):
        text = (ROOT / ".mcp.json").read_text(encoding="utf-8")
        doc = json.loads(text)
        self.assertEqual(doc["mcpServers"]["dkor-image-generator"]["type"], "stdio")
        for marker in ("TOKEN", "SECRET", "PASSWORD", "Bearer", "CF_API", "PROXY"):
            self.assertNotIn(marker, text)

    def test_tracked_and_new_files_do_not_contain_test_secrets(self):
        out = self._git("ls-files", "-co", "--exclude-standard").stdout.split("\n")
        hits = []
        for name in filter(None, out):
            if name.startswith("scripts/test_"):
                continue  # test files legitimately hold the fake values
            path = ROOT / name
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(s in text for s in (FAKE_TOKEN, PROXY_PASS)):
                    hits.append(name)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
