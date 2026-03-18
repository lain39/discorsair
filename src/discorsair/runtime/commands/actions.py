"""Action command handlers for daily/like/reply."""

from __future__ import annotations

import argparse

from discorsair.flows.daily import daily
from discorsair.flows.like import like
from discorsair.flows.reply import reply
from ..types import CommandOutcome
from .context import RuntimeCommandContext


def handle_action_command(args: argparse.Namespace, context: RuntimeCommandContext) -> CommandOutcome:
    if context.services is None:
        raise ValueError("services are required for action commands")
    if args.command == "daily":
        return _handle_action(lambda: daily(context.services.client, topic_id=args.topic if args.topic else None), context)
    if args.command == "like":
        return _handle_action(lambda: like(context.services.client, post_id=args.post, emoji=args.emoji), context)
    if args.command == "reply":
        return _handle_action(
            lambda: reply(context.services.client, topic_id=args.topic, raw=args.raw, category=args.category),
            context,
        )
    raise ValueError(f"unsupported action command: {args.command}")


def _handle_action(action, context: RuntimeCommandContext) -> CommandOutcome:
    payload = action()
    context.state.mark_account_ok()
    context.state.save_cookies(context.services.base_client)
    return CommandOutcome(payload=payload)
