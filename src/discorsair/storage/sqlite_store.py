"""SQLite storage for topics and posts."""

from __future__ import annotations

import sqlite3
import threading
import logging
import json
from pathlib import Path
from typing import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo
import time


class SQLiteStore:
    def __init__(self, path: str, timezone_name: str = "UTC", rotate_daily: bool = False) -> None:
        self._base_path = Path(path)
        if self._base_path.parent:
            self._base_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_daily = bool(rotate_daily)
        self._lock = threading.RLock()
        try:
            self._tz = ZoneInfo(timezone_name)
        except Exception:
            self._tz = ZoneInfo("UTC")
        self._current_day = self._today()
        self._open_conn_for_day(self._current_day)

    def _today(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    def _path_for_day(self, day: str) -> Path:
        if not self._rotate_daily:
            return self._base_path
        base = str(self._base_path)
        if base.endswith(".db"):
            return Path(base.replace(".db", f".{day}.db"))
        return Path(f"{base}.{day}")

    def _open_conn_for_day(self, day: str) -> None:
        self._path = self._path_for_day(day)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        logging.getLogger(__name__).info("sqlite: open %s", self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._migrate()

    def _with_retry(self, fn, attempts: int = 3, base_delay: float = 0.2) -> None:
        for attempt in range(attempts):
            try:
                fn()
                return
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "database is locked" not in msg and "database is busy" not in msg:
                    raise
                if attempt == attempts - 1:
                    raise
                time.sleep(base_delay * (2 ** attempt))

    def _ensure_conn(self) -> None:
        if not self._rotate_daily:
            return
        day = self._today()
        if day == self._current_day:
            return
        with self._lock:
            day = self._today()
            if day == self._current_day:
                return
            self._conn.close()
            self._current_day = day
            self._open_conn_for_day(day)

    def current_path(self) -> str:
        self._ensure_conn()
        return str(self._path)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                topic_id INTEGER PRIMARY KEY,
                last_synced_post_number INTEGER DEFAULT 0,
                last_read_post_number INTEGER DEFAULT 0,
                last_stream_len INTEGER DEFAULT 0,
                last_seen_at TEXT DEFAULT ''
            )
            """
        )
            self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                post_id INTEGER PRIMARY KEY,
                topic_id INTEGER NOT NULL,
                post_number INTEGER NOT NULL,
                created_at TEXT DEFAULT '',
                username TEXT DEFAULT '',
                cooked TEXT DEFAULT ''
            )
            """
        )
            self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications_sent (
                notification_id INTEGER PRIMARY KEY,
                created_at TEXT DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_daily_counters (
                plugin_id TEXT NOT NULL,
                action TEXT NOT NULL,
                day TEXT NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (plugin_id, action, day)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_once_marks (
                plugin_id TEXT NOT NULL,
                key TEXT NOT NULL,
                created_at TEXT DEFAULT '',
                PRIMARY KEY (plugin_id, key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_kv (
                plugin_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT DEFAULT '',
                PRIMARY KEY (plugin_id, key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_total (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                topics_seen INTEGER DEFAULT 0,
                posts_fetched INTEGER DEFAULT 0,
                timings_sent INTEGER DEFAULT 0,
                notifications_sent INTEGER DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_daily (
                day TEXT PRIMARY KEY,
                topics_seen INTEGER DEFAULT 0,
                posts_fetched INTEGER DEFAULT 0,
                timings_sent INTEGER DEFAULT 0,
                notifications_sent INTEGER DEFAULT 0
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_topic_id ON posts(topic_id)")
        self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            current = int(row[0]) if row else 0
            target = 3
            if current < target:
                self._ensure_column("topics", "last_synced_post_number", "INTEGER DEFAULT 0")
                self._backfill_last_synced_post_number()
                self._ensure_column("topics", "last_read_post_number", "INTEGER DEFAULT 0")
                self._ensure_table(
                    "notifications_sent",
                    """
                    CREATE TABLE IF NOT EXISTS notifications_sent (
                        notification_id INTEGER PRIMARY KEY,
                        created_at TEXT DEFAULT ''
                    )
                    """,
                )
                self._ensure_table(
                    "stats_total",
                    """
                    CREATE TABLE IF NOT EXISTS stats_total (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        topics_seen INTEGER DEFAULT 0,
                        posts_fetched INTEGER DEFAULT 0,
                        timings_sent INTEGER DEFAULT 0,
                        notifications_sent INTEGER DEFAULT 0
                    )
                    """,
                )
                self._ensure_table(
                    "stats_daily",
                    """
                    CREATE TABLE IF NOT EXISTS stats_daily (
                        day TEXT PRIMARY KEY,
                        topics_seen INTEGER DEFAULT 0,
                        posts_fetched INTEGER DEFAULT 0,
                        timings_sent INTEGER DEFAULT 0,
                        notifications_sent INTEGER DEFAULT 0
                    )
                    """,
                )
                self._ensure_table(
                    "plugin_daily_counters",
                    """
                    CREATE TABLE IF NOT EXISTS plugin_daily_counters (
                        plugin_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        day TEXT NOT NULL,
                        count INTEGER DEFAULT 0,
                        PRIMARY KEY (plugin_id, action, day)
                    )
                    """,
                )
                self._ensure_table(
                    "plugin_once_marks",
                    """
                    CREATE TABLE IF NOT EXISTS plugin_once_marks (
                        plugin_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        created_at TEXT DEFAULT '',
                        PRIMARY KEY (plugin_id, key)
                    )
                    """,
                )
                self._ensure_table(
                    "plugin_kv",
                    """
                    CREATE TABLE IF NOT EXISTS plugin_kv (
                        plugin_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value_json TEXT DEFAULT '',
                        PRIMARY KEY (plugin_id, key)
                    )
                    """,
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
                    (target,),
                )
                self._conn.commit()

    def _ensure_table(self, name: str, ddl: str) -> None:
        self._conn.execute(ddl)

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in cols}
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _backfill_last_synced_post_number(self) -> None:
        cols = self._conn.execute("PRAGMA table_info(topics)").fetchall()
        existing = {row[1] for row in cols}
        if "last_synced_post_number" not in existing or "last_post_number" not in existing:
            return
        self._conn.execute(
            """
            UPDATE topics
            SET last_synced_post_number = COALESCE(NULLIF(last_synced_post_number, 0), last_post_number, 0)
            """
        )

    def get_existing_post_ids(self, topic_id: int, post_ids: Iterable[int]) -> set[int]:
        ids = list(post_ids)
        if not ids:
            return set()
        self._ensure_conn()
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT post_id FROM posts WHERE topic_id = ? AND post_id IN ({placeholders})",
                [topic_id, *ids],
            ).fetchall()
        return {row[0] for row in rows}

    def insert_posts(self, topic_id: int, posts: Iterable[dict]) -> None:
        rows = []
        for post in posts:
            rows.append(
                (
                    int(post.get("id", 0)),
                    topic_id,
                    int(post.get("post_number", 0)),
                    post.get("created_at", ""),
                    post.get("username", ""),
                    post.get("cooked", ""),
                )
            )
        if not rows:
            return
        self._ensure_conn()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.executemany(
                        """
                        INSERT OR IGNORE INTO posts (post_id, topic_id, post_number, created_at, username, cooked)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    ),
                    self._conn.commit(),
                )
            )

    def upsert_topic(self, topic_id: int, last_synced_post_number: int, last_stream_len: int, last_seen_at: str) -> None:
        self._ensure_conn()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO topics (topic_id, last_synced_post_number, last_read_post_number, last_stream_len, last_seen_at)
                        VALUES (?, ?, 0, ?, ?)
                        ON CONFLICT(topic_id) DO UPDATE SET
                          last_synced_post_number = excluded.last_synced_post_number,
                          last_stream_len = excluded.last_stream_len,
                          last_seen_at = excluded.last_seen_at
                        """,
                        (topic_id, last_synced_post_number, last_stream_len, last_seen_at),
                    ),
                    self._conn.commit(),
                )
            )

    def get_last_synced_post_number(self, topic_id: int) -> int:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT last_synced_post_number FROM topics WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
        if not row:
            return 0
        return int(row[0] or 0)

    def get_last_read_post_number(self, topic_id: int) -> int:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT last_read_post_number FROM topics WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
        if not row:
            return 0
        return int(row[0] or 0)

    def update_last_read_post_number(self, topic_id: int, last_read_post_number: int) -> None:
        self._ensure_conn()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO topics (topic_id, last_synced_post_number, last_read_post_number, last_stream_len, last_seen_at)
                        VALUES (?, 0, ?, 0, '')
                        ON CONFLICT(topic_id) DO UPDATE SET
                          last_read_post_number = excluded.last_read_post_number
                        """,
                        (topic_id, last_read_post_number),
                    ),
                    self._conn.commit(),
                )
            )

    def close(self) -> None:
        self._ensure_conn()
        with self._lock:
            self._conn.close()

    def get_sent_notification_ids(self, ids: Iterable[int]) -> set[int]:
        id_list = list(ids)
        if not id_list:
            return set()
        self._ensure_conn()
        placeholders = ",".join("?" for _ in id_list)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT notification_id FROM notifications_sent WHERE notification_id IN ({placeholders})",
                id_list,
            ).fetchall()
        return {row[0] for row in rows}

    def mark_notifications_sent(self, items: Iterable[dict]) -> None:
        rows = []
        for item in items:
            nid = int(item.get("id", 0) or 0)
            if not nid:
                continue
            rows.append((nid, item.get("created_at", "")))
        if not rows:
            return
        self._ensure_conn()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO notifications_sent (notification_id, created_at) VALUES (?, ?)",
                        rows,
                    ),
                    self._conn.commit(),
                )
            )

    def inc_stat(self, field: str, delta: int = 1) -> None:
        if field not in {"topics_seen", "posts_fetched", "timings_sent", "notifications_sent"}:
            return
        self._ensure_conn()
        with self._lock:
            day = datetime.now(self._tz).strftime("%Y-%m-%d")
            self._with_retry(
                lambda: (
                    self._conn.execute("INSERT OR IGNORE INTO stats_total (id) VALUES (1)"),
                    self._conn.execute(
                        f"UPDATE stats_total SET {field} = {field} + ? WHERE id = 1",
                        (delta,),
                    ),
                    self._conn.execute("INSERT OR IGNORE INTO stats_daily (day) VALUES (?)", (day,)),
                    self._conn.execute(
                        f"UPDATE stats_daily SET {field} = {field} + ? WHERE day = ?",
                        (delta, day),
                    ),
                    self._conn.commit(),
                )
            )

    def get_stats_total(self) -> dict[str, int]:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT topics_seen, posts_fetched, timings_sent, notifications_sent FROM stats_total WHERE id = 1"
            ).fetchone()
        if not row:
            return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}
        return {
            "topics_seen": int(row[0] or 0),
            "posts_fetched": int(row[1] or 0),
            "timings_sent": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
        }

    def get_stats_today(self) -> dict[str, int]:
        self._ensure_conn()
        day = datetime.now(self._tz).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                "SELECT topics_seen, posts_fetched, timings_sent, notifications_sent FROM stats_daily WHERE day = ?",
                (day,),
            ).fetchone()
        if not row:
            return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}
        return {
            "topics_seen": int(row[0] or 0),
            "posts_fetched": int(row[1] or 0),
            "timings_sent": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
        }

    def get_plugin_daily_count(self, plugin_id: str, action: str) -> int:
        self._ensure_conn()
        day = datetime.now(self._tz).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT count FROM plugin_daily_counters
                WHERE plugin_id = ? AND action = ? AND day = ?
                """,
                (plugin_id, action, day),
            ).fetchone()
        if not row:
            return 0
        return int(row[0] or 0)

    def inc_plugin_daily_count(self, plugin_id: str, action: str, delta: int = 1) -> int:
        self._ensure_conn()
        day = datetime.now(self._tz).strftime("%Y-%m-%d")
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO plugin_daily_counters (plugin_id, action, day, count)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(plugin_id, action, day) DO UPDATE SET
                          count = count + excluded.count
                        """,
                        (plugin_id, action, day, int(delta)),
                    ),
                    self._conn.commit(),
                )
            )
            row = self._conn.execute(
                """
                SELECT count FROM plugin_daily_counters
                WHERE plugin_id = ? AND action = ? AND day = ?
                """,
                (plugin_id, action, day),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def get_plugin_daily_counts(self, plugin_id: str) -> dict[str, int]:
        self._ensure_conn()
        day = datetime.now(self._tz).strftime("%Y-%m-%d")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT action, count FROM plugin_daily_counters
                WHERE plugin_id = ? AND day = ?
                ORDER BY action
                """,
                (plugin_id, day),
            ).fetchall()
        return {str(action): int(count or 0) for action, count in rows}

    def plugin_once_exists(self, plugin_id: str, key: str) -> bool:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM plugin_once_marks WHERE plugin_id = ? AND key = ?",
                (plugin_id, key),
            ).fetchone()
        return row is not None

    def mark_plugin_once(self, plugin_id: str, key: str) -> None:
        self._ensure_conn()
        created_at = datetime.now(self._tz).isoformat()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO plugin_once_marks (plugin_id, key, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (plugin_id, key, created_at),
                    ),
                    self._conn.commit(),
                )
            )

    def count_plugin_once_marks(self, plugin_id: str) -> int:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM plugin_once_marks WHERE plugin_id = ?",
                (plugin_id,),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def get_plugin_kv(self, plugin_id: str, key: str, default: object = None) -> object:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM plugin_kv WHERE plugin_id = ? AND key = ?",
                (plugin_id, key),
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_plugin_kv(self, plugin_id: str, key: str, value: object) -> None:
        self._ensure_conn()
        value_json = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO plugin_kv (plugin_id, key, value_json)
                        VALUES (?, ?, ?)
                        ON CONFLICT(plugin_id, key) DO UPDATE SET
                          value_json = excluded.value_json
                        """,
                        (plugin_id, key, value_json),
                    ),
                    self._conn.commit(),
                )
            )

    def list_plugin_kv_keys(self, plugin_id: str) -> list[str]:
        self._ensure_conn()
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM plugin_kv WHERE plugin_id = ? ORDER BY key",
                (plugin_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]
