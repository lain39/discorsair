"""Shared runtime types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CommandOutcome:
    exit_code: int = 0
    payload: dict[str, Any] | None = None
