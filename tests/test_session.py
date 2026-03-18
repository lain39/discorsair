"""Session tests."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.discourse.client import DiscourseAuthError
from discorsair.discourse.client import DiscourseClient
from discorsair.flows.watch import _poll_notifications
from discorsair.server.http_server import WatchController, validate_server_binding


class _DummyClient:
    pass


class _DummyStore:
    def current_path(self) -> str:
        return "data/test.db"

    def get_stats_total(self) -> dict[str, int]:
        return {}

    def get_stats_today(self) -> dict[str, int]:
        return {}


class _RecordingStore:
    def __init__(self) -> None:
        self.marked: list[dict] = []
        self.stats: list[tuple[str, int]] = []

    def get_sent_notification_ids(self, ids):
        return set()

    def mark_notifications_sent(self, items) -> None:
        self.marked.extend(items)

    def inc_stat(self, field: str, delta: int = 1) -> None:
        self.stats.append((field, delta))


class _NotificationClient:
    def get_notifications(self, limit: int = 30, recent: bool = True):
        return {
            "notifications": [
                {"id": 1, "read": False, "created_at": "2026-03-18T00:00:00Z", "data": {"topic_title": "ok"}},
                {"id": 2, "read": False, "created_at": "2026-03-18T00:00:01Z", "data": {"topic_title": "fail"}},
            ]
        }


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return "fail" not in text


class _Response:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


class _Requester:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)

    def request(self, *args, **kwargs):
        return self._responses.pop(0)

    def get_cookie_header(self) -> str:
        return ""

    def last_response_ok(self):
        return True


class WatchAndServerTests(unittest.TestCase):
    def test_validate_server_binding_requires_api_key_for_public_host(self) -> None:
        validate_server_binding("127.0.0.1", "")
        with self.assertRaises(ValueError):
            validate_server_binding("0.0.0.0", "")
        validate_server_binding("0.0.0.0", "secret")

    def test_watch_controller_does_not_restart_on_auth_error(self) -> None:
        controller = WatchController(
            client=_DummyClient(),
            store=_DummyStore(),
            notifier=None,
            interval_secs=1,
            max_posts_per_interval=None,
            crawl_enabled=True,
            use_unseen=False,
            timings_per_topic=5,
            schedule_windows=[],
            notify_interval_secs=60,
            auto_restart=True,
            restart_backoff_secs=1,
            max_restarts=0,
            same_error_stop_threshold=0,
            timezone_name="UTC",
        )

        calls: list[int] = []

        def raise_auth_error(*args, **kwargs) -> None:
            calls.append(1)
            raise DiscourseAuthError("not_logged_in")

        with patch("discorsair.server.http_server.watch", side_effect=raise_auth_error):
            started = controller.start(use_schedule=False)
            self.assertTrue(started)
            controller._runtime.thread.join(timeout=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(controller.status()["last_error"], "not_logged_in")
        self.assertFalse(controller.status()["running"])
        self.assertIsNotNone(controller._runtime.stop_event)
        self.assertTrue(controller._runtime.stop_event.is_set())

    def test_poll_notifications_marks_only_successful_sends(self) -> None:
        store = _RecordingStore()
        notifier = _Notifier()

        _poll_notifications(_NotificationClient(), store, notifier)

        self.assertEqual([item["id"] for item in store.marked], [1])
        self.assertEqual(store.stats, [("notifications_sent", 1)])
        self.assertEqual(len(notifier.sent), 2)

    def test_post_timings_raises_auth_error_from_json_body(self) -> None:
        client = DiscourseClient(
            requester=_Requester([_Response(403, '{"error_type":"not_logged_in"}')]),
            csrf_token="csrf",
        )

        with self.assertRaises(DiscourseAuthError):
            client.post_timings(topic_id=1, timings={1: 1000}, topic_time=1000)

    def test_post_timings_retries_bad_csrf_once(self) -> None:
        requester = _Requester(
            [
                _Response(403, '["BAD CSRF"]'),
                _Response(200, ""),
            ]
        )
        client = DiscourseClient(requester=requester, csrf_token="old")

        with patch.object(client, "get_csrf", return_value="new") as refresh:
            client.post_timings(topic_id=1, timings={1: 1000}, topic_time=1000)

        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
