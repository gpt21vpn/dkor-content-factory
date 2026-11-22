#!/usr/bin/env python3
"""Offline tests for tg_publisher. Fake token + httpx.MockTransport only: no real network,
no real .env, no real Telegram. Run: python scripts/test_tg_publisher.py"""
import contextlib
import io
import json
import logging
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import tg_publisher
from tg_publisher import ALLOWED_CHAT_ID, TelegramPublishError

ROOT = Path(__file__).resolve().parent.parent
REAL_POST = ROOT / "posts" / "2026-11-01-goose-qwen-review-analysis.json"
FAKE_TOKEN = "123456789:FAKE_TOKEN_abcdefghijklmnopqrstuvwxyz012"
PROXY_PASS = "p4ss-SECRET-word"
PROXY_URL = f"http://proxyuser:{PROXY_PASS}@127.0.0.1:3128"
ENV_LIVE = {"TG_BOT_TOKEN": FAKE_TOKEN, "TG_CHANNEL_ID": str(ALLOWED_CHAT_ID), "TG_ALLOW_LIVE_SEND": "1"}


def make_jpeg(width: int = 1024, height: int = 576) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">H", 17) + struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    com = b"\xff\xfe" + struct.pack(">H", 82) + b"\x00" * 80
    return b"\xff\xd8" + app0 + sof + com + b"\xff\xd9"


def ok_response(chat_id: int = ALLOWED_CHAT_ID, message_id: int = 77) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id, "chat": {"id": chat_id, "type": "channel"}}})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "assets").mkdir()
        (self.root / "posts").mkdir()
        self.jpeg = make_jpeg()
        (self.root / "assets" / "pic.jpg").write_bytes(self.jpeg)
        self.doc = json.loads(REAL_POST.read_text(encoding="utf-8"))
        self.doc["image_path"] = "assets/pic.jpg"
        self.post = self.root / "posts" / "p.json"
        self.write_post()
        self.ledger = self.root / "temp" / "tg_sent.json"
        self.requests: list[httpx.Request] = []

    def write_post(self):
        self.post.write_text(json.dumps(self.doc, ensure_ascii=False), encoding="utf-8")

    def prepared(self):
        return tg_publisher.prepare(self.post, self.root)

    def transport(self, handler):
        def wrapped(request):
            self.requests.append(request)
            return handler(request)
        return httpx.MockTransport(wrapped)

    def record_proxy(self):
        """httpx would really dial the proxy even with a mock transport: record it, don't use it."""
        self.proxies: list[str] = []

        def fake_make_client(proxy, transport=None):
            self.proxies.append(proxy)
            return httpx.Client(transport=transport)
        patcher = mock.patch.object(tg_publisher, "make_client", fake_make_client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, handler, env=None, confirm="auto"):
        prepared = self.prepared()
        if confirm == "auto":
            confirm = prepared["fingerprint"]
        return tg_publisher.send_post(prepared, ENV_LIVE if env is None else env, confirm,
                                      ledger_path=self.ledger, transport=self.transport(handler))


class DryRun(Base):
    def test_real_post_dry_run_has_no_network_and_no_env(self):
        with mock.patch.object(httpx.Client, "__init__", side_effect=AssertionError("network")), \
                mock.patch.object(tg_publisher, "parse_dotenv", side_effect=AssertionError(".env read")):
            report = tg_publisher.dry_run_report(tg_publisher.prepare(REAL_POST))
        self.assertEqual(report["mode"], "dry_run")
        self.assertFalse(report["network_request"])
        self.assertFalse(report["env_read"])
        self.assertEqual(report["chat_id"], -1001234567890)
        self.assertEqual(report["image_path"], "assets/goose-qwen-review-01.jpg")
        self.assertEqual(report["image_size"], "1024x576")
        self.assertEqual(report["caption_chars"], len(report["caption"]))
        self.assertLessEqual(report["caption_chars"], 1024)
        self.assertEqual(len(report["fingerprint"]), 12)

    def test_cli_dry_run_exit_zero_and_prints_target(self):
        out = io.StringIO()
        with mock.patch.object(httpx.Client, "__init__", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(out):
            code = tg_publisher.main(["dry-run", str(REAL_POST)])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["chat_id"], ALLOWED_CHAT_ID)
        self.assertNotIn("TG_BOT_TOKEN=", out.getvalue())

    def test_fingerprint_changes_with_caption_and_image(self):
        base = self.prepared()["fingerprint"]
        self.doc["platforms"]["telegram"]["content"] = self.doc["platforms"]["telegram"]["content"].replace("Проверил", "Проверила", 1)
        self.write_post()
        self.assertNotEqual(self.prepared()["fingerprint"], base)
        (self.root / "assets" / "pic.jpg").write_bytes(make_jpeg(1000, 560))
        self.assertNotEqual(self.prepared()["fingerprint"], base)

    def test_post_file_is_not_modified(self):
        before = self.post.read_bytes()
        self.prepared()
        self.assertEqual(self.post.read_bytes(), before)
        self.assertEqual(json.loads(before)["status"], "draft")


