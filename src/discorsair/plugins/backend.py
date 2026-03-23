"""Plugin state backends."""

from __future__ import annotations

import copy
from datetime import datetime
import threading
from typing import Any
from zoneinfo import ZoneInfo

from discorsair.storage import StoreBackend


class PluginStateBackend:
    kind = "unknown"

    def get_kv(self, plugin_id: str, key: str, default: Any = None) -> Any:
        raise NotImplementedError

    def set_kv(self, plugin_id: str, key: str, value: Any) -> None:
        raise NotImplementedError

    def get_daily_count(self, plugin_id: str, action: str) -> int:
        raise NotImplementedError

    def inc_daily_count(self, plugin_id: str, action: str, delta: int = 1) -> int:
        raise NotImplementedError

    def was_done(self, plugin_id: str, key: str) -> bool:
        raise NotImplementedError

    def mark_done(self, plugin_id: str, key: str) -> None:
        raise NotImplementedError

    def snapshot_plugin_state(self, plugin_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def log_action(
        self,
        *,
        cycle_id: str,
        plugin_id: str,
        hook_name: str,
        action: str,
        status: str,
        reason: str = "",
        topic_id: int | None = None,
        post_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        return None


class MemoryPluginStateBackend(PluginStateBackend):
    kind = "memory"

    def __init__(self, timezone_name: str) -> None:
        self._tz = ZoneInfo(timezone_name)
        self._lock = threading.RLock()
        self._kv: dict[tuple[str, str], Any] = {}
        self._daily: dict[tuple[str, str, str], int] = {}
        self._once: set[tuple[str, str]] = set()

    def _today(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    def get_kv(self, plugin_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            if (plugin_id, key) not in self._kv:
                return default
            return copy.deepcopy(self._kv[(plugin_id, key)])

    def set_kv(self, plugin_id: str, key: str, value: Any) -> None:
        with self._lock:
            self._kv[(plugin_id, key)] = copy.deepcopy(value)

    def get_daily_count(self, plugin_id: str, action: str) -> int:
        with self._lock:
            return int(self._daily.get((plugin_id, action, self._today()), 0))

    def inc_daily_count(self, plugin_id: str, action: str, delta: int = 1) -> int:
        day = self._today()
        key = (plugin_id, action, day)
        with self._lock:
            value = int(self._daily.get(key, 0)) + int(delta)
            self._daily[key] = value
            return value

    def was_done(self, plugin_id: str, key: str) -> bool:
        with self._lock:
            return (plugin_id, key) in self._once

    def mark_done(self, plugin_id: str, key: str) -> None:
        with self._lock:
            self._once.add((plugin_id, key))

    def snapshot_plugin_state(self, plugin_id: str) -> dict[str, Any]:
        today = self._today()
        with self._lock:
            daily_counts = {
                action: int(self._daily[(plugin_id, action, today)])
                for action in sorted(
                    action for owner, action, day in self._daily if owner == plugin_id and day == today
                )
            }
            kv_keys = sorted(key for owner, key in self._kv if owner == plugin_id)
            once_mark_count = sum(1 for owner, _ in self._once if owner == plugin_id)
            return {
                "daily_counts": daily_counts,
                "once_mark_count": once_mark_count,
                "kv_keys": kv_keys,
            }


class StorePluginStateBackend(PluginStateBackend):
    kind = "store"

    def __init__(self, store: StoreBackend) -> None:
        self._store = store
        self.kind = store.backend_name()

    def get_kv(self, plugin_id: str, key: str, default: Any = None) -> Any:
        return self._store.get_plugin_kv(plugin_id, key, default=default)

    def set_kv(self, plugin_id: str, key: str, value: Any) -> None:
        self._store.set_plugin_kv(plugin_id, key, value)

    def get_daily_count(self, plugin_id: str, action: str) -> int:
        return self._store.get_plugin_daily_count(plugin_id, action)

    def inc_daily_count(self, plugin_id: str, action: str, delta: int = 1) -> int:
        return self._store.inc_plugin_daily_count(plugin_id, action, delta=delta)

    def was_done(self, plugin_id: str, key: str) -> bool:
        return self._store.plugin_once_exists(plugin_id, key)

    def mark_done(self, plugin_id: str, key: str) -> None:
        self._store.mark_plugin_once(plugin_id, key)

    def snapshot_plugin_state(self, plugin_id: str) -> dict[str, Any]:
        return {
            "daily_counts": self._store.get_plugin_daily_counts(plugin_id),
            "once_mark_count": self._store.count_plugin_once_marks(plugin_id),
            "kv_keys": self._store.list_plugin_kv_keys(plugin_id),
        }

    def log_action(
        self,
        *,
        cycle_id: str,
        plugin_id: str,
        hook_name: str,
        action: str,
        status: str,
        reason: str = "",
        topic_id: int | None = None,
        post_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._store.log_plugin_action(
            cycle_id=cycle_id,
            plugin_id=plugin_id,
            hook_name=hook_name,
            action=action,
            status=status,
            reason=reason,
            topic_id=topic_id,
            post_id=post_id,
            extra=extra,
        )
