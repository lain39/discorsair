"""Lazy compatibility exports for runtime modules."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import RuntimeCommandContext
    from .factory import RuntimeServices
    from .runner import DiscorsairRuntime
    from .settings import RuntimeSettings
    from .state import RuntimeStateStore
    from .types import CommandOutcome

__all__ = [
    "CommandOutcome",
    "DiscorsairRuntime",
    "RuntimeCommandContext",
    "RuntimeServices",
    "RuntimeSettings",
    "RuntimeStateStore",
]

_EXPORTS = {
    "CommandOutcome": (".types", "CommandOutcome"),
    "DiscorsairRuntime": (".runner", "DiscorsairRuntime"),
    "RuntimeCommandContext": (".commands", "RuntimeCommandContext"),
    "RuntimeServices": (".factory", "RuntimeServices"),
    "RuntimeSettings": (".settings", "RuntimeSettings"),
    "RuntimeStateStore": (".state", "RuntimeStateStore"),
}


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
