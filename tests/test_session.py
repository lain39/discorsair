"""Session tests."""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.discourse.client import DiscourseAuthError
from discorsair.discourse.client import DiscourseClient
from discorsair.flows.status import status as status_flow
from discorsair.flows.watch import _poll_notifications
from discorsair.core.requester import ChallengeUnresolvedError
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
    def __init__(self) -> None:
        self.mark_read_calls = 0

    def get_notifications(self, limit: int = 30, recent: bool = True):
        return {
            "notifications": [
                {"id": 1, "read": False, "created_at": "2026-03-18T00:00:00Z", "data": {"topic_title": "ok"}},
                {"id": 2, "read": False, "created_at": "2026-03-18T00:00:01Z", "data": {"topic_title": "fail"}},
            ]
        }

    def mark_notifications_read(self):
        self.mark_read_calls += 1
        return {"ok": True}


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
        self.calls: list[tuple[tuple, dict]] = []
        self._csrf_token_hint = ""
        self._use_flaresolverr_for_csrf = False
        self._flaresolverr_csrf_token = ""

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._responses.pop(0)

    def get_cookie_header(self) -> str:
        return ""

    def last_response_ok(self):
        return True

    def get_csrf_token_hint(self) -> str:
        return self._csrf_token_hint

    def consume_csrf_token_hint(self) -> str:
        hint = self._csrf_token_hint
        self._csrf_token_hint = ""
        return hint

    def should_use_flaresolverr_for_csrf(self) -> bool:
        return self._use_flaresolverr_for_csrf

    def fetch_csrf_token_via_flaresolverr(self) -> str:
        return self._flaresolverr_csrf_token


