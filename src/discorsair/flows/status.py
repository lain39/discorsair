"""Status helpers."""

from __future__ import annotations

from discorsair.storage.sqlite_store import SQLiteStore


def status(store: SQLiteStore | None) -> dict[str, object]:
    if store is None:
        return {
            "storage_enabled": False,
            "stats_total": None,
            "stats_today": None,
            "storage_path": None,
        }
    return {
        "storage_enabled": True,
        "stats_total": store.get_stats_total(),
        "stats_today": store.get_stats_today(),
        "storage_path": store.current_path(),
    }
