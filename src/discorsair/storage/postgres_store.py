"""PostgreSQL storage for runtime state and collected data."""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo


_DSN_PASSWORD_RE = re.compile(r":([^:@/]+)@")

_POSTGRES_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sites (
        site_key TEXT PRIMARY KEY,
        base_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, account_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_crawl_state (
        site_key TEXT NOT NULL,
        topic_id BIGINT NOT NULL,
        last_synced_post_number BIGINT NOT NULL DEFAULT 0,
        last_stream_len INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, topic_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topics (
        site_key TEXT NOT NULL,
        topic_id BIGINT NOT NULL,
        category_id BIGINT NOT NULL DEFAULT 0,
        title TEXT NOT NULL DEFAULT '',
        slug TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]',
        reply_count INTEGER NOT NULL DEFAULT 0,
        views INTEGER NOT NULL DEFAULT 0,
        like_count INTEGER NOT NULL DEFAULT 0,
        highest_post_number BIGINT NOT NULL DEFAULT 0,
        unseen INTEGER NOT NULL DEFAULT 0,
        last_read_post_number BIGINT NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        bumped_at TEXT NOT NULL DEFAULT '',
        last_posted_at TEXT NOT NULL DEFAULT '',
        first_post_updated_at TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL DEFAULT '',
        synced_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, topic_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_snapshots (
        site_key TEXT NOT NULL,
        topic_id BIGINT NOT NULL,
        captured_at TEXT NOT NULL,
        first_post_updated_at TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        category_id BIGINT NOT NULL DEFAULT 0,
        tags_json TEXT NOT NULL DEFAULT '[]',
        raw_json TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, topic_id, captured_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        site_key TEXT NOT NULL,
        post_id BIGINT NOT NULL,
        topic_id BIGINT NOT NULL,
        post_number INTEGER NOT NULL,
        reply_to_post_number INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        fetched_at TEXT NOT NULL DEFAULT '',
        like_count INTEGER NOT NULL DEFAULT 0,
        reply_count INTEGER NOT NULL DEFAULT 0,
        reads INTEGER NOT NULL DEFAULT 0,
        score DOUBLE PRECISION NOT NULL DEFAULT 0,
        incoming_link_count INTEGER NOT NULL DEFAULT 0,
        current_user_reaction TEXT NOT NULL DEFAULT '',
        cooked TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, post_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_dedupe (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        notification_id BIGINT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
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
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (site_key, account_name, plugin_id, action, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_once_marks (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, account_name, plugin_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_kv (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (site_key, account_name, plugin_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_cycles (
        cycle_id TEXT PRIMARY KEY,
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT '',
        ended_at TEXT NOT NULL DEFAULT '',
        topics_fetched INTEGER NOT NULL DEFAULT 0,
        topics_entered INTEGER NOT NULL DEFAULT 0,
        posts_fetched INTEGER NOT NULL DEFAULT 0,
        notifications_sent INTEGER NOT NULL DEFAULT 0,
        success INTEGER NOT NULL DEFAULT 0,
        error_text TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_action_logs (
        id BIGSERIAL PRIMARY KEY,
        cycle_id TEXT NOT NULL DEFAULT '',
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        hook_name TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        topic_id BIGINT NOT NULL DEFAULT 0,
        post_id BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        extra_json TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_total (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        topics_seen INTEGER NOT NULL DEFAULT 0,
        posts_fetched INTEGER NOT NULL DEFAULT 0,
        timings_sent INTEGER NOT NULL DEFAULT 0,
        notifications_sent INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (site_key, account_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_daily (
        site_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        day TEXT NOT NULL,
        topics_seen INTEGER NOT NULL DEFAULT 0,
        posts_fetched INTEGER NOT NULL DEFAULT 0,
        timings_sent INTEGER NOT NULL DEFAULT 0,
        notifications_sent INTEGER NOT NULL DEFAULT 0,
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

_POSTGRES_EXPECTED_COLUMNS: dict[str, set[str]] = {
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


def initialize_postgres_schema(conn) -> None:
    with conn.cursor() as cur:
        for statement in _POSTGRES_SCHEMA_STATEMENTS:
            cur.execute(statement)


def postgres_schema_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'stats_total')")
        row = cur.fetchone()
    return bool(row[0]) if row else False


def assert_postgres_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ANY(%s)
            """,
            (list(_POSTGRES_EXPECTED_COLUMNS.keys()),),
        )
        rows = cur.fetchall()
    found: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        table_key = str(table_name or "")
        found.setdefault(table_key, set()).add(str(column_name or ""))
    for table_name, expected_columns in _POSTGRES_EXPECTED_COLUMNS.items():
        actual_columns = found.get(table_name)
        if actual_columns is None:
            raise ValueError(f"postgres schema mismatch for table {table_name}; initialize a fresh schema and retry")
        if actual_columns != expected_columns:
            raise ValueError(f"postgres schema mismatch for table {table_name}; initialize a fresh schema and retry")


class PostgresStore:
    def __init__(
        self,
        dsn: str,
        *,
        site_key: str,
        account_name: str,
        base_url: str,
        timezone_name: str = "UTC",
        initialize: bool = True,
        ensure_metadata: bool = True,
        read_only: bool = False,
    ) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("postgres backend requires psycopg; install it before use") from exc

        self._psycopg = psycopg
        self._dsn = str(dsn or "").strip()
        self._site_key = str(site_key or "").strip()
        self._account_name = str(account_name or "").strip()
        self._base_url = str(base_url or "").rstrip("/")
        self._read_only = bool(read_only)
        if not self._dsn:
            raise ValueError("postgres dsn is required")
        if not self._site_key:
            raise ValueError("site_key is required")
        if not self._account_name:
            raise ValueError("account_name is required")
        try:
            self._tz = ZoneInfo(timezone_name)
        except Exception:
            self._tz = ZoneInfo("UTC")
        self._conn = psycopg.connect(self._dsn)
        if self._read_only:
            self._set_connection_read_only(True)
        logging.getLogger(__name__).info(
            "postgres: open %s site=%s account=%s",
            _redact_dsn(self._dsn),
            self._site_key,
            self._account_name,
        )
        if initialize:
            self._init_schema()
            self._assert_schema()
            if ensure_metadata:
                self._upsert_metadata_rows()
        elif read_only:
            if not self._schema_exists():
                raise ValueError("postgres schema not initialized")
            self._assert_schema()

    def _today(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    def _now_iso(self) -> str:
        return datetime.now(self._tz).isoformat()

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _normalize_tags(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    def current_path(self) -> str:
        return _redact_dsn(self._dsn)

    def backend_name(self) -> str:
        return "postgres"

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._run_transaction(lambda cur: cur.execute(sql, params))

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        def _query(cur):
            cur.execute(sql, params)
            return cur.fetchone()

        return self._run_query(_query)

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        def _query(cur):
            cur.execute(sql, params)
            return list(cur.fetchall())

        return self._run_query(_query)

    def _run_query(self, fn):
        try:
            with self._conn.cursor() as cur:
                return fn(cur)
        except Exception:
            self._conn.rollback()
            raise

    def _run_transaction(self, fn):
        try:
            with self._conn.cursor() as cur:
                result = fn(cur)
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
            raise

    def _schema_exists(self) -> bool:
        try:
            return postgres_schema_exists(self._conn)
        except Exception:
            self._conn.rollback()
            raise

    def _assert_schema(self) -> None:
        try:
            assert_postgres_schema(self._conn)
        except Exception:
            self._conn.rollback()
            raise

    def _set_connection_read_only(self, enabled: bool) -> None:
        try:
            self._conn.read_only = bool(enabled)
            return
        except Exception:
            statement = "SET default_transaction_read_only = on" if enabled else "SET default_transaction_read_only = off"
            self._execute(statement)

    def _init_schema(self) -> None:
        try:
            initialize_postgres_schema(self._conn)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _upsert_metadata_rows(self) -> None:
        now = self._now_iso()
        def _write(cur) -> None:
            cur.execute(
                """
                INSERT INTO sites (site_key, base_url, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(site_key) DO UPDATE SET
                  base_url = EXCLUDED.base_url,
                  updated_at = EXCLUDED.updated_at
                """,
                (self._site_key, self._base_url, now, now),
            )
            cur.execute(
                """
                INSERT INTO accounts (site_key, account_name, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(site_key, account_name) DO UPDATE SET
                  updated_at = EXCLUDED.updated_at
                """,
                (self._site_key, self._account_name, now, now),
            )

        self._run_transaction(_write)

    def _sanitize_topic_raw_json(self, topic: dict[str, Any]) -> str:
        raw = copy.deepcopy(topic)
        post_stream = raw.get("post_stream")
        if isinstance(post_stream, dict):
            posts = post_stream.get("posts")
            if isinstance(posts, list):
                post_stream["posts"] = posts[:1]
        return self._json_dumps(raw)

    def _existing_topic_snapshot_key(self, topic_id: int) -> tuple[str, str, int, str] | None:
        row = self._fetchone(
            """
            SELECT first_post_updated_at, title, category_id, tags_json
            FROM topics
            WHERE site_key = %s AND topic_id = %s
            """,
            (self._site_key, topic_id),
        )
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
        existing_snapshot_key = self._existing_topic_snapshot_key(topic_id)
        new_snapshot_key = (first_post_updated_at, title, category_id, tags_json)
        raw_json = self._sanitize_topic_raw_json(topic)
        def _write(cur) -> None:
            cur.execute(
                """
                INSERT INTO topics (
                    site_key, topic_id, category_id, title, slug, tags_json,
                    reply_count, views, like_count, highest_post_number,
                    unseen, last_read_post_number, created_at, bumped_at,
                    last_posted_at, first_post_updated_at, first_seen_at, synced_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(site_key, topic_id) DO UPDATE SET
                  category_id = EXCLUDED.category_id,
                  title = EXCLUDED.title,
                  slug = EXCLUDED.slug,
                  tags_json = EXCLUDED.tags_json,
                  reply_count = EXCLUDED.reply_count,
                  views = EXCLUDED.views,
                  like_count = EXCLUDED.like_count,
                  highest_post_number = EXCLUDED.highest_post_number,
                  unseen = EXCLUDED.unseen,
                  last_read_post_number = EXCLUDED.last_read_post_number,
                  created_at = EXCLUDED.created_at,
                  bumped_at = EXCLUDED.bumped_at,
                  last_posted_at = EXCLUDED.last_posted_at,
                  first_post_updated_at = EXCLUDED.first_post_updated_at,
                  synced_at = EXCLUDED.synced_at
                """,
                (
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
                ),
            )
            if existing_snapshot_key is None or existing_snapshot_key != new_snapshot_key:
                cur.execute(
                    """
                    INSERT INTO topic_snapshots (
                        site_key, topic_id, captured_at, first_post_updated_at,
                        title, category_id, tags_json, raw_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (self._site_key, topic_id, now, first_post_updated_at, title, category_id, tags_json, raw_json),
                )

        self._run_transaction(_write)

    def get_existing_post_ids(self, topic_id: int, post_ids: Iterable[int]) -> set[int]:
        ids = list(post_ids)
        if not ids:
            return set()
        sql = (
            "SELECT post_id FROM posts WHERE site_key = %s AND topic_id = %s "
            "AND post_id = ANY(%s)"
        )
        rows = self._fetchall(sql, (self._site_key, topic_id, ids))
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
                    int(post.get("like_count", 0) or 0),
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
        def _write(cur) -> None:
            cur.executemany(
                """
                INSERT INTO posts (
                    site_key, post_id, topic_id, post_number, reply_to_post_number,
                    username, created_at, updated_at, fetched_at, like_count,
                    reply_count, reads, score, incoming_link_count,
                    current_user_reaction, cooked, raw_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(site_key, post_id) DO UPDATE SET
                  topic_id = EXCLUDED.topic_id,
                  post_number = EXCLUDED.post_number,
                  reply_to_post_number = EXCLUDED.reply_to_post_number,
                  username = EXCLUDED.username,
                  created_at = EXCLUDED.created_at,
                  updated_at = EXCLUDED.updated_at,
                  like_count = EXCLUDED.like_count,
                  reply_count = EXCLUDED.reply_count,
                  reads = EXCLUDED.reads,
                  score = EXCLUDED.score,
                  incoming_link_count = EXCLUDED.incoming_link_count,
                  current_user_reaction = EXCLUDED.current_user_reaction,
                  cooked = EXCLUDED.cooked,
                  raw_json = EXCLUDED.raw_json
                """,
                rows,
            )

        self._run_transaction(_write)

    def upsert_topic_crawl_state(self, topic_id: int, last_synced_post_number: int, last_stream_len: int) -> None:
        self._execute(
            """
            INSERT INTO topic_crawl_state (
                site_key, topic_id, last_synced_post_number, last_stream_len, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(site_key, topic_id) DO UPDATE SET
              last_synced_post_number = EXCLUDED.last_synced_post_number,
              last_stream_len = EXCLUDED.last_stream_len,
              updated_at = EXCLUDED.updated_at
            """,
            (self._site_key, topic_id, int(last_synced_post_number), int(last_stream_len), self._now_iso()),
        )

    def get_last_synced_post_number(self, topic_id: int) -> int:
        row = self._fetchone(
            "SELECT last_synced_post_number FROM topic_crawl_state WHERE site_key = %s AND topic_id = %s",
            (self._site_key, topic_id),
        )
        return int(row[0] or 0) if row else 0

    def get_last_read_post_number(self, topic_id: int) -> int:
        row = self._fetchone(
            "SELECT last_read_post_number FROM topics WHERE site_key = %s AND topic_id = %s",
            (self._site_key, topic_id),
        )
        return int(row[0] or 0) if row else 0

    def update_last_read_post_number(self, topic_id: int, last_read_post_number: int) -> None:
        now = self._now_iso()
        self._execute(
            """
            INSERT INTO topics (
                site_key, topic_id, category_id, title, slug, tags_json,
                reply_count, views, like_count, highest_post_number,
                unseen, last_read_post_number, created_at, bumped_at,
                last_posted_at, first_post_updated_at, first_seen_at, synced_at
            )
            VALUES (%s, %s, 0, '', '', '[]', 0, 0, 0, 0, 0, %s, '', '', '', '', %s, %s)
            ON CONFLICT(site_key, topic_id) DO UPDATE SET
              last_read_post_number = EXCLUDED.last_read_post_number,
              synced_at = EXCLUDED.synced_at
            """,
            (self._site_key, topic_id, int(last_read_post_number), now, now),
        )

    def begin_watch_cycle(self, cycle_id: str, started_at: str) -> None:
        self._execute(
            """
            INSERT INTO watch_cycles (
                cycle_id, site_key, account_name, started_at, ended_at,
                topics_fetched, topics_entered, posts_fetched, notifications_sent, success, error_text
            )
            VALUES (%s, %s, %s, %s, '', 0, 0, 0, 0, 0, '')
            ON CONFLICT(cycle_id) DO UPDATE SET
              started_at = EXCLUDED.started_at
            """,
            (cycle_id, self._site_key, self._account_name, started_at),
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
        self._execute(
            """
            UPDATE watch_cycles
            SET ended_at = %s,
                topics_fetched = %s,
                topics_entered = %s,
                posts_fetched = %s,
                notifications_sent = %s,
                success = %s,
                error_text = %s
            WHERE cycle_id = %s
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
        )

    def get_sent_notification_ids(self, ids: Iterable[int]) -> set[int]:
        id_list = list(ids)
        if not id_list:
            return set()
        rows = self._fetchall(
            """
            SELECT notification_id FROM notification_dedupe
            WHERE site_key = %s AND account_name = %s AND notification_id = ANY(%s)
            """,
            (self._site_key, self._account_name, id_list),
        )
        return {int(row[0]) for row in rows}

    def mark_notifications_sent(self, items: Iterable[dict[str, Any]]) -> None:
        rows = []
        for item in items:
            notification_id = int(item.get("id", 0) or 0)
            if notification_id <= 0:
                continue
            rows.append((self._site_key, self._account_name, notification_id, str(item.get("created_at", "") or "")))
        if not rows:
            return
        def _write(cur) -> None:
            cur.executemany(
                """
                INSERT INTO notification_dedupe (site_key, account_name, notification_id, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(site_key, account_name, notification_id) DO NOTHING
                """,
                rows,
            )

        self._run_transaction(_write)

    def inc_stat(self, field: str, delta: int = 1) -> None:
        if field not in {"topics_seen", "posts_fetched", "timings_sent", "notifications_sent"}:
            return
        day = self._today()
        def _write(cur) -> None:
            cur.execute(
                """
                INSERT INTO stats_total (site_key, account_name)
                VALUES (%s, %s)
                ON CONFLICT(site_key, account_name) DO NOTHING
                """,
                (self._site_key, self._account_name),
            )
            cur.execute(
                f"""
                UPDATE stats_total
                SET {field} = {field} + %s
                WHERE site_key = %s AND account_name = %s
                """,
                (int(delta), self._site_key, self._account_name),
            )
            cur.execute(
                """
                INSERT INTO stats_daily (site_key, account_name, day)
                VALUES (%s, %s, %s)
                ON CONFLICT(site_key, account_name, day) DO NOTHING
                """,
                (self._site_key, self._account_name, day),
            )
            cur.execute(
                f"""
                UPDATE stats_daily
                SET {field} = {field} + %s
                WHERE site_key = %s AND account_name = %s AND day = %s
                """,
                (int(delta), self._site_key, self._account_name, day),
            )

        self._run_transaction(_write)

    def get_stats_total(self) -> dict[str, int]:
        row = self._fetchone(
            """
            SELECT topics_seen, posts_fetched, timings_sent, notifications_sent
            FROM stats_total
            WHERE site_key = %s AND account_name = %s
            """,
            (self._site_key, self._account_name),
        )
        if row is None:
            return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}
        return {
            "topics_seen": int(row[0] or 0),
            "posts_fetched": int(row[1] or 0),
            "timings_sent": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
        }

    def get_stats_today(self) -> dict[str, int]:
        row = self._fetchone(
            """
            SELECT topics_seen, posts_fetched, timings_sent, notifications_sent
            FROM stats_daily
            WHERE site_key = %s AND account_name = %s AND day = %s
            """,
            (self._site_key, self._account_name, self._today()),
        )
        if row is None:
            return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}
        return {
            "topics_seen": int(row[0] or 0),
            "posts_fetched": int(row[1] or 0),
            "timings_sent": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
        }

    def get_plugin_daily_count(self, plugin_id: str, action: str) -> int:
        row = self._fetchone(
            """
            SELECT count FROM plugin_daily_counters
            WHERE site_key = %s AND account_name = %s AND plugin_id = %s AND action = %s AND day = %s
            """,
            (self._site_key, self._account_name, plugin_id, action, self._today()),
        )
        return int(row[0] or 0) if row else 0

    def inc_plugin_daily_count(self, plugin_id: str, action: str, delta: int = 1) -> int:
        day = self._today()
        self._execute(
            """
            INSERT INTO plugin_daily_counters (site_key, account_name, plugin_id, action, day, count)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(site_key, account_name, plugin_id, action, day) DO UPDATE SET
              count = plugin_daily_counters.count + EXCLUDED.count
            """,
            (self._site_key, self._account_name, plugin_id, action, day, int(delta)),
        )
        return self.get_plugin_daily_count(plugin_id, action)

    def get_plugin_daily_counts(self, plugin_id: str) -> dict[str, int]:
        rows = self._fetchall(
            """
            SELECT action, count FROM plugin_daily_counters
            WHERE site_key = %s AND account_name = %s AND plugin_id = %s AND day = %s
            ORDER BY action
            """,
            (self._site_key, self._account_name, plugin_id, self._today()),
        )
        return {str(action): int(count or 0) for action, count in rows}

    def plugin_once_exists(self, plugin_id: str, key: str) -> bool:
        row = self._fetchone(
            """
            SELECT 1 FROM plugin_once_marks
            WHERE site_key = %s AND account_name = %s AND plugin_id = %s AND key = %s
            """,
            (self._site_key, self._account_name, plugin_id, key),
        )
        return row is not None

    def mark_plugin_once(self, plugin_id: str, key: str) -> None:
        self._execute(
            """
            INSERT INTO plugin_once_marks (site_key, account_name, plugin_id, key, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(site_key, account_name, plugin_id, key) DO NOTHING
            """,
            (self._site_key, self._account_name, plugin_id, key, self._now_iso()),
        )

    def count_plugin_once_marks(self, plugin_id: str) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) FROM plugin_once_marks
            WHERE site_key = %s AND account_name = %s AND plugin_id = %s
            """,
            (self._site_key, self._account_name, plugin_id),
        )
        return int(row[0] or 0) if row else 0

    def get_plugin_kv(self, plugin_id: str, key: str, default: object = None) -> object:
        row = self._fetchone(
            """
            SELECT value_json FROM plugin_kv
            WHERE site_key = %s AND account_name = %s AND plugin_id = %s AND key = %s
            """,
            (self._site_key, self._account_name, plugin_id, key),
        )
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_plugin_kv(self, plugin_id: str, key: str, value: object) -> None:
        self._execute(
            """
            INSERT INTO plugin_kv (site_key, account_name, plugin_id, key, value_json, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(site_key, account_name, plugin_id, key) DO UPDATE SET
              value_json = EXCLUDED.value_json,
              updated_at = EXCLUDED.updated_at
            """,
            (self._site_key, self._account_name, plugin_id, key, self._json_dumps(value), self._now_iso()),
        )

    def list_plugin_kv_keys(self, plugin_id: str) -> list[str]:
        rows = self._fetchall(
            """
            SELECT key FROM plugin_kv
            WHERE site_key = %s AND account_name = %s AND plugin_id = %s
            ORDER BY key
            """,
            (self._site_key, self._account_name, plugin_id),
        )
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
        self._execute(
            """
            INSERT INTO plugin_action_logs (
                cycle_id, site_key, account_name, plugin_id, hook_name,
                action, topic_id, post_id, status, reason, created_at, extra_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                self._now_iso(),
                self._json_dumps(extra or {}),
            ),
        )

    def close(self) -> None:
        self._conn.close()


def _redact_dsn(dsn: str) -> str:
    return _DSN_PASSWORD_RE.sub(":***@", str(dsn or ""))