class PrepareErrors(Base):
    def assert_rejected(self):
        with self.assertRaises(TelegramPublishError):
            self.prepared()

    def test_caption_over_telegram_limit(self):
        self.doc["platforms"]["telegram"]["content"] = "а" * 1100
        self.write_post()
        self.assert_rejected()

    def test_caption_with_hashtag(self):
        self.doc["platforms"]["telegram"]["content"] += " #ai"
        self.write_post()
        self.assert_rejected()

    def test_published_status_is_rejected(self):
        self.doc["status"] = "published"
        self.write_post()
        self.assert_rejected()

    def test_missing_image_and_null_image_path(self):
        (self.root / "assets" / "pic.jpg").unlink()
        self.assert_rejected()
        self.doc["image_path"] = None
        self.write_post()
        self.assert_rejected()

    def test_image_path_traversal_and_absolute(self):
        for bad in ("../secret.jpg", "assets/../x.jpg", "assets/sub/pic.jpg", "pic.jpg", str(self.root / "assets" / "pic.jpg"), "posts/p.json"):
            self.doc["image_path"] = bad
            self.write_post()
            self.assert_rejected()

    def test_not_a_jpeg_and_broken_jpeg(self):
        (self.root / "assets" / "pic.jpg").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        self.assert_rejected()
        (self.root / "assets" / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")  # no SOF marker
        self.assert_rejected()

    def test_dimension_sum_limit_and_size_limit(self):
        (self.root / "assets" / "pic.jpg").write_bytes(make_jpeg(9000, 1500))
        self.assert_rejected()
        (self.root / "assets" / "pic.jpg").write_bytes(self.jpeg + b"\x00" * (tg_publisher.MAX_PHOTO_BYTES + 1))
        self.assert_rejected()

    def test_invalid_json_post(self):
        self.post.write_text("{not json", encoding="utf-8")
        self.assert_rejected()


class WrongChannelProtection(Base):
    def test_allowed_chat_id_is_the_test_channel(self):
        self.assertEqual(ALLOWED_CHAT_ID, -1001234567890)

    def test_other_channel_in_env_blocks_everything_without_requests(self):
        for other in ("-1009876543210", "@example_channel", "123456789", "-1009876543210"):
            env = {**ENV_LIVE, "TG_CHANNEL_ID": other}
            with self.assertRaises(TelegramPublishError):
                self.send(lambda r: ok_response(), env=env)
            with self.assertRaises(TelegramPublishError):
                tg_publisher.check_access(env, transport=self.transport(lambda r: ok_response()))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.ledger.exists())

    def test_empty_channel_env_still_targets_only_allowed_chat(self):
        env = {**ENV_LIVE, "TG_CHANNEL_ID": ""}
        self.send(lambda r: ok_response(), env=env)
        body = self.requests[0].content.decode("utf-8", "replace")
        self.assertIn(str(ALLOWED_CHAT_ID), body)

    def test_response_from_other_chat_is_flagged_and_blocks_resend(self):
        with self.assertRaises(TelegramPublishError):
            self.send(lambda r: ok_response(chat_id=-1009876543210))
        with self.assertRaises(TelegramPublishError):
            self.send(lambda r: ok_response())
        self.assertEqual(len(self.requests), 1)

    def test_getchat_other_id_is_rejected(self):
        def handler(request):
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"username": "bot4testai2c"}})
            return httpx.Response(200, json={"ok": True, "result": {"id": -1009876543210, "type": "channel"}})
        with self.assertRaises(TelegramPublishError):
            tg_publisher.check_access(ENV_LIVE, transport=self.transport(handler))


