"""HTTP control server for Discorsair."""

from __future__ import annotations

import json
import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from concurrent.futures import TimeoutError

from discorsair.core.requester import ChallengeUnresolvedError
from discorsair.discourse.client import DiscourseAuthError
from discorsair.discourse.queued_client import QueuedDiscourseClient
from discorsair.flows.watch import watch
from discorsair.plugins import PluginManager
from discorsair.storage import StoreBackend
from discorsair.flows.status import status as status_flow
from discorsair.utils.notify import Notifier

_UNSET = object()
_SCHEDULE_WINDOW_RE = re.compile(r"^(?P<start_h>\d{2}):(?P<start_m>\d{2})-(?P<end_h>\d{2}):(?P<end_m>\d{2})$")


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
        store: StoreBackend | None,
        notifier: Notifier | None,
        interval_secs: int,
        max_posts_per_interval: int | None,
        crawl_enabled: bool,
        use_unseen: bool,
        timings_per_topic: int,
        schedule_windows: list[str],
        notify_interval_secs: int,
        notify_auto_mark_read: bool,
        plugin_manager: PluginManager | None,
        auto_restart: bool,
        restart_backoff_secs: int,
        max_restarts: int,
        same_error_stop_threshold: int,
        timezone_name: str,
        on_stop: Callable[[], None] | None = None,
        on_auth_invalid: Callable[[Exception], None] | None = None,
        on_fatal: Callable[[], None] | None = None,
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
        self._notify_auto_mark_read = notify_auto_mark_read
        self._plugin_manager = plugin_manager
        self._auto_restart = auto_restart
        self._restart_backoff_secs = restart_backoff_secs
        self._max_restarts = max_restarts
        self._same_error_stop_threshold = max(0, int(same_error_stop_threshold))
        self._timezone_name = timezone_name
        self._on_stop = on_stop
        self._on_auth_invalid = on_auth_invalid
        self._on_fatal = on_fatal
        self._runtime = WatchRuntime()
        self._control_lock = threading.RLock()
        self._use_schedule = True
        self._last_error_sig: str | None = None
        self._same_error_count = 0
        self._fatal_error: Exception | None = None
        self._sent_notification_ids_mem: set[int] = set()

    def start(self, use_schedule: bool = True) -> bool:
        with self._control_lock:
            if self._runtime.thread and self._runtime.thread.is_alive():
                return False
            stop_event = threading.Event()
            self._runtime.stop_event = stop_event
            self._runtime.started_at = _now()
            self._runtime.last_tick = _now()
            self._use_schedule = use_schedule
            self._last_error_sig = None
            self._same_error_count = 0
            self._fatal_error = None

            def _run() -> None:
                restarts = 0
                while True:
                    if stop_event.is_set():
                        return
                    try:
                        self._run_watch_once(stop_event, use_schedule=use_schedule)
                        return
                    except Exception as exc:  # noqa: BLE001
                        next_restarts = self._handle_watch_exception(exc, restarts, stop_event)
                        if next_restarts is None:
                            return
                        restarts = next_restarts

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
        with self._control_lock:
            next_use_unseen = self._use_unseen
            next_timings_per_topic = self._timings_per_topic
            next_max_posts_per_interval = self._max_posts_per_interval
            if use_unseen is not None:
                if not isinstance(use_unseen, bool):
                    raise ValueError("use_unseen must be a boolean")
                next_use_unseen = use_unseen
            if timings_per_topic is not None:
                try:
                    parsed_timings = int(timings_per_topic)
                except (TypeError, ValueError) as exc:
                    raise ValueError("timings_per_topic must be an integer") from exc
                if parsed_timings < 1:
                    raise ValueError("timings_per_topic must be >= 1")
                next_timings_per_topic = parsed_timings
            if max_posts_per_interval is not _UNSET:
                if max_posts_per_interval is None:
                    next_max_posts_per_interval = None
                else:
                    try:
                        parsed_max_posts = int(max_posts_per_interval)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("max_posts_per_interval must be an integer or null") from exc
                    if parsed_max_posts < 0:
                        raise ValueError("max_posts_per_interval must be >= 0 or null")
                    next_max_posts_per_interval = parsed_max_posts
            changed = (
                next_use_unseen != self._use_unseen
                or next_timings_per_topic != self._timings_per_topic
                or next_max_posts_per_interval != self._max_posts_per_interval
            )
            if not changed:
                return {
                    "ok": True,
                    "use_unseen": self._use_unseen,
                    "timings_per_topic": self._timings_per_topic,
                    "max_posts_per_interval": self._max_posts_per_interval,
                }
            running = bool(self._runtime.thread and self._runtime.thread.is_alive())
            thread = self._runtime.thread
            stop_event = self._runtime.stop_event
            use_schedule = self._use_schedule
            restart_wait_secs = self._stop_wait_timeout_secs()
            if running and thread is not None and stop_event is not None:
                stop_event.set()
                thread.join(timeout=restart_wait_secs)
                if thread.is_alive():
                    raise ValueError("watch reconfigure timed out waiting for current loop to stop")
            self._use_unseen = next_use_unseen
            self._timings_per_topic = next_timings_per_topic
            self._max_posts_per_interval = next_max_posts_per_interval
            if running and thread is not None and stop_event is not None:
                if not self.start(use_schedule=use_schedule):
                    raise ValueError("watch reconfigure failed to restart")
        return {
            "ok": True,
            "use_unseen": self._use_unseen,
            "timings_per_topic": self._timings_per_topic,
            "max_posts_per_interval": self._max_posts_per_interval,
        }

    def stop(self) -> bool:
        return bool(self.stop_result()["ok"])

    def stop_result(self) -> dict[str, bool]:
        with self._control_lock:
            thread = self._runtime.thread
            if thread is None:
                return {"ok": False, "already_stopped": False}
            if not thread.is_alive():
                return {"ok": True, "already_stopped": True}
            if not self._runtime.stop_event:
                return {"ok": False, "already_stopped": False}
            logging.getLogger(__name__).info("watch stop requested")
            self._finalize_stop()
            return {"ok": True, "already_stopped": False}

    def status(self) -> dict[str, Any]:
        running = bool(self._runtime.thread and self._runtime.thread.is_alive())
        stop_requested = bool(self._runtime.stop_event and self._runtime.stop_event.is_set())
        next_run = (
            _next_run_local(self._schedule_windows, self._timezone_name)
            if self._use_schedule and self._schedule_windows
            else None
        )
        return {
            "running": running,
            "started_at": self._runtime.started_at,
            "last_tick": self._runtime.last_tick,
            "last_error": self._runtime.last_error,
            "last_error_at": self._runtime.last_error_at,
            "stop_requested": stop_requested,
            "stopping": bool(running and stop_requested),
            "next_run": next_run,
            **status_flow(
                self._store,
                plugins=self._plugin_manager.snapshot() if self._plugin_manager is not None else None,
            ),
            "schedule": self._schedule_windows,
            "use_schedule": self._use_schedule,
            "use_unseen": self._use_unseen,
            "timings_per_topic": self._timings_per_topic,
            "max_posts_per_interval": self._max_posts_per_interval,
            "timezone": self._timezone_name,
        }

    def _tick(self) -> None:
        self._runtime.last_tick = _now()
        self._same_error_count = 0
        self._last_error_sig = None

    def _run_watch_once(self, stop_event: threading.Event, *, use_schedule: bool) -> None:
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
            notify_auto_mark_read=self._notify_auto_mark_read,
            plugin_manager=self._plugin_manager,
            on_success=self._tick,
            stop_event=stop_event,
            schedule_windows=self._schedule_windows if use_schedule else [],
            timezone_name=self._timezone_name,
            sent_notification_ids_mem=self._sent_notification_ids_mem,
        )

    def _handle_watch_exception(self, exc: Exception, restarts: int, stop_event: threading.Event) -> int | None:
        if isinstance(exc, DiscourseAuthError):
            self.handle_auth_invalid(exc, source="watch stopped")
            return None
        if isinstance(exc, ChallengeUnresolvedError):
            self.handle_unresolved_challenge(exc, source="watch stopped")
            return None

        logging.getLogger(__name__).error("watch thread crashed: %s", exc)
        self.report_error(str(exc), f"watch crashed: {exc}")
        same_error_count = self._record_watch_error_signature(exc)
        if self._same_error_stop_threshold > 0 and same_error_count >= self._same_error_stop_threshold:
            logging.getLogger(__name__).error(
                "watch auto-stopped after %s consecutive same errors",
                same_error_count,
            )
            self._stop_with_type(
                "same_error_threshold",
                detail=str(exc),
                extra={"same_error_count": str(same_error_count)},
            )
            return None
        if not self._auto_restart:
            self._stop_with_type("auto_restart_disabled", detail=str(exc))
            return None

        next_restarts = restarts + 1
        if self._max_restarts > 0 and next_restarts > self._max_restarts:
            self._stop_with_type(
                "max_restarts_exceeded",
                detail=str(exc),
                extra={"max_restarts": str(self._max_restarts)},
            )
            return None

        if stop_event.wait(max(float(self._restart_backoff_secs), 0.0)):
            return None
        return next_restarts

    def _record_watch_error_signature(self, exc: Exception) -> int:
        sig = f"{exc.__class__.__name__}:{exc}"
        if sig == self._last_error_sig:
            self._same_error_count += 1
        else:
            self._same_error_count = 1
            self._last_error_sig = sig
        return self._same_error_count

    def report_error(self, message: str, notify_message: str | None = None) -> None:
        self._set_runtime_error(message)
        if notify_message and self._notifier:
            self._notifier.send_error(notify_message)

    def set_on_fatal(self, callback: Callable[[], None] | None) -> None:
        self._on_fatal = callback

    def fatal_error(self) -> Exception | None:
        return self._fatal_error

    def _set_runtime_error(self, message: str) -> None:
        self._runtime.last_error = message
        self._runtime.last_error_at = _now()

    def _watch_request_timeout_secs(self) -> float:
        inner = getattr(self._client, "_inner", self._client)
        requester = getattr(inner, "_requester", None)
        timeout = getattr(requester, "_timeout_secs", None)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            return float(timeout)
        return 30.0

    def _stop_wait_timeout_secs(self) -> float:
        return max(
            float(self._interval_secs),
            float(self._restart_backoff_secs),
            self._watch_request_timeout_secs(),
            1.0,
        ) + 1.0

    def _notify_stop(self, stop_type: str, *, detail: str, extra: dict[str, str] | None = None) -> None:
        if not self._notifier:
            return
        parts = [f"watch stopped: stop_type={stop_type}"]
        if extra:
            for key, value in extra.items():
                if value:
                    parts.append(f"{key}={value}")
        if detail:
            parts.append(f"detail={detail}")
        self._notifier.send_error(" ".join(parts))

    def _stop_with_type(
        self,
        stop_type: str,
        *,
        detail: str,
        extra: dict[str, str] | None = None,
        notify_fatal: bool = False,
    ) -> None:
        self._notify_stop(stop_type, detail=detail, extra=extra)
        self._finalize_stop()
        if notify_fatal and self._on_fatal:
            self._on_fatal()

    def _handle_fatal_stop(self, stop_type: str, exc: Exception, *, source: str, notify_message: str) -> None:
        self._fatal_error = exc
        self.report_error(str(exc), notify_message)
        self._stop_with_type(stop_type, detail=str(exc), extra={"source": source}, notify_fatal=True)

    def handle_auth_invalid(self, exc: Exception, *, source: str) -> None:
        logging.getLogger(__name__).error("%s due to auth error: %s", source, exc)
        if self._on_auth_invalid:
            self._on_auth_invalid(exc)
        self._handle_fatal_stop("auth_invalid", exc, source=source, notify_message=f"{source}: auth error: {exc}")

    def handle_unresolved_challenge(self, exc: Exception, *, source: str) -> None:
        logging.getLogger(__name__).error("%s due to unresolved challenge: %s", source, exc)
        self._handle_fatal_stop(
            "unresolved_challenge",
            exc,
            source=source,
            notify_message=f"{source}: unresolved challenge: {exc}",
        )

    def _finalize_stop(self) -> None:
        if self._runtime.stop_event:
            self._runtime.stop_event.set()
        if self._on_stop:
            self._on_stop()


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
        parsed = _parse_schedule_window(w)
        if parsed is None:
            continue
        sh, sm, eh, em = parsed
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


