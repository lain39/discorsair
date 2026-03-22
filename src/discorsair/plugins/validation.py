"""Plugin config validation helpers."""

from __future__ import annotations

from typing import Any

VALID_HOOKS = {
    "cycle.started",
    "topics.fetched",
    "topic.before_enter",
    "topic.after_enter",
    "topic.after_crawl",
    "post.fetched",
    "cycle.finished",
}

VALID_PERMISSIONS = {
    "topics.reorder",
    "topics.skip",
    "reply.create",
    "post.like",
    "storage.read",
    "storage.write",
}


def validate_plugins_app_config(plugins_cfg: Any) -> None:
    if plugins_cfg is None:
        return
    if not isinstance(plugins_cfg, dict):
        raise ValueError("config.plugins must be an object")
    items = plugins_cfg.get("items", {})
    if not isinstance(items, dict):
        raise ValueError("config.plugins.items must be an object")
    _validate_non_negative_number(plugins_cfg, "hook_timeout_secs", "config.plugins.hook_timeout_secs")
    _validate_non_negative_int(plugins_cfg, "max_consecutive_failures", "config.plugins.max_consecutive_failures")
    for plugin_id, item_cfg in items.items():
        plugin_path = f"config.plugins.items.{plugin_id}"
        if not isinstance(item_cfg, dict):
            raise ValueError(f"{plugin_path} must be an object")
        _validate_bool(item_cfg, "enabled", f"{plugin_path}.enabled")
        _validate_int(item_cfg, "priority", f"{plugin_path}.priority")
        permissions = item_cfg.get("permissions")
        if permissions is not None:
            if not isinstance(permissions, list):
                raise ValueError(f"{plugin_path}.permissions must be an array")
            invalid_permissions = {str(permission) for permission in permissions} - VALID_PERMISSIONS
            if invalid_permissions:
                raise ValueError(f"plugin {plugin_id} has invalid app permissions: {sorted(invalid_permissions)}")
        item_config = item_cfg.get("config")
        if item_config is not None and not isinstance(item_config, dict):
            raise ValueError(f"{plugin_path}.config must be an object")
        limits = item_cfg.get("limits")
        if limits is not None:
            if not isinstance(limits, dict):
                raise ValueError(f"{plugin_path}.limits must be an object")
            _validate_non_negative_int(limits, "reply_per_day", f"{plugin_path}.limits.reply_per_day")
            _validate_non_negative_int(limits, "like_per_day", f"{plugin_path}.limits.like_per_day")
        _validate_non_negative_number(item_cfg, "hook_timeout_secs", f"{plugin_path}.hook_timeout_secs")
        _validate_non_negative_int(item_cfg, "max_consecutive_failures", f"{plugin_path}.max_consecutive_failures")


def _validate_non_negative_int(container: dict[str, Any], key: str, path: str) -> None:
    value = container.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer >= 0")
    number = value
    if number < 0:
        raise ValueError(f"{path} must be >= 0")


def _validate_bool(container: dict[str, Any], key: str, path: str) -> None:
    if key not in container:
        return
    value = container.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")


def _validate_int(container: dict[str, Any], key: str, path: str) -> None:
    value = container.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer")


def _validate_non_negative_number(container: dict[str, Any], key: str, path: str) -> None:
    value = container.get(key)
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{path} must be a number >= 0")
    number = float(value)
    if number < 0:
        raise ValueError(f"{path} must be >= 0")
