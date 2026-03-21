"""Config loading and defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from discorsair.utils.jsonc import loads as jsonc_loads
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ENV_OVERRIDE_PATHS: dict[str, tuple[str, str]] = {
    "DISCORSAIR_AUTH_NAME": ("auth", "name"),
    "DISCORSAIR_AUTH_COOKIE": ("auth", "cookie"),
    "DISCORSAIR_AUTH_KEY": ("server", "api_key"),
    "DISCORSAIR_NOTIFY_URL": ("notify", "url"),
}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dicts(out[key], value)
        else:
            out[key] = value
    return out


def default_app_config() -> dict[str, Any]:
    return {
        "site": {"base_url": "", "timeout_secs": 20},
        "time": {"timezone": "Asia/Shanghai"},
        "auth": {
            "name": "main",
            "cookie": "",
            "proxy": "",
            "status": "active",
            "disabled": False,
            "last_ok": "",
            "last_fail": "",
            "last_error": "",
            "note": "",
        },
        "debug": False,
        "logging": {"path": ""},
        "storage": {"path": "data/discorsair.db", "auto_per_site": True, "rotate_daily": False},
        "crawl": {"enabled": True},
        "watch": {"use_unseen": False, "timings_per_topic": 30},
        "queue": {"maxsize": 0},
        "server": {
            "host": "127.0.0.1",
            "port": 8080,
            "action_timeout_secs": 60,
            "interval_secs": 30,
            "max_posts_per_interval": 200,
            "auto_restart": True,
            "restart_backoff_secs": 60,
            "max_restarts": 0,
            "same_error_stop_threshold": 0,
            "api_key": "",
            "schedule": ["08:00-12:00", "14:00-23:00"],
        },
        "notify": {
            "enabled": False,
            "interval_secs": 600,
            "url": "",
            "chat_id": "",
            "prefix": "[Discorsair]",
            "error_prefix": "[Discorsair][error]",
            "headers": {"Content-Type": "application/json"},
            "timeout_secs": 15,
        },
        "request": {"impersonate_target": "", "user_agent": "", "max_retries": 1, "min_interval_secs": 1},
        "flaresolverr": {
            "enabled": True,
            "base_url": "http://host.docker.internal:8191",
            "request_timeout_secs": 60,
            "ua_probe_url": "",
            "use_base_url_for_csrf": False,
            "in_docker": True,
        },
    }


def load_app_config(path: str | Path) -> dict[str, Any]:
    data = jsonc_loads(Path(path).read_text(encoding="utf-8"))
    merged = _merge_dicts(default_app_config(), data)
    _apply_env_overrides(merged)
    return merged


def validate_app_config(config: dict[str, Any]) -> None:
    _validate_removed_fields(config)
    base_url = config.get("site", {}).get("base_url", "")
    if not base_url:
        raise ValueError("config.site.base_url is required")
    auth = config.get("auth", {})
    if not isinstance(auth, dict) or not auth.get("cookie"):
        raise ValueError("config.auth.cookie is required")
    tz = config.get("time", {}).get("timezone", "UTC")
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid timezone: {tz}") from exc


def _validate_removed_fields(config: dict[str, Any]) -> None:
    queue_cfg = config.get("queue", {})
    if isinstance(queue_cfg, dict) and "timeout_secs" in queue_cfg:
        raise ValueError("config.queue.timeout_secs has been removed; delete this field")


def _apply_env_overrides(config: dict[str, Any]) -> None:
    for env_name, path in _ENV_OVERRIDE_PATHS.items():
        value = os.getenv(env_name)
        if not value:
            continue
        _set_nested_value(config, path, value)


def _set_nested_value(config: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    current: dict[str, Any] = config
    for key in path[:-1]:
        node = current.setdefault(key, {})
        if not isinstance(node, dict):
            return
        current = node
    current[path[-1]] = value
