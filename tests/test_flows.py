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


class _Client:
    def __init__(self) -> None:
        self.timings_calls: list[tuple[int, dict[int, int], int]] = []

    def get_latest(self):
        return {"topic_list": {"topics": []}}

    def post_timings(self, topic_id: int, timings: dict[int, int], topic_time: int) -> None:
        self.timings_calls.append((topic_id, timings, topic_time))

    def toggle_reaction(self, post_id: int, emoji: str):
        return {"current_user_reaction": emoji, "post_id": post_id}

    def reply(self, topic_id: int, raw: str, category: int | None = None):
        return {"post": {"id": 88}, "topic_id": topic_id, "raw": raw, "category": category}


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


if __name__ == "__main__":
    unittest.main()
