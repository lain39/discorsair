"""Flow return-shape tests."""

from __future__ import annotations

import sys
import threading
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

from discorsair.flows.daily import daily
from discorsair.flows.like import like
from discorsair.flows.reply import reply
from discorsair.flows.watch import _touch_topic
from discorsair.flows.watch import watch


class _Client:
    def __init__(self) -> None:
        self.timings_calls: list[tuple[int, dict[int, int], int]] = []
        self.posts_by_ids_calls: list[tuple[int, list[int]]] = []

    def get_latest(self):
        return {"topic_list": {"topics": []}}

    def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True):
        return {
            "highest_post_number": 5,
            "last_posted_at": "2026-03-22T00:00:00Z",
            "post_stream": {
                "posts": [{"id": 501, "post_number": 1, "cooked": "<p>hello</p>"}],
                "stream": [501, 502, 503, 504, 505],
            },
        }

    def get_posts_by_ids(self, topic_id: int, post_ids: list[int]):
        self.posts_by_ids_calls.append((topic_id, list(post_ids)))
        return {
            "post_stream": {
                "posts": [{"id": post_id, "post_number": index + 1, "cooked": "<p>x</p>"} for index, post_id in enumerate(post_ids)]
            }
        }

    def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
        self.timings_calls.append((topic_id, timings, topic_time))

    def toggle_reaction(self, post_id: int, emoji: str):
        return {"current_user_reaction": emoji, "post_id": post_id}

    def reply(self, topic_id: int, raw: str, category: int | None = None):
        return {"post": {"id": 88}, "topic_id": topic_id, "raw": raw, "category": category}


class _Store:
    def __init__(self) -> None:
        self.post_ids: set[int] = set()
        self.last_synced_post_number = 0
        self.upserts: list[tuple[int, int, int, str]] = []
        self.topic_details: list[tuple[dict, dict]] = []
        self.started_cycles: list[tuple[str, str]] = []
        self.finished_cycles: list[dict[str, object]] = []

    def insert_posts(self, topic_id: int, posts) -> None:
        for post in posts:
            self.post_ids.add(int(post.get("id", 0)))

    def inc_stat(self, field: str, delta: int = 1) -> None:
        return None

    def get_last_synced_post_number(self, topic_id: int) -> int:
        return self.last_synced_post_number

    def get_existing_post_ids(self, topic_id: int, post_ids) -> set[int]:
        return {post_id for post_id in post_ids if post_id in self.post_ids}

    def upsert_topic_detail(self, topic_summary: dict, topic: dict) -> None:
        self.topic_details.append((topic_summary, topic))

    def upsert_topic_crawl_state(self, topic_id: int, last_synced_post_number: int, last_stream_len: int) -> None:
        self.last_synced_post_number = last_synced_post_number
        self.upserts.append((topic_id, last_synced_post_number, last_stream_len, ""))

    def begin_watch_cycle(self, cycle_id: str, started_at: str) -> None:
        self.started_cycles.append((cycle_id, started_at))

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
        self.finished_cycles.append(
            {
                "cycle_id": cycle_id,
                "ended_at": ended_at,
                "topics_fetched": topics_fetched,
                "topics_entered": topics_entered,
                "posts_fetched": posts_fetched,
                "notifications_sent": notifications_sent,
                "success": success,
                "error_text": error_text,
            }
        )

    def get_stats_today(self) -> dict[str, int]:
        return {"topics_seen": 0, "posts_fetched": 0, "timings_sent": 0, "notifications_sent": 0}


class _PluginRecorder:
    def __init__(self, events: list[tuple[str, int | None]]) -> None:
        self.events = events

    def has_plugins(self) -> bool:
        return True

    def new_cycle(self):
        return types.SimpleNamespace(cycle_id="cycle-1")

    def dispatch(self, hook: str, cycle_state=None, **payload) -> None:
        post = payload.get("post") or {}
        self.events.append((hook, int(post.get("id", 0) or 0) or None))

    def sort_topics(self, topics, cycle_state=None):
        return list(topics)

    def is_topic_skipped(self, cycle_state, topic_id: int) -> bool:
        return False


