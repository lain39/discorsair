"""Notification sender."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from curl_cffi import requests


class Notifier:
    def __init__(
        self,
        url: str,
        chat_id: str,
        headers: Dict[str, str],
        timeout_secs: int = 15,
        prefix: str = "[Discorsair]",
        error_prefix: str | None = None,
    ) -> None:
        self._url = url
        self._chat_id = chat_id
        self._headers = headers
        self._timeout_secs = timeout_secs
        self._prefix = prefix
        self._error_prefix = error_prefix or prefix

    def send(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": f"{self._prefix} {text}"}
        self._post(payload)

    def send_error(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": f"{self._error_prefix} {text}"}
        self._post(payload)

    def _post(self, payload: dict[str, str]) -> None:
        try:
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout_secs)
            logging.getLogger(__name__).info("notify: status=%s", resp.status_code)
        except Exception as exc:  
            logging.getLogger(__name__).warning("notify failed: %s", exc)


def format_notification(item: dict[str, Any]) -> str:
    ntype = item.get("notification_type")
    created_at = item.get("created_at", "")
    data = item.get("data", {}) or {}
    parts = [f"type={ntype}", f"time={created_at}"]
    for key in ("badge_name", "badge_slug", "username", "display_username", "topic_title", "original_name"):
        if key in data and data[key]:
            parts.append(f"{key}={data[key]}")
    return " | ".join(parts)
