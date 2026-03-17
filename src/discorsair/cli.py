"""Discorsair CLI entry."""

from __future__ import annotations

import argparse
import logging
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from discorsair.core.requester import Requester
from discorsair.core.session import SessionState
from discorsair.discourse.client import DiscourseAuthError, DiscourseClient
from discorsair.discourse.queued_client import QueuedDiscourseClient
from discorsair.core.request_queue import RequestQueue
from discorsair.flows.daily import daily
from discorsair.flows.like import like
from discorsair.flows.reply import reply
from discorsair.flows.watch import watch
from discorsair.flows.status import status as status_flow
from discorsair.storage.sqlite_store import SQLiteStore
from discorsair.utils.notify import Notifier
from curl_cffi.requests.exceptions import RequestException
from discorsair.server.http_server import WatchController, serve
from discorsair.utils.config import load_app_config, validate_app_config
from discorsair.utils.logging import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discorsair")
    parser.add_argument(
        "--config",
        default="config/app.json",
        help="Path to app config (default: config/app.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run watch loop")
    run_p.add_argument("--interval", type=int, default=30)
    run_p.add_argument("--once", action="store_true")
    run_p.add_argument("--max-posts-per-interval", type=int, default=200)

    watch_p = sub.add_parser("watch", help="Watch latest topics")
    watch_p.add_argument("--interval", type=int, default=30)
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--max-posts-per-interval", type=int, default=200)

    daily_p = sub.add_parser("daily", help="Daily activity")
    daily_p.add_argument("--topic", type=int, default=None)

    like_p = sub.add_parser("like", help="Toggle reaction")
    like_p.add_argument("--post", type=int, required=True)
    like_p.add_argument("--emoji", default="heart")

    reply_p = sub.add_parser("reply", help="Reply to topic")
    reply_p.add_argument("--topic", type=int, required=True)
    reply_p.add_argument("--raw", required=True)
    reply_p.add_argument("--category", type=int, default=None)

    status_p = sub.add_parser("status", help="Show status")

    notify_p = sub.add_parser("notify", help="Notify helpers")
    notify_sub = notify_p.add_subparsers(dest="notify_cmd", required=True)
    notify_sub.add_parser("test", help="Send test notification")

    init_p = sub.add_parser("init", help="Write config template to path")
    init_p.add_argument("--path", default="config/app.json", help="Output path for template")

    serve_p = sub.add_parser("serve", help="Run HTTP control server")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    return parser


