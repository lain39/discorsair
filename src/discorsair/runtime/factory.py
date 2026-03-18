"""Runtime dependency construction helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from discorsair.core.request_queue import RequestQueue
from discorsair.core.requester import Requester
from discorsair.core.session import SessionState
from discorsair.discourse.client import DiscourseClient
from discorsair.discourse.queued_client import QueuedDiscourseClient
from discorsair.storage.sqlite_store import SQLiteStore
from discorsair.utils.config import load_app_config, validate_app_config
from discorsair.utils.logging import setup_logging
from discorsair.utils.notify import Notifier
from .settings import RuntimeSettings
from .settings import build_runtime_settings


@dataclass
class RuntimeServices:
    store: SQLiteStore
    base_client: DiscourseClient
    client: QueuedDiscourseClient
    queue: RequestQueue

    def close(self) -> None:
        try:
            self.queue.stop()
        finally:
            self.store.close()


def load_runtime_app_config(config_path: str) -> dict[str, Any]:
    app_config = load_app_config(config_path)
    app_config["_path"] = config_path
    validate_app_config(app_config)
    log_path = app_config.get("logging", {}).get("path")
    debug_enabled = bool(app_config.get("debug", False))
    setup_logging(logging.DEBUG if debug_enabled else logging.INFO, log_path=log_path)
    return app_config


def load_settings(app_config: dict[str, Any]) -> RuntimeSettings:
    return build_runtime_settings(app_config, resolve_storage_path(app_config))


def build_services(app_config: dict[str, Any], settings: RuntimeSettings) -> RuntimeServices:
    store: SQLiteStore | None = None
    queue: RequestQueue | None = None
    try:
        store = open_store(settings)
        base_client = build_client(app_config)
        queue_cfg = app_config.get("queue", {})
        queue = RequestQueue(maxsize=int(queue_cfg.get("maxsize", 0)))
        client = QueuedDiscourseClient(base_client, queue, timeout_secs=float(queue_cfg.get("timeout_secs", 60)))
        return RuntimeServices(store=store, base_client=base_client, client=client, queue=queue)
    except Exception:
        if queue is not None:
            queue.stop()
        if store is not None:
            store.close()
        raise


def open_store(settings: RuntimeSettings) -> SQLiteStore:
    return SQLiteStore(
        settings.store.path,
        timezone_name=settings.store.timezone_name,
        rotate_daily=settings.store.rotate_daily,
    )


def build_client(app_config: dict[str, Any]) -> DiscourseClient:
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


def build_notifier(app_config: dict[str, Any]) -> Notifier | None:
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


def resolve_storage_path(app_config: dict[str, Any]) -> str:
    storage_cfg = app_config.get("storage", {})
    path = storage_cfg.get("path", "data/discorsair.db")
    if not storage_cfg.get("auto_per_site", False):
        return path

    base_url = app_config.get("site", {}).get("base_url", "")
    safe = base_url.replace("https://", "").replace("http://", "")
    safe = safe.replace("/", "_").replace(":", "_")
    if not safe:
        return path
    if path.endswith(".db"):
        return path.replace(".db", f".{safe}.db")
    return f"{path}.{safe}"
