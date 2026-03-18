"""Runtime state persistence helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discorsair.discourse.client import DiscourseClient


class RuntimeStateStore:
    def __init__(self, app_config: dict[str, Any]) -> None:
        self._app_config = app_config

    @property
    def app_config(self) -> dict[str, Any]:
        return self._app_config

    def mark_account_ok(self) -> None:
        auth = self._app_config.get("auth", {})
        auth["last_ok"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._save_app_config()

    def mark_account_fail(self, exc: Exception, *, mark_invalid: bool, disable: bool) -> None:
        auth = self._app_config.get("auth", {})
        auth["last_fail"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        auth["last_error"] = str(exc)
        if mark_invalid:
            auth["status"] = "invalid"
        if disable:
            auth["disabled"] = True
        self._save_app_config()

    def save_cookies(self, client: DiscourseClient) -> None:
        if client.last_response_ok() is not True:
            return
        cookie_header = client.get_cookie_header().strip()
        if not cookie_header:
            return
        auth = self._app_config.get("auth", {})
        if auth.get("cookie") == cookie_header:
            return
        auth["cookie"] = cookie_header
        self._save_app_config()

    def _save_app_config(self) -> None:
        path = self._app_config.get("_path")
        if not path:
            return
        try:
            payload = {k: v for k, v in self._app_config.items() if k != "_path"}
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("failed to save config: %s", exc)
