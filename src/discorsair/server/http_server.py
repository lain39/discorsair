"""HTTP control server for Discorsair."""

from __future__ import annotations

import json
import ipaddress
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from concurrent.futures import TimeoutError

from discorsair.discourse.client import DiscourseAuthError
from discorsair.discourse.queued_client import QueuedDiscourseClient
from discorsair.flows.watch import watch
from discorsair.storage.sqlite_store import SQLiteStore
from discorsair.utils.notify import Notifier

_UNSET = object()


@dataclass
class WatchRuntime:
    thread: threading.Thread | None = None
    stop_event: threading.Event | None = None
    started_at: str | None = None
    last_tick: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None


class WatchController:
    def __init__(
        self,
        client: QueuedDiscourseClient,
        store: SQLiteStore,
        notifier: Notifier | None,
        interval_secs: int,
        max_posts_per_interval: int | None,
        crawl_enabled: bool,
        use_unseen: bool,
        timings_per_topic: int,
        schedule_windows: list[str],
        notify_interval_secs: int,
        auto_restart: bool,
        restart_backoff_secs: int,
        max_restarts: int,
        same_error_stop_threshold: int,
        timezone_name: str,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._notifier = notifier
        self._interval_secs = interval_secs
        self._max_posts_per_interval = max_posts_per_interval
        self._crawl_enabled = crawl_enabled
        self._use_unseen = use_unseen
        self._timings_per_topic = timings_per_topic
        self._schedule_windows = schedule_windows
        self._notify_interval_secs = notify_interval_secs
        self._auto_restart = auto_restart
        self._restart_backoff_secs = restart_backoff_secs
        self._max_restarts = max_restarts
        self._same_error_stop_threshold = max(0, int(same_error_stop_threshold))
        self._timezone_name = timezone_name
        self._on_stop = on_stop
        self._runtime = WatchRuntime()
        self._last_error_sig: str | None = None
        self._same_error_count = 0

    def start(self, use_schedule: bool = True) -> bool:
        if self._runtime.thread and self._runtime.thread.is_alive():
            return False
        stop_event = threading.Event()
        self._runtime.stop_event = stop_event
        self._runtime.started_at = _now()
        self._runtime.last_tick = _now()
        self._last_error_sig = None
        self._same_error_count = 0

        def _run() -> None:
            restarts = 0
            while True:
                try:
                    watch(
                        self._client,
                        self._store,
                        interval_secs=self._interval_secs,
                        once=False,
                        max_posts_per_interval=self._max_posts_per_interval,
                        crawl_enabled=self._crawl_enabled,
                        use_unseen=self._use_unseen,
                        timings_per_topic=self._timings_per_topic,
                        notifier=self._notifier,
                        notify_interval_secs=self._notify_interval_secs,
                        on_success=self._tick,
                        stop_event=stop_event,
                        schedule_windows=self._schedule_windows if use_schedule else [],
                        timezone_name=self._timezone_name,
                    )
                    return
                except DiscourseAuthError as exc:
                    logging.getLogger(__name__).error("watch stopped due to auth error: %s", exc)
                    self.report_error(str(exc), f"watch stopped: auth error: {exc}")
                    if self._runtime.stop_event:
                        self._runtime.stop_event.set()
                    return
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger(__name__).error("watch thread crashed: %s", exc)
                    self.report_error(str(exc), f"watch crashed: {exc}")
                    sig = f"{exc.__class__.__name__}:{exc}"
                    if sig == self._last_error_sig:
                        self._same_error_count += 1
                    else:
                        self._same_error_count = 1
                        self._last_error_sig = sig
                    if self._same_error_stop_threshold > 0 and self._same_error_count >= self._same_error_stop_threshold:
                        logging.getLogger(__name__).error(
                            "watch auto-stopped after %s consecutive same errors",
                            self._same_error_count,
                        )
                        if self._notifier:
                            self._notifier.send_error(
                                f"watch auto-stopped after {self._same_error_count} consecutive same errors: {exc}"
                            )
                        if self._runtime.stop_event:
                            self._runtime.stop_event.set()
                        return
                    if not self._auto_restart:
                        if self._runtime.stop_event:
                            self._runtime.stop_event.set()
                        return
                    restarts += 1
                    if self._max_restarts > 0 and restarts > self._max_restarts:
                        if self._runtime.stop_event:
                            self._runtime.stop_event.set()
                        return
                    time.sleep(max(self._restart_backoff_secs, 1))

        t = threading.Thread(target=_run, daemon=True)
        self._runtime.thread = t
        t.start()
        logging.getLogger(__name__).info("watch started (schedule=%s)", use_schedule)
        return True

    def configure(
        self,
        *,
        use_unseen: bool | None = None,
        timings_per_topic: int | None = None,
        max_posts_per_interval: int | None | object = _UNSET,
    ) -> dict[str, Any]:
        if use_unseen is not None:
            self._use_unseen = bool(use_unseen)
        if timings_per_topic is not None:
            self._timings_per_topic = max(1, int(timings_per_topic))
        if max_posts_per_interval is not _UNSET:
            self._max_posts_per_interval = None if max_posts_per_interval is None else int(max_posts_per_interval)
        return {
            "ok": True,
            "use_unseen": self._use_unseen,
            "timings_per_topic": self._timings_per_topic,
            "max_posts_per_interval": self._max_posts_per_interval,
        }

    def stop(self) -> bool:
        if not self._runtime.stop_event:
            return False
        self._runtime.stop_event.set()
        logging.getLogger(__name__).info("watch stop requested")
        if self._on_stop:
            self._on_stop()
        return True

    def status(self) -> dict[str, Any]:
        running = bool(self._runtime.thread and self._runtime.thread.is_alive())
        next_run = _next_run_local(self._schedule_windows, self._timezone_name) if self._schedule_windows else None
        return {
            "running": running,
            "started_at": self._runtime.started_at,
            "last_tick": self._runtime.last_tick,
            "last_error": self._runtime.last_error,
            "last_error_at": self._runtime.last_error_at,
            "next_run": next_run,
            "storage_path": self._store.current_path(),
            "stats_total": self._store.get_stats_total(),
            "stats_today": self._store.get_stats_today(),
            "schedule": self._schedule_windows,
            "use_unseen": self._use_unseen,
            "timings_per_topic": self._timings_per_topic,
            "max_posts_per_interval": self._max_posts_per_interval,
            "timezone": self._timezone_name,
        }

    def _tick(self) -> None:
        self._runtime.last_tick = _now()
        self._same_error_count = 0
        self._last_error_sig = None

    def report_error(self, message: str, notify_message: str | None = None) -> None:
        self._set_runtime_error(message)
        if notify_message and self._notifier:
            self._notifier.send_error(notify_message)

    def _set_runtime_error(self, message: str) -> None:
        self._runtime.last_error = message
        self._runtime.last_error_at = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_loopback_host(host: str) -> bool:
    value = (host or "").strip()
    if value in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_server_binding(host: str, api_key: str | None) -> None:
    if _is_loopback_host(host):
        return
    if api_key:
        return
    raise ValueError("server.api_key is required when binding HTTP control server to a non-loopback host")


def _next_run_local(windows: list[str], timezone_name: str) -> str | None:
    now = datetime.now(ZoneInfo(timezone_name))
    mins_now = now.hour * 60 + now.minute
    ranges: list[tuple[int, int]] = []
    for w in windows:
        if "-" not in w:
            continue
        start_s, end_s = w.split("-", 1)
        try:
            sh, sm = [int(x) for x in start_s.split(":")]
            eh, em = [int(x) for x in end_s.split(":")]
        except ValueError:
            continue
        ranges.append((sh * 60 + sm, eh * 60 + em))

    normalized: list[tuple[int, int]] = []
    for start, end in ranges:
        if end >= start:
            normalized.append((start, end))
        else:
            normalized.append((start, 24 * 60 - 1))
            normalized.append((0, end))
    ranges = normalized

    for start, end in ranges:
        if start <= mins_now <= end:
            return now.isoformat(timespec="seconds")

    future_starts = [start for start, _ in ranges if start > mins_now]
    if future_starts:
        next_start = min(future_starts)
        next_dt = now.replace(hour=next_start // 60, minute=next_start % 60, second=0, microsecond=0)
        return next_dt.isoformat(timespec="seconds")

    if ranges:
        next_start = min(r[0] for r in ranges)
        next_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        next_dt = next_dt.replace(hour=next_start // 60, minute=next_start % 60)
        return next_dt.isoformat(timespec="seconds")
    return None


class ControlHandler(BaseHTTPRequestHandler):
    server: "ControlServer"  # type: ignore[override]

    def _auth_ok(self) -> bool:
        api_key = self.server.api_key or ""
        if not api_key:
            return True
        header_key = self.headers.get("X-API-Key", "")
        return header_key == api_key

    def _require_auth(self) -> bool:
        if self._auth_ok():
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not self._require_auth():
                return
            if self.path == "/watch/status":
                self._send(200, self.server.watch_controller.status())
                return
            self._send(404, {"error": "not found"})
        except TimeoutError:
            logging.getLogger(__name__).warning("http GET timeout")
            self._send(504, {"error": "timeout"})
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error("http GET error: %s", exc)
            self.server.watch_controller.report_error(str(exc), f"http GET error: {exc}")
            self._send(500, {"error": "internal"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._require_auth():
                return
            if self.path == "/watch/start":
                data = self._json()
                use_schedule = bool(data.get("use_schedule", True))
                ok = self.server.watch_controller.start(use_schedule=use_schedule)
                self._send(200, {"ok": ok})
                return
            if self.path == "/watch/config":
                data = self._json()
                max_posts_per_interval = _UNSET
                if "max_posts_per_interval" in data:
                    max_posts_per_interval = data.get("max_posts_per_interval")
                self._send(
                    200,
                    self.server.watch_controller.configure(
                        use_unseen=data.get("use_unseen") if "use_unseen" in data else None,
                        timings_per_topic=data.get("timings_per_topic") if "timings_per_topic" in data else None,
                        max_posts_per_interval=max_posts_per_interval,
                    ),
                )
                return
            if self.path == "/watch/stop":
                ok = self.server.watch_controller.stop()
                self._send(200, {"ok": ok})
                return
            if self.path == "/like":
                data = self._json()
                post_id = int(data.get("post_id", 0))
                emoji = data.get("emoji", "heart")
                if not post_id:
                    self._send(400, {"error": "post_id required"})
                    return
                result = self.server.client.toggle_reaction(post_id, emoji)
                self._send(200, {"ok": True, "result": result})
                return
            if self.path == "/reply":
                data = self._json()
                topic_id = int(data.get("topic_id", 0))
                raw = data.get("raw", "")
                category = data.get("category")
                if not topic_id or not raw:
                    self._send(400, {"error": "topic_id and raw required"})
                    return
                result = self.server.client.reply(topic_id, raw, category)
                self._send(200, {"ok": True, "result": result})
                return
            self._send(404, {"error": "not found"})
        except TimeoutError:
            logging.getLogger(__name__).warning("http POST timeout")
            self.server.watch_controller.report_error("timeout", "http POST timeout")
            self._send(504, {"error": "timeout"})
        except DiscourseAuthError as exc:
            logging.getLogger(__name__).error("http auth error: %s", exc)
            self.server.watch_controller.report_error(str(exc), f"http auth error: {exc}")
            self._send(401, {"error": "not_logged_in"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error("http POST error: %s", exc)
            self.server.watch_controller.report_error(str(exc), f"http POST error: {exc}")
            self._send(500, {"error": "internal"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logging.getLogger(__name__).info("http %s", format % args)


class ControlServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        client: QueuedDiscourseClient,
        watch_controller: WatchController,
        api_key: str | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.client = client
        self.watch_controller = watch_controller
        self.api_key = api_key or ""


def serve(
    host: str,
    port: int,
    client: QueuedDiscourseClient,
    watch_controller: WatchController,
    api_key: str | None = None,
) -> None:
    validate_server_binding(host, api_key)
    httpd = ControlServer((host, port), ControlHandler, client, watch_controller, api_key=api_key)
    logging.getLogger(__name__).info("server listening on %s:%s", host, port)
    httpd.serve_forever()
