"""Single-thread request queue for DiscourseClient."""

from __future__ import annotations

import itertools
import heapq
import logging
import threading
import time
from concurrent.futures import Future, TimeoutError
from typing import Any, Callable

from discorsair.core.requester import RateLimitedError

_LOG = logging.getLogger(__name__)

_POLL_TIMEOUT_SECS = 0.2


class RequestQueue:
    def __init__(self, maxsize: int = 0) -> None:
        self._maxsize = max(int(maxsize), 0)
        self._ready: list[tuple[int, int, str | None, Callable[[], Any], Future[Any]]] = []
        self._delayed: list[tuple[float, int, int, str | None, Callable[[], Any], Future[Any]]] = []
        self._cooldowns: dict[str, float] = {}
        self._running = 0
        self._stop = threading.Event()
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._counter = itertools.count(1)
        self._counter_lock = threading.Lock()

    def submit(
        self,
        fn: Callable[[], Any],
        priority: int = 10,
        rate_limit_key: str | None = None,
        not_before: float | None = None,
        fut: Future[Any] | None = None,
        seq: int | None = None,
    ) -> Future[Any]:
        future = fut or Future()
        is_reschedule = fut is not None
        if seq is None:
            with self._counter_lock:
                seq = next(self._counter)
        with self._cv:
            self._prune_stale_locked()
            now = time.monotonic()
            available_at = max(float(not_before or 0.0), self._cooldowns.get(rate_limit_key or "", 0.0))
            delay_seconds = max(available_at - now, 0.0)
            if not is_reschedule and self._maxsize > 0 and delay_seconds <= 0 and len(self._ready) + self._running >= self._maxsize:
                future.set_exception(TimeoutError("request queue full"))
                return future
            if self._stop.is_set():
                future.set_exception(RuntimeError("request queue stopped"))
                return future
            if delay_seconds > 0:
                heapq.heappush(self._delayed, (available_at, priority, seq, rate_limit_key, fn, future))
            else:
                heapq.heappush(self._ready, (priority, seq, rate_limit_key, fn, future))
            self._cv.notify()
        return future

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            for _, _, _, _, fut in self._ready:
                if not fut.done():
                    fut.set_exception(RuntimeError("request queue stopped"))
            for _, _, _, _, _, fut in self._delayed:
                if not fut.done():
                    fut.set_exception(RuntimeError("request queue stopped"))
            self._ready.clear()
            self._delayed.clear()
            self._cv.notify_all()

    def _run(self) -> None:
        while True:
            item = self._next_item()
            if item is None:
                return
            priority, seq, rate_limit_key, fn, fut = item
            if fut.cancelled() or fut.done():
                continue
            if self._stop.is_set():
                if not fut.done():
                    fut.set_exception(RuntimeError("request queue stopped"))
                return
            try:
                with self._cv:
                    self._running += 1
                result = fn()
                if fut.cancelled() or fut.done():
                    continue
                fut.set_result(result)
            except RateLimitedError as exc:
                wait_seconds = max(float(exc.wait_seconds), 0.0)
                if wait_seconds <= 0:
                    if fut.cancelled() or fut.done():
                        continue
                    fut.set_exception(exc)
                    continue
                available_at = time.monotonic() + wait_seconds
                if rate_limit_key:
                    self._cooldowns[rate_limit_key] = max(self._cooldowns.get(rate_limit_key, 0.0), available_at)
                if fut.cancelled() or fut.done():
                    _LOG.info(
                        "request queue: preserved cooldown for cancelled key=%s wait_seconds=%s detail=%s",
                        rate_limit_key or "-",
                        f"{wait_seconds:g}",
                        exc.detail or "-",
                    )
                    continue
                _LOG.info(
                    "request queue: rate limited key=%s wait_seconds=%s detail=%s",
                    rate_limit_key or "-",
                    f"{wait_seconds:g}",
                    exc.detail or "-",
                )
                self.submit(
                    fn,
                    priority=priority,
                    rate_limit_key=rate_limit_key,
                    not_before=available_at,
                    fut=fut,
                    seq=seq,
                )
            except Exception as exc:  # noqa: BLE001
                if fut.cancelled() or fut.done():
                    continue
                fut.set_exception(exc)
            finally:
                with self._cv:
                    if self._running > 0:
                        self._running -= 1
                    self._cv.notify_all()

    def _next_item(self) -> tuple[int, int, str | None, Callable[[], Any], Future[Any]] | None:
        with self._cv:
            while True:
                if self._stop.is_set():
                    return None
                self._prune_stale_locked()
                self._promote_delayed_locked()
                while self._ready:
                    priority, seq, rate_limit_key, fn, fut = heapq.heappop(self._ready)
                    if fut.cancelled() or fut.done():
                        continue
                    cooldown_until = self._cooldowns.get(rate_limit_key or "", 0.0)
                    now = time.monotonic()
                    if cooldown_until > now:
                        heapq.heappush(
                            self._delayed,
                            (cooldown_until, priority, seq, rate_limit_key, fn, fut),
                        )
                        self._promote_delayed_locked()
                        continue
                    return (priority, seq, rate_limit_key, fn, fut)
                wait_timeout = self._next_wait_timeout_locked()
                self._cv.wait(timeout=wait_timeout)

    def _promote_delayed_locked(self) -> None:
        now = time.monotonic()
        while self._delayed and self._delayed[0][0] <= now:
            _, priority, seq, rate_limit_key, fn, fut = heapq.heappop(self._delayed)
            if fut.cancelled() or fut.done():
                continue
            heapq.heappush(self._ready, (priority, seq, rate_limit_key, fn, fut))

    def _next_wait_timeout_locked(self) -> float | None:
        if not self._delayed:
            return None
        wait_seconds = self._delayed[0][0] - time.monotonic()
        if wait_seconds <= 0:
            return 0.0
        return min(wait_seconds, _POLL_TIMEOUT_SECS)

    def _prune_stale_locked(self) -> None:
        ready_filtered = [item for item in self._ready if not item[4].cancelled() and not item[4].done()]
        delayed_filtered = [item for item in self._delayed if not item[5].cancelled() and not item[5].done()]
        if len(ready_filtered) != len(self._ready):
            self._ready = ready_filtered
            heapq.heapify(self._ready)
        if len(delayed_filtered) != len(self._delayed):
            self._delayed = delayed_filtered
            heapq.heapify(self._delayed)
