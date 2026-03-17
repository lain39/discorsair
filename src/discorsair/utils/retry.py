"""Retry helpers."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def retry(fn: Callable[[], T], attempts: int, delay_secs: float) -> T:
    last_err: Exception | None = None
    for _ in range(max(attempts, 1)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(delay_secs)
    if last_err is not None:
        raise last_err
    raise RuntimeError("retry: no attempts executed")
