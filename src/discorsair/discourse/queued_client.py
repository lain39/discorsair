"""Queued wrapper to serialize DiscourseClient calls."""

from __future__ import annotations

from typing import Any, Dict, Optional
import time

from discorsair.core.request_queue import RequestQueue
from discorsair.discourse.client import DiscourseClient


class QueuedDiscourseClient:
    def __init__(self, inner: DiscourseClient, queue: RequestQueue, timeout_secs: float | None = None) -> None:
        self._inner = inner
        self._queue = queue
        self._timeout_secs = timeout_secs

    def _call(self, fn, priority: int = 10):
        deadline = None
        if self._timeout_secs is not None:
            deadline = time.monotonic() + self._timeout_secs
        fut = self._queue.submit(fn, priority=priority, deadline=deadline)
        try:
            return fut.result(timeout=self._timeout_secs)
        except TimeoutError:
            fut.cancel()
            raise

    def get_latest(self) -> Dict[str, Any]:
        return self._call(self._inner.get_latest, priority=10)

    def get_unseen(self) -> Dict[str, Any]:
        return self._call(self._inner.get_unseen, priority=10)

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True) -> Dict[str, Any]:
        return self._call(lambda: self._inner.get_topic(topic_id, track_visit, force_load), priority=10)

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]) -> Dict[str, Any]:
        return self._call(lambda: self._inner.get_posts_by_ids(topic_id, post_ids), priority=10)

    def get_csrf(self, force_refresh: bool = False) -> str:
        return self._call(lambda: self._inner.get_csrf(force_refresh=force_refresh), priority=5)

    def get_notifications(self, limit: int = 30, recent: bool = True) -> Dict[str, Any]:
        return self._call(lambda: self._inner.get_notifications(limit=limit, recent=recent), priority=10)

    def post_timings(self, topic_id: int, timings: Dict[int, int], topic_time: int) -> None:
        return self._call(lambda: self._inner.post_timings(topic_id, timings, topic_time), priority=10)

    def toggle_reaction(self, post_id: int, emoji: str) -> Dict[str, Any]:
        return self._call(lambda: self._inner.toggle_reaction(post_id, emoji), priority=1)

    def reply(self, topic_id: int, raw: str, category: Optional[int] = None) -> Dict[str, Any]:
        return self._call(lambda: self._inner.reply(topic_id, raw, category), priority=1)

    def get_cookie_header(self) -> str:
        return self._inner.get_cookie_header()

    def last_response_ok(self) -> bool | None:
        return self._inner.last_response_ok()
