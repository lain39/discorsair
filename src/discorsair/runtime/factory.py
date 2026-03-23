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
from discorsair.plugins import PluginManager
from discorsair.runtime.crawl_lock import CrawlSiteLock, acquire_site_crawl_lock
from discorsair.storage import PostgresStore
from discorsair.storage import StoreBackend
from discorsair.storage.sqlite_store import SQLiteStore
from discorsair.utils.config import (
    active_env_override_paths,
    derive_runtime_state_path,
    load_raw_app_config,
    load_raw_runtime_state,
    merge_app_config_and_runtime_state,
    validate_app_config,
)
from discorsair.utils.logging import setup_logging
from discorsair.utils.notify import Notifier
from .settings import RuntimeSettings
from .settings import build_runtime_settings


@dataclass
class RuntimeServices:
    store: StoreBackend | None
    base_client: DiscourseClient
    client: QueuedDiscourseClient
    queue: RequestQueue
    plugin_manager: PluginManager | None
    crawl_lock: CrawlSiteLock | None = None

    def close(self) -> None:
        try:
            self.queue.stop()
        finally:
            try:
                if self.store is not None:
                    self.store.close()
            finally:
                if self.crawl_lock is not None:
                    self.crawl_lock.release()


def load_runtime_app_config(config_path: str, *, require_auth_cookie: bool = True) -> dict[str, Any]:
    app_data = load_raw_app_config(config_path)
    state_path = derive_runtime_state_path(config_path)
    try:
        state_config = load_raw_runtime_state(state_path)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("failed to load runtime state %s: %s", state_path, exc)
        state_config = {}
    app_config = merge_app_config_and_runtime_state(app_data, state_config)
    app_config["_config_path"] = config_path
    app_config["_state_path"] = str(state_path)
    app_config["_path"] = config_path
    app_config["_env_override_paths"] = sorted(active_env_override_paths())
    validate_app_config(app_config, require_auth_cookie=require_auth_cookie)
    log_path = app_config.get("logging", {}).get("path")
    debug_enabled = bool(app_config.get("debug", False))
    setup_logging(logging.DEBUG if debug_enabled else logging.INFO, log_path=log_path)
    return app_config


def load_settings(app_config: dict[str, Any]) -> RuntimeSettings:
    site_key = derive_site_key(str(app_config.get("site", {}).get("base_url", "") or ""))
    return build_runtime_settings(app_config, resolve_storage_path(app_config), site_key)


def build_services(
    app_config: dict[str, Any],
    settings: RuntimeSettings,
    *,
    with_crawl_resources: bool = True,
    with_plugins: bool = True,
) -> RuntimeServices:
    store: StoreBackend | None = None
    queue: RequestQueue | None = None
    crawl_lock: CrawlSiteLock | None = None
    try:
        if with_crawl_resources and settings.watch.crawl_enabled:
            crawl_lock = acquire_site_crawl_lock(
                settings.store.lock_dir,
                site_key=settings.store.site_key,
                account_name=settings.store.account_name,
                config_path=str(app_config.get("_path", "") or ""),
            )
            store = open_store(settings)
        base_client = build_client(app_config)
        queue_cfg = app_config.get("queue", {})
        queue = RequestQueue(maxsize=int(queue_cfg.get("maxsize", 0)))
        client = QueuedDiscourseClient(base_client, queue)
        plugin_manager = None
        if with_plugins:
            plugin_manager = PluginManager.from_app_config(
                app_config,
                client=client,
                store=store,
                timezone_name=settings.timezone_name,
            )
        return RuntimeServices(
            store=store,
            base_client=base_client,
            client=client,
            queue=queue,
            plugin_manager=plugin_manager,
            crawl_lock=crawl_lock,
        )
    except Exception:
        if queue is not None:
            queue.stop()
        if store is not None:
            store.close()
        if crawl_lock is not None:
            crawl_lock.release()
        raise


def open_store(
    settings: RuntimeSettings,
    *,
    initialize: bool = True,
    ensure_metadata: bool = True,
    read_only: bool = False,
) -> StoreBackend:
    if settings.store.backend == "postgres":
        return PostgresStore(
            settings.store.path,
            site_key=settings.store.site_key,
            account_name=settings.store.account_name,
            base_url=settings.store.base_url,
            timezone_name=settings.store.timezone_name,
            initialize=initialize,
            ensure_metadata=ensure_metadata,
            read_only=read_only,
        )
    return SQLiteStore(
        settings.store.path,
        site_key=settings.store.site_key,
        account_name=settings.store.account_name,
        base_url=settings.store.base_url,
        timezone_name=settings.store.timezone_name,
        initialize=initialize,
        ensure_metadata=ensure_metadata,
        read_only=read_only,
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
        impersonate_target=req_cfg.get("impersonate_target") or "",
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
        max_retries=int(req_cfg.get("max_retries", 1)),
        timeout_secs=float(site.get("timeout_secs", 30)),
        flaresolverr_use_base_url_for_csrf=bool(flaresolverr.get("use_base_url_for_csrf", False)),
        flaresolverr_in_docker=bool(flaresolverr.get("in_docker", True)),
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
    backend = str(storage_cfg.get("backend", "sqlite") or "sqlite")
    if backend == "postgres":
        pg_cfg = storage_cfg.get("postgres", {})
        if not isinstance(pg_cfg, dict):
            return ""
        return str(pg_cfg.get("dsn", "") or "")
    path = storage_cfg.get("path", "data/discorsair.db")
    if not storage_cfg.get("auto_per_site", False):
        return path

    safe = derive_site_key(str(app_config.get("site", {}).get("base_url", "") or ""))
    if not safe:
        return path
    if path.endswith(".db"):
        return path.replace(".db", f".{safe}.db")
    return f"{path}.{safe}"


def derive_site_key(base_url: str) -> str:
    safe = str(base_url or "").replace("https://", "").replace("http://", "")
    safe = safe.replace("/", "_").replace(":", "_")
    return safe.strip("_")
