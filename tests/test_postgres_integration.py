"""PostgreSQL integration tests.

These tests are opt-in and require DISCORSAIR_PG_TEST_DSN.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discorsair.runtime.factory import load_runtime_app_config
from discorsair.runtime.factory import load_settings
from discorsair.runtime.factory import open_store
from discorsair.storage.postgres_store import PostgresStore
from discorsair.storage.transfer import export_backend
from discorsair.storage.transfer import import_backend


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    split = urlsplit(dsn)
    query = parse_qsl(split.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dsn = os.getenv("DISCORSAIR_PG_TEST_DSN", "").strip()
        if not dsn:
            raise unittest.SkipTest("DISCORSAIR_PG_TEST_DSN is not set")
        try:
            import psycopg  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"psycopg is not installed: {exc}") from exc
        cls._base_dsn = dsn
        cls._psycopg = psycopg

    def setUp(self) -> None:
        self._source_schema = f"discorsair_src_{uuid.uuid4().hex[:12]}"
        self._target_schema = f"discorsair_dst_{uuid.uuid4().hex[:12]}"
        self._create_schema(self._source_schema)
        self._create_schema(self._target_schema)
        self.addCleanup(self._drop_schema, self._target_schema)
        self.addCleanup(self._drop_schema, self._source_schema)
        self.source_dsn = _dsn_with_search_path(self._base_dsn, self._source_schema)
        self.target_dsn = _dsn_with_search_path(self._base_dsn, self._target_schema)

    def _admin_conn(self):
        return self._psycopg.connect(self._base_dsn)

    def _create_schema(self, schema: str) -> None:
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')

    def _drop_schema(self, schema: str) -> None:
        with self._admin_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    def _seed_source(self) -> None:
        store = PostgresStore(
            self.source_dsn,
            site_key="forum.example",
            account_name="main",
            base_url="https://forum.example",
            timezone_name="UTC",
        )
        try:
            topic_summary = {
                "id": 1,
                "title": "Topic One",
                "slug": "topic-one",
                "category_id": 2,
                "tags": [{"id": 11, "name": "tag-a", "slug": "tag-a"}],
                "reply_count": 5,
                "views": 10,
                "like_count": 3,
                "highest_post_number": 2,
                "unseen": True,
                "last_read_post_number": 0,
                "bumped_at": "2026-03-23T00:00:00Z",
                "last_posted_at": "2026-03-23T00:00:00Z",
            }
            topic = {
                "id": 1,
                "title": "Topic One",
                "slug": "topic-one",
                "category_id": 2,
                "tags": [{"id": 11, "name": "tag-a", "slug": "tag-a"}],
                "reply_count": 5,
                "views": 10,
                "like_count": 3,
                "highest_post_number": 2,
                "created_at": "2026-03-20T00:00:00Z",
                "bumped_at": "2026-03-23T00:00:00Z",
                "last_posted_at": "2026-03-23T00:00:00Z",
                "post_stream": {
                    "stream": [101, 102],
                    "posts": [
                        {
                            "id": 101,
                            "post_number": 1,
                            "reply_to_post_number": None,
                            "username": "alice",
                            "created_at": "2026-03-20T00:00:00Z",
                            "updated_at": "2026-03-22T00:00:00Z",
                            "reaction_users_count": 4,
                            "reply_count": 1,
                            "reads": 9,
                            "score": 1.5,
                            "incoming_link_count": 2,
                            "current_user_reaction": None,
                            "cooked": "<p>hello</p>",
                        },
                        {
                            "id": 102,
                            "post_number": 2,
                            "reply_to_post_number": 1,
                            "username": "bob",
                            "created_at": "2026-03-21T00:00:00Z",
                            "updated_at": "2026-03-22T00:00:00Z",
                            "reaction_users_count": 1,
                            "reply_count": 0,
                            "reads": 3,
                            "score": 0.2,
                            "incoming_link_count": 0,
                            "current_user_reaction": "heart",
                            "cooked": "<p>reply</p>",
                        },
                    ],
                },
            }
            store.upsert_topic_detail(topic_summary, topic)
            store.insert_posts(1, topic["post_stream"]["posts"])
            store.upsert_topic_crawl_state(1, 2, 2)
            store.begin_watch_cycle("cycle-1", "2026-03-23T00:00:00Z")
            store.finish_watch_cycle(
                "cycle-1",
                ended_at="2026-03-23T00:01:00Z",
                topics_fetched=1,
                topics_entered=1,
                posts_fetched=2,
                notifications_sent=1,
                success=True,
                error_text="",
            )
            store.mark_notifications_sent([{"id": 301, "created_at": "2026-03-23T00:00:00Z"}])
            store.inc_stat("topics_seen", 1)
            store.inc_stat("posts_fetched", 2)
            store.inc_stat("timings_sent", 1)
            store.inc_stat("notifications_sent", 1)
            store.inc_plugin_daily_count("demo", "reply", 2)
            store.mark_plugin_once("demo", "done:1")
            store.set_plugin_kv("demo", "answer", 42)
            store.log_plugin_action(
                cycle_id="cycle-1",
                plugin_id="demo",
                hook_name="topics.fetched",
                action="record_trigger",
                status="applied",
                topic_id=1,
                post_id=101,
                extra={"count": 1},
            )
        finally:
            store.close()

    def test_postgres_store_supports_read_only_reopen(self) -> None:
        self._seed_source()

        store = PostgresStore(
            self.source_dsn,
            site_key="forum.example",
            account_name="main",
            base_url="https://forum.example",
            timezone_name="UTC",
            initialize=False,
            ensure_metadata=False,
            read_only=True,
        )
        try:
            self.assertEqual(
                store.get_stats_total(),
                {
                    "topics_seen": 1,
                    "posts_fetched": 2,
                    "timings_sent": 1,
                    "notifications_sent": 1,
                },
            )
            self.assertEqual(store.get_plugin_daily_counts("demo"), {"reply": 2})
            self.assertTrue(store.plugin_once_exists("demo", "done:1"))
            self.assertEqual(store.get_plugin_kv("demo", "answer"), 42)
            self.assertEqual(store.get_sent_notification_ids([301, 999]), {301})
        finally:
            store.close()

    def test_postgres_export_import_round_trip(self) -> None:
        self._seed_source()
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = Path(tmpdir) / "export"

            export_result = export_backend(
                backend="postgres",
                path=self.source_dsn,
                output_dir=export_dir,
                site_key="forum.example",
                account_name="main",
            )
            import_result = import_backend(
                backend="postgres",
                path=self.target_dsn,
                input_dir=export_dir,
            )

            meta = json.loads((export_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["source_site_key"], "forum.example")
            self.assertEqual(meta["source_account_name"], "main")
            self.assertEqual(export_result["tables"]["plugin_action_logs"], 1)
            self.assertEqual(import_result["tables"]["posts"], 2)

            imported = PostgresStore(
                self.target_dsn,
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
                initialize=False,
                ensure_metadata=False,
                read_only=True,
            )
            try:
                self.assertEqual(
                    imported.get_stats_today(),
                    {
                        "topics_seen": 1,
                        "posts_fetched": 2,
                        "timings_sent": 1,
                        "notifications_sent": 1,
                    },
                )
                self.assertEqual(imported.get_plugin_daily_counts("demo"), {"reply": 2})
                self.assertTrue(imported.plugin_once_exists("demo", "done:1"))
                self.assertEqual(imported.get_plugin_kv("demo", "answer"), 42)
                self.assertEqual(imported.get_sent_notification_ids([301, 999]), {301})
            finally:
                imported.close()

    def test_runtime_config_can_source_postgres_dsn_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            config_path.write_text(
                """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"name": "main", "cookie": "_t=file-token"},
  "storage": {
    "backend": "postgres",
    "postgres": {"dsn": ""}
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"DISCORSAIR_POSTGRES_DSN": self.target_dsn}, clear=False):
                with patch("discorsair.runtime.factory.setup_logging"):
                    app_config = load_runtime_app_config(str(config_path))
                settings = load_settings(app_config)

            self.assertEqual(app_config["storage"]["postgres"]["dsn"], self.target_dsn)
            self.assertEqual(settings.store.backend, "postgres")
            self.assertEqual(settings.store.path, self.target_dsn)
            self.assertIn(("storage", "postgres", "dsn"), app_config["_env_override_paths"])

            store = open_store(settings)
            try:
                self.assertEqual(store.backend_name(), "postgres")
                self.assertEqual(
                    store.get_stats_total(),
                    {
                        "topics_seen": 0,
                        "posts_fetched": 0,
                        "timings_sent": 0,
                        "notifications_sent": 0,
                    },
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
