#!/usr/bin/env python3
"""MCP-layer tests. Skipped (honestly reported) when the `mcp` package is not installed."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HAS_MCP = importlib.util.find_spec("mcp") is not None
FAKE_TOKEN = "cf-FAKE-TOKEN-0123456789abcdefXYZ"
ENV = {"CF_ACCOUNT_ID": "0123456789abcdef0123456789abcdef", "CF_API_TOKEN": FAKE_TOKEN}


@unittest.skipUnless(HAS_MCP, "пакет mcp не установлен (ожидается mcp==2.3.0 в .venv)")
class McpServerTests(unittest.TestCase):
    def setUp(self):
        import mcp_server
        self.m = mcp_server
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.assets = Path(self._tmp.name) / "assets"
        self.assets.mkdir()

    def test_tool_registered_with_safe_defaults(self):
        import inspect
        sig = inspect.signature(self.m.generate_image)
        self.assertIs(sig.parameters["dry_run"].default, True)
        self.assertIsNone(sig.parameters["seed"].default)

    def test_dry_run_result(self):
        result = self.m.generate_image_impl("a dark desk", "shot", env_file=None, environ=ENV, assets_dir=self.assets)
        self.assertFalse(result["network_request"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))

    def test_expected_errors_become_tool_error_without_secrets(self):
        from mcp.server.mcpserver.exceptions import ToolError
        with self.assertRaises(ToolError) as ctx:
            self.m.generate_image_impl("p", "x", dry_run=False, env_file=None, environ=ENV, assets_dir=self.assets)
        self.assertIn("выключена", str(ctx.exception))
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_unexpected_errors_are_masked(self):
        from unittest import mock
        from mcp.server.mcpserver.exceptions import ToolError
        with mock.patch.object(self.m, "run_generate_image", side_effect=RuntimeError(f"boom {FAKE_TOKEN}")):
            with self.assertRaises(ToolError) as ctx:
                self.m.generate_image_impl("p", "x")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