def _build_client(app_config: dict[str, Any]) -> DiscourseClient:
    site = app_config.get("site", {})
    req_cfg = app_config.get("request", {})
    flaresolverr = app_config.get("flaresolverr", {})
    auth = app_config.get("auth", {})
    if auth.get("disabled") is True:
        raise ValueError("account is disabled")

    session = SessionState(
        base_url=site.get("base_url", "").rstrip("/"),
        cookie_header=auth.get("cookie", ""),
        impersonate_target=req_cfg.get("impersonate_target", "chrome110"),
        user_agent=req_cfg.get("user_agent", ""),
        proxy=auth.get("proxy") or None,
    )
    requester = Requester(
        session=session,
        flaresolverr_base_url=flaresolverr.get("base_url") if flaresolverr.get("enabled", True) else None,
        flaresolverr_timeout_secs=int(flaresolverr.get("request_timeout_secs", 60)),
        ua_probe_url=flaresolverr.get("ua_probe_url"),
        debug=bool(app_config.get("debug", False)),
        min_interval_secs=float(req_cfg.get("min_interval_secs", 0)),
        max_retries=int(req_cfg.get("max_retries", 2)),
        timeout_secs=float(site.get("timeout_secs", 30)),
    )
    return DiscourseClient(requester)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        setup_logging(logging.INFO)
        _write_template(args.path)
        return 0

    app_config = load_app_config(args.config)
    app_config["_path"] = args.config
    validate_app_config(app_config)
    log_path = app_config.get("logging", {}).get("path")
    debug_enabled = bool(app_config.get("debug", False))
    setup_logging(logging.DEBUG if debug_enabled else logging.INFO, log_path=log_path)
    auth_name = app_config.get("auth", {}).get("name", "main")
    notifier = _build_notifier(app_config)

    base_client: DiscourseClient | None = None
    queue: RequestQueue | None = None
    store: SQLiteStore | None = None
    try:
        if args.command == "status":
            store_path = _resolve_storage_path(app_config)
            tz_name = app_config.get("time", {}).get("timezone", "Asia/Shanghai")
            rotate_daily = bool(app_config.get("storage", {}).get("rotate_daily", False))
            store = SQLiteStore(store_path, timezone_name=tz_name, rotate_daily=rotate_daily)
            data = status_flow(store)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.command == "notify":
            if args.notify_cmd == "test":
                if notifier:
                    notifier.send("notification test")
                else:
                    print("notify not configured")
                return 0
        store_path = _resolve_storage_path(app_config)
        tz_name = app_config.get("time", {}).get("timezone", "Asia/Shanghai")
        rotate_daily = bool(app_config.get("storage", {}).get("rotate_daily", False))
        store = SQLiteStore(store_path, timezone_name=tz_name, rotate_daily=rotate_daily)
        base_client = _build_client(app_config)
        queue_cfg = app_config.get("queue", {})
        queue = RequestQueue(maxsize=int(queue_cfg.get("maxsize", 0)))
        client = QueuedDiscourseClient(base_client, queue, timeout_secs=float(queue_cfg.get("timeout_secs", 60)))
        try:
            crawl_enabled = bool(app_config.get("crawl", {}).get("enabled", True))
            watch_cfg = app_config.get("watch", {})
            use_unseen = bool(watch_cfg.get("use_unseen", False))
            timings_per_topic = int(watch_cfg.get("timings_per_topic", 30))
            tz_name = app_config.get("time", {}).get("timezone", "Asia/Shanghai")
            if args.command == "run":
                try:
                    watch(
                        client,
                        store,
                        interval_secs=args.interval,
                        once=args.once,
                        max_posts_per_interval=args.max_posts_per_interval,
                        crawl_enabled=crawl_enabled,
                        use_unseen=use_unseen,
                        timings_per_topic=timings_per_topic,
                        timezone_name=tz_name,
                        notifier=notifier,
                        notify_interval_secs=int(app_config.get("notify", {}).get("interval_secs", 600)),
                        on_success=lambda: _mark_account_ok(app_config),
                    )
                finally:
                    _save_cookies(app_config, base_client)
                return 0
            if args.command == "watch":
                try:
                    watch(
                        client,
                        store,
                        interval_secs=args.interval,
                        once=args.once,
                        max_posts_per_interval=args.max_posts_per_interval,
                        crawl_enabled=crawl_enabled,
                        use_unseen=use_unseen,
                        timings_per_topic=timings_per_topic,
                        timezone_name=tz_name,
                        notifier=notifier,
                        notify_interval_secs=int(app_config.get("notify", {}).get("interval_secs", 600)),
                        on_success=lambda: _mark_account_ok(app_config),
                    )
                finally:
                    _save_cookies(app_config, base_client)
                return 0
            if args.command == "daily":
                topic_id = args.topic if args.topic else None
                daily(client, topic_id=topic_id)
                _mark_account_ok(app_config)
                _save_cookies(app_config, base_client)
                return 0
            if args.command == "like":
                like(client, post_id=args.post, emoji=args.emoji)
                _mark_account_ok(app_config)
                _save_cookies(app_config, base_client)
                return 0
            if args.command == "reply":
                reply(client, topic_id=args.topic, raw=args.raw, category=args.category)
                _mark_account_ok(app_config)
                _save_cookies(app_config, base_client)
                return 0
            if args.command == "serve":
                server_cfg = app_config.get("server", {})
                host = args.host or server_cfg.get("host", "0.0.0.0")
                port = int(args.port or server_cfg.get("port", 8080))
                schedule = list(server_cfg.get("schedule", []))
                logging.getLogger(__name__).info("serve: host=%s port=%s schedule=%s", host, port, schedule)
                controller = WatchController(
                    client=client,
                    store=store,
                    notifier=notifier,
                    interval_secs=server_cfg.get("interval_secs", 30),
                    max_posts_per_interval=server_cfg.get("max_posts_per_interval"),
                    crawl_enabled=crawl_enabled,
                    use_unseen=use_unseen,
                    timings_per_topic=timings_per_topic,
                    timezone_name=tz_name,
                    schedule_windows=schedule,
                    notify_interval_secs=int(app_config.get("notify", {}).get("interval_secs", 600)),
                    auto_restart=bool(server_cfg.get("auto_restart", True)),
                    restart_backoff_secs=int(server_cfg.get("restart_backoff_secs", 60)),
                    max_restarts=int(server_cfg.get("max_restarts", 0)),
                    same_error_stop_threshold=int(server_cfg.get("same_error_stop_threshold", 0)),
                    on_stop=lambda: _save_cookies(app_config, base_client),
                )
                api_key = server_cfg.get("api_key", "")
                serve(host=host, port=port, client=client, watch_controller=controller, api_key=api_key)
                return 0
        finally:
            if queue is not None:
                queue.stop()
    except DiscourseAuthError as exc:
        if notifier:
            notifier.send_error(f"runtime error: login invalid: {exc}")
        _mark_account_fail(app_config, exc, mark_invalid=True, disable=True)
        raise
    except KeyboardInterrupt:
        _mark_account_fail(app_config, RuntimeError("interrupted"), mark_invalid=False, disable=False)
        if base_client is not None:
            _save_cookies(app_config, base_client)
        raise
    except Exception as exc: 
        if isinstance(exc, RequestException) and "curl: (23)" in str(exc):
            return 0
        if notifier:
            notifier.send_error(f"runtime error: {exc}")
        _mark_account_fail(app_config, exc, mark_invalid=False, disable=False)
        if base_client is not None:
            _save_cookies(app_config, base_client)
        raise
    finally:
        if store is not None:
            store.close()

    parser.print_help()
    return 1


