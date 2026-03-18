"""Serve command handler."""

from __future__ import annotations

import argparse
import logging

from discorsair.server.http_server import WatchController, serve, validate_server_binding
from .context import RuntimeCommandContext


def handle_serve_command(args: argparse.Namespace, context: RuntimeCommandContext) -> None:
    if context.services is None:
        raise ValueError("services are required for serve command")
    server = context.settings.server
    host = args.host or server.host
    port = int(args.port or server.port)
    schedule = list(server.schedule)
    api_key = server.api_key
    validate_server_binding(host, api_key)
    logging.getLogger(__name__).info("serve: host=%s port=%s schedule=%s", host, port, schedule)
    controller = WatchController(
        client=context.services.client,
        store=context.services.store,
        notifier=context.notifier,
        interval_secs=server.interval_secs,
        max_posts_per_interval=server.max_posts_per_interval,
        crawl_enabled=context.settings.watch.crawl_enabled,
        use_unseen=context.settings.watch.use_unseen,
        timings_per_topic=context.settings.watch.timings_per_topic,
        timezone_name=context.settings.timezone_name,
        schedule_windows=schedule,
        notify_interval_secs=context.settings.watch.notify_interval_secs,
        auto_restart=server.auto_restart,
        restart_backoff_secs=server.restart_backoff_secs,
        max_restarts=server.max_restarts,
        same_error_stop_threshold=server.same_error_stop_threshold,
        on_stop=lambda: context.state.save_cookies(context.services.base_client),
    )
    serve(host=host, port=port, client=context.services.client, watch_controller=controller, api_key=api_key)
