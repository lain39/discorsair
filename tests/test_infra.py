"""Infrastructure tests for queue, storage, and HTTP control handler."""

from __future__ import annotations

import json
import http.client
import io
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import TimeoutError
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.core.request_queue import RequestQueue
from discorsair.core.requester import ChallengeUnresolvedError, RateLimitedError
from discorsair.discourse.client import DiscourseAuthError
from discorsair.discourse.queued_client import QueuedDiscourseClient
from discorsair.server.http_server import ControlHandler, ControlServer, WatchController, serve
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

    def test_rate_limited_task_is_rescheduled_without_blocking_other_keys(self) -> None:
        queue = RequestQueue()
        calls: list[str] = []
        rate_limited_once = threading.Event()

        def rate_limited() -> str:
            calls.append("rate_limited")
            if not rate_limited_once.is_set():
                rate_limited_once.set()
                raise RateLimitedError(0.05, detail="topics")
            return "retried"

        def other() -> str:
            calls.append("other")
            return "other"

        first = queue.submit(rate_limited, rate_limit_key="get_latest")
        second = queue.submit(other, rate_limit_key="reply")

        self.assertEqual(second.result(timeout=1), "other")
        self.assertEqual(first.result(timeout=1), "retried")
        self.assertEqual(calls, ["rate_limited", "other", "rate_limited"])
        queue.stop()

    def test_same_rate_limit_key_waits_for_cooldown_before_running_again(self) -> None:
        queue = RequestQueue()
        calls: list[str] = []
        rate_limited_once = threading.Event()

        def first_call() -> str:
            calls.append("first")
            if not rate_limited_once.is_set():
                rate_limited_once.set()
                raise RateLimitedError(0.05, detail="notifications")
            return "first-retried"

        def second_call() -> str:
            calls.append("second")
            return "second-ok"

        first = queue.submit(first_call, rate_limit_key="get_notifications")
        second = queue.submit(second_call, rate_limit_key="get_notifications")

        self.assertEqual(first.result(timeout=1), "first-retried")
        self.assertEqual(second.result(timeout=1), "second-ok")
        self.assertEqual(calls, ["first", "first", "second"])
        queue.stop()

    def test_rate_limited_retry_is_not_dropped_when_running_task_occupies_maxsize(self) -> None:
        queue = RequestQueue(maxsize=1)
        calls: list[str] = []
        first_started = threading.Event()
        allow_retry = threading.Event()
        rate_limited_once = threading.Event()

        def rate_limited() -> str:
            calls.append("first")
            if not rate_limited_once.is_set():
                rate_limited_once.set()
                first_started.set()
                allow_retry.wait(timeout=1)
                raise RateLimitedError(0.05, detail="latest")
            return "first-retried"

        first = queue.submit(rate_limited, rate_limit_key="get_latest")
        self.assertTrue(first_started.wait(timeout=1))
        allow_retry.set()

        self.assertEqual(first.result(timeout=1), "first-retried")
        self.assertEqual(calls, ["first", "first"])
        queue.stop()

    def test_delayed_rate_limited_task_does_not_block_other_key_when_queue_is_idle(self) -> None:
        queue = RequestQueue(maxsize=1)
        calls: list[str] = []
        delayed = threading.Event()

        def rate_limited() -> str:
            calls.append("first")
            if not delayed.is_set():
                delayed.set()
                raise RateLimitedError(0.15, detail="latest")
            return "first-retried"

        def other() -> str:
            calls.append("other")
            return "other"

        first = queue.submit(rate_limited, rate_limit_key="get_latest")
        self.assertTrue(delayed.wait(timeout=1))

        second = queue.submit(other, rate_limit_key="reply")

        self.assertEqual(second.result(timeout=1), "other")
        self.assertEqual(first.result(timeout=1), "first-retried")
        self.assertEqual(calls, ["first", "other", "first"])
        queue.stop()

    def test_delayed_rate_limited_task_does_not_fail_queue_full_while_other_task_is_running(self) -> None:
        queue = RequestQueue(maxsize=1)
        running_started = threading.Event()
        release_running = threading.Event()

        def running() -> str:
            running_started.set()
            release_running.wait(timeout=1)
            return "running-done"

        running_future = queue.submit(running, rate_limit_key="reply")
        self.assertTrue(running_started.wait(timeout=1))
        queue._cooldowns["get_latest"] = time.monotonic() + 0.15

        first = queue.submit(lambda: "rate-limited-done", rate_limit_key="get_latest")

        release_running.set()
        self.assertEqual(running_future.result(timeout=1), "running-done")
        self.assertEqual(first.result(timeout=1), "rate-limited-done")
        queue.stop()

    def test_new_same_key_request_waits_for_existing_cooldown(self) -> None:
        queue = RequestQueue()
        first_finished = threading.Event()

        class _Inner:
            def __init__(self) -> None:
                self.calls = 0

            def get_latest(self):
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitedError(0.15, detail="topics")
                first_finished.set()
                return {"call": self.calls}

        inner = _Inner()
        client = QueuedDiscourseClient(inner, queue)
        results: list[dict[str, int]] = []
        errors: list[Exception] = []

        def run_first() -> None:
            try:
                results.append(client.get_latest())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        first_thread = threading.Thread(target=run_first, daemon=True)
        first_thread.start()
        time.sleep(0.02)

        second_result = client.get_latest()

        first_thread.join(timeout=1)
        queue.stop()

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(first_finished.is_set())
        self.assertEqual(results, [{"call": 2}])
        self.assertEqual(second_result, {"call": 3})

    def test_same_key_request_waits_for_extended_cooldown(self) -> None:
        queue = RequestQueue()
        first_finished = threading.Event()
        second_attempt_ready = threading.Event()

        class _Inner:
            def __init__(self) -> None:
                self.calls = 0

            def get_latest(self):
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitedError(0.05, detail="topics-1")
                if self.calls == 2:
                    second_attempt_ready.set()
                    raise RateLimitedError(0.15, detail="topics-2")
                first_finished.set()
                return {"call": self.calls}

        inner = _Inner()
        client = QueuedDiscourseClient(inner, queue)
        results: list[dict[str, int]] = []
        errors: list[Exception] = []

        def run_first() -> None:
            try:
                results.append(client.get_latest())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        first_thread = threading.Thread(target=run_first, daemon=True)
        first_thread.start()
        self.assertTrue(second_attempt_ready.wait(timeout=1))

        second_result = client.get_latest()

        first_thread.join(timeout=1)
        queue.stop()

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(first_finished.is_set())
        self.assertEqual(results, [{"call": 3}])
        self.assertEqual(second_result, {"call": 4})

    def test_queued_client_propagates_completed_timeout_error(self) -> None:
        queue = RequestQueue()

        class _Inner:
            def get_latest(self):
                raise TimeoutError("upstream timeout")

        client = QueuedDiscourseClient(_Inner(), queue)
        errors: list[Exception] = []

        def run() -> None:
            try:
                client.get_latest()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=1)
        queue.stop()

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TimeoutError)
        self.assertEqual(str(errors[0]), "upstream timeout")

    def test_queued_client_optional_timeout_returns_timeout_and_cancels_pending_future(self) -> None:
        queue = RequestQueue()
        release = threading.Event()

        class _Inner:
            def toggle_reaction(self, post_id: int, emoji: str):
                release.wait(timeout=1)
                return {"post_id": post_id, "emoji": emoji}

        client = QueuedDiscourseClient(_Inner(), queue)

        with self.assertRaises(TimeoutError):
            client.toggle_reaction(7, "heart", timeout_secs=0.05)

        release.set()
        self.assertEqual(queue.submit(lambda: "ok").result(timeout=1), "ok")
        queue.stop()

    def test_queued_client_zero_timeout_means_no_timeout(self) -> None:
        queue = RequestQueue()
        release = threading.Event()

        class _Inner:
            def toggle_reaction(self, post_id: int, emoji: str):
                release.wait(timeout=1)
                return {"post_id": post_id, "emoji": emoji}

        client = QueuedDiscourseClient(_Inner(), queue)

        result_holder: list[dict[str, object]] = []

        def run() -> None:
            result_holder.append(client.toggle_reaction(7, "heart", timeout_secs=0))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.05)
        release.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder, [{"post_id": 7, "emoji": "heart"}])
        queue.stop()

    def test_queued_client_exposes_persist_candidate_cookie_header(self) -> None:
        queue = RequestQueue()

        class _Inner:
            def get_cookie_header(self) -> str:
                return "_t=runtime-cookie"

            def get_persist_candidate_cookie_header(self) -> str:
                return "_t=validated-cookie"

            def last_response_ok(self) -> bool | None:
                return True

        client = QueuedDiscourseClient(_Inner(), queue)

        self.assertEqual(client.get_persist_candidate_cookie_header(), "_t=validated-cookie")
        queue.stop()

    def test_cancelled_delayed_task_does_not_keep_queue_full(self) -> None:
        queue = RequestQueue(maxsize=1)
        delayed = queue.submit(lambda: "late", not_before=time.monotonic() + 0.3)
        delayed.cancel()
        next_task = queue.submit(lambda: "ok")

        self.assertTrue(delayed.cancelled())
        self.assertEqual(next_task.result(timeout=1), "ok")
        queue.stop()

