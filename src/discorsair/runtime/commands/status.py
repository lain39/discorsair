"""Status command handler."""

from __future__ import annotations

import types

from discorsair.flows.status import status as status_flow
from discorsair.plugins import PluginManager
from ..factory import open_store
from ..settings import RuntimeSettings
from ..types import CommandOutcome


def handle_status(app_config: dict[str, object], settings: RuntimeSettings) -> CommandOutcome:
    disabled_plugins = {"enabled": False, "count": 0, "backend": None, "runtime_live": False, "items": []}
    if not settings.watch.crawl_enabled:
        plugin_manager = PluginManager.from_app_config(
            app_config,
            client=types.SimpleNamespace(),
            store=None,
            timezone_name=settings.timezone_name,
            initialize=False,
            instantiate=False,
        )
        plugins = (
            plugin_manager.snapshot()
            if plugin_manager is not None
            else disabled_plugins
        )
        return CommandOutcome(payload=status_flow(None, plugins=plugins))
    try:
        store = open_store(settings, initialize=False, ensure_metadata=False, read_only=True)
    except FileNotFoundError:
        plugin_manager = PluginManager.from_app_config(
            app_config,
            client=types.SimpleNamespace(),
            store=None,
            timezone_name=settings.timezone_name,
            initialize=False,
            instantiate=False,
        )
        plugins = plugin_manager.snapshot() if plugin_manager is not None else dict(disabled_plugins)
        return CommandOutcome(payload=status_flow(None, plugins=plugins))
    except ValueError as exc:
        if str(exc) != "postgres schema not initialized":
            raise
        plugin_manager = PluginManager.from_app_config(
            app_config,
            client=types.SimpleNamespace(),
            store=None,
            timezone_name=settings.timezone_name,
            initialize=False,
            instantiate=False,
        )
        plugins = plugin_manager.snapshot() if plugin_manager is not None else dict(disabled_plugins)
        return CommandOutcome(payload=status_flow(None, plugins=plugins))
    try:
        plugin_manager = PluginManager.from_app_config(
            app_config,
            client=types.SimpleNamespace(),
            store=store,
            timezone_name=settings.timezone_name,
            initialize=False,
            instantiate=False,
        )
        plugins = (
            plugin_manager.snapshot()
            if plugin_manager is not None
            else disabled_plugins
        )
        return CommandOutcome(payload=status_flow(store, plugins=plugins))
    finally:
        store.close()
