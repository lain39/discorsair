"""High-level Discourse client."""

from __future__ import annotations

import json
from urllib.parse import urlencode
import logging
from typing import Any, Dict, Optional

from discorsair.core.requester import Requester
from discorsair.discourse import endpoints


class DiscourseAuthError(RuntimeError):
    """Raised when auth is invalid or expired."""


class DiscourseClient:
    def __init__(self, requester: Requester, csrf_token: str | None = None):
        self._requester = requester
        self._csrf_token = csrf_token

    def get_latest(self) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: get_latest")
        resp = self._requester.request(
            "get",
            endpoints.latest(),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
        )
        return resp.json()

    def get_unseen(self) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: get_unseen")
        resp = self._requester.request(
            "get",
            endpoints.unseen(),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
        )
        return resp.json()

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: get_topic topic=%s track_visit=%s", topic_id, track_visit)
        params = {
            "track_visit": "true" if track_visit else "false",
            "forceLoad": "true" if force_load else "false",
        }
        resp = self._requester.request(
            "get",
            endpoints.topic_json(topic_id),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "discourse-logged-in": "true",
                "discourse-present": "true",
                "discourse-track-view": "true",
                "discourse-track-view-topic-id": str(topic_id),
                "accept": "application/json, text/javascript, */*; q=0.01",
                "x-csrf-token": self._csrf_token or "",
            },
            params=params,
        )
        return resp.json()

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: get_posts_by_ids topic=%s count=%s", topic_id, len(post_ids))
        params = {
            "post_ids[]": [str(pid) for pid in post_ids],
            "include_suggested": "false",
        }
        resp = self._requester.request(
            "get",
            endpoints.topic_posts(topic_id),
            headers={
                "discourse-logged-in": "true",
                "discourse-present": "true",
                "x-requested-with": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
                "accept": "application/json, text/javascript, */*; q=0.01",
            },
            params=params,
        )
        return resp.json()

    def get_csrf(self) -> str:
        logging.getLogger(__name__).info("discourse: get_csrf")
        resp = self._requester.request(
            "get",
            endpoints.csrf(),
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        data = resp.json()
        self._csrf_token = data.get("csrf", "")
        return self._csrf_token or ""

    def get_notifications(self, limit: int = 30, recent: bool = True) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: get_notifications limit=%s recent=%s", limit, recent)
        params = {
            "limit": str(limit),
            "recent": "true" if recent else "false",
        }
        resp = self._requester.request(
            "get",
            endpoints.notifications(),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
            params=params,
        )
        return resp.json()

    def post_timings(self, topic_id: int, timings: Dict[int, int], topic_time: int, _retried: bool = False) -> None:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: post_timings topic=%s posts=%s", topic_id, list(timings.keys()))
        body = "&".join(
            [*(f"timings%5B{post}%5D={ms}" for post, ms in timings.items()), f"topic_time={topic_time}", f"topic_id={topic_id}"]
        )
        resp = self._requester.request(
            "post",
            endpoints.timings(),
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "discourse-logged-in": "true",
                "discourse-background": "true",
                "accept": "*/*",
                "x-csrf-token": self._csrf_token or "",
            },
            data=body,
        )
        if resp.status == 200:
            return
        if resp.status == 403:
            text = resp.text.strip()
            if text == "[\"BAD CSRF\"]":
                logging.getLogger(__name__).warning("discourse: BAD CSRF, refreshing token and retrying")
                if _retried:
                    raise RuntimeError("post_timings failed after CSRF retry")
                self.get_csrf()
                self.post_timings(topic_id, timings, topic_time, _retried=True)
                return
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict) and data.get("error_type") == "not_logged_in":
                raise DiscourseAuthError("not_logged_in")
            if isinstance(data, dict) and data.get("error_type") == "invalid_access":
                raise DiscourseAuthError("invalid_access")
        raise RuntimeError(f"post_timings failed: status={resp.status}")

    def toggle_reaction(self, post_id: int, emoji: str) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: toggle_reaction post=%s emoji=%s", post_id, emoji)
        resp = self._requester.request(
            "put",
            endpoints.reactions(post_id, emoji),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
        )
        return resp.json()

    def reply(self, topic_id: int, raw: str, category: Optional[int] = None) -> Dict[str, Any]:
        if not self._csrf_token:
            self.get_csrf()
        logging.getLogger(__name__).info("discourse: reply topic=%s len=%s", topic_id, len(raw))
        typing_ms, open_ms = _estimate_composer_timings(raw)
        payload = {
            "raw": raw,
            "unlist_topic": "false",
            "topic_id": str(topic_id),
            "is_warning": "false",
            "archetype": "regular",
            "typing_duration_msecs": str(typing_ms),
            "composer_open_duration_msecs": str(open_ms),
        }
        if category is not None:
            payload["category"] = str(category)
        body = urlencode(payload)
        resp = self._requester.request(
            "post",
            endpoints.create_post(),
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
            data=body,
        )
        return resp.json()

    def get_cookie_header(self) -> str:
        return self._requester.get_cookie_header()

    def last_response_ok(self) -> bool | None:
        return self._requester.last_response_ok()


def _estimate_composer_timings(raw: str) -> tuple[int, int]:
    length = len(raw or "")
    typing_ms = 800 + length * 80
    if typing_ms < 2000:
        typing_ms = 2000
    if typing_ms > 20000:
        typing_ms = 20000
    open_ms = typing_ms + 1200
    return typing_ms, open_ms
