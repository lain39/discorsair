"""Discorsair CLI entry."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from discorsair.runtime.runner import DiscorsairRuntime
from discorsair.runtime.types import CommandOutcome
from discorsair.utils.logging import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discorsair")
    parser.add_argument(
        "--config",
        default="config/app.json",
        help="Path to app config (default: config/app.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run watch loop")
    run_p.add_argument("--interval", type=_positive_int, default=30)
    run_p.add_argument("--once", action="store_true")
    run_p.add_argument("--max-posts-per-interval", type=_non_negative_int, default=200)

    watch_p = sub.add_parser("watch", help="Watch latest topics")
    watch_p.add_argument("--interval", type=_positive_int, default=30)
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--max-posts-per-interval", type=_non_negative_int, default=200)

    daily_p = sub.add_parser("daily", help="Daily activity")
    daily_p.add_argument("--topic", type=int, default=None)

    like_p = sub.add_parser("like", help="Toggle reaction")
    like_p.add_argument("--post", type=int, required=True)
    like_p.add_argument("--emoji", default="heart")

    reply_p = sub.add_parser("reply", help="Reply to topic")
    reply_p.add_argument("--topic", type=int, required=True)
    reply_p.add_argument("--raw", required=True)
    reply_p.add_argument("--category", type=int, default=None)

    export_p = sub.add_parser("export", help="Export storage to NDJSON")
    export_p.add_argument("--output", default="export", help="Output directory for NDJSON export")

    import_p = sub.add_parser("import", help="Import storage from NDJSON")
    import_p.add_argument("--input", required=True, help="Input directory containing NDJSON export")

    sub.add_parser("status", help="Show status")

    notify_p = sub.add_parser("notify", help="Notify helpers")
    notify_sub = notify_p.add_subparsers(dest="notify_cmd", required=True)
    notify_sub.add_parser("test", help="Send test notification")

    init_p = sub.add_parser("init", help="Write config template to path")
    init_p.add_argument("--path", default="config/app.json", help="Output path for template")

    serve_p = sub.add_parser("serve", help="Run HTTP control server")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    return parser


def _extract_config_from_unknown(unknown: list[str]) -> tuple[str | None, list[str]]:
    if not unknown:
        return None, []
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    cfg_args, rest = config_parser.parse_known_args(unknown)
    return cfg_args.config, rest


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer >= 0") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        config_override, rest = _extract_config_from_unknown(unknown)
        if rest:
            parser.error(f"unrecognized arguments: {' '.join(rest)}")
        if config_override:
            args.config = config_override

    if args.command == "init":
        setup_logging(logging.INFO)
        _write_template(args.path)
        return 0

    require_auth_cookie = not (
        args.command in {"status", "export", "import"}
        or (args.command == "notify" and args.notify_cmd == "test")
    )
    outcome = DiscorsairRuntime.from_config_path(args.config, require_auth_cookie=require_auth_cookie).run(args)
    _render_outcome(outcome)
    return outcome.exit_code


def _render_outcome(outcome: CommandOutcome) -> None:
    if outcome.payload is not None:
        print(json.dumps(outcome.payload, ensure_ascii=False, indent=2))


def _write_template(path: str) -> None:
    template = Path("config/app.json.template")
    if not template.exists():
        raise FileNotFoundError("config/app.json.template not found")
    out_path = Path(path)
    if out_path.parent:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
