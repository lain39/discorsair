"""Watch command handler."""

from __future__ import annotations

import argparse

from discorsair.flows.watch import watch
from .context import RuntimeCommandContext


def handle_watch_command(args: argparse.Namespace, context: RuntimeCommandContext) -> None:
    if context.services is None:
        raise ValueError("services are required for watch commands")
    try:
        watch(
            context.services.client,
            context.services.store,
            interval_secs=args.interval,
            once=args.once,
            max_posts_per_interval=args.max_posts_per_interval,
            crawl_enabled=context.settings.watch.crawl_enabled,
            use_unseen=context.settings.watch.use_unseen,
            timings_per_topic=context.settings.watch.timings_per_topic,
            timezone_name=context.settings.timezone_name,
            notifier=context.notifier,
            notify_interval_secs=context.settings.watch.notify_interval_secs,
            notify_auto_mark_read=context.settings.watch.notify_auto_mark_read,
            plugin_manager=context.services.plugin_manager,
            on_success=context.state.mark_account_ok,
        )
    finally:
        context.state.save_cookies(context.services.base_client)
