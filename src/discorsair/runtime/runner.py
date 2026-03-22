"""Runtime lifecycle and exception boundaries."""

from __future__ import annotations

import argparse
from typing import Any

from curl_cffi.requests.exceptions import RequestException

from discorsair.discourse.client import DiscourseAuthError
from .commands import RuntimeCommandContext
from .commands import handle_authenticated_command
from .commands import handle_notify_test
from .commands import handle_status
from .factory import RuntimeServices
from .factory import build_notifier
from .factory import build_services
from .factory import load_runtime_app_config
from .factory import load_settings
from .state import RuntimeStateStore
from .types import CommandOutcome


class DiscorsairRuntime:
    def __init__(self, app_config: dict[str, Any]) -> None:
        self._state = RuntimeStateStore(app_config)
        self._app_config = app_config
        self._settings = load_settings(app_config)
        self._notifier = build_notifier(app_config)

    @classmethod
    def from_config_path(cls, config_path: str) -> "DiscorsairRuntime":
        return cls(load_runtime_app_config(config_path))

    def run(self, args: argparse.Namespace) -> CommandOutcome:
        if args.command == "status":
            return handle_status(self._app_config, self._settings)
        if args.command == "notify" and args.notify_cmd == "test":
            return handle_notify_test(self._notifier)

        services: RuntimeServices | None = None
        try:
            services = self._open_services()
            return handle_authenticated_command(
                args,
                RuntimeCommandContext(
                    settings=self._settings,
                    state=self._state,
                    notifier=self._notifier,
                    services=services,
                ),
            )
        except DiscourseAuthError as exc:
            if self._notifier:
                self._notifier.send_error(f"runtime error: login invalid: {exc}")
            self._state.mark_account_fail(exc, mark_invalid=True, disable=True)
            raise
        except KeyboardInterrupt:
            self._state.mark_account_fail(RuntimeError("interrupted"), mark_invalid=False, disable=False)
            if services is not None:
                self._state.save_cookies(services.base_client)
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, RequestException) and "curl: (23)" in str(exc):
                return CommandOutcome(exit_code=0)
            if self._notifier:
                self._notifier.send_error(f"runtime error: {exc}")
            self._state.mark_account_fail(exc, mark_invalid=False, disable=False)
            if services is not None:
                self._state.save_cookies(services.base_client)
            raise
        finally:
            if services is not None:
                services.close()

    def _open_services(self) -> RuntimeServices:
        return build_services(self._app_config, self._settings)
