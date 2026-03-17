"""Single-thread request queue for DiscourseClient."""

from __future__ import annotations

import queue
import threading
import itertools
import time
from concurrent.futures import Future, TimeoutError
from typing import Any, Callable


class RequestQueue:
    def __init__(self, maxsize: int = 0) -> None:
        self._q: "queue.PriorityQueue[tuple[int, int, float | None, Callable[[], Any], Future[Any]]]" = (
            queue.PriorityQueue(maxsize=maxsize)
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._counter = itertools.count(1)
        self._counter_lock = threading.Lock()

    def submit(
        self,
        fn: Callable[[], Any],
        priority: int = 10,
        deadline: float | None = None,
    ) -> Future[Any]:
        fut: Future[Any] = Future()
        with self._counter_lock:
            seq = next(self._counter)
        try:
            if self._q.maxsize > 0:
                self._q.put((priority, seq, deadline, fn, fut), block=False)
            else:
                self._q.put((priority, seq, deadline, fn, fut))
        except queue.Full:
            fut.set_exception(TimeoutError("request queue full"))
        return fut

    def stop(self) -> None:
        self._stop.set()
        while True:
            try:
                _, _, _, _, fut = self._q.get_nowait()
            except queue.Empty:
                break
            if not fut.done():
                fut.set_exception(RuntimeError("request queue stopped"))
        self._q.put((0, 0, None, lambda: None, Future()))

    def _run(self) -> None:
        while not self._stop.is_set():
            _, _, deadline, fn, fut = self._q.get()
            if self._stop.is_set():
                if not fut.done():
                    fut.set_exception(RuntimeError("request queue stopped"))
                return
            if fut.cancelled():
                continue
            if deadline is not None and time.monotonic() > deadline:
                if not fut.done():
                    fut.set_exception(TimeoutError("request queue timeout"))
                continue
            try:
                result = fn()
                fut.set_result(result)
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
