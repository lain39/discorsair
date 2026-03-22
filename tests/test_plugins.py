"""Plugin runtime tests."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import time
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

from discorsair.flows.watch import watch
from discorsair.plugins import PluginManager
from discorsair.server.http_server import WatchController
from discorsair.storage.sqlite_store import SQLiteStore


class _PluginClient:
    def __init__(self) -> None:
        self.get_topic_calls: list[int] = []
        self.like_calls: list[tuple[int, str]] = []
        self.reply_calls: list[tuple[int, str, int | None]] = []
        self._topics: dict[int, dict] = {}

    def get_latest(self):
        return {
            "topic_list": {
                "topics": [
                    {"id": 1, "title": "normal", "unseen": False, "last_read_post_number": 5},
                    {"id": 2, "title": "priority topic", "unseen": True, "last_read_post_number": 0},
                    {"id": 3, "title": "skip me", "unseen": True, "last_read_post_number": 0},
                ]
            }
        }

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True):
        self.get_topic_calls.append(topic_id)
        if topic_id == 2:
            return {
                "highest_post_number": 2,
                "last_posted_at": "2026-03-22T00:00:00Z",
                "post_stream": {
                    "posts": [
                        {
                            "id": 201,
                            "post_number": 1,
                            "cooked": "<p>reply-me</p>",
                            "current_user_reaction": None,
                            "like_count": 20,
                        },
                        {
                            "id": 202,
                            "post_number": 2,
                            "cooked": "<p>other</p>",
                            "current_user_reaction": "laugh",
                            "like_count": 20,
                        },
                    ],
                    "stream": [201, 202],
                },
            }
        return {
            "highest_post_number": 1,
            "last_posted_at": "2026-03-22T00:00:00Z",
            "post_stream": {
                "posts": [
                    {
                        "id": 101,
                        "post_number": 1,
                        "cooked": "<p>ignored</p>",
                        "current_user_reaction": None,
                        "like_count": 99,
                    }
                ],
                "stream": [101],
            },
        }

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]):
        return {"post_stream": {"posts": []}}

    def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
        return None

    def toggle_reaction(self, post_id: int, emoji: str):
        self.like_calls.append((post_id, emoji))
        return {"current_user_reaction": emoji}

    def reply(self, topic_id: int, raw: str, category: int | None = None):
        self.reply_calls.append((topic_id, raw, category))
        return {"post": {"id": 501}}


class _BackfillClient:
    def __init__(self) -> None:
        self.posts_by_ids_calls: list[list[int]] = []

    def get_latest(self):
        return {
            "topic_list": {
                "topics": [
                    {"id": 7, "title": "backfill", "unseen": False, "last_read_post_number": 10},
                ]
            }
        }

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True):
        return {
            "highest_post_number": 3,
            "last_posted_at": "2026-03-22T00:00:00Z",
            "post_stream": {
                "posts": [
                    {
                        "id": 701,
                        "post_number": 1,
                        "cooked": "<p>entered</p>",
                        "current_user_reaction": None,
                    }
                ],
                "stream": [701, 703, 702],
            },
        }

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]):
        self.posts_by_ids_calls.append(list(post_ids))
        return {
            "post_stream": {
                "posts": [
                    {"id": 703, "post_number": 3, "cooked": "<p>third</p>", "current_user_reaction": None},
                    {"id": 702, "post_number": 2, "cooked": "<p>second</p>", "current_user_reaction": None},
                ]
            }
        }

    def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
        return None


class _SlowReplyClient(_PluginClient):
    def reply(self, topic_id: int, raw: str, category: int | None = None):
        time.sleep(0.05)
        return super().reply(topic_id, raw, category)


class _SlowLikeClient(_PluginClient):
    def toggle_reaction(self, post_id: int, emoji: str):
        time.sleep(0.05)
        return super().toggle_reaction(post_id, emoji)


class _OrderClient:
    def __init__(self) -> None:
        self.get_topic_calls: list[int] = []

    def get_latest(self):
        return {
            "topic_list": {
                "topics": [
                    {"id": 1, "title": "first", "unseen": False, "last_read_post_number": 0},
                    {"id": 2, "title": "second", "unseen": False, "last_read_post_number": 0},
                ]
            }
        }

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True):
        self.get_topic_calls.append(topic_id)
        return {
            "highest_post_number": 1,
            "last_posted_at": "2026-03-22T00:00:00Z",
            "post_stream": {"posts": [], "stream": []},
        }

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]):
        return {"post_stream": {"posts": []}}

    def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
        return None


class PluginTests(unittest.TestCase):
    def _write_plugin_tree(self, tmpdir: str, plugin_id: str, manifest_text: str, plugin_text: str) -> Path:
        root = Path(tmpdir)
        (root / "config").mkdir()
        plugin_dir = root / "plugins" / plugin_id
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "manifest.json").write_text(textwrap.dedent(manifest_text).strip() + "\n", encoding="utf-8")
        (plugin_dir / "plugin.py").write_text(textwrap.dedent(plugin_text).strip() + "\n", encoding="utf-8")
        config_path = root / "config" / "app.json"
        config_path.write_text("{}", encoding="utf-8")
        return config_path

    def test_watch_plugins_can_reorder_skip_like_and_reply_without_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "auto_ops",
                """
                {
                  "id": "auto_ops",
                  "name": "Auto Ops",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched", "post.fetched", "topic.after_crawl"],
                  "permissions": ["topics.reorder", "topics.skip", "post.like", "reply.create"],
                  "default_priority": 100,
                  "default_config": {"min_like_count": 10}
                }
                """,
                """
                class Plugin:
                    def on_topics_fetched(self, ctx, event):
                        for topic in event.topics:
                            title = str(topic.get("title", ""))
                            if "priority" in title:
                                ctx.prioritize_topic(int(topic["id"]), 100)
                            if "skip" in title:
                                ctx.skip_topic(int(topic["id"]), "skip")

                    def on_post_fetched(self, ctx, event):
                        if int(event.post.get("like_count", 0)) >= int(ctx.config.get("min_like_count", 0)):
                            ctx.like(event.post)

                    def on_topic_after_crawl(self, ctx, event):
                        if any("reply-me" in str(post.get("cooked", "")) for post in event.posts):
                            ctx.reply(int(event.topic_summary["id"]), "auto reply", once_key=f"reply:{int(event.topic_summary['id'])}")


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _PluginClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "items": {
                        "auto_ops": {
                            "enabled": True,
                            "priority": 100,
                            "limits": {"reply_per_day": 1, "like_per_day": 1},
                            "config": {"min_like_count": 10},
                        }
                    }
                },
            }
            manager = PluginManager.from_app_config(app_config, client=client, store=None, timezone_name="UTC")

            watch(
                client,
                store=None,
                interval_secs=1,
                once=True,
                crawl_enabled=False,
                timings_per_topic=1,
                plugin_manager=manager,
            )

            self.assertEqual(client.get_topic_calls, [2, 1])
            self.assertEqual(client.like_calls, [(201, "heart")])
            self.assertEqual(client.reply_calls, [(2, "auto reply", None)])
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["hook_successes"]["topics.fetched"], 1)
            self.assertEqual(snapshot["items"][0]["hook_successes"]["post.fetched"], 2)
            self.assertEqual(snapshot["items"][0]["hook_successes"]["topic.after_crawl"], 1)
            self.assertEqual(snapshot["items"][0]["action_counts"]["prioritize_topic"], 1)
            self.assertEqual(snapshot["items"][0]["action_counts"]["skip_topic"], 1)
            self.assertEqual(snapshot["items"][0]["action_counts"]["like.acted"], 1)
            self.assertEqual(snapshot["items"][0]["action_counts"]["like.skipped"], 1)
            self.assertEqual(snapshot["items"][0]["action_counts"]["reply.acted"], 1)

    def test_crawl_plugins_only_emit_backfill_posts_when_topic_is_not_unseen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "recorder",
                """
                {
                  "id": "recorder",
                  "name": "Recorder",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["post.fetched", "topic.after_crawl"],
                  "permissions": ["storage.write", "storage.read"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_post_fetched(self, ctx, event):
                        ids = list(ctx.get_kv("post_ids", []))
                        ids.append(int(event.post["id"]))
                        ctx.set_kv("post_ids", ids)

                    def on_topic_after_crawl(self, ctx, event):
                        ctx.set_kv("after_posts", [int(post["id"]) for post in event.posts])


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _BackfillClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"recorder": {"enabled": True}}},
            }
            store = SQLiteStore(str(Path(tmpdir) / "discorsair.db"), timezone_name="UTC")
            try:
                manager = PluginManager.from_app_config(app_config, client=client, store=store, timezone_name="UTC")
                watch(
                    client,
                    store=store,
                    interval_secs=1,
                    once=True,
                    crawl_enabled=True,
                    timings_per_topic=1,
                    plugin_manager=manager,
                )
                self.assertEqual(client.posts_by_ids_calls, [[703, 702]])
                self.assertEqual(store.get_plugin_kv("recorder", "post_ids", default=[]), [702, 703])
                self.assertEqual(store.get_plugin_kv("recorder", "after_posts", default=[]), [702, 703])
            finally:
                store.close()

    def test_app_permissions_can_clip_manifest_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "perm_test",
                """
                {
                  "id": "perm_test",
                  "name": "Perm Test",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["post.fetched"],
                  "permissions": ["post.like"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_post_fetched(self, ctx, event):
                        ctx.like(event.post)


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _PluginClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "items": {
                        "perm_test": {
                            "enabled": True,
                            "permissions": [],
                        }
                    }
                },
            }
            manager = PluginManager.from_app_config(app_config, client=client, store=None, timezone_name="UTC")
            watch(
                client,
                store=None,
                interval_secs=1,
                once=True,
                crawl_enabled=False,
                timings_per_topic=1,
                plugin_manager=manager,
            )
            self.assertEqual(client.like_calls, [])

    def test_unrelated_invalid_manifest_does_not_break_enabled_plugin_loading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config").mkdir()
            good_dir = root / "plugins" / "good_plugin"
            good_dir.mkdir(parents=True)
            (good_dir / "manifest.json").write_text(
                textwrap.dedent(
                    """
                    {
                      "id": "good_plugin",
                      "name": "Good Plugin",
                      "version": "0.1.0",
                      "api_version": 1,
                      "entry": "plugin.py",
                      "hooks": ["topics.fetched"],
                      "permissions": [],
                      "default_priority": 10,
                      "default_config": {}
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (good_dir / "plugin.py").write_text("def create_plugin():\n    return object()\n", encoding="utf-8")
            bad_dir = root / "plugins" / "broken_plugin"
            bad_dir.mkdir(parents=True)
            (bad_dir / "manifest.json").write_text("{broken json\n", encoding="utf-8")
            config_path = root / "config" / "app.json"
            config_path.write_text("{}", encoding="utf-8")

            manager = PluginManager.from_app_config(
                {
                    "_path": str(config_path),
                    "plugins": {"items": {"good_plugin": {"enabled": True}}},
                },
                client=object(),
                store=None,
                timezone_name="UTC",
                initialize=False,
                instantiate=False,
            )

            self.assertIsNotNone(manager)
            self.assertEqual(manager.snapshot()["count"], 1)

    def test_duplicate_plugin_id_is_rejected_even_when_direct_path_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config").mkdir()
            for dirname in ("dup_plugin", "alias_plugin"):
                plugin_dir = root / "plugins" / dirname
                plugin_dir.mkdir(parents=True)
                (plugin_dir / "manifest.json").write_text(
                    textwrap.dedent(
                        """
                        {
                          "id": "dup_plugin",
                          "name": "Dup Plugin",
                          "version": "0.1.0",
                          "api_version": 1,
                          "entry": "plugin.py",
                          "hooks": ["topics.fetched"],
                          "permissions": [],
                          "default_priority": 10,
                          "default_config": {}
                        }
                        """
                    ).strip()
                    + "\n",
                    encoding="utf-8",
                )
                (plugin_dir / "plugin.py").write_text("def create_plugin():\n    return object()\n", encoding="utf-8")
            config_path = root / "config" / "app.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate plugin id: dup_plugin"):
                PluginManager.from_app_config(
                    {
                        "_path": str(config_path),
                        "plugins": {"items": {"dup_plugin": {"enabled": True}}},
                    },
                    client=object(),
                    store=None,
                    timezone_name="UTC",
                    initialize=False,
                    instantiate=False,
                )

    def test_plugin_is_disabled_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "timeout_test",
                """
                {
                  "id": "timeout_test",
                  "name": "Timeout Test",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["post.fetched"],
                  "permissions": ["storage.write"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                import time


                class Plugin:
                    def on_post_fetched(self, ctx, event):
                        count = int(ctx.get_kv("calls", 0) or 0)
                        ctx.set_kv("calls", count + 1)
                        time.sleep(0.2)


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "hook_timeout_secs": 0.01,
                    "max_consecutive_failures": 1,
                    "items": {
                        "timeout_test": {
                            "enabled": True,
                            "permissions": ["storage.write", "storage.read"],
                        }
                    },
                },
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            cycle = manager.new_cycle()
            manager.dispatch(
                "post.fetched",
                cycle,
                topic_summary={"id": 1, "title": "t", "unseen": True, "last_read_post_number": 0},
                topic={"id": 1},
                post={"id": 1, "current_user_reaction": None, "cooked": "x"},
            )
            time.sleep(0.05)
            manager.dispatch(
                "post.fetched",
                cycle,
                topic_summary={"id": 1, "title": "t", "unseen": True, "last_read_post_number": 0},
                topic={"id": 1},
                post={"id": 2, "current_user_reaction": None, "cooked": "x"},
            )
            self.assertEqual(manager.backend.get_kv("timeout_test", "calls", default=0), 1)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["items"][0]["disabled"], True)
            self.assertEqual(snapshot["items"][0]["consecutive_failures"], 1)
            self.assertEqual(snapshot["items"][0]["hook_timeouts"]["post.fetched"], 1)
            self.assertEqual(snapshot["items"][0]["last_error"], "plugin hook timed out after 0.01s")

    def test_timeout_blocks_post_timeout_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "late_effects",
                """
                {
                  "id": "late_effects",
                  "name": "Late Effects",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["post.fetched"],
                  "permissions": ["storage.read", "storage.write"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                import time


                class Plugin:
                    def on_post_fetched(self, ctx, event):
                        time.sleep(0.05)
                        ctx.set_kv("after_timeout", True)
                        ctx.mark_done("done:after-timeout")
                        ctx.record_trigger("after-timeout")


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "hook_timeout_secs": 0.01,
                    "max_consecutive_failures": 1,
                    "items": {"late_effects": {"enabled": True}},
                },
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            manager.dispatch(
                "post.fetched",
                manager.new_cycle(),
                topic_summary={"id": 1, "title": "t", "unseen": True, "last_read_post_number": 0},
                topic={"id": 1},
                post={"id": 1, "current_user_reaction": None, "cooked": "x"},
            )
            time.sleep(0.1)
            self.assertEqual(manager.backend.get_kv("late_effects", "after_timeout", default=False), False)
            self.assertEqual(manager.backend.was_done("late_effects", "done:after-timeout"), False)
            self.assertEqual(manager.backend.get_daily_count("late_effects", "trigger:after-timeout"), 0)

    def test_timeout_blocks_reply_local_side_effects_after_inflight_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "slow_reply",
                """
                {
                  "id": "slow_reply",
                  "name": "Slow Reply",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topic.after_crawl"],
                  "permissions": ["reply.create", "storage.read", "storage.write"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_topic_after_crawl(self, ctx, event):
                        ctx.reply(int(event.topic_summary["id"]), "hello", once_key="reply:1")


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _SlowReplyClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "hook_timeout_secs": 0.01,
                    "max_consecutive_failures": 1,
                    "items": {"slow_reply": {"enabled": True, "limits": {"reply_per_day": 1}}},
                },
            }
            manager = PluginManager.from_app_config(app_config, client=client, store=None, timezone_name="UTC")
            manager.dispatch(
                "topic.after_crawl",
                manager.new_cycle(),
                topic_summary={"id": 1, "title": "t", "unseen": True, "last_read_post_number": 0},
                topic={"id": 1},
                posts=[],
            )
            time.sleep(0.1)
            self.assertEqual(client.reply_calls, [(1, "hello", None)])
            self.assertEqual(manager.backend.get_daily_count("slow_reply", "reply"), 0)
            self.assertEqual(manager.backend.was_done("slow_reply", "reply:1"), False)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["action_counts"], {})

    def test_timeout_blocks_like_local_side_effects_after_inflight_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "slow_like",
                """
                {
                  "id": "slow_like",
                  "name": "Slow Like",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["post.fetched"],
                  "permissions": ["post.like"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_post_fetched(self, ctx, event):
                        ctx.like(event.post)


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _SlowLikeClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "hook_timeout_secs": 0.01,
                    "max_consecutive_failures": 1,
                    "items": {"slow_like": {"enabled": True, "limits": {"like_per_day": 1}}},
                },
            }
            manager = PluginManager.from_app_config(app_config, client=client, store=None, timezone_name="UTC")
            manager.dispatch(
                "post.fetched",
                manager.new_cycle(),
                topic_summary={"id": 1, "title": "t", "unseen": True, "last_read_post_number": 0},
                topic={"id": 1},
                post={"id": 10, "current_user_reaction": None, "cooked": "x"},
            )
            time.sleep(0.1)
            self.assertEqual(client.like_calls, [(10, "heart")])
            self.assertEqual(manager.backend.get_daily_count("slow_like", "like"), 0)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["action_counts"], {})

    def test_timeout_does_not_commit_topic_order_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "slow_order",
                """
                {
                  "id": "slow_order",
                  "name": "Slow Order",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched"],
                  "permissions": ["topics.reorder", "topics.skip"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                import time


                class Plugin:
                    def on_topics_fetched(self, ctx, event):
                        ctx.prioritize_topic(2, 100)
                        ctx.skip_topic(1, "skip")
                        time.sleep(0.05)


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _OrderClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {
                    "hook_timeout_secs": 0.01,
                    "max_consecutive_failures": 1,
                    "items": {"slow_order": {"enabled": True}},
                },
            }
            manager = PluginManager.from_app_config(app_config, client=client, store=None, timezone_name="UTC")

            watch(
                client,
                store=None,
                interval_secs=1,
                once=True,
                crawl_enabled=False,
                timings_per_topic=1,
                plugin_manager=manager,
            )

            self.assertEqual(client.get_topic_calls, [1, 2])
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["action_counts"], {})

    def test_memory_backend_get_kv_returns_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "copy_test",
                """
                {
                  "id": "copy_test",
                  "name": "Copy Test",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched"],
                  "permissions": ["storage.read", "storage.write"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    pass


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"copy_test": {"enabled": True}}},
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            manager.backend.set_kv("copy_test", "items", [1, 2])
            value = manager.backend.get_kv("copy_test", "items", default=[])
            value.append(3)
            self.assertEqual(manager.backend.get_kv("copy_test", "items", default=[]), [1, 2])

    def test_prioritize_topic_returns_unsupported_hook_outside_topics_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "bad_phase",
                """
                {
                  "id": "bad_phase",
                  "name": "Bad Phase",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topic.before_enter"],
                  "permissions": ["topics.reorder"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_topic_before_enter(self, ctx, event):
                        result = ctx.prioritize_topic(int(event.topic_summary["id"]), 100)
                        ctx.logger.info("result=%s", result.get("reason"))


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"bad_phase": {"enabled": True}}},
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            cycle = manager.new_cycle()
            manager.dispatch(
                "topic.before_enter",
                cycle,
                topic_summary={"id": 2, "title": "t", "unseen": True, "last_read_post_number": 0},
            )
            self.assertEqual(cycle.topic_scores, {})
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["action_counts"], {})

    def test_skip_topic_returns_unsupported_hook_outside_allowed_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "late_skip",
                """
                {
                  "id": "late_skip",
                  "name": "Late Skip",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topic.after_enter"],
                  "permissions": ["topics.skip"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_topic_after_enter(self, ctx, event):
                        ctx.skip_topic(int(event.topic_summary["id"]), "skip")


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"late_skip": {"enabled": True}}},
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            cycle = manager.new_cycle()
            manager.dispatch(
                "topic.after_enter",
                cycle,
                topic_summary={"id": 2, "title": "t", "unseen": True, "last_read_post_number": 0},
                topic={"id": 2},
            )
            self.assertEqual(cycle.skipped_topics, {})
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["action_counts"], {})

    def test_on_load_accepts_ctx_only_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "load_test",
                """
                {
                  "id": "load_test",
                  "name": "Load Test",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched"],
                  "permissions": ["storage.read", "storage.write"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_load(self, ctx):
                        ctx.set_kv("loaded", True)


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"load_test": {"enabled": True}}},
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            self.assertEqual(manager.backend.get_kv("load_test", "loaded", default=False), True)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"][0]["hook_successes"]["on_load"], 1)
            self.assertEqual(snapshot["items"][0]["kv_keys"], ["loaded"])
            self.assertEqual(snapshot["runtime_live"], True)

    def test_plugin_snapshot_includes_persisted_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "stateful",
                """
                {
                  "id": "stateful",
                  "name": "Stateful",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched"],
                  "permissions": ["storage.read", "storage.write"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_topics_fetched(self, ctx, event):
                        ctx.record_trigger("hello")
                        ctx.mark_done("done:1")
                        ctx.set_kv("answer", 42)


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"stateful": {"enabled": True}}},
            }
            store = SQLiteStore(str(Path(tmpdir) / "discorsair.db"), timezone_name="UTC")
            try:
                manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=store, timezone_name="UTC")
                manager.dispatch("topics.fetched", manager.new_cycle(), topics=[])
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["backend"], "sqlite")
                self.assertEqual(snapshot["runtime_live"], True)
                self.assertEqual(snapshot["items"][0]["daily_counts"], {"trigger:hello": 1})
                self.assertEqual(snapshot["items"][0]["once_mark_count"], 1)
                self.assertEqual(snapshot["items"][0]["kv_keys"], ["answer"])
            finally:
                store.close()

    def test_watch_controller_status_includes_plugin_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "status_test",
                """
                {
                  "id": "status_test",
                  "name": "Status Test",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched"],
                  "permissions": ["topics.reorder"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_topics_fetched(self, ctx, event):
                        return None


                def create_plugin():
                    return Plugin()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"status_test": {"enabled": True}}},
            }
            manager = PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")
            controller = WatchController(
                client=types.SimpleNamespace(),
                store=None,
                notifier=None,
                interval_secs=1,
                max_posts_per_interval=None,
                crawl_enabled=False,
                use_unseen=False,
                timings_per_topic=1,
                schedule_windows=[],
                notify_interval_secs=60,
                notify_auto_mark_read=False,
                plugin_manager=manager,
                auto_restart=True,
                restart_backoff_secs=1,
                max_restarts=0,
                same_error_stop_threshold=0,
                timezone_name="UTC",
            )
            status = controller.status()
            self.assertEqual(status["plugins"]["enabled"], True)
            self.assertEqual(status["plugins"]["count"], 1)
            self.assertEqual(status["plugins"]["backend"], "memory")
            self.assertEqual(status["plugins"]["runtime_live"], True)
            self.assertEqual(status["plugins"]["items"][0]["plugin_id"], "status_test")
            self.assertEqual(status["plugins"]["items"][0]["hook_successes"], {})

    def test_plugin_manifest_validates_api_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "bad_api",
                """
                {
                  "id": "bad_api",
                  "name": "Bad API",
                  "version": "0.1.0",
                  "api_version": 2,
                  "entry": "plugin.py",
                  "hooks": ["topics.fetched"],
                  "permissions": [],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                def create_plugin():
                    return object()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"bad_api": {"enabled": True}}},
            }
            with self.assertRaisesRegex(ValueError, "unsupported api_version"):
                PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")

    def test_plugin_manifest_validates_non_empty_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "bad_hooks",
                """
                {
                  "id": "bad_hooks",
                  "name": "Bad Hooks",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": [],
                  "permissions": [],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                def create_plugin():
                    return object()
                """,
            )
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"bad_hooks": {"enabled": True}}},
            }
            with self.assertRaisesRegex(ValueError, "hooks must be a non-empty array"):
                PluginManager.from_app_config(app_config, client=_PluginClient(), store=None, timezone_name="UTC")

    def test_plugin_logs_structured_like_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_plugin_tree(
                tmpdir,
                "log_test",
                """
                {
                  "id": "log_test",
                  "name": "Log Test",
                  "version": "0.1.0",
                  "api_version": 1,
                  "entry": "plugin.py",
                  "hooks": ["post.fetched"],
                  "permissions": ["post.like"],
                  "default_priority": 10,
                  "default_config": {}
                }
                """,
                """
                class Plugin:
                    def on_post_fetched(self, ctx, event):
                        ctx.like(event.post)


                def create_plugin():
                    return Plugin()
                """,
            )
            client = _PluginClient()
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"log_test": {"enabled": True}}},
            }
            manager = PluginManager.from_app_config(app_config, client=client, store=None, timezone_name="UTC")
            with self.assertLogs("discorsair.plugins.manager.log_test", level="INFO") as logs:
                manager.dispatch(
                    "post.fetched",
                    manager.new_cycle(),
                    topic_summary={"id": 2, "title": "t", "unseen": True, "last_read_post_number": 0},
                    topic={"id": 2},
                    post={"id": 201, "current_user_reaction": None, "cooked": "<p>x</p>"},
                )
            self.assertTrue(any("plugin action: action=like acted=true post_id=201 emoji=heart" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
