"""Infrastructure tests for queue, storage, and HTTP control handler."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import TimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.core.request_queue import RequestQueue
from discorsair.server.http_server import ControlHandler
from discorsair.storage.sqlite_store import SQLiteStore


class RequestQueueTests(unittest.TestCase):
    def test_pending_future_fails_when_queue_stops(self) -> None:
        queue = RequestQueue()
        started = threading.Event()
        release = threading.Event()

        def blocking() -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        running = queue.submit(blocking)
        self.assertTrue(started.wait(timeout=1))
        pending = queue.submit(lambda: "never")

        queue.stop()
        release.set()

        self.assertEqual(running.result(timeout=1), "done")
        with self.assertRaises(RuntimeError):
            pending.result(timeout=1)

    def test_expired_deadline_returns_timeout(self) -> None:
        queue = RequestQueue()
        started = threading.Event()
        release = threading.Event()

        def blocking() -> None:
            started.set()
            release.wait(timeout=2)

        queue.submit(blocking)
        self.assertTrue(started.wait(timeout=1))
        expired = queue.submit(lambda: "late", deadline=time.monotonic() - 1)

        release.set()

        with self.assertRaises(TimeoutError):
            expired.result(timeout=1)
        queue.stop()


class SQLiteStoreTests(unittest.TestCase):
    def test_store_persists_posts_topics_notifications_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(str(Path(tmpdir) / "discorsair.db"), timezone_name="UTC")
            try:
                store.insert_posts(
                    10,
                    [
                        {
                            "id": 101,
                            "post_number": 1,
                            "created_at": "2026-03-18T00:00:00Z",
                            "username": "alice",
                            "cooked": "<p>hello</p>",
                        }
                    ],
                )
                self.assertEqual(store.get_existing_post_ids(10, [101, 202]), {101})

                store.upsert_topic(10, last_post_number=8, last_stream_len=3, last_seen_at="2026-03-18T00:00:00Z")
                self.assertEqual(store.get_last_post_number(10), 8)

                store.update_last_read_post_number(10, 5)
                self.assertEqual(store.get_last_read_post_number(10), 5)

                store.mark_notifications_sent([{"id": 7, "created_at": "2026-03-18T00:00:00Z"}])
                self.assertEqual(store.get_sent_notification_ids([7, 8]), {7})

                store.inc_stat("topics_seen", 2)
                store.inc_stat("posts_fetched", 4)
                self.assertEqual(store.get_stats_total()["topics_seen"], 2)
                self.assertEqual(store.get_stats_total()["posts_fetched"], 4)
                self.assertEqual(store.get_stats_today()["topics_seen"], 2)
                self.assertEqual(store.get_stats_today()["posts_fetched"], 4)
            finally:
                store.close()

    def test_rotate_daily_switches_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(str(Path(tmpdir) / "discorsair.db"), timezone_name="UTC", rotate_daily=True)
            try:
                first = store.current_path()
                day_one = store._current_day
                store._today = lambda: "2099-12-31"  # type: ignore[method-assign]
                rotated = store.current_path()
                self.assertNotEqual(first, rotated)
                self.assertNotEqual(day_one, store._current_day)
                self.assertTrue(rotated.endswith(".2099-12-31.db"))
            finally:
                store.close()


class _DummyRuntime:
    def __init__(self) -> None:
        self.last_error = None
        self.last_error_at = None


class _DummyWatchController:
    def __init__(self) -> None:
        self._runtime = _DummyRuntime()
        self._notifier = None
        self._use_unseen = False
        self._timings_per_topic = 30
        self._max_posts_per_interval = 200
        self.start_calls: list[bool] = []
        self.stop_calls = 0

    def status(self) -> dict[str, object]:
        return {"running": False, "stats_total": {}, "stats_today": {}}

    def start(self, use_schedule: bool = True) -> bool:
        self.start_calls.append(use_schedule)
        return True

    def stop(self) -> bool:
        self.stop_calls += 1
        return True

    def configure(
        self,
        *,
        use_unseen: bool | None = None,
        timings_per_topic: int | None = None,
        max_posts_per_interval: int | None | object = None,
    ) -> dict[str, object]:
        if use_unseen is not None:
            self._use_unseen = bool(use_unseen)
        if timings_per_topic is not None:
            self._timings_per_topic = max(1, int(timings_per_topic))
        if max_posts_per_interval is not False and max_posts_per_interval is not None:
            self._max_posts_per_interval = int(max_posts_per_interval)
        if max_posts_per_interval is None:
            self._max_posts_per_interval = None
        return {
            "ok": True,
            "use_unseen": self._use_unseen,
            "timings_per_topic": self._timings_per_topic,
            "max_posts_per_interval": self._max_posts_per_interval,
        }

    def report_error(self, message: str, notify_message: str | None = None) -> None:
        self._runtime.last_error = message
        self._runtime.last_error_at = "now"


class _DummyClient:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str, int | None]] = []

    def toggle_reaction(self, post_id: int, emoji: str) -> dict[str, object]:
        self.reactions.append((post_id, emoji))
        return {"post_id": post_id, "emoji": emoji}

    def reply(self, topic_id: int, raw: str, category: int | None = None) -> dict[str, object]:
        self.replies.append((topic_id, raw, category))
        return {"topic_id": topic_id, "raw": raw, "category": category}


class ControlHandlerTests(unittest.TestCase):
    def _run_handler(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        handler = object.__new__(ControlHandler)
        request_headers = dict(headers or {})
        payload = ""
        if body is not None:
            payload = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(payload.encode("utf-8")))
        handler.headers = request_headers
        handler.path = path
        handler.rfile = io.BytesIO(payload.encode("utf-8"))
        handler.server = types.SimpleNamespace(
            api_key="secret",
            client=_DummyClient(),
            watch_controller=_DummyWatchController(),
            shutdown=lambda: None,
        )
        captured: list[tuple[int, dict[str, object]]] = []
        handler._send = lambda code, data: captured.append((code, data))  # type: ignore[method-assign]
        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()
        return captured[0], handler.server

    def test_requires_api_key(self) -> None:
        (status, data), _ = self._run_handler("GET", "/watch/status")
        self.assertEqual(status, 401)
        self.assertEqual(data, {"error": "unauthorized"})

    def test_watch_config_updates_runtime_settings(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/config",
            body={"use_unseen": True, "timings_per_topic": 12, "max_posts_per_interval": 50},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["use_unseen"], True)
        self.assertEqual(data["timings_per_topic"], 12)
        self.assertEqual(data["max_posts_per_interval"], 50)
        self.assertTrue(server.watch_controller._use_unseen)
        self.assertEqual(server.watch_controller._timings_per_topic, 12)
        self.assertEqual(server.watch_controller._max_posts_per_interval, 50)

    def test_like_requires_post_id(self) -> None:
        (status, data), _ = self._run_handler("POST", "/like", body={}, headers={"X-API-Key": "secret"})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "post_id required"})

    def test_watch_start_forwards_use_schedule(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/start",
            body={"use_schedule": False},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(server.watch_controller.start_calls, [False])

    def test_watch_stop_calls_controller(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/stop",
            body={},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(server.watch_controller.stop_calls, 1)

    def test_reply_requires_topic_id_and_raw(self) -> None:
        (status, data), _ = self._run_handler("POST", "/reply", body={}, headers={"X-API-Key": "secret"})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "topic_id and raw required"})

    def test_reply_success_returns_payload(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/reply",
            body={"topic_id": 9, "raw": "hello", "category": 3},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["result"]["topic_id"], 9)
        self.assertEqual(server.client.replies, [(9, "hello", 3)])


if __name__ == "__main__":
    unittest.main()
