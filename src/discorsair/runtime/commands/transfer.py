"""Storage export/import command handlers."""

from __future__ import annotations

from pathlib import Path

from discorsair.runtime.crawl_lock import acquire_site_crawl_lock
from discorsair.storage.transfer import export_backend
from discorsair.storage.transfer import import_backend
from discorsair.storage.transfer import validate_import_bundle
from ..settings import RuntimeSettings
from ..types import CommandOutcome


def handle_export_command(app_config: dict[str, object], settings: RuntimeSettings, *, output_dir: str) -> CommandOutcome:
    if settings.store.backend == "sqlite" and not Path(settings.store.path).exists():
        raise ValueError(f"sqlite database not found: {settings.store.path}")
    crawl_lock = acquire_site_crawl_lock(
        settings.store.lock_dir,
        site_key=settings.store.site_key,
        account_name=settings.store.account_name,
        config_path=str(app_config.get("_path", "") or ""),
    )
    try:
        return CommandOutcome(
            payload=export_backend(
                backend=settings.store.backend,
                path=settings.store.path,
                output_dir=output_dir,
                site_key=settings.store.site_key,
                account_name=settings.store.account_name,
            )
        )
    finally:
        crawl_lock.release()


def handle_import_command(app_config: dict[str, object], settings: RuntimeSettings, *, input_dir: str) -> CommandOutcome:
    validate_import_bundle(
        input_dir,
        expected_site_key=settings.store.site_key,
        expected_account_name=settings.store.account_name,
    )
    crawl_lock = acquire_site_crawl_lock(
        settings.store.lock_dir,
        site_key=settings.store.site_key,
        account_name=settings.store.account_name,
        config_path=str(app_config.get("_path", "") or ""),
    )
    try:
        payload = import_backend(
            backend=settings.store.backend,
            path=settings.store.path,
            input_dir=input_dir,
        )
    finally:
        crawl_lock.release()
    return CommandOutcome(payload=payload)
