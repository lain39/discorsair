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
        self._ensure_csrf_token()
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
        self._ensure_csrf_token()
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
        self._ensure_csrf_token()
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
        self._ensure_csrf_token()
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

    def get_csrf(self, force_refresh: bool = False) -> str:
        self._sync_csrf_token_hint()
        if self._csrf_token and not force_refresh:
            logging.getLogger(__name__).info("discourse: get_csrf using cached token")
            return self._csrf_token
        if getattr(self._requester, "should_use_flaresolverr_for_csrf", lambda: False)():
            logging.getLogger(__name__).info("discourse: get_csrf via FlareSolverr base_url")
            self._csrf_token = self._requester.fetch_csrf_token_via_flaresolverr()
            return self._csrf_token or ""
        if force_refresh:
            logging.getLogger(__name__).info("discourse: get_csrf force refresh")
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
        self._ensure_csrf_token()
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

    def mark_notifications_read(self) -> Dict[str, Any]:
        self._ensure_csrf_token()
        logging.getLogger(__name__).info("discourse: mark_notifications_read")
        return self._request_json(
            "put",
            endpoints.notifications_mark_read(),
            operation="mark_notifications_read",
            retry_bad_csrf=True,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "discourse-logged-in": "true",
                "discourse-present": "true",
                "x-csrf-token": self._csrf_token or "",
            },
        )

    def post_timings(self, topic_id: int, timings: Dict[int, int], topic_time: int, _retried: bool = False) -> None:
        self._ensure_csrf_token()
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
        text = resp.text.strip()
        if resp.status == 403 and text == "[\"BAD CSRF\"]":
            logging.getLogger(__name__).warning("discourse: BAD CSRF, refreshing token and retrying")
            if _retried:
                raise RuntimeError("post_timings failed after CSRF retry")
            self.get_csrf(force_refresh=True)
            self.post_timings(topic_id, timings, topic_time, _retried=True)
            return
        payload: Any
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        self._raise_for_error(resp.status, payload, "post_timings")

    def toggle_reaction(self, post_id: int, emoji: str) -> Dict[str, Any]:
        self._ensure_csrf_token()
        logging.getLogger(__name__).info("discourse: toggle_reaction post=%s emoji=%s", post_id, emoji)
        return self._request_json(
            "put",
            endpoints.reactions(post_id, emoji),
            operation="toggle_reaction",
            retry_bad_csrf=True,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
        )

    def reply(self, topic_id: int, raw: str, category: Optional[int] = None) -> Dict[str, Any]:
        self._ensure_csrf_token()
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
        return self._request_json(
            "post",
            endpoints.create_post(),
            operation="reply",
            retry_bad_csrf=True,
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "x-csrf-token": self._csrf_token or "",
            },
            data=body,
        )

    def get_cookie_header(self) -> str:
        return self._requester.get_cookie_header()

    def last_response_ok(self) -> bool | None:
        return self._requester.last_response_ok()

    def _ensure_csrf_token(self) -> str:
        self._sync_csrf_token_hint()
        if not self._csrf_token:
            self.get_csrf()
        return self._csrf_token or ""

    def _sync_csrf_token_hint(self) -> None:
        hint_getter = getattr(self._requester, "consume_csrf_token_hint", None)
        if not callable(hint_getter):
            hint_getter = getattr(self._requester, "get_csrf_token_hint", None)
        if not callable(hint_getter):
            return
        hinted = str(hint_getter() or "")
        if hinted and hinted != self._csrf_token:
            logging.getLogger(__name__).info("discourse: synced csrf token from FlareSolverr solution")
            self._csrf_token = hinted

    def _request_json(
        self,
        method: str,
        path: str,
        operation: str,
        headers: Dict[str, str],
        params: Dict[str, str] | None = None,
        data: str | None = None,
        json_body: Dict[str, Any] | None = None,
        retry_bad_csrf: bool = False,
        _retried: bool = False,
    ) -> Dict[str, Any]:
        resp = self._requester.request(
            method,
            path,
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
        )
        if resp.status == 403 and retry_bad_csrf and resp.text.strip() == "[\"BAD CSRF\"]":
            logging.getLogger(__name__).warning("discourse: %s got BAD CSRF, refreshing token and retrying", operation)
            if _retried:
                raise RuntimeError(f"{operation} failed after CSRF retry")
            self.get_csrf(force_refresh=True)
            retry_headers = dict(headers)
            retry_headers["x-csrf-token"] = self._csrf_token or ""
            return self._request_json(
                method,
                path,
                operation=operation,
                headers=retry_headers,
                params=params,
                data=data,
                json_body=json_body,
                retry_bad_csrf=retry_bad_csrf,
                _retried=True,
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{operation} failed: status={resp.status} returned non-JSON response") from exc
        self._raise_for_error(resp.status, payload, operation)
        return payload

    def _raise_for_error(self, status: int, payload: Any, operation: str) -> None:
        if isinstance(payload, dict):
            error_type = payload.get("error_type")
            if error_type in {"not_logged_in", "invalid_access"}:
                raise DiscourseAuthError(str(error_type))
            detail = payload.get("errors") or payload.get("message") or error_type
        else:
            detail = None
        if status >= 400:
            if detail:
                raise RuntimeError(f"{operation} failed: status={status} detail={detail}")
            raise RuntimeError(f"{operation} failed: status={status}")


def _estimate_composer_timings(raw: str) -> tuple[int, int]:
    length = len(raw or "")
    typing_ms = 800 + length * 80
    if typing_ms < 2000:
        typing_ms = 2000
    if typing_ms > 20000:
        typing_ms = 20000
    open_ms = typing_ms + 1200
    return typing_ms, open_ms
