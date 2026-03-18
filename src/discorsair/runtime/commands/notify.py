"""Notify command handler."""

from __future__ import annotations

from discorsair.utils.notify import Notifier
from ..types import CommandOutcome


def handle_notify_test(notifier: Notifier | None) -> CommandOutcome:
    if not notifier:
        return CommandOutcome(payload={"ok": False, "action": "notify_test", "reason": "notify_not_configured"})
    sent = notifier.send("notification test")
    return CommandOutcome(payload={"ok": sent, "action": "notify_test"})
