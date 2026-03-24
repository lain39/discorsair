"""Queued wrapper to serialize DiscourseClient calls."""

from __future__ import annotations

from concurrent.futures import TimeoutError
from typing import Any, Dict, Optional

from discorsair.core.request_queue import RequestQueue
from discorsair.discourse.client import DiscourseClient


class QueuedDiscourseClient:
    def __init__(self, inner: DiscourseClient, queue: RequestQueue) -> None:
        self._inner = inner
        self._queue = queue

    def _call(
        self,
        fn,
        priority: int = 10,
        rate_limit_key: str | None = None,
        timeout_secs: float | None = None,
    ):
        wait_timeout = timeout_secs
        if wait_timeout is not None and wait_timeout <= 0:
            wait_timeout = None
        fut = self._queue.submit(fn, priority=priority, rate_limit_key=rate_limit_key)
        try:
            return fut.result(timeout=wait_timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def get_latest(self) -> Dict[str, Any]:
        return self._call(self._inner.get_latest, priority=10, rate_limit_key="get_latest")

    def get_unseen(self) -> Dict[str, Any]:
        return self._call(self._inner.get_unseen, priority=10, rate_limit_key="get_unseen")

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True) -> Dict[str, Any]:
        return self._call(
            lambda: self._inner.get_topic(topic_id, track_visit, force_load),
            priority=10,
            rate_limit_key="get_topic",
        )

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]) -> Dict[str, Any]:
        return self._call(
            lambda: self._inner.get_posts_by_ids(topic_id, post_ids),
            priority=10,
            rate_limit_key="get_posts_by_ids",
        )

    def get_csrf(self, force_refresh: bool = False) -> str:
        return self._call(lambda: self._inner.get_csrf(force_refresh=force_refresh), priority=5, rate_limit_key="get_csrf")

    def get_notifications(self, limit: int = 30, recent: bool = True) -> Dict[str, Any]:
        return self._call(
            lambda: self._inner.get_notifications(limit=limit, recent=recent),
            priority=10,
            rate_limit_key="get_notifications",
        )

    def mark_notifications_read(self) -> Dict[str, Any]:
        return self._call(
            self._inner.mark_notifications_read,
            priority=5,
            rate_limit_key="mark_notifications_read",
        )

    def post_timings(self, topic_id: int, timings: Dict[int, int], topic_time: int) -> None:
        return self._call(
            lambda: self._inner.post_timings(topic_id, timings, topic_time),
            priority=10,
            rate_limit_key="post_timings",
        )

    def toggle_reaction(self, post_id: int, emoji: str, timeout_secs: float | None = None) -> Dict[str, Any]:
        return self._call(
            lambda: self._inner.toggle_reaction(post_id, emoji),
            priority=1,
            rate_limit_key="toggle_reaction",
            timeout_secs=timeout_secs,
        )

    def reply(
        self,
        topic_id: int,
        raw: str,
        category: Optional[int] = None,
        timeout_secs: float | None = None,
    ) -> Dict[str, Any]:
        return self._call(
            lambda: self._inner.reply(topic_id, raw, category),
            priority=1,
            rate_limit_key="reply",
            timeout_secs=timeout_secs,
        )

    def get_cookie_header(self) -> str:
        return self._inner.get_cookie_header()

    def get_persist_candidate_cookie_header(self) -> str:
        getter = getattr(self._inner, "get_persist_candidate_cookie_header", None)
        if callable(getter):
            return str(getter() or "")
        return self.get_cookie_header()

    def last_response_ok(self) -> bool | None:
        return self._inner.last_response_ok()
