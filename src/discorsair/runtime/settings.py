"""Structured runtime settings derived from app config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoreSettings:
    path: str
    timezone_name: str
    rotate_daily: bool


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


def build_runtime_settings(app_config: dict[str, Any], storage_path: str) -> RuntimeSettings:
    timezone_name = app_config.get("time", {}).get("timezone", "Asia/Shanghai")
    storage_cfg = app_config.get("storage", {})
    watch_cfg = app_config.get("watch", {})
    server_cfg = app_config.get("server", {})
    notify_cfg = app_config.get("notify", {})
    return RuntimeSettings(
        timezone_name=timezone_name,
        store=StoreSettings(
            path=storage_path,
            timezone_name=timezone_name,
            rotate_daily=bool(storage_cfg.get("rotate_daily", False)),
        ),
        watch=WatchSettings(
            crawl_enabled=bool(app_config.get("crawl", {}).get("enabled", True)),
            use_unseen=bool(watch_cfg.get("use_unseen", False)),
            timings_per_topic=int(watch_cfg.get("timings_per_topic", 30)),
            notify_interval_secs=int(notify_cfg.get("interval_secs", 600)),
            notify_auto_mark_read=bool(notify_cfg.get("auto_mark_read", False)),
        ),
        server=ServerSettings(
            host=server_cfg.get("host", "127.0.0.1"),
            port=int(server_cfg.get("port", 8080)),
            schedule=list(server_cfg.get("schedule", [])),
            api_key=server_cfg.get("api_key", ""),
            action_timeout_secs=float(server_cfg.get("action_timeout_secs", 60)),
            interval_secs=int(server_cfg.get("interval_secs", 30)),
            max_posts_per_interval=server_cfg.get("max_posts_per_interval"),
            auto_restart=bool(server_cfg.get("auto_restart", True)),
            restart_backoff_secs=int(server_cfg.get("restart_backoff_secs", 60)),
            max_restarts=int(server_cfg.get("max_restarts", 0)),
            same_error_stop_threshold=int(server_cfg.get("same_error_stop_threshold", 0)),
        ),
    )
