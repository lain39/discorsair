"""Storage transfer tests."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.storage.sqlite_store import SQLiteStore
from discorsair.storage.postgres_store import assert_postgres_schema
from discorsair.storage.transfer import export_backend
from discorsair.storage.transfer import import_backend
from discorsair.storage.transfer import validate_import_bundle


class StorageTransferTests(unittest.TestCase):
    def test_assert_postgres_schema_rejects_missing_columns(self) -> None:
        class _Cursor:
            def __init__(self) -> None:
                self._rows = [("stats_total", "site_key")]

            def execute(self, sql, params=()) -> None:
                return None

            def fetchall(self):
                return list(self._rows)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _Conn:
            def cursor(self):
                return _Cursor()

        with self.assertRaisesRegex(ValueError, "postgres schema mismatch for table"):
            assert_postgres_schema(_Conn())

    def test_sqlite_export_and_import_round_trip_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.db"
            target_path = root / "target.db"
            export_dir = root / "export"

            source = SQLiteStore(
                str(source_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            try:
                self._seed_all_tables(source._conn)
            finally:
                source.close()

            export_result = export_backend(
                backend="sqlite",
                path=str(source_path),
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )

            self.assertEqual(export_result["action"], "export")
            self.assertEqual(export_result["backend"], "sqlite")
            self.assertEqual(export_result["tables"]["sites"], 1)
            self.assertEqual(export_result["tables"]["plugin_action_logs"], 1)
            self.assertTrue((export_dir / "meta.json").exists())
            self.assertTrue((export_dir / "posts.ndjson").exists())

            meta = json.loads((export_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["format"], "discorsair-ndjson-v1")
            self.assertEqual(meta["source_backend"], "sqlite")

            target = SQLiteStore(
                str(target_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            target.close()

            import_result = import_backend(backend="sqlite", path=str(target_path), input_dir=export_dir)
            self.assertEqual(import_result["action"], "import")
            self.assertEqual(import_result["tables"]["plugin_action_logs"], 1)

            import_backend(backend="sqlite", path=str(target_path), input_dir=export_dir)

            conn = sqlite3.connect(target_path)
            try:
                expected_counts = {
                    "sites": 1,
                    "accounts": 1,
                    "topic_crawl_state": 1,
                    "topics": 1,
                    "topic_snapshots": 1,
                    "posts": 1,
                    "notification_dedupe": 1,
                    "plugin_daily_counters": 1,
                    "plugin_once_marks": 1,
                    "plugin_kv": 1,
                    "watch_cycles": 1,
                    "plugin_action_logs": 1,
                    "stats_total": 1,
                    "stats_daily": 1,
                }
                for table, expected in expected_counts.items():
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    self.assertEqual(count, expected, table)
            finally:
                conn.close()

    def test_export_missing_sqlite_database_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.db"
            with self.assertRaisesRegex(ValueError, "sqlite database not found"):
                export_backend(
                    backend="sqlite",
                    path=str(missing),
                    output_dir=Path(tmpdir) / "export",
                    site_key="forum.example",
                    account_name="main",
                )

    def test_export_filters_rows_to_current_site_and_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.db"
            export_dir = root / "export"
            source = SQLiteStore(
                str(source_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            try:
                self._seed_all_tables(source._conn)
                source._conn.execute(
                    "INSERT INTO topics (site_key, topic_id, category_id, title, slug, tags_json, reply_count, views, like_count, highest_post_number, unseen, last_read_post_number, created_at, bumped_at, last_posted_at, first_post_updated_at, first_seen_at, synced_at) VALUES (?, ?, 0, '', '', '[]', 0, 0, 0, 0, 0, 0, '', '', '', '', '', '')",
                    ("other.example", 2),
                )
                source._conn.execute(
                    "INSERT INTO stats_total (site_key, account_name, topics_seen, posts_fetched, timings_sent, notifications_sent) VALUES (?, ?, ?, ?, ?, ?)",
                    ("forum.example", "other", 1, 1, 1, 1),
                )
                source._conn.commit()
            finally:
                source.close()

            export_backend(
                backend="sqlite",
                path=str(source_path),
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )

            topics_rows = [json.loads(line) for line in (export_dir / "topics.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
            stats_rows = [json.loads(line) for line in (export_dir / "stats_total.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(topics_rows), 1)
            self.assertEqual(topics_rows[0]["site_key"], "forum.example")
            self.assertEqual(len(stats_rows), 1)
            self.assertEqual(stats_rows[0]["account_name"], "main")

    def test_import_requires_complete_export_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target_path = root / "target.db"
            export_dir = root / "broken-export"
            export_dir.mkdir()
            (export_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "format": "discorsair-ndjson-v1",
                        "source_site_key": "forum.example",
                        "source_account_name": "main",
                    }
                ),
                encoding="utf-8",
            )
            target = SQLiteStore(
                str(target_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            target.close()

            with self.assertRaisesRegex(ValueError, "import input missing table file"):
                import_backend(backend="sqlite", path=str(target_path), input_dir=export_dir)

    def test_validate_import_bundle_rejects_site_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.db"
            export_dir = root / "export"
            source = SQLiteStore(
                str(source_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            source.close()

            export_backend(
                backend="sqlite",
                path=str(source_path),
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )

            with self.assertRaisesRegex(ValueError, "import site mismatch"):
                validate_import_bundle(
                    export_dir,
                    expected_site_key="other.example",
                    expected_account_name="main",
                )

    def test_validate_import_bundle_rejects_account_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.db"
            export_dir = root / "export"
            source = SQLiteStore(
                str(source_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            source.close()

            export_backend(
                backend="sqlite",
                path=str(source_path),
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )

            with self.assertRaisesRegex(ValueError, "import account mismatch"):
                validate_import_bundle(
                    export_dir,
                    expected_site_key="forum.example",
                    expected_account_name="other",
                )

    def test_import_rejects_rows_with_scope_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.db"
            target_path = root / "target.db"
            export_dir = root / "export"
            source = SQLiteStore(
                str(source_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            try:
                self._seed_all_tables(source._conn)
            finally:
                source.close()

            export_backend(
                backend="sqlite",
                path=str(source_path),
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )
            posts_path = export_dir / "posts.ndjson"
            rows = [json.loads(line) for line in posts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            rows[0]["site_key"] = "other.example"
            posts_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "import row scope mismatch in posts"):
                import_backend(backend="sqlite", path=str(target_path), input_dir=export_dir)

    def test_import_failure_does_not_leave_new_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.db"
            target_path = root / "target.db"
            export_dir = root / "export"
            source = SQLiteStore(
                str(source_path),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            source.close()

            export_backend(
                backend="sqlite",
                path=str(source_path),
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )
            stats_path = export_dir / "accounts.ndjson"
            rows = [json.loads(line) for line in stats_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            rows[0]["account_name"] = "other"
            stats_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "import row scope mismatch in accounts"):
                import_backend(backend="sqlite", path=str(target_path), input_dir=export_dir)

            self.assertFalse(target_path.exists())

    def _seed_all_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO topic_crawl_state (
                site_key, topic_id, last_synced_post_number, last_stream_len, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            ("forum.example", 1, 20, 20, "2026-03-23T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO topics (
                site_key, topic_id, category_id, title, slug, tags_json, reply_count, views, like_count,
                highest_post_number, unseen, last_read_post_number, created_at, bumped_at, last_posted_at,
                first_post_updated_at, first_seen_at, synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "forum.example",
                1,
                2,
                "Topic One",
                "topic-one",
                '["tag-a"]',
                5,
                10,
                3,
                20,
                1,
                7,
                "2026-03-20T00:00:00Z",
                "2026-03-23T00:00:00Z",
                "2026-03-23T00:00:00Z",
                "2026-03-22T00:00:00Z",
                "2026-03-20T00:00:00Z",
                "2026-03-23T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO topic_snapshots (
                site_key, topic_id, captured_at, first_post_updated_at, title, category_id, tags_json, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "forum.example",
                1,
                "2026-03-23T00:00:00Z",
                "2026-03-22T00:00:00Z",
                "Topic One",
                2,
                '["tag-a"]',
                '{"id":1}',
            ),
        )
        conn.execute(
            """
            INSERT INTO posts (
                site_key, post_id, topic_id, post_number, reply_to_post_number, username, created_at, updated_at,
                fetched_at, like_count, reply_count, reads, score, incoming_link_count, current_user_reaction,
                cooked, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "forum.example",
                101,
                1,
                1,
                0,
                "alice",
                "2026-03-20T00:00:00Z",
                "2026-03-22T00:00:00Z",
                "2026-03-23T00:00:00Z",
                4,
                1,
                9,
                1.5,
                2,
                "",
                "<p>hello</p>",
                '{"id":101}',
            ),
        )
        conn.execute(
            """
            INSERT INTO notification_dedupe (site_key, account_name, notification_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("forum.example", "main", 301, "2026-03-23T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO plugin_daily_counters (site_key, account_name, plugin_id, action, day, count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("forum.example", "main", "demo", "reply", "2026-03-23", 2),
        )
        conn.execute(
            """
            INSERT INTO plugin_once_marks (site_key, account_name, plugin_id, key, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("forum.example", "main", "demo", "done:1", "2026-03-23T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO plugin_kv (site_key, account_name, plugin_id, key, value_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("forum.example", "main", "demo", "answer", "42", "2026-03-23T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO watch_cycles (
                cycle_id, site_key, account_name, started_at, ended_at, topics_fetched, topics_entered,
                posts_fetched, notifications_sent, success, error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("cycle-1", "forum.example", "main", "2026-03-23T00:00:00Z", "2026-03-23T00:01:00Z", 3, 2, 1, 1, 1, ""),
        )
        conn.execute(
            """
            INSERT INTO plugin_action_logs (
                cycle_id, site_key, account_name, plugin_id, hook_name, action, topic_id, post_id,
                status, reason, created_at, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cycle-1",
                "forum.example",
                "main",
                "demo",
                "topics.fetched",
                "record_trigger",
                1,
                101,
                "applied",
                "",
                "2026-03-23T00:00:00Z",
                '{"count":1}',
            ),
        )
        conn.execute(
            """
            INSERT INTO stats_total (site_key, account_name, topics_seen, posts_fetched, timings_sent, notifications_sent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("forum.example", "main", 10, 8, 6, 4),
        )
        conn.execute(
            """
            INSERT INTO stats_daily (site_key, account_name, day, topics_seen, posts_fetched, timings_sent, notifications_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("forum.example", "main", "2026-03-23", 3, 2, 1, 1),
        )
        conn.commit()


if __name__ == "__main__":
    unittest.main()