class MethodWhitelist(Base):
    def test_only_three_methods_are_allowed(self):
        self.assertEqual(tg_publisher.ALLOWED_METHODS, {"getMe", "getChat", "sendPhoto"})

    def test_updates_and_webhook_methods_are_blocked_without_requests(self):
        for method in ("getUpdates", "setWebhook", "deleteWebhook", "getWebhookInfo", "sendMessage", "logOut", "close"):
            with self.assertRaises(TelegramPublishError):
                tg_publisher.call_api(FAKE_TOKEN, method, transport=self.transport(lambda r: ok_response()))
        self.assertEqual(self.requests, [])


class LiveGuards(Base):
    def assert_blocked(self, **kwargs):
        with self.assertRaises(TelegramPublishError):
            self.send(lambda r: ok_response(), **kwargs)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.ledger.exists())

    def test_live_flag_required(self):
        self.assert_blocked(env={**ENV_LIVE, "TG_ALLOW_LIVE_SEND": "0"})
        self.assert_blocked(env={k: v for k, v in ENV_LIVE.items() if k != "TG_ALLOW_LIVE_SEND"})

    def test_confirm_required_and_must_match(self):
        self.assert_blocked(confirm=None)
        self.assert_blocked(confirm="")
        self.assert_blocked(confirm="000000000000")

    def test_confirm_of_old_text_does_not_work_for_changed_caption(self):
        old = self.prepared()["fingerprint"]
        self.doc["platforms"]["telegram"]["content"] = self.doc["platforms"]["telegram"]["content"].replace("Проверил", "Проверила", 1)
        self.write_post()
        self.assert_blocked(confirm=old)

    def test_token_missing_or_malformed(self):
        self.assert_blocked(env={**ENV_LIVE, "TG_BOT_TOKEN": ""})
        self.assert_blocked(env={**ENV_LIVE, "TG_BOT_TOKEN": "not-a-token"})

    def test_unsupported_proxy_scheme(self):
        self.assert_blocked(env={**ENV_LIVE, "TG_PROXY_URL": f"socks5://u:{PROXY_PASS}@127.0.0.1:1080"})

    def test_cli_check_access_requires_yes(self):
        with mock.patch.object(tg_publisher, "load_settings", side_effect=AssertionError(".env read")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tg_publisher.main(["check-access"]), 1)

    def test_cli_send_without_confirm_fails_without_network(self):
        env = dict(ENV_LIVE)
        with mock.patch.object(tg_publisher, "load_settings", return_value=env), \
                mock.patch.object(httpx.Client, "__init__", side_effect=AssertionError("network")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tg_publisher.main(["send", str(REAL_POST)]), 1)


class SendSuccess(Base):
    def test_request_shape_result_and_ledger(self):
        result = self.send(lambda r: ok_response(message_id=4242))
        self.assertEqual(result, {"mode": "live", "network_request": True, "chat_id": ALLOWED_CHAT_ID, "message_id": 4242})
        self.assertEqual(len(self.requests), 1)
        req = self.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(str(req.url), f"https://api.telegram.org/bot{FAKE_TOKEN}/sendPhoto")
        body = req.content
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(str(ALLOWED_CHAT_ID).encode(), body)
        self.assertIn(self.doc["platforms"]["telegram"]["content"].encode("utf-8"), body)
        self.assertIn(b'name="photo"; filename="pic.jpg"', body)
        self.assertIn(b"Content-Type: image/jpeg", body)
        self.assertIn(self.jpeg, body)
        self.assertNotIn(b"parse_mode", body)
        entry = next(iter(json.loads(self.ledger.read_text(encoding="utf-8")).values()))
        self.assertEqual((entry["state"], entry["message_id"], entry["chat_id"]), ("sent", 4242, ALLOWED_CHAT_ID))
        self.assertNotIn(FAKE_TOKEN, self.ledger.read_text(encoding="utf-8"))

    def test_duplicate_send_is_blocked_without_request(self):
        self.send(lambda r: ok_response())
        with self.assertRaises(TelegramPublishError) as ctx:
            self.send(lambda r: ok_response())
        self.assertIn("уже отправлен", str(ctx.exception))
        self.assertEqual(len(self.requests), 1)

    def test_proxy_is_passed_to_client(self):
        self.record_proxy()
        self.send(lambda r: ok_response(), env={**ENV_LIVE, "TG_PROXY_URL": PROXY_URL})
        self.assertEqual(self.proxies, [PROXY_URL])

    def test_check_access_success(self):
        def handler(request):
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"username": "bot4testai2c"}})
            self.assertIn(str(ALLOWED_CHAT_ID).encode(), request.content)
            return httpx.Response(200, json={"ok": True, "result": {"id": ALLOWED_CHAT_ID, "type": "channel", "title": "DKORWORLD2 TEST"}})
        info = tg_publisher.check_access(ENV_LIVE, transport=self.transport(handler))
        self.assertEqual(info, {"bot_username": "bot4testai2c", "chat_id": ALLOWED_CHAT_ID, "chat_type": "channel", "chat_title": "DKORWORLD2 TEST"})
        self.assertEqual([r.url.path.rsplit("/", 1)[1] for r in self.requests], ["getMe", "getChat"])


