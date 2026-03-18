"""Status command handler."""

from __future__ import annotations

from discorsair.flows.status import status as status_flow
from ..factory import open_store
from ..settings import RuntimeSettings
from ..types import CommandOutcome


def handle_status(settings: RuntimeSettings) -> CommandOutcome:
    store = open_store(settings)
    try:
        return CommandOutcome(payload=status_flow(store))
    finally:
        store.close()
