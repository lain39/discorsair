"""Status helpers."""

from __future__ import annotations

from typing import Any

from discorsair.storage.sqlite_store import SQLiteStore


def status(store: SQLiteStore | None, plugins: dict[str, Any] | None = None) -> dict[str, object]:
    plugins_payload = plugins if plugins is not None else {"enabled": False, "count": 0, "backend": None, "runtime_live": False, "items": []}
    if store is None:
        return {
            "storage_enabled": False,
            "stats_total": None,
            "stats_today": None,
            "storage_path": None,
            "plugins": plugins_payload,
        }
    return {
        "storage_enabled": True,
        "stats_total": store.get_stats_total(),
        "stats_today": store.get_stats_today(),
        "storage_path": store.current_path(),
        "plugins": plugins_payload,
    }