class WatchAndServerTests(unittest.TestCase):
    def _build_watch_controller(self, **overrides) -> WatchController:
        params = {
            "client": _DummyClient(),
            "store": _DummyStore(),
            "notifier": None,
            "interval_secs": 1,
            "max_posts_per_interval": None,
            "crawl_enabled": True,
            "use_unseen": False,
            "timings_per_topic": 5,
            "schedule_windows": [],
            "notify_interval_secs": 60,
            "notify_auto_mark_read": False,
            "plugin_manager": None,
            "auto_restart": True,
            "restart_backoff_secs": 1,
            "max_restarts": 0,
            "same_error_stop_threshold": 0,
            "timezone_name": "UTC",
            "on_stop": None,
            "on_auth_invalid": None,
            "on_fatal": None,
        }
        params.update(overrides)
        return WatchController(**params)

    def _start_watch_with_side_effect(
        self,
        controller: WatchController,
        side_effect,
        *,
        patch_sleep: bool = False,
    ) -> None:
        watch_patch = patch("discorsair.server.http_server.watch", side_effect=side_effect)
        if patch_sleep:
            controller._restart_backoff_secs = 0
        with watch_patch:
            started = controller.start(use_schedule=False)
            self.assertTrue(started)
            controller._runtime.thread.join(timeout=2)

    def _assert_watch_stopped(self, controller: WatchController, expected_error: str) -> None:
        self.assertEqual(controller.status()["last_error"], expected_error)
        self.assertFalse(controller.status()["running"])
        self.assertIsNotNone(controller._runtime.stop_event)
        self.assertTrue(controller._runtime.stop_event.is_set())

    def test_validate_server_binding_requires_api_key_for_public_host(self) -> None:
        validate_server_binding("127.0.0.1", "")
        with self.assertRaises(ValueError):
            validate_server_binding("0.0.0.0", "")
        validate_server_binding("0.0.0.0", "secret")

    def test_watch_controller_does_not_restart_on_auth_error(self) -> None:
        on_stop = Mock()
        on_auth_invalid = Mock()
        on_fatal = Mock()
        notifier = Mock()
        controller = self._build_watch_controller(
            notifier=notifier,
            on_stop=on_stop,
            on_auth_invalid=on_auth_invalid,
            on_fatal=on_fatal,
        )

        calls: list[int] = []

        def raise_auth_error(*args, **kwargs) -> None:
            calls.append(1)
            raise DiscourseAuthError("not_logged_in")

        self._start_watch_with_side_effect(controller, raise_auth_error)

        self.assertEqual(len(calls), 1)
        self._assert_watch_stopped(controller, "not_logged_in")
        self.assertIsInstance(controller.fatal_error(), DiscourseAuthError)
        on_stop.assert_called_once_with()
        on_auth_invalid.assert_called_once()
        on_fatal.assert_called_once_with()
        self.assertEqual(notifier.send_error.call_count, 2)
        self.assertEqual(notifier.send_error.call_args_list[0].args, ("watch stopped: auth error: not_logged_in",))
        self.assertEqual(
            notifier.send_error.call_args_list[1].args,
            ("watch stopped: stop_type=auth_invalid source=watch stopped detail=not_logged_in",),
        )

    def test_watch_controller_does_not_restart_on_unresolved_challenge(self) -> None:
        on_stop = Mock()
        on_fatal = Mock()
        notifier = Mock()
        controller = self._build_watch_controller(
            notifier=notifier,
            on_stop=on_stop,
            on_fatal=on_fatal,
        )

        calls: list[int] = []

        def raise_unresolved_challenge(*args, **kwargs) -> None:
            calls.append(1)
            raise ChallengeUnresolvedError("challenge still present after solve")

        self._start_watch_with_side_effect(controller, raise_unresolved_challenge)

        self.assertEqual(len(calls), 1)
        self._assert_watch_stopped(controller, "challenge still present after solve")
        self.assertIsInstance(controller.fatal_error(), ChallengeUnresolvedError)
        on_stop.assert_called_once_with()
        on_fatal.assert_called_once_with()
        self.assertEqual(notifier.send_error.call_count, 2)
        self.assertEqual(
            notifier.send_error.call_args_list[1].args,
            (
                "watch stopped: stop_type=unresolved_challenge "
                "source=watch stopped detail=challenge still present after solve",
            ),
        )

    def test_watch_controller_runs_on_stop_when_same_error_threshold_hits(self) -> None:
        on_stop = Mock()
        notifier = Mock()
        controller = self._build_watch_controller(
            notifier=notifier,
            same_error_stop_threshold=2,
            on_stop=on_stop,
        )

        calls: list[int] = []

        def raise_runtime_error(*args, **kwargs) -> None:
            calls.append(1)
            raise RuntimeError("boom")

        self._start_watch_with_side_effect(controller, raise_runtime_error)

        self.assertEqual(len(calls), 2)
        self._assert_watch_stopped(controller, "boom")
        on_stop.assert_called_once_with()
        self.assertEqual(notifier.send_error.call_count, 3)
        self.assertEqual(
            notifier.send_error.call_args_list[-1].args,
            ("watch stopped: stop_type=same_error_threshold same_error_count=2 detail=boom",),
        )

    def test_watch_controller_notifies_stop_type_when_auto_restart_disabled(self) -> None:
        notifier = Mock()
        controller = self._build_watch_controller(
            notifier=notifier,
            auto_restart=False,
        )

        self._start_watch_with_side_effect(controller, RuntimeError("boom"))

        self.assertEqual(notifier.send_error.call_count, 2)
        self.assertEqual(
            notifier.send_error.call_args_list[-1].args,
            ("watch stopped: stop_type=auto_restart_disabled detail=boom",),
        )

    def test_watch_controller_notifies_stop_type_when_max_restarts_exceeded(self) -> None:
        notifier = Mock()
        controller = self._build_watch_controller(
            notifier=notifier,
            max_restarts=1,
        )

        self._start_watch_with_side_effect(controller, RuntimeError("boom"), patch_sleep=True)

        self.assertEqual(notifier.send_error.call_count, 3)
        self.assertEqual(
            notifier.send_error.call_args_list[-1].args,
            ("watch stopped: stop_type=max_restarts_exceeded max_restarts=1 detail=boom",),
        )

    def test_watch_controller_reuses_memory_notification_dedupe_across_restarts(self) -> None:
        controller = self._build_watch_controller(
            store=None,
            max_restarts=1,
        )
        seen_sets: list[set[int]] = []

        def raise_runtime_error(*args, **kwargs) -> None:
            seen_sets.append(kwargs["sent_notification_ids_mem"])
            raise RuntimeError("boom")

        self._start_watch_with_side_effect(controller, raise_runtime_error, patch_sleep=True)

        self.assertEqual(len(seen_sets), 2)
        self.assertIs(seen_sets[0], seen_sets[1])

    def test_watch_controller_restarts_running_watch_when_runtime_config_changes(self) -> None:
        controller = self._build_watch_controller(
            interval_secs=1,
            max_posts_per_interval=20,
            use_unseen=False,
            timings_per_topic=5,
        )
        started = threading.Event()
        allow_exit = threading.Event()
        calls: list[dict[str, object]] = []

        def fake_watch(*args, **kwargs) -> None:
            calls.append(
                {
                    "use_unseen": kwargs["use_unseen"],
                    "timings_per_topic": kwargs["timings_per_topic"],
                    "max_posts_per_interval": kwargs["max_posts_per_interval"],
                }
            )
            started.set()
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)
            allow_exit.wait(timeout=1)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(started.wait(timeout=1))
            started.clear()
            allow_exit.set()
            updated = controller.configure(
                use_unseen=True,
                timings_per_topic=12,
                max_posts_per_interval=50,
            )
            self.assertEqual(
                updated,
                {
                    "ok": True,
                    "use_unseen": True,
                    "timings_per_topic": 12,
                    "max_posts_per_interval": 50,
                },
            )
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(
                calls,
                [
                    {"use_unseen": False, "timings_per_topic": 5, "max_posts_per_interval": 20},
                    {"use_unseen": True, "timings_per_topic": 12, "max_posts_per_interval": 50},
                ],
            )
            self.assertEqual(controller.status()["use_unseen"], True)
            self.assertEqual(controller.status()["timings_per_topic"], 12)
            self.assertEqual(controller.status()["max_posts_per_interval"], 50)
            controller.stop()
            controller._runtime.thread.join(timeout=1)

    def test_watch_controller_noop_config_does_not_restart_running_watch(self) -> None:
        controller = self._build_watch_controller(
            interval_secs=1,
            max_posts_per_interval=20,
            use_unseen=False,
            timings_per_topic=5,
        )
        started = threading.Event()
        calls: list[int] = []

        def fake_watch(*args, **kwargs) -> None:
            calls.append(1)
            started.set()
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(started.wait(timeout=1))
            started_at = controller.status()["started_at"]
            updated = controller.configure()
            self.assertEqual(
                updated,
                {
                    "ok": True,
                    "use_unseen": False,
                    "timings_per_topic": 5,
                    "max_posts_per_interval": 20,
                },
            )
            time.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertEqual(controller.status()["started_at"], started_at)
            controller.stop()
            controller._runtime.thread.join(timeout=1)

    def test_watch_controller_status_hides_next_run_when_schedule_disabled(self) -> None:
        controller = self._build_watch_controller(
            schedule_windows=["08:00-12:00"],
        )
        started = threading.Event()

        def fake_watch(*args, **kwargs) -> None:
            started.set()
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(started.wait(timeout=1))
            status = controller.status()
            self.assertEqual(status["use_schedule"], False)
            self.assertEqual(status["schedule"], ["08:00-12:00"])
            self.assertEqual(status["stop_requested"], False)
            self.assertEqual(status["stopping"], False)
            self.assertIsNone(status["next_run"])
            controller.stop()
            controller._runtime.thread.join(timeout=1)

    def test_watch_controller_status_reports_stopping_while_thread_is_winding_down(self) -> None:
        controller = self._build_watch_controller()
        started = threading.Event()

        def fake_watch(*args, **kwargs) -> None:
            started.set()
            time.sleep(0.2)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(controller.stop())
            status = controller.status()
            self.assertEqual(status["running"], True)
            self.assertEqual(status["stop_requested"], True)
            self.assertEqual(status["stopping"], True)
            controller._runtime.thread.join(timeout=1)
            status = controller.status()
            self.assertEqual(status["running"], False)
            self.assertEqual(status["stop_requested"], True)
            self.assertEqual(status["stopping"], False)

    def test_watch_controller_stop_returns_true_after_thread_already_exited(self) -> None:
        controller = self._build_watch_controller()
        finished = threading.Event()

        def fake_watch(*args, **kwargs) -> None:
            finished.set()

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(finished.wait(timeout=1))
            controller._runtime.thread.join(timeout=1)
            self.assertFalse(controller.status()["running"])
            self.assertEqual(controller.status()["stop_requested"], False)
            self.assertTrue(controller.stop())

    def test_watch_controller_configure_interrupts_restart_backoff(self) -> None:
        controller = self._build_watch_controller(
            interval_secs=1,
            restart_backoff_secs=30,
            max_posts_per_interval=20,
            use_unseen=False,
            timings_per_topic=5,
        )
        first_call = threading.Event()
        restarted = threading.Event()
        calls: list[dict[str, object]] = []

        def fake_watch(*args, **kwargs) -> None:
            calls.append(
                {
                    "use_unseen": kwargs["use_unseen"],
                    "timings_per_topic": kwargs["timings_per_topic"],
                    "max_posts_per_interval": kwargs["max_posts_per_interval"],
                }
            )
            if len(calls) == 1:
                first_call.set()
                raise RuntimeError("boom")
            restarted.set()
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(first_call.wait(timeout=1))
            start = time.monotonic()
            updated = controller.configure(use_unseen=True)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 5.0)
            self.assertEqual(
                updated,
                {
                    "ok": True,
                    "use_unseen": True,
                    "timings_per_topic": 5,
                    "max_posts_per_interval": 20,
                },
            )
            self.assertTrue(restarted.wait(timeout=1))
            self.assertEqual(
                calls,
                [
                    {"use_unseen": False, "timings_per_topic": 5, "max_posts_per_interval": 20},
                    {"use_unseen": True, "timings_per_topic": 5, "max_posts_per_interval": 20},
                ],
            )
            controller.stop()
            controller._runtime.thread.join(timeout=1)

    def test_watch_controller_configure_waits_for_inflight_request_timeout_budget(self) -> None:
        client = types.SimpleNamespace(
            _inner=types.SimpleNamespace(
                _requester=types.SimpleNamespace(_timeout_secs=2.0),
            )
        )
        controller = self._build_watch_controller(
            client=client,
            interval_secs=1,
            restart_backoff_secs=1,
            max_posts_per_interval=20,
            use_unseen=False,
            timings_per_topic=5,
        )
        started = threading.Event()
        restarted = threading.Event()
        calls: list[dict[str, object]] = []

        def fake_watch(*args, **kwargs) -> None:
            calls.append(
                {
                    "use_unseen": kwargs["use_unseen"],
                    "timings_per_topic": kwargs["timings_per_topic"],
                    "max_posts_per_interval": kwargs["max_posts_per_interval"],
                }
            )
            if len(calls) == 1:
                started.set()
                time.sleep(1.2)
                return
            restarted.set()
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(started.wait(timeout=1))
            updated = controller.configure(use_unseen=True)
            self.assertEqual(
                updated,
                {
                    "ok": True,
                    "use_unseen": True,
                    "timings_per_topic": 5,
                    "max_posts_per_interval": 20,
                },
            )
            self.assertTrue(restarted.wait(timeout=1))
            controller.stop()
            controller._runtime.thread.join(timeout=1)

    def test_watch_controller_stop_interrupts_restart_backoff(self) -> None:
        controller = self._build_watch_controller(
            interval_secs=1,
            restart_backoff_secs=30,
        )
        first_call = threading.Event()

        def fake_watch(*args, **kwargs) -> None:
            first_call.set()
            raise RuntimeError("boom")

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(first_call.wait(timeout=1))
            start = time.monotonic()
            self.assertTrue(controller.stop())
            controller._runtime.thread.join(timeout=1)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 5.0)
            self.assertFalse(controller._runtime.thread.is_alive())

    def test_poll_notifications_marks_only_successful_sends(self) -> None:
        store = _RecordingStore()
        notifier = _Notifier()
        client = _NotificationClient()

        _poll_notifications(client, store, notifier, auto_mark_read=False)

        self.assertEqual([item["id"] for item in store.marked], [1])
        self.assertEqual(store.stats, [("notifications_sent", 1)])
        self.assertEqual(len(notifier.sent), 2)
        self.assertEqual(client.mark_read_calls, 0)

    def test_poll_notifications_keeps_memory_dedupe_without_store(self) -> None:
        notifier = _Notifier()
        sent_ids_mem: set[int] = set()
        client = _NotificationClient()

        _poll_notifications(client, None, notifier, sent_notification_ids_mem=sent_ids_mem, auto_mark_read=False)
        _poll_notifications(client, None, notifier, sent_notification_ids_mem=sent_ids_mem, auto_mark_read=False)

        self.assertEqual(sent_ids_mem, {1})
        self.assertEqual(len(notifier.sent), 3)
        self.assertEqual(client.mark_read_calls, 0)

    def test_poll_notifications_marks_read_when_all_unread_are_locally_marked(self) -> None:
        client = _NotificationClient()
        client.get_notifications = lambda limit=30, recent=True: {
            "notifications": [
                {"id": 9, "read": False, "created_at": "2026-03-18T00:00:02Z", "data": {"topic_title": "ok-9"}},
                {"id": 7, "read": False, "created_at": "2026-03-18T00:00:01Z", "data": {"topic_title": "ok-7"}},
            ]
        }
        notifier = _Notifier()

        _poll_notifications(client, None, notifier, sent_notification_ids_mem=set(), auto_mark_read=True)

        self.assertEqual(client.mark_read_calls, 1)

    def test_poll_notifications_does_not_mark_read_when_any_send_fails(self) -> None:
        client = _NotificationClient()
        notifier = _Notifier()

        _poll_notifications(client, None, notifier, sent_notification_ids_mem=set(), auto_mark_read=True)

        self.assertEqual(client.mark_read_calls, 0)

    def test_poll_notifications_persists_partial_success_when_auto_mark_read_enabled(self) -> None:
        store = _RecordingStore()
        notifier = _Notifier()
        client = _NotificationClient()

        _poll_notifications(client, store, notifier, auto_mark_read=True)

        self.assertEqual([item["id"] for item in store.marked], [1])
        self.assertEqual(store.stats, [("notifications_sent", 1)])
        self.assertEqual(client.mark_read_calls, 0)

    def test_poll_notifications_memory_dedupe_persists_partial_success_when_auto_mark_read_enabled(self) -> None:
        notifier = _Notifier()
        client = _NotificationClient()
        sent_ids_mem: set[int] = set()

        _poll_notifications(client, None, notifier, sent_notification_ids_mem=sent_ids_mem, auto_mark_read=True)

        self.assertEqual(sent_ids_mem, {1})
        self.assertEqual(client.mark_read_calls, 0)

    def test_poll_notifications_marks_read_when_all_unread_were_already_locally_marked(self) -> None:
        notifier = _Notifier()
        client = _NotificationClient()
        sent_ids_mem = {1, 2}

        _poll_notifications(client, None, notifier, sent_notification_ids_mem=sent_ids_mem, auto_mark_read=True)

        self.assertEqual(client.mark_read_calls, 1)
        self.assertEqual(notifier.sent, [])

    def test_poll_notifications_keeps_local_dedupe_when_mark_read_fails(self) -> None:
        store = _RecordingStore()
        notifier = _Notifier()
        client = _NotificationClient()
        client.get_notifications = lambda limit=30, recent=True: {
            "notifications": [
                {"id": 9, "read": False, "created_at": "2026-03-18T00:00:02Z", "data": {"topic_title": "ok-9"}},
                {"id": 7, "read": False, "created_at": "2026-03-18T00:00:01Z", "data": {"topic_title": "ok-7"}},
            ]
        }
        client.mark_notifications_read = lambda: (_ for _ in ()).throw(RuntimeError("mark-read failed"))

        with self.assertRaisesRegex(RuntimeError, "mark-read failed"):
            _poll_notifications(client, store, notifier, auto_mark_read=True)

        self.assertEqual([item["id"] for item in store.marked], [9, 7])
        self.assertEqual(store.stats, [("notifications_sent", 2)])

    def test_status_without_store_reports_storage_disabled(self) -> None:
        self.assertEqual(
            status_flow(None),
            {
                "storage_enabled": False,
                "stats_total": None,
                "stats_today": None,
                "storage_path": None,
                "plugins": {"enabled": False, "count": 0, "backend": None, "runtime_live": False, "items": []},
            },
        )

    def test_post_timings_raises_auth_error_from_json_body(self) -> None:
        client = DiscourseClient(
            requester=_Requester([_Response(403, '{"error_type":"not_logged_in"}')]),
            csrf_token="csrf",
        )

        with self.assertRaises(DiscourseAuthError):
            client.post_timings(topic_id=1, timings={1: 1000}, topic_time=1000)

    def test_mark_notifications_read_uses_put_request_without_body(self) -> None:
        requester = _Requester([_Response(200, '{"ok":true}')])
        client = DiscourseClient(requester=requester, csrf_token="csrf")

        result = client.mark_notifications_read()

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requester.calls), 1)
        args, kwargs = requester.calls[0]
        self.assertEqual(args[0], "put")
        self.assertEqual(args[1], "/notifications/mark-read")
        self.assertIsNone(kwargs["data"])
        self.assertEqual(kwargs["headers"]["discourse-logged-in"], "true")
        self.assertEqual(kwargs["headers"]["discourse-present"], "true")
        self.assertNotIn("content-type", kwargs["headers"])

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

        refresh.assert_called_once_with(force_refresh=True)

    def test_get_csrf_force_refresh_ignores_cached_token(self) -> None:
        requester = _Requester([_Response(200, '{"csrf":"fresh"}')])
        requester._csrf_token_hint = "hinted-csrf"
        client = DiscourseClient(requester=requester, csrf_token="old")

        token = client.get_csrf(force_refresh=True)

        self.assertEqual(token, "fresh")
        self.assertEqual(len(requester.calls), 1)
        self.assertEqual(requester.calls[0][0][1], "/session/csrf")

    def test_get_csrf_uses_flaresolverr_base_url_when_enabled(self) -> None:
        requester = _Requester([])
        requester._use_flaresolverr_for_csrf = True
        requester._flaresolverr_csrf_token = "fs-csrf"
        client = DiscourseClient(requester=requester, csrf_token="")

        token = client.get_csrf()

        self.assertEqual(token, "fs-csrf")
        self.assertEqual(requester.calls, [])

    def test_get_latest_uses_requester_csrf_hint(self) -> None:
        requester = _Requester([_Response(200, '{"topic_list": {}}')])
        requester._csrf_token_hint = "hinted-csrf"
        client = DiscourseClient(requester=requester, csrf_token="")

        client.get_latest()

        self.assertEqual(len(requester.calls), 1)
        self.assertEqual(requester.calls[0][0][1], "/latest.json")
        self.assertEqual(requester.calls[0][1]["headers"]["x-csrf-token"], "hinted-csrf")
        self.assertEqual(requester._csrf_token_hint, "")

    def test_force_refreshed_csrf_is_not_overwritten_by_stale_hint(self) -> None:
        requester = _Requester([_Response(200, '{"csrf":"fresh"}'), _Response(200, '{"topic_list": {}}')])
        requester._csrf_token_hint = "hinted-csrf"
        client = DiscourseClient(requester=requester, csrf_token="old")

        token = client.get_csrf(force_refresh=True)
        client.get_latest()

        self.assertEqual(token, "fresh")
        self.assertEqual(len(requester.calls), 2)
        self.assertEqual(requester.calls[1][0][1], "/latest.json")
        self.assertEqual(requester.calls[1][1]["headers"]["x-csrf-token"], "fresh")


if __name__ == "__main__":
    unittest.main()