def _parse_schedule_window(window: str) -> tuple[int, int, int, int] | None:
    match = _SCHEDULE_WINDOW_RE.fullmatch(str(window or "").strip())
    if match is None:
        return None
    sh = int(match.group("start_h"))
    sm = int(match.group("start_m"))
    eh = int(match.group("end_h"))
    em = int(match.group("end_m"))
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return sh, sm, eh, em


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
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

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
                use_schedule = _json_bool(data, "use_schedule", default=True)
                ok = self.server.watch_controller.start(use_schedule=use_schedule)
                self._send(200, {"ok": ok})
                return
            if self.path == "/watch/config":
                data = self._json()
                max_posts_per_interval = _UNSET
                if "max_posts_per_interval" in data:
                    max_posts_per_interval = _json_optional_int(data, "max_posts_per_interval")
                self._send(
                    200,
                    self.server.watch_controller.configure(
                        use_unseen=_json_bool(data, "use_unseen") if "use_unseen" in data else None,
                        timings_per_topic=_json_int(data, "timings_per_topic") if "timings_per_topic" in data else None,
                        max_posts_per_interval=max_posts_per_interval,
                    ),
                )
                return
            if self.path == "/watch/stop":
                self._send(200, self.server.watch_controller.stop_result())
                return
            if self.path == "/like":
                data = self._json()
                post_id = _json_int(data, "post_id", default=0)
                emoji = data.get("emoji", "heart")
                if not post_id:
                    self._send(400, {"error": "post_id required"})
                    return
                result = self.server.client.toggle_reaction(
                    post_id,
                    emoji,
                    timeout_secs=self.server.action_timeout_secs,
                )
                if self.server.on_action_success:
                    self.server.on_action_success()
                self._send(200, {"ok": True, "result": result})
                return
            if self.path == "/reply":
                data = self._json()
                topic_id = _json_int(data, "topic_id", default=0)
                raw = _json_string(data, "raw", default="")
                category = _json_optional_int(data, "category") if "category" in data else None
                if not topic_id or not raw:
                    self._send(400, {"error": "topic_id and raw required"})
                    return
                result = self.server.client.reply(
                    topic_id,
                    raw,
                    category,
                    timeout_secs=self.server.action_timeout_secs,
                )
                if self.server.on_action_success:
                    self.server.on_action_success()
                self._send(200, {"ok": True, "result": result})
                return
            self._send(404, {"error": "not found"})
        except ValueError as exc:
            logging.getLogger(__name__).warning("http POST bad request: %s", exc)
            self._send(400, {"error": "bad_request", "detail": str(exc)})
        except TimeoutError:
            logging.getLogger(__name__).warning("http POST timeout")
            self.server.watch_controller.report_error("timeout", "http POST timeout")
            self._send(504, {"error": "timeout"})
        except DiscourseAuthError as exc:
            self.server.watch_controller.handle_auth_invalid(exc, source="http auth error")
            self._send(401, {"error": "not_logged_in"})
            self.server.request_shutdown()
        except ChallengeUnresolvedError as exc:
            self.server.watch_controller.handle_unresolved_challenge(exc, source="http unresolved challenge")
            self._send(503, {"error": "challenge_unresolved"})
            self.server.request_shutdown()
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
        action_timeout_secs: float = 60.0,
        on_action_success: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.client = client
        self.watch_controller = watch_controller
        self.api_key = api_key or ""
        self.action_timeout_secs = max(float(action_timeout_secs), 0.0)
        self.on_action_success = on_action_success
        self._shutdown_requested = False
        self._shutdown_lock = threading.Lock()

    def request_shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True
        threading.Thread(target=self.shutdown, daemon=True).start()


def serve(
    host: str,
    port: int,
    client: QueuedDiscourseClient,
    watch_controller: WatchController,
    api_key: str | None = None,
    action_timeout_secs: float = 60.0,
    on_action_success: Callable[[], None] | None = None,
) -> None:
    validate_server_binding(host, api_key)
    httpd = ControlServer(
        (host, port),
        ControlHandler,
        client,
        watch_controller,
        api_key=api_key,
        action_timeout_secs=action_timeout_secs,
        on_action_success=on_action_success,
    )
    watch_controller.set_on_fatal(httpd.request_shutdown)
    logging.getLogger(__name__).info("server listening on %s:%s", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _json_bool(data: dict[str, Any], key: str, *, default: bool | None = None) -> bool:
    if key not in data:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _json_int(data: dict[str, Any], key: str, *, default: int | None = None) -> int:
    if key not in data:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _json_optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer or null")
    return value


def _json_string(data: dict[str, Any], key: str, *, default: str | None = None) -> str:
    if key not in data:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value
