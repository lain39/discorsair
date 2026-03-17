"""UA mapping for impersonate targets."""

from __future__ import annotations

from typing import Dict


_UA_MAP: Dict[str, str] = {
    "chrome110": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
    "chrome120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def get_default_ua(impersonate_target: str) -> str:
    return _UA_MAP.get(impersonate_target, "")
