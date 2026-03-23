"""Structured runtime settings derived from app config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoreSettings:
    backend: str
    path: str
    lock_dir: str
    timezone_name: str
    site_key: str
    account_name: str
    base_url: str


@dataclass(frozen=True)
class WatchSettings:
    crawl_enabled: bool
    use_unseen: bool
    timings_per_topic: int
    notify_interval_secs: int
    notify_auto_mark_read: bool


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    schedule: list[str]
    api_key: str
    action_timeout_secs: float
    interval_secs: int
    max_posts_per_interval: int | None
    auto_restart: bool
    restart_backoff_secs: int
    max_restarts: int
    same_error_stop_threshold: int


@dataclass(frozen=True)
class RuntimeSettings:
    timezone_name: str
    store: StoreSettings
    watch: WatchSettings
    server: ServerSettings


def build_runtime_settings(app_config: dict[str, Any], storage_path: str, site_key: str) -> RuntimeSettings:
    timezone_name = app_config.get("time", {}).get("timezone", "Asia/Shanghai")
    storage_cfg = app_config.get("storage", {})
    watch_cfg = app_config.get("watch", {})
    server_cfg = app_config.get("server", {})
    notify_cfg = app_config.get("notify", {})
    return RuntimeSettings(
        timezone_name=timezone_name,
        store=StoreSettings(
            backend=str(storage_cfg.get("backend", "sqlite") or "sqlite"),
            path=storage_path,
            lock_dir=str(storage_cfg.get("lock_dir", "data/locks") or "data/locks"),
            timezone_name=timezone_name,
            site_key=site_key,
            account_name=str(app_config.get("auth", {}).get("name", "main") or "main"),
            base_url=str(app_config.get("site", {}).get("base_url", "") or ""),
        ),
        watch=WatchSettings(
            crawl_enabled=_as_bool(app_config.get("crawl", {}).get("enabled", True), "crawl.enabled"),
            use_unseen=_as_bool(watch_cfg.get("use_unseen", False), "watch.use_unseen"),
            timings_per_topic=int(watch_cfg.get("timings_per_topic", 30)),
            notify_interval_secs=int(notify_cfg.get("interval_secs", 600)),
            notify_auto_mark_read=_as_bool(notify_cfg.get("auto_mark_read", False), "notify.auto_mark_read"),
        ),
        server=ServerSettings(
            host=server_cfg.get("host", "127.0.0.1"),
            port=int(server_cfg.get("port", 17880)),
            schedule=list(server_cfg.get("schedule", [])),
            api_key=server_cfg.get("api_key", ""),
            action_timeout_secs=float(server_cfg.get("action_timeout_secs", 60)),
            interval_secs=int(server_cfg.get("interval_secs", 30)),
            max_posts_per_interval=_as_optional_int(server_cfg.get("max_posts_per_interval"), "server.max_posts_per_interval"),
            auto_restart=_as_bool(server_cfg.get("auto_restart", True), "server.auto_restart"),
            restart_backoff_secs=int(server_cfg.get("restart_backoff_secs", 60)),
            max_restarts=int(server_cfg.get("max_restarts", 0)),
            same_error_stop_threshold=int(server_cfg.get("same_error_stop_threshold", 0)),
        ),
    )


def _as_bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{path} must be a boolean")


def _as_optional_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer or null")
    return value
