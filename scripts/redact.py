#!/usr/bin/env python3
"""Secret masking helpers; standard library only. Never prints anything itself."""
from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlsplit

MASK = "***"
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    """Mask known secrets, URL credentials and Bearer tokens in arbitrary text."""
    out = str(text)
    # Longest first, so a full proxy URL is masked before its password fragment.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        out = out.replace(secret, MASK)
    out = _USERINFO_RE.sub(lambda m: m.group(1) + MASK + "@", out)
    return _BEARER_RE.sub("Bearer " + MASK, out)


def proxy_password(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    try:
        return urlsplit(proxy_url).password
    except ValueError:
        return None


def safe_proxy_label(proxy_url: str | None) -> str:
    """Return scheme://host:port without credentials, or 'direct'."""
    if not proxy_url:
        return "direct"
    try:
        parts = urlsplit(proxy_url)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except ValueError:
        return "configured"
