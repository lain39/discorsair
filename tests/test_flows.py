"""Flow return-shape tests."""

from __future__ import annotations

import sys
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

    def insert_posts(self, topic_id: int, posts) -> None:
        for post in posts:
            self.post_ids.add(int(post.get("id", 0)))

    def inc_stat(self, field: str, delta: int = 1) -> None:
        return None

    def get_last_synced_post_number(self, topic_id: int) -> int:
        return self.last_synced_post_number

    def get_existing_post_ids(self, topic_id: int, post_ids) -> set[int]:
        return {post_id for post_id in post_ids if post_id in self.post_ids}

    def upsert_topic(self, topic_id: int, last_synced_post_number: int, last_stream_len: int, last_seen_at: str) -> None:
        self.last_synced_post_number = last_synced_post_number
        self.upserts.append((topic_id, last_synced_post_number, last_stream_len, last_seen_at))


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
            {"id": 123, "title": "hello", "last_read_post_number": 0},
            123,
            remaining_posts=10,
            crawl_enabled=True,
            timings_per_topic=1,
        )

        self.assertEqual(remaining, 10)
        self.assertEqual(store.last_synced_post_number, 5)
        self.assertEqual(len(store.upserts), 1)
        self.assertEqual(store.upserts[0][1], 5)

    def test_touch_topic_does_not_advance_synced_progress_when_budget_truncates_missing_posts(self) -> None:
        client = _Client()
        store = _Store()

        remaining = _touch_topic(
            client,
            store,
            {"id": 123, "title": "hello", "last_read_post_number": 0},
            123,
            remaining_posts=0,
            crawl_enabled=True,
            timings_per_topic=1,
        )

        self.assertEqual(remaining, 0)
        self.assertEqual(store.last_synced_post_number, 0)
        self.assertEqual(store.upserts, [])


if __name__ == "__main__":
    unittest.main()
