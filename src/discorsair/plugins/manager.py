"""Plugin manager and execution context."""

from __future__ import annotations

import importlib.util
import inspect
import ast
import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from discorsair.plugins.backend import MemoryPluginStateBackend
from discorsair.plugins.backend import PluginStateBackend
from discorsair.plugins.backend import StorePluginStateBackend
from discorsair.storage import StoreBackend
from discorsair.plugins.validation import VALID_HOOKS
from discorsair.plugins.validation import VALID_PERMISSIONS
from discorsair.plugins.validation import validate_plugins_app_config
from discorsair.utils.jsonc import loads as jsonc_loads

_HOOK_METHODS = {
    "cycle.started": "on_cycle_started",
    "topics.fetched": "on_topics_fetched",
    "topic.before_enter": "on_topic_before_enter",
    "topic.after_enter": "on_topic_after_enter",
    "topic.after_crawl": "on_topic_after_crawl",
    "post.fetched": "on_post_fetched",
    "cycle.finished": "on_cycle_finished",
}


class PluginHookTimeoutError(RuntimeError):
    """Raised when a plugin hook exceeds its timeout."""


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dicts(out[key], value)
        else:
            out[key] = value
    return out


def _plugin_items(app_config: dict[str, Any]) -> dict[str, Any]:
    plugins_cfg = app_config.get("plugins", {})
    validate_plugins_app_config(plugins_cfg)
    items = plugins_cfg.get("items", {})
    return items


@dataclass(frozen=True)
class LoadedPlugin:
    plugin_id: str
    instance: Any | None
    hooks: set[str]
    permissions: set[str]
    priority: int
    config: dict[str, Any]
    limits: dict[str, int]
    hook_timeout_secs: float
    max_consecutive_failures: int
    logger: logging.Logger


@dataclass
class PluginCycleState:
    cycle_id: str = field(default_factory=lambda: uuid4().hex)
    topic_scores: dict[int, float] = field(default_factory=dict)
    skipped_topics: dict[int, str] = field(default_factory=dict)


@dataclass
class PluginRuntimeState:
    consecutive_failures: int = 0
    disabled: bool = False
    hook_successes: dict[str, int] = field(default_factory=dict)
    hook_failures: dict[str, int] = field(default_factory=dict)
    hook_timeouts: dict[str, int] = field(default_factory=dict)
    action_counts: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None
    last_error_at: str | None = None


@dataclass
class HookExecutionState:
    cancelled: threading.Event = field(default_factory=threading.Event)


