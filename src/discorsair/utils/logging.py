"""Logging helpers."""

from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(level: int = logging.INFO, log_path: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path:
        path = Path(log_path)
        if path.parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
