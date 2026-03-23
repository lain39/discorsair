"""Config loading and defaults."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from discorsair.plugins.validation import validate_plugins_app_config
from discorsair.utils.jsonc import loads as jsonc_loads
from discorsair.utils.jsonc import strip_comments as jsonc_strip_comments
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ENV_OVERRIDE_PATHS: dict[str, tuple[str, str]] = {
    "DISCORSAIR_AUTH_NAME": ("auth", "name"),
    "DISCORSAIR_AUTH_COOKIE": ("auth", "cookie"),
    "DISCORSAIR_AUTH_KEY": ("server", "api_key"),
    "DISCORSAIR_NOTIFY_URL": ("notify", "url"),
}
_RUNTIME_STATE_AUTH_PATHS: tuple[tuple[str, str], ...] = (
    ("auth", "cookie"),
    ("auth", "status"),
    ("auth", "disabled"),
    ("auth", "last_ok"),
    ("auth", "last_fail"),
    ("auth", "last_error"),
)

_SCHEDULE_WINDOW_RE = re.compile(r"^(?P<start_h>\d{2}):(?P<start_m>\d{2})-(?P<end_h>\d{2}):(?P<end_m>\d{2})$")


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
        "plugins": {"dir": "plugins", "hook_timeout_secs": 10, "max_consecutive_failures": 3, "items": {}},
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
            "auto_mark_read": False,
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
    data = load_raw_app_config(path)
    return merge_app_config_and_runtime_state(data, {})


def load_raw_app_config(path: str | Path) -> dict[str, Any]:
    data = jsonc_loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data


def derive_runtime_state_path(config_path: str | Path) -> Path:
    path = Path(config_path)
    if path.suffix == ".json":
        return path.with_name(f"{path.stem}.state.json")
    return path.with_name(f"{path.name}.state.json")


def load_raw_runtime_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {}
    raw_text = state_path.read_text(encoding="utf-8")
    if not jsonc_strip_comments(raw_text).strip():
        return {}
    data = jsonc_loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("runtime state root must be an object")
    return data


def apply_runtime_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    for path in _RUNTIME_STATE_AUTH_PATHS:
        exists, value = _get_nested_value(state, path)
        if not exists:
            continue
        _set_nested_value(config, path, value)


def merge_app_config_and_runtime_state(app_data: dict[str, Any], state_data: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_dicts(default_app_config(), app_data)
    apply_runtime_state(merged, state_data)
    _apply_env_overrides(merged)
    return merged


def active_env_override_paths() -> set[tuple[str, str]]:
    active: set[tuple[str, str]] = set()
    for env_name, path in _ENV_OVERRIDE_PATHS.items():
        value = os.getenv(env_name)
        if not value:
            continue
        active.add(path)
    return active


def validate_app_config(config: dict[str, Any]) -> None:
    _validate_removed_fields(config)
    site = _require_object(config, "site", "config.site")
    auth = _require_object(config, "auth", "config.auth")
    time_cfg = _require_object(config, "time", "config.time")
    logging_cfg = _require_object(config, "logging", "config.logging")
    storage_cfg = _require_object(config, "storage", "config.storage")
    crawl_cfg = _require_object(config, "crawl", "config.crawl")
    watch_cfg = _require_object(config, "watch", "config.watch")
    queue_cfg = _require_object(config, "queue", "config.queue")
    server_cfg = _require_object(config, "server", "config.server")
    notify_cfg = _require_object(config, "notify", "config.notify")
    request_cfg = _require_object(config, "request", "config.request")
    flaresolverr_cfg = _require_object(config, "flaresolverr", "config.flaresolverr")
    validate_plugins_app_config(config.get("plugins", {}))
    _validate_bool(config, "debug", "config.debug")
    _validate_bool(auth, "disabled", "config.auth.disabled")
    _validate_bool(storage_cfg, "auto_per_site", "config.storage.auto_per_site")
    _validate_bool(storage_cfg, "rotate_daily", "config.storage.rotate_daily")
    _validate_bool(crawl_cfg, "enabled", "config.crawl.enabled")
    _validate_bool(watch_cfg, "use_unseen", "config.watch.use_unseen")
    _validate_bool(server_cfg, "auto_restart", "config.server.auto_restart")
    _validate_bool(notify_cfg, "enabled", "config.notify.enabled")
    _validate_bool(notify_cfg, "auto_mark_read", "config.notify.auto_mark_read")
    _validate_bool(flaresolverr_cfg, "enabled", "config.flaresolverr.enabled")
    _validate_bool(flaresolverr_cfg, "use_base_url_for_csrf", "config.flaresolverr.use_base_url_for_csrf")
    _validate_bool(flaresolverr_cfg, "in_docker", "config.flaresolverr.in_docker")
    _validate_non_negative_int(site, "timeout_secs", "config.site.timeout_secs")
    _validate_positive_int(watch_cfg, "timings_per_topic", "config.watch.timings_per_topic")
    _validate_non_negative_int(queue_cfg, "maxsize", "config.queue.maxsize")
    _validate_non_negative_int(notify_cfg, "interval_secs", "config.notify.interval_secs")
    _validate_non_negative_int(notify_cfg, "timeout_secs", "config.notify.timeout_secs")
    _validate_non_negative_number(request_cfg, "min_interval_secs", "config.request.min_interval_secs")
    _validate_non_negative_int(request_cfg, "max_retries", "config.request.max_retries")
    _validate_non_negative_int(flaresolverr_cfg, "request_timeout_secs", "config.flaresolverr.request_timeout_secs")
    _validate_port(server_cfg, "port", "config.server.port")
    _validate_non_negative_number(server_cfg, "action_timeout_secs", "config.server.action_timeout_secs")
    _validate_positive_int(server_cfg, "interval_secs", "config.server.interval_secs")
    _validate_optional_non_negative_int(server_cfg, "max_posts_per_interval", "config.server.max_posts_per_interval")
    _validate_non_negative_int(server_cfg, "restart_backoff_secs", "config.server.restart_backoff_secs")
    _validate_non_negative_int(server_cfg, "max_restarts", "config.server.max_restarts")
    _validate_non_negative_int(server_cfg, "same_error_stop_threshold", "config.server.same_error_stop_threshold")
    _validate_string(site, "base_url", "config.site.base_url")
    _validate_string(server_cfg, "host", "config.server.host")
    _validate_string(server_cfg, "api_key", "config.server.api_key")
    if "schedule" in server_cfg and not isinstance(server_cfg.get("schedule"), list):
        raise ValueError("config.server.schedule must be an array")
    _validate_string_list(server_cfg, "schedule", "config.server.schedule")
    _validate_schedule_windows(server_cfg.get("schedule", []), "config.server.schedule")
    if "headers" in notify_cfg and not isinstance(notify_cfg.get("headers"), dict):
        raise ValueError("config.notify.headers must be an object")
    if "path" in logging_cfg and not isinstance(logging_cfg.get("path"), str):
        raise ValueError("config.logging.path must be a string")
    base_url = site.get("base_url", "")
    if not base_url:
        raise ValueError("config.site.base_url is required")
    if not auth.get("cookie"):
        raise ValueError("config.auth.cookie is required")
    tz = time_cfg.get("timezone", "UTC")
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


def _get_nested_value(config: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _require_object(container: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    value = container.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _validate_bool(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    if not isinstance(container.get(key), bool):
        raise ValueError(f"{path} must be a boolean")


def _validate_non_negative_int(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer >= 0")
    if value < 0:
        raise ValueError(f"{path} must be >= 0")


def _validate_optional_non_negative_int(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer or null")
    parsed = value
    if parsed < 0:
        raise ValueError(f"{path} must be >= 0")


def _validate_positive_int(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer >= 1")
    if value < 1:
        raise ValueError(f"{path} must be >= 1")


def _validate_port(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer between 1 and 65535")
    if value < 1 or value > 65535:
        raise ValueError(f"{path} must be between 1 and 65535")


def _validate_non_negative_number(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{path} must be a number >= 0")
    if value < 0:
        raise ValueError(f"{path} must be >= 0")


def _validate_string(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")


def _validate_string_list(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, list):
        return
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path} items must be strings")


def _validate_schedule_windows(windows: list[Any], path: str) -> None:
    for index, window in enumerate(windows):
        if not isinstance(window, str):
            continue
        match = _SCHEDULE_WINDOW_RE.fullmatch(window)
        item_path = f"{path}[{index}]"
        if match is None:
            raise ValueError(f"{item_path} must match HH:MM-HH:MM")
        start_h = int(match.group("start_h"))
        start_m = int(match.group("start_m"))
        end_h = int(match.group("end_h"))
        end_m = int(match.group("end_m"))
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            raise ValueError(f"{item_path} must use valid 24-hour times")


def _set_nested_value(config: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    current: dict[str, Any] = config
    for key in path[:-1]:
        node = current.setdefault(key, {})
        if not isinstance(node, dict):
            return
        current = node
    current[path[-1]] = value