def _write_template(path: str) -> None:
    template = Path("config/app.json.template")
    if not template.exists():
        raise FileNotFoundError("config/app.json.template not found")
    out_path = Path(path)
    if out_path.parent:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")


def _mark_account_ok(app_config: dict[str, Any]) -> None:
    auth = app_config.get("auth", {})
    auth["last_ok"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_app_config(app_config)


def _mark_account_fail(
    app_config: dict[str, Any],
    exc: Exception,
    mark_invalid: bool,
    disable: bool,
) -> None:
    auth = app_config.get("auth", {})
    auth["last_fail"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    auth["last_error"] = str(exc)
    if mark_invalid:
        auth["status"] = "invalid"
    if disable:
        auth["disabled"] = True
    _save_app_config(app_config)


def _save_cookies(app_config: dict[str, Any], client: DiscourseClient) -> None:
    if client.last_response_ok() is False:
        return
    auth = app_config.get("auth", {})
    auth["cookie"] = client.get_cookie_header()
    _save_app_config(app_config)


def _save_app_config(app_config: dict[str, Any]) -> None:
    path = app_config.get("_path")
    if not path:
        return
    try:
        payload = {k: v for k, v in app_config.items() if k != "_path"}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("failed to save config: %s", exc)


def _build_notifier(app_config: dict[str, Any]) -> Notifier | None:
    notify_cfg = app_config.get("notify", {})
    if not notify_cfg or not notify_cfg.get("enabled"):
        return None
    url = notify_cfg.get("url", "")
    chat_id = notify_cfg.get("chat_id", "")
    if not url or not chat_id:
        return None
    headers = notify_cfg.get("headers") or {"Content-Type": "application/json"}
    timeout_secs = int(notify_cfg.get("timeout_secs", 15))
    auth_name = app_config.get("auth", {}).get("name", "main")
    base_prefix = notify_cfg.get("prefix", "[Discorsair]")
    error_prefix = notify_cfg.get("error_prefix", "[Discorsair][error]")
    prefix = f"{base_prefix}[{auth_name}]"
    error_prefix = f"{error_prefix}[{auth_name}]"
    return Notifier(
        url=url,
        chat_id=chat_id,
        headers=headers,
        timeout_secs=timeout_secs,
        prefix=prefix,
        error_prefix=error_prefix,
    )


def _resolve_storage_path(app_config: dict[str, Any]) -> str:
    storage_cfg = app_config.get("storage", {})
    path = storage_cfg.get("path", "data/discorsair.db")
    if not storage_cfg.get("auto_per_site", False):
        base_path = path
    else:
        base_url = app_config.get("site", {}).get("base_url", "")
        safe = base_url.replace("https://", "").replace("http://", "")
        safe = safe.replace("/", "_").replace(":", "_")
        if safe:
            if path.endswith(".db"):
                base_path = path.replace(".db", f".{safe}.db")
            else:
                base_path = f"{path}.{safe}"
        else:
            base_path = path
    return base_path


if __name__ == "__main__":
    raise SystemExit(main())