class _OrderedStore(_Store):
    def __init__(self, timeline: list[str]) -> None:
        super().__init__()
        self.timeline = timeline

    def upsert_topic_detail(self, topic_summary: dict, topic: dict) -> None:
        self.timeline.append("upsert_topic_detail")
        super().upsert_topic_detail(topic_summary, topic)

    def insert_posts(self, topic_id: int, posts) -> None:
        self.timeline.append("insert_posts")
        super().insert_posts(topic_id, posts)


class FlowTests(unittest.TestCase):
    def test_daily_returns_reason_when_no_topic_found(self) -> None:
        client = _Client()
        result = daily(client, topic_id=None)
        self.assertEqual(result, {"ok": False, "topic_id": None, "reason": "no_topic_found"})
        self.assertEqual(client.timings_calls, [])

    def test_daily_returns_success_for_explicit_topic(self) -> None:
        client = _Client()
        result = daily(client, topic_id=123)
        self.assertEqual(result, {"ok": True, "topic_id": 123})
        self.assertEqual(client.timings_calls, [(123, {1: 1000}, 1000)])

    def test_like_returns_structured_payload(self) -> None:
        client = _Client()
        result = like(client, post_id=11, emoji="heart")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["post_id"], 11)
        self.assertEqual(result["emoji"], "heart")
        self.assertEqual(result["current_user_reaction"], "heart")

    def test_reply_returns_structured_payload(self) -> None:
        client = _Client()
        result = reply(client, topic_id=99, raw="hello", category=5)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["topic_id"], 99)
        self.assertEqual(result["post_id"], 88)
        self.assertEqual(result["category"], 5)

    def test_watch_reads_last_read_post_number_from_topic_summary(self) -> None:
        client = _Client()
        client.get_latest = lambda: {
            "topic_list": {
                "topics": [
                    {
                        "id": 123,
                        "title": "hello",
                        "last_read_post_number": 3,
                    }
                ]
            }
        }

        watch(client, store=None, interval_secs=1, once=True, crawl_enabled=False, timings_per_topic=3)

        self.assertEqual(len(client.timings_calls), 1)
        topic_id, timings, topic_time = client.timings_calls[0]
        self.assertEqual(topic_id, 123)
        self.assertEqual(sorted(timings.keys()), [4, 5])
        self.assertGreater(topic_time, 0)
        self.assertEqual(client.posts_by_ids_calls, [])

    def test_watch_without_crawl_does_not_fetch_missing_posts(self) -> None:
        client = _Client()
        client.get_latest = lambda: {"topic_list": {"topics": [{"id": 123, "title": "hello"}]}}

        watch(client, store=None, interval_secs=1, once=True, crawl_enabled=False, timings_per_topic=1)

        self.assertEqual(client.posts_by_ids_calls, [])

    def test_watch_marks_cycle_failed_when_topic_fetch_crashes(self) -> None:
        client = _Client()
        client.get_latest = lambda: {"topic_list": {"topics": [{"id": 123, "title": "hello"}]}}
        client.get_topic = lambda topic_id, track_visit=True, force_load=True: (_ for _ in ()).throw(RuntimeError("boom"))
        store = _Store()

        with self.assertRaisesRegex(RuntimeError, "boom"):
            watch(client, store=store, interval_secs=1, once=True, crawl_enabled=True, timings_per_topic=1)

        self.assertEqual(len(store.started_cycles), 1)
        self.assertEqual(len(store.finished_cycles), 1)
        self.assertEqual(store.finished_cycles[0]["topics_fetched"], 1)
        self.assertEqual(store.finished_cycles[0]["topics_entered"], 0)
        self.assertEqual(store.finished_cycles[0]["success"], False)
        self.assertEqual(store.finished_cycles[0]["error_text"], "boom")

    def test_touch_topic_updates_synced_progress_when_initial_posts_cover_stream(self) -> None:
        client = _Client()
        client.get_topic = lambda topic_id, track_visit=True, force_load=True: {
            "highest_post_number": 5,
            "last_posted_at": "2026-03-22T00:00:00Z",
            "post_stream": {
                "posts": [
                    {"id": 501, "post_number": 1, "cooked": "<p>1</p>"},
                    {"id": 502, "post_number": 2, "cooked": "<p>2</p>"},
                    {"id": 503, "post_number": 3, "cooked": "<p>3</p>"},
                    {"id": 504, "post_number": 4, "cooked": "<p>4</p>"},
                    {"id": 505, "post_number": 5, "cooked": "<p>5</p>"},
                ],
                "stream": [501, 502, 503, 504, 505],
            },
        }
        store = _Store()

        remaining = _touch_topic(
            client,
            store,
            {"id": 123, "title": "hello", "last_read_post_number": 0, "unseen": True},
            123,
            remaining_posts=10,
            crawl_enabled=True,
            timings_per_topic=1,
        )

        self.assertEqual(remaining.remaining_posts, 10)
        self.assertEqual([post["id"] for post in remaining.content_posts], [501, 502, 503, 504, 505])
        self.assertEqual(store.last_synced_post_number, 5)
        self.assertEqual(len(store.upserts), 1)
        self.assertEqual(store.upserts[0][1], 5)

    def test_touch_topic_does_not_advance_synced_progress_when_budget_truncates_missing_posts(self) -> None:
        client = _Client()
        store = _Store()

        remaining = _touch_topic(
            client,
            store,
            {"id": 123, "title": "hello", "last_read_post_number": 0, "unseen": True},
            123,
            remaining_posts=0,
            crawl_enabled=True,
            timings_per_topic=1,
        )

        self.assertEqual(remaining.remaining_posts, 0)
        self.assertEqual([post["id"] for post in remaining.content_posts], [501])
        self.assertEqual(store.last_synced_post_number, 0)
        self.assertEqual(store.upserts, [])

    def test_touch_topic_excludes_entered_posts_from_content_when_not_unseen_in_crawl_mode(self) -> None:
        client = _Client()
        store = _Store()

        result = _touch_topic(
            client,
            store,
            {"id": 123, "title": "hello", "last_read_post_number": 0, "unseen": False},
            123,
            remaining_posts=10,
            crawl_enabled=True,
            timings_per_topic=1,
        )

        self.assertEqual([post["id"] for post in result.content_posts], [502, 503, 504, 505])

    def test_touch_topic_dispatches_after_enter_before_timings(self) -> None:
        timeline: list[str] = []
        plugin_events: list[tuple[str, int | None]] = []

        class _OrderedClient(_Client):
            def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True):
                timeline.append("get_topic")
                return super().get_topic(topic_id, track_visit=track_visit, force_load=force_load)

            def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
                timeline.append("post_timings")
                super().post_timings(topic_id, timings, topic_time)

        class _OrderedPluginRecorder(_PluginRecorder):
            def dispatch(self, hook: str, cycle_state=None, **payload) -> None:
                timeline.append(hook)
                super().dispatch(hook, cycle_state=cycle_state, **payload)

        _touch_topic(
            _OrderedClient(),
            store=None,
            topic_summary={"id": 123, "title": "hello", "last_read_post_number": 0, "unseen": True},
            topic_id=123,
            remaining_posts=10,
            crawl_enabled=False,
            timings_per_topic=1,
            plugin_manager=_OrderedPluginRecorder(plugin_events),
            cycle_state=types.SimpleNamespace(cycle_id="cycle-1"),
        )

        self.assertEqual(timeline[:3], ["get_topic", "topic.after_enter", "post.fetched"])
        self.assertLess(timeline.index("topic.after_enter"), timeline.index("post_timings"))
        self.assertLess(timeline.index("post.fetched"), timeline.index("post_timings"))
        self.assertEqual(plugin_events, [("topic.after_enter", None), ("post.fetched", 501)])

    def test_touch_topic_persists_first_screen_posts_before_dispatching_hooks(self) -> None:
        timeline: list[str] = []

        class _OrderedClient(_Client):
            def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
                timeline.append("post_timings")
                super().post_timings(topic_id, timings, topic_time)

        class _OrderedPluginRecorder(_PluginRecorder):
            def dispatch(self, hook: str, cycle_state=None, **payload) -> None:
                timeline.append(hook)
                super().dispatch(hook, cycle_state=cycle_state, **payload)

        _touch_topic(
            _OrderedClient(),
            store=_OrderedStore(timeline),
            topic_summary={"id": 123, "title": "hello", "last_read_post_number": 0, "unseen": True},
            topic_id=123,
            remaining_posts=10,
            crawl_enabled=True,
            timings_per_topic=1,
            plugin_manager=_OrderedPluginRecorder([]),
            cycle_state=types.SimpleNamespace(cycle_id="cycle-1"),
        )

        self.assertLess(timeline.index("upsert_topic_detail"), timeline.index("topic.after_enter"))
        self.assertLess(timeline.index("insert_posts"), timeline.index("post.fetched"))
        self.assertLess(timeline.index("post.fetched"), timeline.index("post_timings"))

    def test_touch_topic_does_not_dispatch_hooks_after_stop_requested(self) -> None:
        events: list[tuple[str, int | None]] = []
        stop_event = threading.Event()

        class _StoppingClient(_Client):
            def get_topic(self, topic_id: int, track_visit: bool = True, force_load: bool = True):
                topic = super().get_topic(topic_id, track_visit=track_visit, force_load=force_load)
                stop_event.set()
                return topic

        _touch_topic(
            _StoppingClient(),
            store=None,
            topic_summary={"id": 123, "title": "hello", "last_read_post_number": 0, "unseen": True},
            topic_id=123,
            remaining_posts=10,
            crawl_enabled=False,
            timings_per_topic=1,
            plugin_manager=_PluginRecorder(events),
            cycle_state=types.SimpleNamespace(cycle_id="cycle-1"),
            stop_event=stop_event,
        )

        self.assertEqual(events, [])

    def test_watch_emits_after_enter_and_post_fetched_for_unseen_topic_without_crawl(self) -> None:
        client = _Client()
        client.get_latest = lambda: {
            "topic_list": {
                "topics": [
                    {"id": 123, "title": "hello", "unseen": True, "last_read_post_number": 0},
                ]
            }
        }
        events: list[tuple[str, int | None]] = []

        watch(
            client,
            store=None,
            interval_secs=1,
            once=True,
            crawl_enabled=False,
            timings_per_topic=1,
            plugin_manager=_PluginRecorder(events),
        )

        self.assertEqual(
            events,
            [
                ("cycle.started", None),
                ("topics.fetched", None),
                ("topic.before_enter", None),
                ("topic.after_enter", None),
                ("post.fetched", 501),
                ("topic.after_crawl", None),
                ("cycle.finished", None),
            ],
        )

    def test_watch_stops_at_after_enter_for_seen_topic_without_crawl(self) -> None:
        client = _Client()
        client.get_latest = lambda: {
            "topic_list": {
                "topics": [
                    {"id": 123, "title": "hello", "unseen": False, "last_read_post_number": 0},
                ]
            }
        }
        events: list[tuple[str, int | None]] = []

        watch(
            client,
            store=None,
            interval_secs=1,
            once=True,
            crawl_enabled=False,
            timings_per_topic=1,
            plugin_manager=_PluginRecorder(events),
        )

        self.assertEqual(
            events,
            [
                ("cycle.started", None),
                ("topics.fetched", None),
                ("topic.before_enter", None),
                ("topic.after_enter", None),
                ("cycle.finished", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
