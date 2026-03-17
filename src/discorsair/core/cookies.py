"""Cookie parsing and merge helpers."""

from __future__ import annotations

from typing import Dict


def parse_cookie_header(cookie_header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    if not cookie_header:
        return cookies
    parts = cookie_header.split(";")
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def cookies_to_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def merge_cookies(base: Dict[str, str], update: Dict[str, str]) -> Dict[str, str]:
    merged = dict(base)
    merged.update(update)
    return merged
