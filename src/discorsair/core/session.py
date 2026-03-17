"""Session state: CSRF, cookies, UA, impersonate target."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class SessionState:
    base_url: str
    cookie_header: str
    impersonate_target: str
    user_agent: str
    proxy: str | None = None
    cookies: Dict[str, str] = field(default_factory=dict)
    cf_clearance_cache: Dict[str, str] = field(default_factory=dict)
    last_request_ts: float = 0.0
    last_response_ok: bool | None = None
