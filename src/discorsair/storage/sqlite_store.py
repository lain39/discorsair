"""SQLite storage for runtime state and collected data."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


_SQLITE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sites (
        site_key TEXT PRIMARY KEY,
        base_url TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (site_key, account_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_crawl_state (
        site_key TEXT NOT NULL,
        topic_id INTEGER NOT NULL,
        last_synced_post_number INTEGER DEFAULT 0,
        last_stream_len INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (site_key, topic_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topics (
        site_key TEXT NOT NULL,
        topic_id INTEGER NOT NULL,
        category_id INTEGER DEFAULT 0,
        title TEXT DEFAULT '',
        slug TEXT DEFAULT '',
        tags_json TEXT DEFAULT '[]',
        reply_count INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        like_count INTEGER DEFAULT 0,
        highest_post_number INTEGER DEFAULT 0,
        unseen INTEGER DEFAULT 0,
        last_read_post_number INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        bumped_at TEXT DEFAULT '',
        last_posted_at TEXT DEFAULT '',
        first_post_updated_at TEXT DEFAULT '',
        first_seen_at TEXT DEFAULT '',
        synced_at TEXT DEFAULT '',
        PRIMARY KEY (site_key, topic_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_snapshots (
        site_key TEXT NOT NULL,
        topic_id INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        first_post_updated_at TEXT DEFAULT '',
        title TEXT DEFAULT '',
        category_id INTEGER DEFAULT 0,
        tags_json TEXT DEFAULT '[]',
        raw_json TEXT DEFAULT '',
        PRIMARY KEY (site_key, topic_id, captured_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        site_key TEXT NOT NULL,
        post_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL,
        post_number INTEGER NOT NULL,
        reply_to_post_number INTEGER DEFAULT 0,
        username TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        fetched_at TEXT DEFAULT '',
        like_count INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        reads INTEGER DEFAULT 0,
        score REAL DEFAULT 0,
        incoming_link_count INTEGER DEFAULT 0,
        current_user_reaction TEXT DEFAULT '',
        cooked TEXT DEFAULT '',
        raw_json TEXT DEFAULT '',
        PRIMARY KEY (site_key, post_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_dedupe (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        notification_id INTEGER NOT NULL,
        created_at TEXT DEFAULT '',
        PRIMARY KEY (site_key, account_name, notification_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_daily_counters (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        action TEXT NOT NULL,
        day TEXT NOT NULL,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (site_key, account_name, plugin_id, action, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_once_marks (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        key TEXT NOT NULL,
        created_at TEXT DEFAULT '',
        PRIMARY KEY (site_key, account_name, plugin_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_kv (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (site_key, account_name, plugin_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_cycles (
        cycle_id TEXT PRIMARY KEY,
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        started_at TEXT DEFAULT '',
        ended_at TEXT DEFAULT '',
        topics_fetched INTEGER DEFAULT 0,
        topics_entered INTEGER DEFAULT 0,
        posts_fetched INTEGER DEFAULT 0,
        notifications_sent INTEGER DEFAULT 0,
        success INTEGER DEFAULT 0,
        error_text TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_action_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id TEXT DEFAULT '',
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        hook_name TEXT DEFAULT '',
        action TEXT DEFAULT '',
        topic_id INTEGER DEFAULT 0,
        post_id INTEGER DEFAULT 0,
        status TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        extra_json TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_total (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        topics_seen INTEGER DEFAULT 0,
        posts_fetched INTEGER DEFAULT 0,
        timings_sent INTEGER DEFAULT 0,
        notifications_sent INTEGER DEFAULT 0,
        PRIMARY KEY (site_key, account_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_daily (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        day TEXT NOT NULL,
        topics_seen INTEGER DEFAULT 0,
        posts_fetched INTEGER DEFAULT 0,
        timings_sent INTEGER DEFAULT 0,
        notifications_sent INTEGER DEFAULT 0,
        PRIMARY KEY (site_key, account_name, day)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_site_topic_post_number ON posts(site_key, topic_id, post_number)",
    "CREATE INDEX IF NOT EXISTS idx_posts_site_created_at ON posts(site_key, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_topic_snapshots_site_topic_captured ON topic_snapshots(site_key, topic_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_topics_site_last_posted_at ON topics(site_key, last_posted_at)",
    "CREATE INDEX IF NOT EXISTS idx_topics_site_category_id ON topics(site_key, category_id)",
    "CREATE INDEX IF NOT EXISTS idx_watch_cycles_site_account_started_at ON watch_cycles(site_key, account_name, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_plugin_action_logs_site_plugin_created_at ON plugin_action_logs(site_key, plugin_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notification_dedupe_site_account_created_at ON notification_dedupe(site_key, account_name, created_at DESC)",
)

_SQLITE_EXPECTED_COLUMNS: dict[str, set[str]] = {
    "sites": {"site_key", "base_url", "created_at", "updated_at"},
    "accounts": {"site_key", "account_name", "created_at", "updated_at"},
    "topic_crawl_state": {"site_key", "topic_id", "last_synced_post_number", "last_stream_len", "updated_at"},
    "topics": {
        "site_key",
        "topic_id",
        "category_id",
        "title",
        "slug",
        "tags_json",
        "reply_count",
        "views",
        "like_count",
        "highest_post_number",
        "unseen",
        "last_read_post_number",
        "created_at",
        "bumped_at",
        "last_posted_at",
        "first_post_updated_at",
        "first_seen_at",
        "synced_at",
    },
    "topic_snapshots": {
        "site_key",
        "topic_id",
        "captured_at",
        "first_post_updated_at",
        "title",
        "category_id",
        "tags_json",
        "raw_json",
    },
    "posts": {
        "site_key",
        "post_id",
        "topic_id",
        "post_number",
        "reply_to_post_number",
        "username",
        "created_at",
        "updated_at",
        "fetched_at",
        "like_count",
        "reply_count",
        "reads",
        "score",
        "incoming_link_count",
        "current_user_reaction",
        "cooked",
        "raw_json",
    },
    "notification_dedupe": {"site_key", "account_name", "notification_id", "created_at"},
    "plugin_daily_counters": {"site_key", "account_name", "plugin_id", "action", "day", "count"},
    "plugin_once_marks": {"site_key", "account_name", "plugin_id", "key", "created_at"},
    "plugin_kv": {"site_key", "account_name", "plugin_id", "key", "value_json", "updated_at"},
    "watch_cycles": {
        "cycle_id",
        "site_key",
        "account_name",
        "started_at",
        "ended_at",
        "topics_fetched",
        "topics_entered",
        "posts_fetched",
        "notifications_sent",
        "success",
        "error_text",
    },
    "plugin_action_logs": {
        "id",
        "cycle_id",
        "site_key",
        "account_name",
        "plugin_id",
        "hook_name",
        "action",
        "topic_id",
        "post_id",
        "status",
        "reason",
        "created_at",
        "extra_json",
    },
    "stats_total": {"site_key", "account_name", "topics_seen", "posts_fetched", "timings_sent", "notifications_sent"},
    "stats_daily": {
        "site_key",
        "account_name",
        "day",
        "topics_seen",
        "posts_fetched",
        "timings_sent",
        "notifications_sent",
    },
}


def initialize_sqlite_schema(conn: sqlite3.Connection) -> None:
    for statement in _SQLITE_SCHEMA_STATEMENTS:
        conn.execute(statement)


def assert_sqlite_schema(conn: sqlite3.Connection) -> None:
    for table, columns in _SQLITE_EXPECTED_COLUMNS.items():
        found = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if found != columns:
            raise ValueError(f"sqlite schema mismatch for table {table}; delete the existing database and retry")


class SQLiteStore:
    def __init__(
        self,
        path: str,
        *,
        site_key: str,
        account_name: str,
        base_url: str,
        timezone_name: str = "UTC",
        initialize: bool = True,
        ensure_metadata: bool = True,
        read_only: bool = False,
    ) -> None:
        self._path = Path(path)
        if read_only and not self._path.exists():
            raise FileNotFoundError(f"sqlite database not found: {self._path}")
        if self._path.parent and not read_only:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._site_key = str(site_key or "").strip()
        self._account_name = str(account_name or "").strip()
        self._base_url = str(base_url or "").rstrip("/")
        if not self._site_key:
            raise ValueError("site_key is required")
        if not self._account_name:
            raise ValueError("account_name is required")
        self._lock = threading.RLock()
        try:
            self._tz = ZoneInfo(timezone_name)
        except Exception:
            self._tz = ZoneInfo("UTC")
        if read_only:
            self._conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
        logging.getLogger(__name__).info("sqlite: open %s site=%s account=%s", self._path, self._site_key, self._account_name)
        self._conn.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        try:
            if initialize:
                self._init_schema()
            self._assert_schema()
            if initialize and ensure_metadata:
                self._upsert_metadata_rows()
        except sqlite3.OperationalError as exc:
            raise ValueError("sqlite schema mismatch; delete the existing database and retry") from exc

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
                time.sleep(base_delay * (2**attempt))

    def _today(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    def _now_iso(self) -> str:
        return datetime.now(self._tz).isoformat()

    def current_path(self) -> str:
        return str(self._path)

    def backend_name(self) -> str:
        return "sqlite"

    def _init_schema(self) -> None:
        with self._lock:
            initialize_sqlite_schema(self._conn)
            self._conn.commit()

    def _assert_schema(self) -> None:
        assert_sqlite_schema(self._conn)

    def _upsert_metadata_rows(self) -> None:
        now = self._now_iso()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO sites (site_key, base_url, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(site_key) DO UPDATE SET
                          base_url = excluded.base_url,
                          updated_at = excluded.updated_at
                        """,
                        (self._site_key, self._base_url, now, now),
                    ),
                    self._conn.execute(
                        """
                        INSERT INTO accounts (site_key, account_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(site_key, account_name) DO UPDATE SET
                          updated_at = excluded.updated_at
                        """,
                        (self._site_key, self._account_name, now, now),
                    ),
                    self._conn.commit(),
                )
            )

    def _normalize_tags(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        tags: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            if name:
                tags.append(name)
        return tags

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _sanitize_topic_raw_json(self, topic: dict[str, Any]) -> str:
        raw = copy.deepcopy(topic)
        post_stream = raw.get("post_stream")
        if isinstance(post_stream, dict):
            posts = post_stream.get("posts")
            if isinstance(posts, list):
                post_stream["posts"] = posts[:1]
        return self._json_dumps(raw)

    def _existing_topic_snapshot_key(self, topic_id: int) -> tuple[str, str, int, str] | None:
        row = self._conn.execute(
            """
            SELECT first_post_updated_at, title, category_id, tags_json
            FROM topics
            WHERE site_key = ? AND topic_id = ?
            """,
            (self._site_key, topic_id),
        ).fetchone()
        if row is None:
            return None
        return (str(row[0] or ""), str(row[1] or ""), int(row[2] or 0), str(row[3] or "[]"))

    def upsert_topic_detail(self, topic_summary: dict[str, Any], topic: dict[str, Any]) -> None:
        topic_id = int(topic_summary.get("id", topic.get("id", 0)) or 0)
        if topic_id <= 0:
            return
        now = self._now_iso()
        post_stream = topic.get("post_stream", {})
        posts = post_stream.get("posts", []) if isinstance(post_stream, dict) else []
        first_post = posts[0] if isinstance(posts, list) and posts else {}
        category_id = int(topic.get("category_id", topic_summary.get("category_id", 0)) or 0)
        tags = self._normalize_tags(topic.get("tags", topic_summary.get("tags", [])))
        tags_json = self._json_dumps(tags)
        title = str(topic.get("title", topic_summary.get("title", "")) or "")
        first_post_updated_at = str(first_post.get("updated_at", "") or "")
        created_at = str(topic.get("created_at", first_post.get("created_at", "")) or "")
        bumped_at = str(topic.get("bumped_at", topic_summary.get("bumped_at", "")) or "")
        last_posted_at = str(topic.get("last_posted_at", topic_summary.get("last_posted_at", "")) or "")
        row = (
            self._site_key,
            topic_id,
            category_id,
            title,
            str(topic.get("slug", topic_summary.get("slug", "")) or ""),
            tags_json,
            int(topic.get("reply_count", topic_summary.get("reply_count", 0)) or 0),
            int(topic.get("views", topic_summary.get("views", 0)) or 0),
            int(topic.get("like_count", topic_summary.get("like_count", 0)) or 0),
            int(topic.get("highest_post_number", topic_summary.get("highest_post_number", 0)) or 0),
            1 if bool(topic_summary.get("unseen", False)) else 0,
            int(topic_summary.get("last_read_post_number", 0) or 0),
            created_at,
            bumped_at,
            last_posted_at,
            first_post_updated_at,
            now,
            now,
        )
        existing_snapshot_key = self._existing_topic_snapshot_key(topic_id)
        new_snapshot_key = (first_post_updated_at, title, category_id, tags_json)
        raw_json = self._sanitize_topic_raw_json(topic)
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO topics (
                            site_key, topic_id, category_id, title, slug, tags_json,
                            reply_count, views, like_count, highest_post_number,
                            unseen, last_read_post_number, created_at, bumped_at,
                            last_posted_at, first_post_updated_at, first_seen_at, synced_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(site_key, topic_id) DO UPDATE SET
                          category_id = excluded.category_id,
                          title = excluded.title,
                          slug = excluded.slug,
                          tags_json = excluded.tags_json,
                          reply_count = excluded.reply_count,
                          views = excluded.views,
                          like_count = excluded.like_count,
                          highest_post_number = excluded.highest_post_number,
                          unseen = excluded.unseen,
                          last_read_post_number = excluded.last_read_post_number,
                          created_at = excluded.created_at,
                          bumped_at = excluded.bumped_at,
                          last_posted_at = excluded.last_posted_at,
                          first_post_updated_at = excluded.first_post_updated_at,
                          synced_at = excluded.synced_at
                        """,
                        row,
                    ),
                    self._conn.execute(
                        """
                        INSERT INTO topic_snapshots (
                            site_key, topic_id, captured_at, first_post_updated_at,
                            title, category_id, tags_json, raw_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._site_key,
                            topic_id,
                            now,
                            first_post_updated_at,
                            title,
                            category_id,
                            tags_json,
                            raw_json,
                        ),
                    )
                    if existing_snapshot_key is None or existing_snapshot_key != new_snapshot_key
                    else None,
                    self._conn.commit(),
                )
            )

    def get_existing_post_ids(self, topic_id: int, post_ids: Iterable[int]) -> set[int]:
        ids = list(post_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT post_id FROM posts
                WHERE site_key = ? AND topic_id = ? AND post_id IN ({placeholders})
                """,
                [self._site_key, topic_id, *ids],
            ).fetchall()
        return {int(row[0]) for row in rows}

    def insert_posts(self, topic_id: int, posts: Iterable[dict[str, Any]]) -> None:
        now = self._now_iso()
        rows: list[tuple[Any, ...]] = []
        for post in posts:
            post_id = int(post.get("id", 0) or 0)
            if post_id <= 0:
                continue
            rows.append(
                (
                    self._site_key,
                    post_id,
                    topic_id,
                    int(post.get("post_number", 0) or 0),
                    int(post.get("reply_to_post_number", 0) or 0),
                    str(post.get("username", "") or ""),
                    str(post.get("created_at", "") or ""),
                    str(post.get("updated_at", "") or ""),
                    now,
                    int(post.get("reaction_users_count", 0) or 0),
                    int(post.get("reply_count", 0) or 0),
                    int(post.get("reads", 0) or 0),
                    float(post.get("score", 0) or 0),
                    int(post.get("incoming_link_count", 0) or 0),
                    str(post.get("current_user_reaction", "") or ""),
                    str(post.get("cooked", "") or ""),
                    self._json_dumps(post),
                )
            )
        if not rows:
            return
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.executemany(
                        """
                        INSERT INTO posts (
                            site_key, post_id, topic_id, post_number, reply_to_post_number,
                            username, created_at, updated_at, fetched_at, like_count,
                            reply_count, reads, score, incoming_link_count,
                            current_user_reaction, cooked, raw_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(site_key, post_id) DO UPDATE SET
                          topic_id = excluded.topic_id,
                          post_number = excluded.post_number,
                          reply_to_post_number = excluded.reply_to_post_number,
                          username = excluded.username,
                          created_at = excluded.created_at,
                          updated_at = excluded.updated_at,
                          like_count = excluded.like_count,
                          reply_count = excluded.reply_count,
                          reads = excluded.reads,
                          score = excluded.score,
                          incoming_link_count = excluded.incoming_link_count,
                          current_user_reaction = excluded.current_user_reaction,
                          cooked = excluded.cooked,
                          raw_json = excluded.raw_json
                        """,
                        rows,
                    ),
                    self._conn.commit(),
                )
            )

    def upsert_topic_crawl_state(self, topic_id: int, last_synced_post_number: int, last_stream_len: int) -> None:
        now = self._now_iso()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO topic_crawl_state (
                            site_key, topic_id, last_synced_post_number, last_stream_len, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(site_key, topic_id) DO UPDATE SET
                          last_synced_post_number = excluded.last_synced_post_number,
                          last_stream_len = excluded.last_stream_len,
                          updated_at = excluded.updated_at
                        """,
                        (self._site_key, topic_id, int(last_synced_post_number), int(last_stream_len), now),
                    ),
                    self._conn.commit(),
                )
            )

    def get_last_synced_post_number(self, topic_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT last_synced_post_number
                FROM topic_crawl_state
                WHERE site_key = ? AND topic_id = ?
                """,
                (self._site_key, topic_id),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def get_last_read_post_number(self, topic_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT last_read_post_number
                FROM topics
                WHERE site_key = ? AND topic_id = ?
                """,
                (self._site_key, topic_id),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def update_last_read_post_number(self, topic_id: int, last_read_post_number: int) -> None:
        now = self._now_iso()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO topics (
                            site_key, topic_id, category_id, title, slug, tags_json,
                            reply_count, views, like_count, highest_post_number,
                            unseen, last_read_post_number, created_at, bumped_at,
                            last_posted_at, first_post_updated_at, first_seen_at, synced_at
                        )
                        VALUES (?, ?, 0, '', '', '[]', 0, 0, 0, 0, 0, ?, '', '', '', '', ?, ?)
                        ON CONFLICT(site_key, topic_id) DO UPDATE SET
                          last_read_post_number = excluded.last_read_post_number,
                          synced_at = excluded.synced_at
                        """,
                        (self._site_key, topic_id, int(last_read_post_number), now, now),
                    ),
                    self._conn.commit(),
                )
            )

    def begin_watch_cycle(self, cycle_id: str, started_at: str) -> None:
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO watch_cycles (
                            cycle_id, site_key, account_name, started_at, ended_at,
                            topics_fetched, topics_entered, posts_fetched, notifications_sent,
                            success, error_text
                        )
                        VALUES (?, ?, ?, ?, '', 0, 0, 0, 0, 0, '')
                        """,
                        (cycle_id, self._site_key, self._account_name, started_at),
                    ),
                    self._conn.commit(),
                )
            )

    def finish_watch_cycle(
        self,
        cycle_id: str,
        *,
        ended_at: str,
        topics_fetched: int,
        topics_entered: int,
        posts_fetched: int,
        notifications_sent: int,
        success: bool,
        error_text: str = "",
    ) -> None:
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        UPDATE watch_cycles
                        SET ended_at = ?,
                            topics_fetched = ?,
                            topics_entered = ?,
                            posts_fetched = ?,
                            notifications_sent = ?,
                            success = ?,
                            error_text = ?
                        WHERE cycle_id = ?
                        """,
                        (
                            ended_at,
                            int(topics_fetched),
                            int(topics_entered),
                            int(posts_fetched),
                            int(notifications_sent),
                            1 if success else 0,
                            str(error_text or ""),
                            cycle_id,
                        ),
                    ),
                    self._conn.commit(),
                )
            )

    def get_sent_notification_ids(self, ids: Iterable[int]) -> set[int]:
        id_list = list(ids)
        if not id_list:
            return set()
        placeholders = ",".join("?" for _ in id_list)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT notification_id FROM notification_dedupe
                WHERE site_key = ? AND account_name = ? AND notification_id IN ({placeholders})
                """,
                [self._site_key, self._account_name, *id_list],
            ).fetchall()
        return {int(row[0]) for row in rows}

    def mark_notifications_sent(self, items: Iterable[dict[str, Any]]) -> None:
        rows: list[tuple[Any, ...]] = []
        for item in items:
            notification_id = int(item.get("id", 0) or 0)
            if notification_id <= 0:
                continue
            rows.append(
                (
                    self._site_key,
                    self._account_name,
                    notification_id,
                    str(item.get("created_at", "") or ""),
                )
            )
        if not rows:
            return
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.executemany(
                        """
                        INSERT OR IGNORE INTO notification_dedupe (
                            site_key, account_name, notification_id, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        rows,
                    ),
                    self._conn.commit(),
                )
            )

    def inc_stat(self, field: str, delta: int = 1) -> None:
        if field not in {"topics_seen", "posts_fetched", "timings_sent", "notifications_sent"}:
            return
        day = self._today()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        "INSERT OR IGNORE INTO stats_total (site_key, account_name) VALUES (?, ?)",
                        (self._site_key, self._account_name),
                    ),
                    self._conn.execute(
                        f"""
                        UPDATE stats_total
                        SET {field} = {field} + ?
                        WHERE site_key = ? AND account_name = ?
                        """,
                        (int(delta), self._site_key, self._account_name),
                    ),
                    self._conn.execute(
                        "INSERT OR IGNORE INTO stats_daily (site_key, account_name, day) VALUES (?, ?, ?)",
                        (self._site_key, self._account_name, day),
                    ),
                    self._conn.execute(
                        f"""
                        UPDATE stats_daily
                        SET {field} = {field} + ?
                        WHERE site_key = ? AND account_name = ? AND day = ?
                        """,
                        (int(delta), self._site_key, self._account_name, day),
                    ),
                    self._conn.commit(),
                )
            )

    def get_stats_total(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT topics_seen, posts_fetched, timings_sent, notifications_sent
                FROM stats_total
                WHERE site_key = ? AND account_name = ?
                """,
                (self._site_key, self._account_name),
            ).fetchone()
        if row is None:
            return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}
        return {
            "topics_seen": int(row[0] or 0),
            "posts_fetched": int(row[1] or 0),
            "timings_sent": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
        }

    def get_stats_today(self) -> dict[str, int]:
        day = self._today()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT topics_seen, posts_fetched, timings_sent, notifications_sent
                FROM stats_daily
                WHERE site_key = ? AND account_name = ? AND day = ?
                """,
                (self._site_key, self._account_name, day),
            ).fetchone()
        if row is None:
            return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}
        return {
            "topics_seen": int(row[0] or 0),
            "posts_fetched": int(row[1] or 0),
            "timings_sent": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
        }

    def get_plugin_daily_count(self, plugin_id: str, action: str) -> int:
        day = self._today()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT count FROM plugin_daily_counters
                WHERE site_key = ? AND account_name = ? AND plugin_id = ? AND action = ? AND day = ?
                """,
                (self._site_key, self._account_name, plugin_id, action, day),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def inc_plugin_daily_count(self, plugin_id: str, action: str, delta: int = 1) -> int:
        day = self._today()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO plugin_daily_counters (
                            site_key, account_name, plugin_id, action, day, count
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(site_key, account_name, plugin_id, action, day) DO UPDATE SET
                          count = count + excluded.count
                        """,
                        (self._site_key, self._account_name, plugin_id, action, day, int(delta)),
                    ),
                    self._conn.commit(),
                )
            )
        return self.get_plugin_daily_count(plugin_id, action)

    def get_plugin_daily_counts(self, plugin_id: str) -> dict[str, int]:
        day = self._today()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT action, count FROM plugin_daily_counters
                WHERE site_key = ? AND account_name = ? AND plugin_id = ? AND day = ?
                ORDER BY action
                """,
                (self._site_key, self._account_name, plugin_id, day),
            ).fetchall()
        return {str(action): int(count or 0) for action, count in rows}

    def plugin_once_exists(self, plugin_id: str, key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM plugin_once_marks
                WHERE site_key = ? AND account_name = ? AND plugin_id = ? AND key = ?
                """,
                (self._site_key, self._account_name, plugin_id, key),
            ).fetchone()
        return row is not None

    def mark_plugin_once(self, plugin_id: str, key: str) -> None:
        created_at = self._now_iso()
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO plugin_once_marks (
                            site_key, account_name, plugin_id, key, created_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (self._site_key, self._account_name, plugin_id, key, created_at),
                    ),
                    self._conn.commit(),
                )
            )

    def count_plugin_once_marks(self, plugin_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) FROM plugin_once_marks
                WHERE site_key = ? AND account_name = ? AND plugin_id = ?
                """,
                (self._site_key, self._account_name, plugin_id),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def get_plugin_kv(self, plugin_id: str, key: str, default: object = None) -> object:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT value_json FROM plugin_kv
                WHERE site_key = ? AND account_name = ? AND plugin_id = ? AND key = ?
                """,
                (self._site_key, self._account_name, plugin_id, key),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_plugin_kv(self, plugin_id: str, key: str, value: object) -> None:
        updated_at = self._now_iso()
        value_json = self._json_dumps(value)
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO plugin_kv (
                            site_key, account_name, plugin_id, key, value_json, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(site_key, account_name, plugin_id, key) DO UPDATE SET
                          value_json = excluded.value_json,
                          updated_at = excluded.updated_at
                        """,
                        (self._site_key, self._account_name, plugin_id, key, value_json, updated_at),
                    ),
                    self._conn.commit(),
                )
            )

    def list_plugin_kv_keys(self, plugin_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT key FROM plugin_kv
                WHERE site_key = ? AND account_name = ? AND plugin_id = ?
                ORDER BY key
                """,
                (self._site_key, self._account_name, plugin_id),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def log_plugin_action(
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
        created_at = self._now_iso()
        extra_json = self._json_dumps(extra or {})
        with self._lock:
            self._with_retry(
                lambda: (
                    self._conn.execute(
                        """
                        INSERT INTO plugin_action_logs (
                            cycle_id, site_key, account_name, plugin_id, hook_name,
                            action, topic_id, post_id, status, reason, created_at, extra_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cycle_id,
                            self._site_key,
                            self._account_name,
                            plugin_id,
                            hook_name,
                            action,
                            int(topic_id or 0),
                            int(post_id or 0),
                            status,
                            reason,
                            created_at,
                            extra_json,
                        ),
                    ),
                    self._conn.commit(),
                )
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