class TelegramErrors(Base):
    def api_error(self, status, description="x", **extra):
        return lambda r: httpx.Response(status, json={"ok": False, "error_code": status, "description": description, **extra})

    def test_definite_errors_clear_pending_and_allow_retry(self):
        cases = [
            (401, "Unauthorized", "401"),
            (403, "Forbidden: bot was kicked", "403"),
            (400, "Bad Request: chat not found", "chat not found"),
            (429, "Too Many Requests", "30"),
        ]
        for status, description, expected in cases:
            extra = {"parameters": {"retry_after": 30}} if status == 429 else {}
            with self.assertRaises(TelegramPublishError) as ctx:
                self.send(self.api_error(status, description, **extra))
            self.assertIn(expected, str(ctx.exception))
            self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
            self.assertEqual(json.loads(self.ledger.read_text(encoding="utf-8")), {})
        self.assertEqual(len(self.requests), len(cases))  # exactly one request each: no automatic retries
        self.send(lambda r: ok_response())  # a later, explicit retry is allowed

    def test_ambiguous_failures_block_blind_resend(self):
        def timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)

        def network(request):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        for handler in (timeout, network, self.api_error(502, "Bad Gateway"), lambda r: httpx.Response(200, text="<html>oops</html>")):
            self.ledger.unlink(missing_ok=True)
            with self.assertRaises(TelegramPublishError) as ctx:
                self.send(handler)
            self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
            with self.assertRaises(TelegramPublishError) as again:
                self.send(lambda r: ok_response())
            self.assertIn("неопределённо", str(again.exception))

    def test_ok_false_with_http_200_is_not_success(self):
        with self.assertRaises(TelegramPublishError):
            self.send(lambda r: httpx.Response(200, json={"ok": False, "description": "nope"}))

    def test_missing_message_id_is_not_success(self):
        with self.assertRaises(TelegramPublishError):
            self.send(lambda r: httpx.Response(200, json={"ok": True, "result": {"chat": {"id": ALLOWED_CHAT_ID}}}))

    def test_corrupted_ledger_stops_sending(self):
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("{broken", encoding="utf-8")
        with self.assertRaises(TelegramPublishError):
            self.send(lambda r: ok_response())
        self.assertEqual(self.requests, [])


class NoLeaks(Base):
    def test_token_and_proxy_password_never_in_logs_errors_or_output(self):
        self.record_proxy()
        env = {**ENV_LIVE, "TG_PROXY_URL": PROXY_URL}
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("dkor.tg_publisher")
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)
        messages = []

        def leaky(request):
            raise httpx.ConnectError(f"boom {request.url} via {PROXY_URL}", request=request)

        for h in (leaky, lambda r: httpx.Response(400, json={"ok": False, "description": f"Bad token {FAKE_TOKEN} {PROXY_PASS}"})):
            self.ledger.unlink(missing_ok=True)
            try:
                self.send(h, env=env)
            except TelegramPublishError as exc:
                messages.append(str(exc))
        self.send(lambda r: ok_response(), env=env)
        text = stream.getvalue() + "\n".join(messages) + json.dumps(tg_publisher.dry_run_report(self.prepared()), ensure_ascii=False)
        for secret in (FAKE_TOKEN, PROXY_PASS, PROXY_URL):
            self.assertNotIn(secret, text)
        self.assertIn("proxy=http://127.0.0.1:3128", stream.getvalue())


class GitHygiene(unittest.TestCase):
    def test_ledger_and_env_are_ignored(self):
        for rel in ("temp/tg_sent.json", ".env"):
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
            self.assertEqual(result.returncode, 0, f"{rel} должен быть в .gitignore")

    def test_source_has_no_hardcoded_secrets(self):
        source = (ROOT / "scripts" / "tg_publisher.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\d{8,}:[A-Za-z0-9_-]{30,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
