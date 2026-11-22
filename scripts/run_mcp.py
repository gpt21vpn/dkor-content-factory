#!/usr/bin/env python3
"""Portable launcher for the local MCP server (stdio).

Picks the project's .venv interpreter (resolved from this file's location, not
from the current directory) and falls back to the current interpreter. stdin,
stdout and stderr are inherited so the MCP protocol passes through untouched.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "scripts" / "mcp_server.py"


def venv_python(root: Path = ROOT) -> Path | None:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    python = venv_python() or Path(sys.executable)
    return subprocess.call([str(python), str(SERVER)], cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
