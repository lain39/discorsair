"""Status helpers."""

from __future__ import annotations

from discorsair.storage.sqlite_store import SQLiteStore


def status(store: SQLiteStore) -> dict[str, object]:
    return {
        "stats_total": store.get_stats_total(),
        "stats_today": store.get_stats_today(),
        "storage_path": store.current_path(),
    }
