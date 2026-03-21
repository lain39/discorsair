"""CLI/runtime boundary tests."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.cli import main
from discorsair.discourse.client import DiscourseAuthError
from discorsair.core.requester import ChallengeUnresolvedError
from discorsair.runtime.commands import RuntimeCommandContext, handle_notify_test, handle_authenticated_command
from discorsair.runtime.state import RuntimeStateStore
from discorsair.runtime.settings import RuntimeSettings, StoreSettings, WatchSettings, ServerSettings
from discorsair.runtime.types import CommandOutcome


class _Client:
    def __init__(self, *, ok: bool | None, cookie: str) -> None:
        self._ok = ok
        self._cookie = cookie

    def last_response_ok(self) -> bool | None:
        return self._ok

    def get_cookie_header(self) -> str:
        return self._cookie


class CliRuntimeTests(unittest.TestCase):
    def _settings(self) -> RuntimeSettings:
        return RuntimeSettings(
            timezone_name="UTC",
            store=StoreSettings(path="data/test.db", timezone_name="UTC", rotate_daily=False),
            watch=WatchSettings(crawl_enabled=True, use_unseen=False, timings_per_topic=30, notify_interval_secs=600),
            server=ServerSettings(
                host="127.0.0.1",
                port=8080,
                schedule=[],
                api_key="",
                interval_secs=30,
                max_posts_per_interval=200,
                auto_restart=True,
                restart_backoff_secs=60,
                max_restarts=0,
                same_error_stop_threshold=0,
            ),
        )

    def test_state_store_saves_cookie_only_for_successful_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            config = {"_path": str(config_path), "auth": {"cookie": "old"}}
            state = RuntimeStateStore(config)

            state.save_cookies(_Client(ok=False, cookie="new-cookie"))
            state.save_cookies(_Client(ok=None, cookie="new-cookie"))

            self.assertEqual(config["auth"]["cookie"], "old")
            self.assertFalse(config_path.exists())

            state.save_cookies(_Client(ok=True, cookie="_t=new-token; cf_clearance=abc; session=xyz"))

            self.assertEqual(config["auth"]["cookie"], "_t=new-token")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["auth"]["cookie"], "_t=new-token")

    def test_state_store_does_not_overwrite_cookie_with_empty_or_same_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            config = {"_path": str(config_path), "auth": {"cookie": "_t=old-cookie"}}
            state = RuntimeStateStore(config)

            state.save_cookies(_Client(ok=True, cookie=""))
            self.assertEqual(config["auth"]["cookie"], "_t=old-cookie")
            self.assertFalse(config_path.exists())

            state.save_cookies(_Client(ok=True, cookie="cf_clearance=abc"))
            self.assertEqual(config["auth"]["cookie"], "_t=old-cookie")
            self.assertFalse(config_path.exists())

            state.save_cookies(_Client(ok=True, cookie="_t=old-cookie; cf_clearance=abc"))
            self.assertEqual(config["auth"]["cookie"], "_t=old-cookie")
            self.assertFalse(config_path.exists())

    def test_main_renders_runtime_payload_and_accepts_config_after_subcommand(self) -> None:
        runtime = Mock()
        runtime.run.return_value = CommandOutcome(exit_code=0, payload={"ok": True, "action": "status"})
        stdout = io.StringIO()

        with patch("discorsair.cli.DiscorsairRuntime.from_config_path", return_value=runtime) as factory:
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["status", "--config", "custom.json"])

        self.assertEqual(exit_code, 0)
        factory.assert_called_once_with("custom.json")
        runtime.run.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True, "action": "status"})

    def test_handle_notify_test_without_notifier_returns_reason(self) -> None:
        outcome = handle_notify_test(None)
        self.assertEqual(
            outcome.payload,
            {"ok": False, "action": "notify_test", "reason": "notify_not_configured"},
        )

    def test_handle_authenticated_like_marks_state_and_saves_cookie(self) -> None:
        state = Mock()
        services = types.SimpleNamespace(client=object(), base_client=object())
        args = types.SimpleNamespace(command="like", post=7, emoji="heart")
        context = RuntimeCommandContext(
            settings=self._settings(),
            state=state,
            notifier=None,
            services=services,
        )

        with patch("discorsair.runtime.commands.actions.like", return_value={"ok": True, "post_id": 7}) as like_fn:
            outcome = handle_authenticated_command(args, context)

        self.assertEqual(outcome.payload, {"ok": True, "post_id": 7})
        like_fn.assert_called_once_with(services.client, post_id=7, emoji="heart")
        state.mark_account_ok.assert_called_once_with()
        state.save_cookies.assert_called_once_with(services.base_client)

    def test_handle_authenticated_serve_wires_stop_and_auth_invalid_callbacks(self) -> None:
        state = Mock()
        services = types.SimpleNamespace(client=object(), base_client=object(), store=object())
        args = types.SimpleNamespace(command="serve", host=None, port=None)
        context = RuntimeCommandContext(
            settings=self._settings(),
            state=state,
            notifier=None,
            services=services,
        )
        controller = Mock()
        controller.fatal_error.return_value = None

        with patch("discorsair.runtime.commands.serve.WatchController", return_value=controller) as controller_cls:
            with patch("discorsair.runtime.commands.serve.serve") as serve_fn:
                outcome = handle_authenticated_command(args, context)

        self.assertEqual(outcome.exit_code, 0)
        controller_kwargs = controller_cls.call_args.kwargs
        serve_kwargs = serve_fn.call_args.kwargs
        controller_kwargs["on_stop"]()
        controller_kwargs["on_auth_invalid"](RuntimeError("not_logged_in"))
        serve_kwargs["on_action_success"]()
        self.assertEqual(state.mark_account_ok.call_count, 1)
        self.assertEqual(state.mark_account_fail.call_count, 1)
        self.assertEqual(state.save_cookies.call_count, 2)
        self.assertEqual(state.save_cookies.call_args_list[0].args, (services.base_client,))
        self.assertEqual(state.save_cookies.call_args_list[1].args, (services.base_client,))
        mark_args, mark_kwargs = state.mark_account_fail.call_args
        self.assertEqual(str(mark_args[0]), "not_logged_in")
        self.assertEqual(mark_kwargs, {"mark_invalid": True, "disable": True})
        serve_fn.assert_called_once_with(
            host="127.0.0.1",
            port=8080,
            client=services.client,
            watch_controller=controller,
            api_key="",
            on_action_success=serve_kwargs["on_action_success"],
        )

    def test_handle_authenticated_serve_returns_nonzero_on_auth_fatal(self) -> None:
        state = Mock()
        services = types.SimpleNamespace(client=object(), base_client=object(), store=object())
        args = types.SimpleNamespace(command="serve", host=None, port=None)
        context = RuntimeCommandContext(
            settings=self._settings(),
            state=state,
            notifier=None,
            services=services,
        )
        controller = Mock()
        controller.fatal_error.return_value = DiscourseAuthError("not_logged_in")

        with patch("discorsair.runtime.commands.serve.WatchController", return_value=controller):
            with patch("discorsair.runtime.commands.serve.serve"):
                outcome = handle_authenticated_command(args, context)

        self.assertEqual(outcome.exit_code, 1)
        state.mark_account_fail.assert_not_called()

    def test_handle_authenticated_serve_marks_fail_on_unresolved_challenge_fatal(self) -> None:
        state = Mock()
        services = types.SimpleNamespace(client=object(), base_client=object(), store=object())
        args = types.SimpleNamespace(command="serve", host=None, port=None)
        context = RuntimeCommandContext(
            settings=self._settings(),
            state=state,
            notifier=None,
            services=services,
        )
        controller = Mock()
        exc = ChallengeUnresolvedError("challenge still present after solve")
        controller.fatal_error.return_value = exc

        with patch("discorsair.runtime.commands.serve.WatchController", return_value=controller):
            with patch("discorsair.runtime.commands.serve.serve"):
                outcome = handle_authenticated_command(args, context)

        self.assertEqual(outcome.exit_code, 1)
        state.mark_account_fail.assert_called_once_with(exc, mark_invalid=False, disable=False)


if __name__ == "__main__":
    unittest.main()