class SQLiteStoreTests(unittest.TestCase):
    def test_store_persists_posts_topics_notifications_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(
                str(Path(tmpdir) / "discorsair.db"),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
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

                store.upsert_topic_detail(
                    {"id": 10, "title": "hello", "reply_count": 7, "views": 123, "unseen": True, "last_read_post_number": 5},
                    {
                        "id": 10,
                        "title": "hello",
                        "slug": "hello",
                        "tags": [{"id": 1, "name": "x", "slug": "x"}],
                        "category_id": 9,
                        "reply_count": 7,
                        "views": 123,
                        "highest_post_number": 8,
                        "last_posted_at": "2026-03-18T00:00:00Z",
                        "post_stream": {
                            "posts": [{"id": 101, "post_number": 1, "created_at": "2026-03-18T00:00:00Z", "updated_at": "2026-03-18T00:00:00Z"}],
                            "stream": [101],
                        },
                    },
                )
                store.upsert_topic_crawl_state(10, last_synced_post_number=8, last_stream_len=3)
                self.assertEqual(store.get_last_synced_post_number(10), 8)

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

                self.assertEqual(store.get_plugin_daily_count("plug", "like"), 0)
                self.assertEqual(store.inc_plugin_daily_count("plug", "like", 2), 2)
                self.assertEqual(store.get_plugin_daily_count("plug", "like"), 2)
                self.assertFalse(store.plugin_once_exists("plug", "topic:1"))
                store.mark_plugin_once("plug", "topic:1")
                self.assertTrue(store.plugin_once_exists("plug", "topic:1"))
                self.assertEqual(store.get_plugin_kv("plug", "k", default=None), None)
                store.set_plugin_kv("plug", "k", {"v": 1})
                self.assertEqual(store.get_plugin_kv("plug", "k", default=None), {"v": 1})
            finally:
                store.close()

    def test_store_bootstraps_site_and_account_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(
                str(Path(tmpdir) / "discorsair.db"),
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
                timezone_name="UTC",
            )
            try:
                row = store._conn.execute("SELECT site_key, base_url FROM sites").fetchone()
                self.assertEqual(row, ("forum.example", "https://forum.example"))
                row = store._conn.execute("SELECT site_key, account_name FROM accounts").fetchone()
                self.assertEqual(row, ("forum.example", "main"))
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
        self._start_blocked_reason: str | None = None
        self._last_stop_reason: str | None = None
        self.start_calls: list[tuple[bool, bool]] = []
        self.stop_calls = 0
        self.auth_invalid_calls: list[tuple[str, str]] = []
        self.unresolved_challenge_calls: list[tuple[str, str]] = []
        self.fatal_callback = None

    def status(self) -> dict[str, object]:
        return {
            "running": False,
            "stats_total": {},
            "stats_today": {},
            "start_blocked_reason": self._start_blocked_reason,
            "last_stop_reason": self._last_stop_reason,
            "recovery_required": bool(self._start_blocked_reason),
        }

    def start(self, use_schedule: bool = True) -> bool:
        self.start_calls.append((use_schedule, False))
        return True

    def start_result(self, *, use_schedule: bool = True, force: bool = False) -> dict[str, object]:
        self.start_calls.append((use_schedule, force))
        if self._start_blocked_reason and not force:
            return {"ok": False, "reason": self._start_blocked_reason}
        self._start_blocked_reason = None
        return {"ok": True}

    def stop(self) -> bool:
        self.stop_calls += 1
        return True

    def stop_result(self) -> dict[str, bool]:
        self.stop_calls += 1
        self._last_stop_reason = "manual_stop"
        return {"ok": True, "already_stopped": True}

    def handle_auth_invalid(self, exc: Exception, *, source: str) -> None:
        self.auth_invalid_calls.append((source, str(exc)))
        self._start_blocked_reason = "auth_invalid"
        self._last_stop_reason = "auth_invalid"
        self.stop()

    def handle_unresolved_challenge(self, exc: Exception, *, source: str) -> None:
        self.unresolved_challenge_calls.append((source, str(exc)))
        self._start_blocked_reason = "unresolved_challenge"
        self._last_stop_reason = "unresolved_challenge"
        self.stop()

    def clear_start_blocked_reason(self) -> None:
        self._start_blocked_reason = None

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
            parsed_timings = int(timings_per_topic)
            if parsed_timings < 1:
                raise ValueError("timings_per_topic must be >= 1")
            self._timings_per_topic = parsed_timings
        if max_posts_per_interval is not False and max_posts_per_interval is not None:
            parsed_max_posts = int(max_posts_per_interval)
            if parsed_max_posts < 0:
                raise ValueError("max_posts_per_interval must be >= 0 or null")
            self._max_posts_per_interval = parsed_max_posts
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

    def set_on_fatal(self, callback) -> None:
        self.fatal_callback = callback


class _DummyClient:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str, int | None]] = []
        self.reaction_error: Exception | None = None
        self.reply_error: Exception | None = None

    def toggle_reaction(self, post_id: int, emoji: str, timeout_secs: float | None = None) -> dict[str, object]:
        if self.reaction_error is not None:
            raise self.reaction_error
        self.reactions.append((post_id, emoji))
        return {"post_id": post_id, "emoji": emoji}

    def reply(
        self,
        topic_id: int,
        raw: str,
        category: int | None = None,
        timeout_secs: float | None = None,
    ) -> dict[str, object]:
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append((topic_id, raw, category))
        return {"topic_id": topic_id, "raw": raw, "category": category}


