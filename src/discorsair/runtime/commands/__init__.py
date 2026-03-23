"""Runtime command handlers."""

from __future__ import annotations

import argparse

from ..types import CommandOutcome
from .actions import handle_action_command
from .context import RuntimeCommandContext
from .notify import handle_notify_test as handle_notify_test
from .serve import handle_serve_command
from .status import handle_status as handle_status
from .watch import handle_watch_command


def handle_authenticated_command(args: argparse.Namespace, context: RuntimeCommandContext) -> CommandOutcome:
    if context.services is None:
        raise ValueError("services are required for authenticated commands")
    if args.command in {"run", "watch"}:
        handle_watch_command(args, context)
        return CommandOutcome(exit_code=0)
    if args.command in {"daily", "like", "reply"}:
        return handle_action_command(args, context)
    if args.command == "serve":
        return handle_serve_command(args, context)
    raise ValueError(f"unsupported command: {args.command}")