class PluginContext:
    def __init__(
        self,
        manager: "PluginManager",
        plugin: LoadedPlugin,
        cycle_state: PluginCycleState | None,
        hook: str,
        execution_state: HookExecutionState | None = None,
    ) -> None:
        self._manager = manager
        self._plugin = plugin
        self._cycle_state = cycle_state
        self._hook = hook
        self._execution_state = execution_state
        self._pending_topic_scores: dict[int, float] = {}
        self._pending_skipped_topics: dict[int, str] = {}
        self._pending_action_logs: list[dict[str, Any]] = []
        self.logger = plugin.logger
        self.config = plugin.config
        self.plugin_id = plugin.plugin_id

    def now(self) -> datetime:
        return self._manager.now()

    def _cycle_id(self) -> str:
        return self._cycle_state.cycle_id if self._cycle_state is not None else ""

    def _log_action(
        self,
        action: str,
        *,
        status: str,
        reason: str = "",
        topic_id: int | None = None,
        post_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._manager.log_plugin_action(
            plugin_id=self.plugin_id,
            hook_name=self._hook,
            cycle_id=self._cycle_id(),
            action=action,
            status=status,
            reason=reason,
            topic_id=topic_id,
            post_id=post_id,
            extra=extra,
        )

    def prioritize_topic(self, topic_id: int, score: float) -> dict[str, Any]:
        if not self._ensure_hook_active():
            self._log_action("prioritize_topic", status="rejected", reason="hook_cancelled")
            return {"ok": False, "reason": "hook_cancelled"}
        if self._hook != "topics.fetched":
            self._log_action("prioritize_topic", status="rejected", reason="unsupported_hook")
            return {"ok": False, "reason": "unsupported_hook"}
        if not self._require_permission("topics.reorder"):
            self._log_action("prioritize_topic", status="rejected", reason="permission_denied")
            return {"ok": False, "reason": "permission_denied"}
        if self._cycle_state is None:
            self._log_action("prioritize_topic", status="rejected", reason="no_cycle")
            return {"ok": False, "reason": "no_cycle"}
        topic_id = int(topic_id or 0)
        if topic_id <= 0:
            self._log_action("prioritize_topic", status="rejected", reason="invalid_topic_id")
            return {"ok": False, "reason": "invalid_topic_id"}
        self._pending_topic_scores[topic_id] = float(self._pending_topic_scores.get(topic_id, 0.0)) + float(score)
        self._pending_action_logs.append(
            {
                "action": "prioritize_topic",
                "message": "plugin action: action=prioritize_topic topic_id=%s score=%s",
                "args": (topic_id, score),
                "status": "applied",
                "reason": "",
                "topic_id": topic_id,
                "post_id": None,
                "extra": {"score": float(score)},
            }
        )
        return {"ok": True}

    def skip_topic(self, topic_id: int, reason: str = "") -> dict[str, Any]:
        if not self._ensure_hook_active():
            self._log_action("skip_topic", status="rejected", reason="hook_cancelled")
            return {"ok": False, "reason": "hook_cancelled"}
        if self._hook not in {"topics.fetched", "topic.before_enter"}:
            self._log_action("skip_topic", status="rejected", reason="unsupported_hook")
            return {"ok": False, "reason": "unsupported_hook"}
        if not self._require_permission("topics.skip"):
            self._log_action("skip_topic", status="rejected", reason="permission_denied")
            return {"ok": False, "reason": "permission_denied"}
        if self._cycle_state is None:
            self._log_action("skip_topic", status="rejected", reason="no_cycle")
            return {"ok": False, "reason": "no_cycle"}
        topic_id = int(topic_id or 0)
        if topic_id <= 0:
            self._log_action("skip_topic", status="rejected", reason="invalid_topic_id")
            return {"ok": False, "reason": "invalid_topic_id"}
        self._pending_skipped_topics[topic_id] = reason
        self._pending_action_logs.append(
            {
                "action": "skip_topic",
                "message": "plugin action: action=skip_topic topic_id=%s reason=%s",
                "args": (topic_id, reason),
                "status": "applied",
                "reason": reason,
                "topic_id": topic_id,
                "post_id": None,
                "extra": {},
            }
        )
        return {"ok": True, "reason": reason}

    def get_kv(self, key: str, default: Any = None) -> Any:
        if not self._require_permission("storage.read"):
            return default
        return self._manager.backend.get_kv(self.plugin_id, key, default=default)

    def set_kv(self, key: str, value: Any) -> dict[str, Any]:
        if not self._ensure_hook_active():
            self._log_action("set_kv", status="rejected", reason="hook_cancelled", extra={"key": key})
            return {"ok": False, "reason": "hook_cancelled"}
        if not self._require_permission("storage.write"):
            self._log_action("set_kv", status="rejected", reason="permission_denied", extra={"key": key})
            return {"ok": False, "reason": "permission_denied"}
        self._manager.backend.set_kv(self.plugin_id, key, value)
        self._manager.record_action(self.plugin_id, "set_kv")
        self._log_action("set_kv", status="applied", extra={"key": key})
        self.logger.info("plugin action: action=set_kv key=%s", key)
        return {"ok": True}

    def check_limit(self, key: str, daily_limit: int) -> bool:
        if not self._require_permission("storage.read"):
            return False
        limit = int(daily_limit or 0)
        if limit <= 0:
            return True
        return self._manager.backend.get_daily_count(self.plugin_id, f"trigger:{key}") < limit

    def record_trigger(self, key: str) -> int:
        if not self._ensure_hook_active():
            self._log_action("record_trigger", status="rejected", reason="hook_cancelled", extra={"key": key})
            return 0
        if not self._require_permission("storage.write"):
            self._log_action("record_trigger", status="rejected", reason="permission_denied", extra={"key": key})
            return 0
        count = self._manager.backend.inc_daily_count(self.plugin_id, f"trigger:{key}", delta=1)
        self._log_action("record_trigger", status="applied", extra={"key": key, "count": count})
        return count

    def was_done(self, key: str) -> bool:
        if not self._require_permission("storage.read"):
            return False
        return self._manager.backend.was_done(self.plugin_id, key)

    def mark_done(self, key: str) -> dict[str, Any]:
        if not self._ensure_hook_active():
            self._log_action("mark_done", status="rejected", reason="hook_cancelled", extra={"key": key})
            return {"ok": False, "reason": "hook_cancelled"}
        if not self._require_permission("storage.write"):
            self._log_action("mark_done", status="rejected", reason="permission_denied", extra={"key": key})
            return {"ok": False, "reason": "permission_denied"}
        self._manager.backend.mark_done(self.plugin_id, key)
        self._manager.record_action(self.plugin_id, "mark_done")
        self._log_action("mark_done", status="applied", extra={"key": key})
        self.logger.info("plugin action: action=mark_done key=%s", key)
        return {"ok": True}

    def reply(self, topic_id: int, content: str, *, once_key: str | None = None, category: int | None = None) -> dict[str, Any]:
        if not self._ensure_hook_active():
            self._log_action("reply", status="rejected", reason="hook_cancelled", topic_id=topic_id)
            return {"ok": False, "reason": "hook_cancelled"}
        if not self._require_permission("reply.create"):
            self._log_action("reply", status="rejected", reason="permission_denied", topic_id=topic_id)
            return {"ok": False, "reason": "permission_denied"}
        if not content:
            self._log_action("reply", status="rejected", reason="empty_content", topic_id=topic_id)
            return {"ok": False, "reason": "empty_content"}
        if once_key and self._manager.backend.was_done(self.plugin_id, once_key):
            self._manager.record_action(self.plugin_id, "reply.skipped")
            self._log_action(
                "reply",
                status="skipped",
                reason="once_key_exists",
                topic_id=topic_id,
                extra={"once_key": once_key, "category": category},
            )
            self.logger.info("plugin action: action=reply acted=false reason=once_key_exists topic_id=%s", topic_id)
            return {"ok": True, "acted": False, "reason": "once_key_exists"}
        limit = int(self._plugin.limits.get("reply_per_day", 0) or 0)
        if limit > 0 and self._manager.backend.get_daily_count(self.plugin_id, "reply") >= limit:
            self._manager.record_action(self.plugin_id, "reply.skipped")
            self._log_action(
                "reply",
                status="rejected",
                reason="daily_limit_exceeded",
                topic_id=topic_id,
                extra={"once_key": once_key, "category": category},
            )
            self.logger.info("plugin action: action=reply acted=false reason=daily_limit_exceeded topic_id=%s", topic_id)
            return {"ok": False, "reason": "daily_limit_exceeded"}
        result = self._manager.client.reply(topic_id=topic_id, raw=content, category=category)
        if not self._ensure_hook_active():
            self._log_action(
                "reply",
                status="rejected",
                reason="hook_cancelled",
                topic_id=topic_id,
                post_id=int(result.get("post", {}).get("id", 0) or 0) or None,
                extra={"once_key": once_key, "category": category},
            )
            self.logger.warning("plugin action cancelled after reply request completed: topic_id=%s", topic_id)
            return {"ok": False, "reason": "hook_cancelled"}
        post = result.get("post", {})
        self._manager.backend.inc_daily_count(self.plugin_id, "reply", delta=1)
        if once_key:
            self._manager.backend.mark_done(self.plugin_id, once_key)
        self._manager.record_action(self.plugin_id, "reply.acted")
        self._log_action(
            "reply",
            status="applied",
            topic_id=topic_id,
            post_id=int(post.get("id", 0) or 0) or None,
            extra={"once_key": once_key, "category": category},
        )
        self.logger.info("plugin action: action=reply acted=true topic_id=%s post_id=%s", topic_id, post.get("id"))
        return {
            "ok": True,
            "acted": True,
            "topic_id": topic_id,
            "post_id": post.get("id"),
            "category": category,
            "result": result,
        }

    def like(self, post: dict[str, Any], *, emoji: str = "heart") -> dict[str, Any]:
        if not self._ensure_hook_active():
            self._log_action("like", status="rejected", reason="hook_cancelled")
            return {"ok": False, "reason": "hook_cancelled"}
        if not self._require_permission("post.like"):
            self._log_action(
                "like",
                status="rejected",
                reason="permission_denied",
                post_id=int(post.get("id", 0) or 0) or None,
            )
            return {"ok": False, "reason": "permission_denied"}
        if post.get("current_user_reaction"):
            self._manager.record_action(self.plugin_id, "like.skipped")
            self._log_action(
                "like",
                status="skipped",
                reason="already_reacted",
                post_id=int(post.get("id", 0) or 0) or None,
                extra={"emoji": emoji},
            )
            self.logger.info(
                "plugin action: action=like acted=false reason=already_reacted post_id=%s",
                int(post.get("id", 0) or 0),
            )
            return {"ok": True, "acted": False, "reason": "already_reacted"}
        limit = int(self._plugin.limits.get("like_per_day", 0) or 0)
        if limit > 0 and self._manager.backend.get_daily_count(self.plugin_id, "like") >= limit:
            self._manager.record_action(self.plugin_id, "like.skipped")
            self._log_action(
                "like",
                status="rejected",
                reason="daily_limit_exceeded",
                post_id=int(post.get("id", 0) or 0) or None,
                extra={"emoji": emoji},
            )
            self.logger.info(
                "plugin action: action=like acted=false reason=daily_limit_exceeded post_id=%s",
                int(post.get("id", 0) or 0),
            )
            return {"ok": False, "reason": "daily_limit_exceeded"}
        post_id = int(post.get("id", 0) or 0)
        if post_id <= 0:
            self._log_action("like", status="rejected", reason="invalid_post", extra={"emoji": emoji})
            return {"ok": False, "reason": "invalid_post"}
        result = self._manager.client.toggle_reaction(post_id, emoji)
        if not self._ensure_hook_active():
            self._log_action(
                "like",
                status="rejected",
                reason="hook_cancelled",
                post_id=post_id,
                extra={"emoji": emoji},
            )
            self.logger.warning("plugin action cancelled after like request completed: post_id=%s", post_id)
            return {"ok": False, "reason": "hook_cancelled"}
        self._manager.backend.inc_daily_count(self.plugin_id, "like", delta=1)
        self._manager.record_action(self.plugin_id, "like.acted")
        self._log_action("like", status="applied", post_id=post_id, extra={"emoji": emoji})
        self.logger.info("plugin action: action=like acted=true post_id=%s emoji=%s", post_id, emoji)
        return {
            "ok": True,
            "acted": True,
            "post_id": post_id,
            "emoji": emoji,
            "current_user_reaction": result.get("current_user_reaction"),
            "result": result,
        }

    def _require_permission(self, permission: str) -> bool:
        return permission in self._plugin.permissions

    def cancel(self) -> None:
        if self._execution_state is not None:
            self._execution_state.cancelled.set()

    def _ensure_hook_active(self) -> bool:
        return self._execution_state is None or not self._execution_state.cancelled.is_set()

    def commit_cycle_changes(self) -> None:
        for item in self._pending_action_logs:
            self._manager.record_action(self.plugin_id, item["action"])
            self.logger.info(item["message"], *item["args"])
            self._log_action(
                item["action"],
                status=str(item.get("status", "applied") or "applied"),
                reason=str(item.get("reason", "") or ""),
                topic_id=item.get("topic_id"),
                post_id=item.get("post_id"),
                extra=item.get("extra"),
            )
        self._pending_action_logs.clear()
        if self._cycle_state is None:
            return
        for topic_id, score in self._pending_topic_scores.items():
            self._cycle_state.topic_scores[topic_id] = float(self._cycle_state.topic_scores.get(topic_id, 0.0)) + float(score)
        for topic_id, reason in self._pending_skipped_topics.items():
            self._cycle_state.skipped_topics[topic_id] = reason
        self._pending_topic_scores.clear()
        self._pending_skipped_topics.clear()


class PluginManager:
    def __init__(
        self,
        client: Any,
        backend: PluginStateBackend,
        plugins: list[LoadedPlugin],
        timezone_name: str,
        *,
        initialize: bool = True,
        runtime_live: bool = True,
    ) -> None:
        self.client = client
        self.backend = backend
        self._plugins = list(plugins)
        self._logger = logging.getLogger(__name__)
        self._tz = ZoneInfo(timezone_name)
        self._runtime_live = bool(runtime_live)
        self._state_lock = threading.RLock()
        self._hooks: dict[str, list[LoadedPlugin]] = {hook: [] for hook in VALID_HOOKS}
        self._runtime_states: dict[str, PluginRuntimeState] = {
            plugin.plugin_id: PluginRuntimeState() for plugin in self._plugins
        }
        for plugin in sorted(self._plugins, key=lambda item: (-item.priority, item.plugin_id)):
            for hook in plugin.hooks:
                self._hooks.setdefault(hook, []).append(plugin)
        if initialize:
            for plugin in self._plugins:
                on_load = getattr(plugin.instance, "on_load", None)
                if callable(on_load):
                    self._run_plugin_hook(
                        plugin,
                        "on_load",
                        PluginContext(self, plugin, None, "on_load", HookExecutionState()),
                        SimpleNamespace(name="on_load"),
                    )

    @classmethod
    def from_app_config(
        cls,
        app_config: dict[str, Any],
        *,
        client: Any,
        store: StoreBackend | None,
        timezone_name: str,
        initialize: bool = True,
        instantiate: bool = True,
    ) -> "PluginManager | None":
        items = _plugin_items(app_config)
        if not items:
            return None
        plugins_cfg = app_config.get("plugins", {})
        if not isinstance(plugins_cfg, dict):
            raise ValueError("config.plugins must be an object")
        plugins_dir = cls._resolve_plugins_dir(app_config)
        enabled = {plugin_id: cfg for plugin_id, cfg in items.items() if (cfg or {}).get("enabled") is True}
        if not enabled:
            return None
        loaded_plugins: list[LoadedPlugin] = []
        for plugin_id, item_cfg in enabled.items():
            manifest_path = cls._find_manifest_path(plugins_dir, plugin_id)
            if manifest_path is None:
                raise ValueError(f"enabled plugin not found: {plugin_id}")
            loaded_plugins.append(cls._load_plugin(manifest_path, plugin_id, item_cfg, plugins_cfg, instantiate=instantiate))
        backend: PluginStateBackend
        if store is not None:
            backend = StorePluginStateBackend(store)
        else:
            backend = MemoryPluginStateBackend(timezone_name)
        return cls(
            client=client,
            backend=backend,
            plugins=loaded_plugins,
            timezone_name=timezone_name,
            initialize=initialize,
            runtime_live=instantiate,
        )

    @staticmethod
    def _resolve_plugins_dir(app_config: dict[str, Any]) -> Path:
        plugins_cfg = app_config.get("plugins", {})
        rel_path = str(plugins_cfg.get("dir", "plugins") or "plugins")
        config_path = str(app_config.get("_path", "") or "")
        if config_path:
            config_dir = Path(config_path).resolve().parent
            base_dir = config_dir.parent if config_dir.name == "config" else config_dir
            return (base_dir / rel_path).resolve()
        return Path(rel_path).resolve()

    @staticmethod
    def _find_manifest_path(plugins_dir: Path, plugin_id: str) -> Path | None:
        direct_path = plugins_dir / plugin_id / "manifest.json"
        if not plugins_dir.exists():
            return None
        matches: list[Path] = []
        for manifest_path in plugins_dir.glob("*/manifest.json"):
            if direct_path.exists() and manifest_path == direct_path:
                continue
            try:
                data = jsonc_loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            manifest_id = str(data.get("id", "") or "")
            if manifest_id == plugin_id:
                matches.append(manifest_path)
        if direct_path.exists():
            if matches:
                raise ValueError(f"duplicate plugin id: {plugin_id}")
            return direct_path
        if len(matches) > 1:
            raise ValueError(f"duplicate plugin id: {plugin_id}")
        return matches[0] if matches else None

    @classmethod
    def _load_plugin(
        cls,
        manifest_path: Path,
        plugin_id: str,
        item_cfg: dict[str, Any],
        plugins_cfg: dict[str, Any],
        *,
        instantiate: bool = True,
    ) -> LoadedPlugin:
        manifest = jsonc_loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError(f"plugin manifest must be an object: {manifest_path}")
        api_version = int(manifest.get("api_version", 0) or 0)
        if api_version != 1:
            raise ValueError(f"plugin {plugin_id} has unsupported api_version: {api_version}")
        manifest_id = str(manifest.get("id", "") or "")
        if manifest_id != plugin_id:
            raise ValueError(f"plugin id mismatch in manifest: expected={plugin_id} actual={manifest_id}")
        hooks_raw = manifest.get("hooks", [])
        if not isinstance(hooks_raw, list) or not hooks_raw:
            raise ValueError(f"plugin {plugin_id} hooks must be a non-empty array")
        hooks = {str(hook) for hook in hooks_raw}
        invalid_hooks = hooks - VALID_HOOKS
        if invalid_hooks:
            raise ValueError(f"plugin {plugin_id} has invalid hooks: {sorted(invalid_hooks)}")
        if not isinstance(item_cfg, dict):
            raise ValueError(f"config.plugins.items.{plugin_id} must be an object")
        permissions_raw = manifest.get("permissions", [])
        if not isinstance(permissions_raw, list):
            raise ValueError(f"plugin {plugin_id} permissions must be an array")
        permissions = {str(permission) for permission in permissions_raw}
        invalid_permissions = permissions - VALID_PERMISSIONS
        if invalid_permissions:
            raise ValueError(f"plugin {plugin_id} has invalid permissions: {sorted(invalid_permissions)}")
        item_permissions = (item_cfg or {}).get("permissions")
        if item_permissions is not None and not isinstance(item_permissions, list):
            raise ValueError(f"config.plugins.items.{plugin_id}.permissions must be an array")
        if isinstance(item_permissions, list):
            requested = {str(permission) for permission in item_permissions}
            invalid_requested = requested - VALID_PERMISSIONS
            if invalid_requested:
                raise ValueError(f"plugin {plugin_id} has invalid app permissions: {sorted(invalid_requested)}")
            permissions = permissions & requested
        entry = str(manifest.get("entry", "plugin.py") or "plugin.py")
        if not entry:
            raise ValueError(f"plugin {plugin_id} missing entry")
        entry_path = (manifest_path.parent / entry).resolve()
        if not entry_path.exists():
            raise ValueError(f"plugin {plugin_id} entry not found: {entry_path}")
        instance: Any | None = None
        if instantiate:
            module = cls._load_module(plugin_id, entry_path)
            create_plugin = getattr(module, "create_plugin", None)
            if not callable(create_plugin):
                raise ValueError(f"plugin {plugin_id} missing create_plugin()")
            instance = create_plugin()
        else:
            cls._validate_plugin_source(plugin_id, entry_path)
        default_config = manifest.get("default_config", {})
        if default_config is not None and not isinstance(default_config, dict):
            raise ValueError(f"plugin {plugin_id} default_config must be an object")
        item_config = (item_cfg or {}).get("config", {})
        if item_config is not None and not isinstance(item_config, dict):
            raise ValueError(f"config.plugins.items.{plugin_id}.config must be an object")
        final_config = _merge_dicts(default_config if isinstance(default_config, dict) else {}, item_config if isinstance(item_config, dict) else {})
        item_limits = (item_cfg or {}).get("limits", {})
        if item_limits is not None and not isinstance(item_limits, dict):
            raise ValueError(f"config.plugins.items.{plugin_id}.limits must be an object")
        limits = {
            "reply_per_day": int((item_limits or {}).get("reply_per_day", 0) or 0),
            "like_per_day": int((item_limits or {}).get("like_per_day", 0) or 0),
        }
        if limits["reply_per_day"] < 0 or limits["like_per_day"] < 0:
            raise ValueError(f"config.plugins.items.{plugin_id}.limits values must be >= 0")
        default_priority = int(manifest.get("default_priority", 0) or 0)
        priority = int((item_cfg or {}).get("priority", default_priority) or 0)
        timeout_default = float((item_cfg or {}).get("hook_timeout_secs", plugins_cfg.get("hook_timeout_secs", 10)) or 0)
        max_failures = int((item_cfg or {}).get("max_consecutive_failures", plugins_cfg.get("max_consecutive_failures", 3)) or 0)
        if timeout_default < 0:
            raise ValueError(f"config.plugins.items.{plugin_id}.hook_timeout_secs must be >= 0")
        if max_failures < 0:
            raise ValueError(f"config.plugins.items.{plugin_id}.max_consecutive_failures must be >= 0")
        logger = logging.getLogger(f"{__name__}.{plugin_id}")
        return LoadedPlugin(
            plugin_id=plugin_id,
            instance=instance,
            hooks=hooks,
            permissions=permissions,
            priority=priority,
            config=final_config,
            limits=limits,
            hook_timeout_secs=timeout_default,
            max_consecutive_failures=max_failures,
            logger=logger,
        )

    @staticmethod
    def _load_module(plugin_id: str, entry_path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(f"discorsair_plugin_{plugin_id}", entry_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"failed to load plugin module: {entry_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _validate_plugin_source(plugin_id: str, entry_path: Path) -> None:
        source = entry_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(entry_path))
        except SyntaxError as exc:
            raise ValueError(f"plugin {plugin_id} entry has invalid syntax: {exc}") from exc
        if not any(isinstance(node, ast.FunctionDef) and node.name == "create_plugin" for node in tree.body):
            raise ValueError(f"plugin {plugin_id} missing create_plugin()")

    def has_plugins(self) -> bool:
        return bool(self._plugins)

    def snapshot(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for plugin in sorted(self._plugins, key=lambda item: item.plugin_id):
            with self._state_lock:
                runtime_state = self._runtime_states[plugin.plugin_id]
                runtime_snapshot = {
                    "disabled": runtime_state.disabled,
                    "consecutive_failures": runtime_state.consecutive_failures,
                    "hook_successes": dict(runtime_state.hook_successes),
                    "hook_failures": dict(runtime_state.hook_failures),
                    "hook_timeouts": dict(runtime_state.hook_timeouts),
                    "action_counts": dict(runtime_state.action_counts),
                    "last_error": runtime_state.last_error,
                    "last_error_at": runtime_state.last_error_at,
                }
            state_snapshot = self.backend.snapshot_plugin_state(plugin.plugin_id)
            items.append(
                {
                    "plugin_id": plugin.plugin_id,
                    "priority": plugin.priority,
                    "hooks": sorted(plugin.hooks),
                    "permissions": sorted(plugin.permissions),
                    "limits": dict(plugin.limits),
                    "hook_timeout_secs": plugin.hook_timeout_secs,
                    "max_consecutive_failures": plugin.max_consecutive_failures,
                    "disabled": runtime_snapshot["disabled"] if self._runtime_live else None,
                    "consecutive_failures": runtime_snapshot["consecutive_failures"] if self._runtime_live else None,
                    "hook_successes": runtime_snapshot["hook_successes"] if self._runtime_live else None,
                    "hook_failures": runtime_snapshot["hook_failures"] if self._runtime_live else None,
                    "hook_timeouts": runtime_snapshot["hook_timeouts"] if self._runtime_live else None,
                    "action_counts": runtime_snapshot["action_counts"] if self._runtime_live else None,
                    "last_error": runtime_snapshot["last_error"] if self._runtime_live else None,
                    "last_error_at": runtime_snapshot["last_error_at"] if self._runtime_live else None,
                    "daily_counts": state_snapshot["daily_counts"],
                    "once_mark_count": state_snapshot["once_mark_count"],
                    "kv_keys": state_snapshot["kv_keys"],
                }
            )
        return {
            "enabled": bool(items),
            "count": len(items),
            "backend": self.backend.kind,
            "runtime_live": self._runtime_live,
            "items": items,
        }

    def new_cycle(self) -> PluginCycleState:
        return PluginCycleState()

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def dispatch(self, hook: str, cycle_state: PluginCycleState | None = None, **payload: Any) -> None:
        for plugin in self._hooks.get(hook, []):
            if self._is_disabled(plugin.plugin_id):
                continue
            method_name = _HOOK_METHODS[hook]
            method = getattr(plugin.instance, method_name, None)
            if not callable(method):
                continue
            event = SimpleNamespace(
                name=hook,
                ts=self.now().isoformat(),
                cycle_id=cycle_state.cycle_id if cycle_state is not None else "",
                plugin_config=plugin.config,
                **payload,
            )
            ctx = PluginContext(self, plugin, cycle_state, hook, HookExecutionState())
            plugin.logger.debug("plugin hook dispatch: hook=%s cycle_id=%s", hook, event.cycle_id)
            self._run_plugin_hook(plugin, hook, ctx, event)

    def sort_topics(self, topics: list[dict[str, Any]], cycle_state: PluginCycleState | None) -> list[dict[str, Any]]:
        if cycle_state is None:
            return list(topics)
        indexed = list(enumerate(topics))
        indexed.sort(
            key=lambda item: (
                -float(cycle_state.topic_scores.get(int(item[1].get("id", 0) or 0), 0.0)),
                item[0],
            )
        )
        return [topic for _, topic in indexed if int(topic.get("id", 0) or 0) not in cycle_state.skipped_topics]

    def is_topic_skipped(self, cycle_state: PluginCycleState | None, topic_id: int) -> bool:
        if cycle_state is None:
            return False
        return int(topic_id or 0) in cycle_state.skipped_topics

    def record_action(self, plugin_id: str, action: str) -> None:
        with self._state_lock:
            runtime_state = self._runtime_states.get(plugin_id)
            if runtime_state is None:
                return
            runtime_state.action_counts[action] = int(runtime_state.action_counts.get(action, 0)) + 1

    def log_plugin_action(
        self,
        *,
        plugin_id: str,
        hook_name: str,
        cycle_id: str,
        action: str,
        status: str,
        reason: str = "",
        topic_id: int | None = None,
        post_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.backend.log_action(
            cycle_id=cycle_id,
            plugin_id=plugin_id,
            hook_name=hook_name,
            action=action,
            status=status,
            reason=reason,
            topic_id=topic_id,
            post_id=post_id,
            extra=extra,
        )

    def _run_plugin_hook(self, plugin: LoadedPlugin, hook: str, ctx: PluginContext, event: Any) -> None:
        method_name = "on_load" if hook == "on_load" else _HOOK_METHODS.get(hook, "")
        method = getattr(plugin.instance, method_name, None)
        if not callable(method):
            return
        if self._is_disabled(plugin.plugin_id):
            return
        try:
            invoke = self._invoke_on_load if hook == "on_load" else self._invoke_with_timeout
            invoke(method, ctx, event, plugin.hook_timeout_secs)
        except PluginHookTimeoutError as exc:
            with self._state_lock:
                runtime_state = self._runtime_states[plugin.plugin_id]
                runtime_state.consecutive_failures += 1
                runtime_state.hook_timeouts[hook] = int(runtime_state.hook_timeouts.get(hook, 0)) + 1
                runtime_state.last_error = str(exc)
                runtime_state.last_error_at = self.now().isoformat()
                should_disable = plugin.max_consecutive_failures > 0 and runtime_state.consecutive_failures >= plugin.max_consecutive_failures
                failure_count = runtime_state.consecutive_failures
                if should_disable:
                    runtime_state.disabled = True
            plugin.logger.exception("plugin hook failed: hook=%s error=%s", hook, exc)
            if should_disable:
                plugin.logger.error(
                    "plugin disabled after consecutive failures: hook=%s failures=%s",
                    hook,
                    failure_count,
                )
            return
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                runtime_state = self._runtime_states[plugin.plugin_id]
                runtime_state.consecutive_failures += 1
                runtime_state.hook_failures[hook] = int(runtime_state.hook_failures.get(hook, 0)) + 1
                runtime_state.last_error = str(exc)
                runtime_state.last_error_at = self.now().isoformat()
                should_disable = plugin.max_consecutive_failures > 0 and runtime_state.consecutive_failures >= plugin.max_consecutive_failures
                failure_count = runtime_state.consecutive_failures
                if should_disable:
                    runtime_state.disabled = True
            plugin.logger.exception("plugin hook failed: hook=%s error=%s", hook, exc)
            if should_disable:
                plugin.logger.error(
                    "plugin disabled after consecutive failures: hook=%s failures=%s",
                    hook,
                    failure_count,
                )
            return
        with self._state_lock:
            runtime_state = self._runtime_states[plugin.plugin_id]
            runtime_state.consecutive_failures = 0
            runtime_state.hook_successes[hook] = int(runtime_state.hook_successes.get(hook, 0)) + 1
        ctx.commit_cycle_changes()

    @staticmethod
    def _invoke_with_timeout(method, ctx: PluginContext, event: Any, timeout_secs: float) -> None:
        timeout = float(timeout_secs or 0)
        if timeout <= 0:
            method(ctx, event)
            return
        result_q: queue.Queue[tuple[str, BaseException | None]] = queue.Queue(maxsize=1)

        def _target() -> None:
            try:
                method(ctx, event)
                result_q.put(("ok", None))
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("error", exc))

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            ctx.cancel()
            raise PluginHookTimeoutError(f"plugin hook timed out after {timeout}s")
        status, exc = result_q.get_nowait()
        if status == "error" and exc is not None:
            raise exc

    @classmethod
    def _invoke_on_load(cls, method, ctx: PluginContext, event: Any, timeout_secs: float) -> None:
        signature = cls._callable_signature(method)
        if signature is None:
            cls._invoke_with_timeout(method, ctx, event, timeout_secs)
            return
        positional_count = 0
        accepts_varargs = False
        for parameter in signature.parameters.values():
            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                accepts_varargs = True
                continue
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                positional_count += 1
        if accepts_varargs or positional_count >= 2:
            cls._invoke_with_timeout(method, ctx, event, timeout_secs)
            return
        if positional_count == 1:
            cls._invoke_with_timeout(lambda bound_ctx, _: method(bound_ctx), ctx, event, timeout_secs)
            return
        cls._invoke_with_timeout(lambda _bound_ctx, _event: method(), ctx, event, timeout_secs)

    @staticmethod
    def _callable_signature(method) -> inspect.Signature | None:
        try:
            return inspect.signature(method)
        except (TypeError, ValueError):
            return None

    def _is_disabled(self, plugin_id: str) -> bool:
        with self._state_lock:
            runtime_state = self._runtime_states.get(plugin_id)
            return bool(runtime_state.disabled) if runtime_state is not None else False
