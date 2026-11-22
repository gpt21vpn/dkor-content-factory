#!/usr/bin/env python3
"""Local MCP server (stdio) exposing generate_image for DKORWORLD2 Content Factory.

MCP SDK 2.x. stdout is reserved for the MCP protocol: never print() here;
diagnostics go to stderr only.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from cf_images import PROJECT_ROOT, CloudflareImageError, run_generate_image
from redact import redact

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dkor.mcp_server")

ENV_FILE = PROJECT_ROOT / ".env"

server = MCPServer("dkor-image-generator")


def generate_image_impl(prompt: str, filename: str, seed: int | None = None, dry_run: bool = True, *, env_file: Path | None = ENV_FILE, **kwargs: Any) -> dict[str, Any]:
    """Testable core: maps expected failures to ToolError without leaking details."""
    try:
        return run_generate_image(prompt, filename, seed, dry_run, env_file=env_file, **kwargs)
    except CloudflareImageError as exc:
        raise ToolError(redact(exc)) from None
    except Exception as exc:  # unknown failure: class name only, no message/traceback to the client
        log.error("Unexpected error in generate_image: %s", type(exc).__name__)
        raise ToolError(f"Внутренняя ошибка генератора ({type(exc).__name__}).") from None


@server.tool()
def generate_image(prompt: str, filename: str, seed: int | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Generate a 1024x576 image via Cloudflare Workers AI into assets/.

    dry_run=True (default) only returns a plan: no network, no token, no file.
    A real request needs dry_run=False AND CF_ALLOW_LIVE_GENERATION=1 in the local
    environment. filename is a bare name (e.g. "goose-qwen"); existing files are never overwritten.
    """
    return generate_image_impl(prompt, filename, seed, dry_run)


if __name__ == "__main__":
    server.run(transport="stdio")
