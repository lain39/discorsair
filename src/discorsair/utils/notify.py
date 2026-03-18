"""Notification sender."""

from __future__ import annotations

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

    def send(self, text: str) -> bool:
        payload = {"chat_id": self._chat_id, "text": f"{self._prefix} {text}"}
        return self._post(payload)

    def send_error(self, text: str) -> bool:
        payload = {"chat_id": self._chat_id, "text": f"{self._error_prefix} {text}"}
        return self._post(payload)

    def _post(self, payload: dict[str, str]) -> bool:
        try:
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout_secs)
            logging.getLogger(__name__).info("notify: status=%s", resp.status_code)
            if 200 <= resp.status_code < 300:
                return True
            logging.getLogger(__name__).warning("notify failed: status=%s body=%s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("notify failed: %s", exc)
            return False


def format_notification(item: dict[str, Any]) -> str:
    ntype = item.get("notification_type")
    created_at = item.get("created_at", "")
    data = item.get("data", {}) or {}
    parts = [f"type={ntype}", f"time={created_at}"]
    for key in ("badge_name", "badge_slug", "username", "display_username", "topic_title", "original_name"):
        if key in data and data[key]:
            parts.append(f"{key}={data[key]}")
    return " | ".join(parts)