class ControlHandlerTests(unittest.TestCase):
    def _real_watch_controller(self) -> WatchController:
        return WatchController(
            client=types.SimpleNamespace(),
            store=None,
            notifier=None,
            interval_secs=1,
            max_posts_per_interval=None,
            crawl_enabled=False,
            use_unseen=False,
            timings_per_topic=5,
            schedule_windows=[],
            notify_interval_secs=60,
            notify_auto_mark_read=False,
            plugin_manager=None,
            auto_restart=True,
            restart_backoff_secs=1,
            max_restarts=0,
            same_error_stop_threshold=0,
            timezone_name="UTC",
        )

    def _run_handler(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        raw_body: str | None = None,
        headers: dict[str, str] | None = None,
        *,
        client: _DummyClient | None = None,
        watch_controller: _DummyWatchController | None = None,
        shutdown: Mock | None = None,
        on_action_success: Mock | None = None,
        auth_cookie_updater: Mock | None = None,
    ) -> tuple[int, dict[str, object]]:
        handler = object.__new__(ControlHandler)
        request_headers = dict(headers or {})
        payload = ""
        if raw_body is not None:
            payload = raw_body
            request_headers["Content-Type"] = "application/json"
        elif body is not None:
            payload = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(payload.encode("utf-8")))
        handler.headers = request_headers
        handler.path = path
        handler.rfile = io.BytesIO(payload.encode("utf-8"))
        watch = watch_controller or _DummyWatchController()
        handler.server = types.SimpleNamespace(
            api_key="secret",
            action_timeout_secs=60,
            client=client or _DummyClient(),
            watch_controller=watch,
            shutdown=shutdown or (lambda: None),
            request_shutdown=shutdown or (lambda: None),
            on_action_success=on_action_success,
            auth_cookie_updater=auth_cookie_updater,
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

    def test_healthz_is_public_without_api_key(self) -> None:
        (status, data), _ = self._run_handler("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})

    def test_root_is_public_without_api_key(self) -> None:
        (status, data), _ = self._run_handler("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})

    def test_root_with_query_is_public_without_api_key(self) -> None:
        (status, data), _ = self._run_handler("GET", "/?__sign=abc")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})

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

    def test_watch_config_rejects_non_boolean_use_unseen(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/config",
            body={"use_unseen": "false"},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "use_unseen must be a boolean"})
        self.assertFalse(server.watch_controller._use_unseen)

    def test_watch_config_rejects_negative_max_posts_per_interval(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/config",
            body={"max_posts_per_interval": -1},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "max_posts_per_interval must be >= 0 or null"})
        self.assertEqual(server.watch_controller._max_posts_per_interval, 200)

    def test_reply_rejects_non_string_raw(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/reply",
            body={"topic_id": 9, "raw": 123},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "raw must be a string"})
        self.assertEqual(server.client.replies, [])

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
        self.assertEqual(server.watch_controller.start_calls, [(False, False)])

    def test_watch_start_forwards_force_flag(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/start",
            body={"use_schedule": True, "force": True},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(server.watch_controller.start_calls, [(True, True)])

    def test_watch_start_reports_blocked_reason_without_force(self) -> None:
        watch_controller = _DummyWatchController()
        watch_controller._start_blocked_reason = "auth_invalid"
        (status, data), server = self._run_handler(
            "POST",
            "/watch/start",
            body={"use_schedule": False},
            headers={"X-API-Key": "secret"},
            watch_controller=watch_controller,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": False, "reason": "auth_invalid"})
        self.assertEqual(server.watch_controller.start_calls, [(False, False)])

    def test_watch_start_rejects_invalid_json(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/start",
            raw_body="{bad json",
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "invalid JSON body"})
        self.assertEqual(server.watch_controller.start_calls, [])

    def test_watch_start_rejects_non_boolean_use_schedule(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/start",
            body={"use_schedule": "false"},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "use_schedule must be a boolean"})
        self.assertEqual(server.watch_controller.start_calls, [])

    def test_watch_start_rejects_non_boolean_force(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/start",
            body={"force": "yes"},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "force must be a boolean"})
        self.assertEqual(server.watch_controller.start_calls, [])

    def test_auth_cookie_updates_runtime_and_clears_blocked_reason(self) -> None:
        updater = Mock(return_value={"cookie_updated": True})
        watch_controller = _DummyWatchController()
        watch_controller._start_blocked_reason = "auth_invalid"
        (status, data), server = self._run_handler(
            "POST",
            "/auth/cookie",
            body={"cookie": "_t=new-token; cf_clearance=abc"},
            headers={"X-API-Key": "secret"},
            watch_controller=watch_controller,
            auth_cookie_updater=updater,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "cookie_updated": True})
        updater.assert_called_once_with("_t=new-token; cf_clearance=abc")
        self.assertIsNone(watch_controller.status()["start_blocked_reason"])

    def test_auth_cookie_requires_configured_updater(self) -> None:
        (status, data), _ = self._run_handler(
            "POST",
            "/auth/cookie",
            body={"cookie": "_t=new-token"},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 500)
        self.assertEqual(data, {"error": "internal"})

    def test_auth_cookie_returns_bad_request_when_updater_rejects_cookie(self) -> None:
        updater = Mock(side_effect=ValueError("cookie must include a non-empty _t"))
        (status, data), _ = self._run_handler(
            "POST",
            "/auth/cookie",
            body={"cookie": "cf_clearance=abc"},
            headers={"X-API-Key": "secret"},
            auth_cookie_updater=updater,
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "bad_request", "detail": "cookie must include a non-empty _t"})

    def test_watch_stop_calls_controller(self) -> None:
        (status, data), server = self._run_handler(
            "POST",
            "/watch/stop",
            body={},
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "already_stopped": True})
        self.assertEqual(server.watch_controller.stop_calls, 1)

    def test_watch_stop_reports_running_real_controller_as_not_already_stopped(self) -> None:
        controller = self._real_watch_controller()
        started = threading.Event()

        def fake_watch(*args, **kwargs) -> None:
            started.set()
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(started.wait(timeout=1))
            (status, data), _ = self._run_handler(
                "POST",
                "/watch/stop",
                body={},
                headers={"X-API-Key": "secret"},
                watch_controller=controller,
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "already_stopped": False})
        controller._runtime.thread.join(timeout=1)

    def test_watch_stop_reports_finished_real_controller_as_already_stopped(self) -> None:
        controller = self._real_watch_controller()
        finished = threading.Event()

        def fake_watch(*args, **kwargs) -> None:
            finished.set()

        with patch("discorsair.server.http_server.watch", side_effect=fake_watch):
            self.assertTrue(controller.start(use_schedule=False))
            self.assertTrue(finished.wait(timeout=1))
            controller._runtime.thread.join(timeout=1)
            (status, data), _ = self._run_handler(
                "POST",
                "/watch/stop",
                body={},
                headers={"X-API-Key": "secret"},
                watch_controller=controller,
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "already_stopped": True})

    def test_reply_requires_topic_id_and_raw(self) -> None:
        (status, data), _ = self._run_handler("POST", "/reply", body={}, headers={"X-API-Key": "secret"})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "topic_id and raw required"})

    def test_reply_success_returns_payload(self) -> None:
        on_action_success = Mock()
        (status, data), server = self._run_handler(
            "POST",
            "/reply",
            body={"topic_id": 9, "raw": "hello", "category": 3},
            headers={"X-API-Key": "secret"},
            on_action_success=on_action_success,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["result"]["topic_id"], 9)
        self.assertEqual(server.client.replies, [(9, "hello", 3)])
        on_action_success.assert_called_once_with()

    def test_like_auth_error_stops_watch_without_shutting_down_server(self) -> None:
        client = _DummyClient()
        client.reaction_error = DiscourseAuthError("not_logged_in")
        shutdown = Mock()
        watch_controller = _DummyWatchController()

        with patch(
            "discorsair.server.http_server.threading.Thread",
            side_effect=lambda *args, **kwargs: types.SimpleNamespace(start=lambda: kwargs["target"]()),
        ):
            (status, data), server = self._run_handler(
                "POST",
                "/like",
                body={"post_id": 7},
                headers={"X-API-Key": "secret"},
                client=client,
                watch_controller=watch_controller,
                shutdown=shutdown,
            )

        self.assertEqual(status, 401)
        self.assertEqual(data, {"error": "not_logged_in"})
        self.assertEqual(server.watch_controller.stop_calls, 1)
        self.assertEqual(server.watch_controller.auth_invalid_calls, [("http auth error", "not_logged_in")])
        shutdown.assert_not_called()
        self.assertEqual(server.watch_controller.status()["start_blocked_reason"], "auth_invalid")

    def test_reply_challenge_error_stops_watch_without_shutting_down_server(self) -> None:
        client = _DummyClient()
        client.reply_error = ChallengeUnresolvedError("challenge still present after solve")
        shutdown = Mock()
        watch_controller = _DummyWatchController()

        with patch(
            "discorsair.server.http_server.threading.Thread",
            side_effect=lambda *args, **kwargs: types.SimpleNamespace(start=lambda: kwargs["target"]()),
        ):
            (status, data), server = self._run_handler(
                "POST",
                "/reply",
                body={"topic_id": 9, "raw": "hello"},
                headers={"X-API-Key": "secret"},
                client=client,
                watch_controller=watch_controller,
                shutdown=shutdown,
            )

        self.assertEqual(status, 503)
        self.assertEqual(data, {"error": "challenge_unresolved"})
        self.assertEqual(server.watch_controller.stop_calls, 1)
        self.assertEqual(
            server.watch_controller.unresolved_challenge_calls,
            [("http unresolved challenge", "challenge still present after solve")],
        )
        shutdown.assert_not_called()
        self.assertEqual(server.watch_controller.status()["start_blocked_reason"], "unresolved_challenge")

    def test_like_timeout_returns_504(self) -> None:
        client = _DummyClient()
        client.reaction_error = TimeoutError("upstream timeout")

        (status, data), server = self._run_handler(
            "POST",
            "/like",
            body={"post_id": 7},
            headers={"X-API-Key": "secret"},
            client=client,
        )

        self.assertEqual(status, 504)
        self.assertEqual(data, {"error": "timeout"})
        self.assertEqual(server.watch_controller._runtime.last_error, "timeout")
        self.assertEqual(server.watch_controller._runtime.last_error_at, "now")

    def test_serve_wires_watch_controller_fatal_shutdown_callback(self) -> None:
        controller = Mock()
        httpd = Mock()

        with patch("discorsair.server.http_server.ControlServer", return_value=httpd):
            serve(
                host="127.0.0.1",
                port=8080,
                client=Mock(),
                watch_controller=controller,
                api_key="",
                on_action_success=Mock(),
            )

        controller.set_on_fatal.assert_called_once_with(httpd.request_shutdown)
        httpd.serve_forever.assert_called_once_with()


class ControlServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            raise unittest.SkipTest(f"localhost bind not permitted: {exc}") from exc

    def _request_json(
        self,
        port: int,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        payload = json.dumps(body) if body is not None else None
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        deadline = time.monotonic() + 2
        while True:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                conn.request(method, path, body=payload, headers=request_headers)
                resp = conn.getresponse()
                data = json.loads(resp.read().decode("utf-8"))
                return resp.status, data
            except (ConnectionRefusedError, ConnectionResetError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            finally:
                conn.close()

    def test_control_server_request_shutdown_stops_serve_forever(self) -> None:
        server = ControlServer(
            ("127.0.0.1", 0),
            ControlHandler,
            _DummyClient(),
            _DummyWatchController(),
            api_key="secret",
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            self._request_json(server.server_address[1], "GET", "/watch/status", headers={"X-API-Key": "secret"})
            server.request_shutdown()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                server.request_shutdown()
                thread.join(timeout=2)
            server.server_close()

    def test_real_http_healthz_is_public_when_api_key_is_configured(self) -> None:
        server = ControlServer(
            ("127.0.0.1", 0),
            ControlHandler,
            _DummyClient(),
            _DummyWatchController(),
            api_key="secret",
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            status, data = self._request_json(server.server_address[1], "GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(data, {"ok": True})
        finally:
            if thread.is_alive():
                server.request_shutdown()
                thread.join(timeout=2)
            server.server_close()

    def test_real_http_root_is_public_when_api_key_is_configured(self) -> None:
        server = ControlServer(
            ("127.0.0.1", 0),
            ControlHandler,
            _DummyClient(),
            _DummyWatchController(),
            api_key="secret",
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            status, data = self._request_json(server.server_address[1], "GET", "/")
            self.assertEqual(status, 200)
            self.assertEqual(data, {"ok": True})
        finally:
            if thread.is_alive():
                server.request_shutdown()
                thread.join(timeout=2)
            server.server_close()

    def test_real_http_root_with_query_is_public_when_api_key_is_configured(self) -> None:
        server = ControlServer(
            ("127.0.0.1", 0),
            ControlHandler,
            _DummyClient(),
            _DummyWatchController(),
            api_key="secret",
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            status, data = self._request_json(server.server_address[1], "GET", "/?__sign=abc")
            self.assertEqual(status, 200)
            self.assertEqual(data, {"ok": True})
        finally:
            if thread.is_alive():
                server.request_shutdown()
                thread.join(timeout=2)
            server.server_close()

    def test_real_http_reply_challenge_error_keeps_server_running(self) -> None:
        client = _DummyClient()
        client.reply_error = ChallengeUnresolvedError("challenge still present after solve")
        watch_controller = _DummyWatchController()
        server = ControlServer(
            ("127.0.0.1", 0),
            ControlHandler,
            client,
            watch_controller,
            api_key="secret",
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            status, data = self._request_json(
                server.server_address[1],
                "POST",
                "/reply",
                body={"topic_id": 9, "raw": "hello"},
                headers={"X-API-Key": "secret"},
            )
            self.assertEqual(status, 503)
            self.assertEqual(data, {"error": "challenge_unresolved"})
            status_data, payload = self._request_json(
                server.server_address[1],
                "GET",
                "/watch/status",
                headers={"X-API-Key": "secret"},
            )
            self.assertEqual(status_data, 200)
            self.assertEqual(payload["start_blocked_reason"], "unresolved_challenge")
            self.assertTrue(thread.is_alive())
            self.assertEqual(watch_controller.stop_calls, 1)
            self.assertEqual(
                watch_controller.unresolved_challenge_calls,
                [("http unresolved challenge", "challenge still present after solve")],
            )
        finally:
            if thread.is_alive():
                server.request_shutdown()
                thread.join(timeout=2)
            server.server_close()

    def test_real_http_like_timeout_returns_504(self) -> None:
        release = threading.Event()

        class _Inner:
            def toggle_reaction(self, post_id: int, emoji: str) -> dict[str, object]:
                release.wait(timeout=1)
                return {"post_id": post_id, "emoji": emoji}

            def reply(self, topic_id: int, raw: str, category: int | None = None) -> dict[str, object]:
                return {"topic_id": topic_id, "raw": raw, "category": category}

        watch_controller = _DummyWatchController()
        queue = RequestQueue()
        server = ControlServer(
            ("127.0.0.1", 0),
            ControlHandler,
            QueuedDiscourseClient(_Inner(), queue),
            watch_controller,
            api_key="secret",
            action_timeout_secs=0.05,
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            status, data = self._request_json(
                server.server_address[1],
                "POST",
                "/like",
                body={"post_id": 7},
                headers={"X-API-Key": "secret"},
            )
            self.assertEqual(status, 504)
            self.assertEqual(data, {"error": "timeout"})
            self.assertEqual(watch_controller._runtime.last_error, "timeout")
        finally:
            release.set()
            queue.stop()
            server.request_shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
