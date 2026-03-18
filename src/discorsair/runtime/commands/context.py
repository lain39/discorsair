"""Shared runtime command context."""

from __future__ import annotations

from dataclasses import dataclass

from discorsair.utils.notify import Notifier
from ..factory import RuntimeServices
from ..settings import RuntimeSettings
from ..state import RuntimeStateStore


@dataclass
class RuntimeCommandContext:
    settings: RuntimeSettings
    state: RuntimeStateStore
    notifier: Notifier | None
    services: RuntimeServices | None = None
