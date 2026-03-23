"""CLI/runtime boundary tests."""

from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
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

from discorsair.cli import _build_parser, main
from discorsair.discourse.client import DiscourseAuthError
from discorsair.core.requester import ChallengeUnresolvedError
from discorsair.runtime.commands import RuntimeCommandContext, handle_notify_test, handle_authenticated_command
from discorsair.runtime.commands.status import handle_status
from discorsair.runtime.state import RuntimeStateStore
from discorsair.runtime.settings import RuntimeSettings, StoreSettings, WatchSettings, ServerSettings
from discorsair.runtime.types import CommandOutcome
from discorsair.utils.config import derive_runtime_state_path
from discorsair.utils.jsonc import loads as jsonc_loads


class _Client:
    def __init__(self, *, ok: bool | None, cookie: str) -> None:
        self._ok = ok
        self._cookie = cookie

    def last_response_ok(self) -> bool | None:
        return self._ok

    def get_cookie_header(self) -> str:
        return self._cookie


class CliRuntimeTests(unittest.TestCase):
    def _state_path(self, config_path: Path) -> Path:
        return derive_runtime_state_path(config_path)

    def _settings(self) -> RuntimeSettings:
        return RuntimeSettings(
            timezone_name="UTC",
            store=StoreSettings(path="data/test.db", timezone_name="UTC", rotate_daily=False),
            watch=WatchSettings(
                crawl_enabled=True,
                use_unseen=False,
                timings_per_topic=30,
                notify_interval_secs=600,
                notify_auto_mark_read=False,
            ),
            server=ServerSettings(
                host="127.0.0.1",
                port=8080,
                schedule=[],
                api_key="",
                action_timeout_secs=60.0,
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
            state_path = self._state_path(config_path)
            config = {"_path": str(config_path), "auth": {"cookie": "old"}}
            state = RuntimeStateStore(config)

            state.save_cookies(_Client(ok=False, cookie="new-cookie"))
            state.save_cookies(_Client(ok=None, cookie="new-cookie"))

            self.assertEqual(config["auth"]["cookie"], "old")
            self.assertFalse(state_path.exists())

            state.save_cookies(_Client(ok=True, cookie="_t=new-token; cf_clearance=abc; session=xyz"))

            self.assertEqual(config["auth"]["cookie"], "_t=new-token")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["auth"]["cookie"], "_t=new-token")

    def test_state_store_does_not_overwrite_cookie_with_empty_or_same_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config = {"_path": str(config_path), "auth": {"cookie": "_t=old-cookie"}}
            state = RuntimeStateStore(config)

            state.save_cookies(_Client(ok=True, cookie=""))
            self.assertEqual(config["auth"]["cookie"], "_t=old-cookie")
            self.assertFalse(state_path.exists())

            state.save_cookies(_Client(ok=True, cookie="cf_clearance=abc"))
            self.assertEqual(config["auth"]["cookie"], "_t=old-cookie")
            self.assertFalse(state_path.exists())

            state.save_cookies(_Client(ok=True, cookie="_t=old-cookie; cf_clearance=abc"))
            self.assertEqual(config["auth"]["cookie"], "_t=old-cookie")
            self.assertFalse(state_path.exists())

    def test_state_store_does_not_persist_env_overridden_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config = {
                "_path": str(config_path),
                "_env_override_paths": [("auth", "cookie"), ("server", "api_key"), ("notify", "url")],
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {"cookie": "_t=env-cookie", "last_ok": "", "status": "active", "disabled": False},
                "server": {"api_key": "env-key"},
                "notify": {"url": "https://env-notify.example"},
            }
            state = RuntimeStateStore(config)

            state.mark_account_ok()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("cookie", saved["auth"])
            self.assertIn("last_ok", saved["auth"])
            self.assertNotIn("server", saved)
            self.assertNotIn("notify", saved)

    def test_state_store_rewrites_jsonc_config_as_plain_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config_path.write_text(
                textwrap.dedent(
                    """
                    {
                      "site": {
                        "base_url": "https://forum.example"
                      },
                      "auth": {
                        // keep this comment
                        "cookie": "_t=file-cookie",
                        "status": "active",
                        "disabled": false,
                        "last_ok": "",
                        "last_fail": "",
                        "last_error": ""
                      }
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {
                    "cookie": "_t=file-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }
            state = RuntimeStateStore(config)

            state.mark_account_ok()

            saved_text = state_path.read_text(encoding="utf-8")
            self.assertIn("// keep this comment", config_path.read_text(encoding="utf-8"))
            saved = json.loads(saved_text)
            self.assertEqual(saved["auth"]["cookie"], "_t=file-cookie")
            self.assertTrue(saved["auth"]["last_ok"].endswith("Z"))

    def test_state_store_rewrites_jsonc_config_as_plain_json_when_env_overrides_skip_cookie_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config_path.write_text(
                textwrap.dedent(
                    """
                    {
                      "site": {
                        "base_url": "https://forum.example"
                      },
                      "auth": {
                        // cookie should stay from file
                        "cookie": "_t=file-cookie",
                        "status": "active",
                        "disabled": false,
                        "last_ok": "",
                        "last_fail": "",
                        "last_error": ""
                      }
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = {
                "_path": str(config_path),
                "_env_override_paths": [("auth", "cookie")],
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {
                    "cookie": "_t=env-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }
            state = RuntimeStateStore(config)

            state.mark_account_ok()

            saved_text = state_path.read_text(encoding="utf-8")
            self.assertIn("// cookie should stay from file", config_path.read_text(encoding="utf-8"))
            saved = json.loads(saved_text)
            self.assertNotIn("cookie", saved["auth"])
            self.assertTrue(saved["auth"]["last_ok"].endswith("Z"))

    def test_state_store_removes_stale_cookie_from_existing_state_when_env_override_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "account-a.json"
            state_path = self._state_path(config_path)
            state_path.write_text(
                json.dumps(
                    {
                        "auth": {
                            "cookie": "_t=stale-cookie",
                            "last_fail": "2026-03-22T00:00:00Z",
                            "last_error": "old error",
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "_path": str(config_path),
                "_env_override_paths": [("auth", "cookie")],
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {
                    "cookie": "_t=env-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }

            RuntimeStateStore(config).mark_account_ok()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("cookie", saved["auth"])
            self.assertEqual(saved["auth"]["last_fail"], "2026-03-22T00:00:00Z")
            self.assertEqual(saved["auth"]["last_error"], "old error")
            self.assertTrue(saved["auth"]["last_ok"].endswith("Z"))

    def test_state_store_falls_back_to_json_write_when_original_config_has_no_auth_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config_path.write_text(
                textwrap.dedent(
                    """
                    {
                      "site": {
                        "base_url": "https://forum.example"
                      }
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {
                    "cookie": "_t=file-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }
            state = RuntimeStateStore(config)

            state.mark_account_ok()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["auth"]["cookie"], "_t=file-cookie")
            self.assertEqual(saved["auth"]["status"], "active")
            self.assertFalse(saved["auth"]["disabled"])
            self.assertTrue(saved["auth"]["last_ok"].endswith("Z"))

    def test_state_store_fallback_write_preserves_external_disk_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config_path.write_text(
                textwrap.dedent(
                    """
                    {
                      "site": {
                        "base_url": "https://forum.example"
                      },
                      "logging": {
                        "path": "old.log"
                      }
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "logging": {"path": "old.log"},
                "auth": {
                    "cookie": "_t=file-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }
            state = RuntimeStateStore(config)
            config_path.write_text(
                textwrap.dedent(
                    """
                    {
                      "site": {
                        "base_url": "https://forum.example"
                      },
                      "logging": {
                        "path": "new.log"
                      }
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            state.mark_account_ok()

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["logging"]["path"], "new.log")
            self.assertNotIn("auth", saved)
            state_saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_saved["auth"]["cookie"], "_t=file-cookie")
            self.assertEqual(state_saved["auth"]["status"], "active")
            self.assertFalse(state_saved["auth"]["disabled"])
            self.assertTrue(state_saved["auth"]["last_ok"].endswith("Z"))

    def test_state_store_only_persists_fields_changed_by_current_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            raw_config = {
                "site": {"base_url": "https://forum.example"},
                "auth": {
                    "cookie": "_t=file-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }
            config_one = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example"},
                "auth": copy.deepcopy(raw_config["auth"]),
            }
            config_two = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example"},
                "auth": copy.deepcopy(raw_config["auth"]),
            }
            state_one = RuntimeStateStore(config_one)
            state_two = RuntimeStateStore(config_two)

            state_one.mark_account_ok()
            first_saved = json.loads(state_path.read_text(encoding="utf-8"))
            state_two.mark_account_fail(RuntimeError("boom"), mark_invalid=False, disable=False)
            second_saved = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertTrue(first_saved["auth"]["last_ok"].endswith("Z"))
            self.assertEqual(second_saved["auth"]["last_ok"], first_saved["auth"]["last_ok"])
            self.assertTrue(second_saved["auth"]["last_fail"].endswith("Z"))
            self.assertEqual(second_saved["auth"]["last_error"], "boom")

    def test_state_store_overwrites_invalid_runtime_state_with_current_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config_path.write_text(
                textwrap.dedent(
                    """
                    {
                      "site": {
                        "base_url": "https://forum.example"
                      },
                      "logging": {
                        "path": "old.log"
                      }
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "logging": {"path": "old.log"},
                "auth": {
                    "cookie": "_t=file-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }
            state = RuntimeStateStore(config)
            partial_text = textwrap.dedent(
                """
                {
                  "auth": {
                    "cookie": "_t=broken"
                  },
                """
            ).lstrip()
            state_path.write_text(partial_text, encoding="utf-8")

            with self.assertLogs("discorsair.runtime.state", level="WARNING") as logs:
                state.mark_account_ok()

            self.assertIn("failed to reload runtime state", "\n".join(logs.output))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["auth"]["cookie"], "_t=file-cookie")
            self.assertTrue(saved["auth"]["last_ok"].endswith("Z"))

    def test_state_store_preserves_existing_config_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            config_path.write_text(
                json.dumps(
                    {
                        "site": {"base_url": "https://forum.example"},
                        "auth": {"cookie": "_t=file-cookie"},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            state_path.write_text("{}\n", encoding="utf-8")
            os.chmod(state_path, 0o644)
            config = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {
                    "cookie": "_t=file-cookie",
                    "status": "active",
                    "disabled": False,
                    "last_ok": "",
                    "last_fail": "",
                    "last_error": "",
                },
            }

            RuntimeStateStore(config).mark_account_ok()

            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o644)

    def test_state_store_serializes_writes_per_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = self._state_path(config_path)
            raw_config = {"site": {"base_url": "https://forum.example"}, "auth": {"cookie": "_t=file-cookie"}}
            config_one = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example"},
                "auth": {"cookie": "_t=file-cookie", "status": "active", "disabled": False},
            }
            config_two = {
                "_path": str(config_path),
                "site": {"base_url": "https://forum.example"},
                "auth": {"cookie": "_t=file-cookie", "status": "active", "disabled": False},
            }
            state_one = RuntimeStateStore(config_one)
            state_two = RuntimeStateStore(config_two)
            gate = threading.Barrier(2)
            active = 0
            max_active = 0
            guard = threading.Lock()

            def fake_write(path: Path, payload: dict[str, object]) -> None:
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                    self.assertEqual(path, state_path)
                    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                finally:
                    with guard:
                        active -= 1

            def write_ok() -> None:
                gate.wait(timeout=1)
                state_one.mark_account_ok()

            def write_fail() -> None:
                gate.wait(timeout=1)
                state_two.mark_account_fail(RuntimeError("boom"), mark_invalid=True, disable=True)

            with patch("discorsair.runtime.state._write_json_atomically", side_effect=fake_write):
                first = threading.Thread(target=write_ok)
                second = threading.Thread(target=write_fail)
                first.start()
                second.start()
                first.join(timeout=1)
                second.join(timeout=1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(max_active, 1)

    def test_jsonc_loader_preserves_double_slash_inside_strings(self) -> None:
        payload = jsonc_loads(
            textwrap.dedent(
                """
                {
                  "site": {
                    "base_url": "https://forum.example/path // not a comment"
                  },
                  // real comment
                  "auth": {
                    "cookie": "_t=value"
                  }
                }
                """
            )
        )

        self.assertEqual(payload["site"]["base_url"], "https://forum.example/path // not a comment")

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

    def test_watch_parser_rejects_negative_max_posts_per_interval(self) -> None:
        parser = _build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["watch", "--max-posts-per-interval", "-1"])

    def test_watch_parser_rejects_non_positive_interval(self) -> None:
        parser = _build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["watch", "--interval", "0"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["watch", "--interval", "-1"])

    def test_run_parser_rejects_non_positive_interval(self) -> None:
        parser = _build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "--interval", "0"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "--interval", "-1"])

    def test_handle_notify_test_without_notifier_returns_reason(self) -> None:
        outcome = handle_notify_test(None)
        self.assertEqual(
            outcome.payload,
            {"ok": False, "action": "notify_test", "reason": "notify_not_configured"},
        )

    def test_handle_status_includes_plugin_snapshot(self) -> None:
        app_config = {
            "_path": "config/app.json",
            "plugins": {"items": {"demo": {"enabled": True}}},
        }
        plugin_manager = Mock()
        plugin_manager.snapshot.return_value = {
            "enabled": True,
            "count": 1,
            "backend": "memory",
            "runtime_live": False,
            "items": [{"plugin_id": "demo"}],
        }

        with patch("discorsair.runtime.commands.status.PluginManager.from_app_config", return_value=plugin_manager):
            outcome = handle_status(app_config, self._settings())

        self.assertEqual(outcome.payload["plugins"]["enabled"], True)
        self.assertEqual(outcome.payload["plugins"]["items"], [{"plugin_id": "demo"}])

    def test_handle_status_uses_store_for_plugin_snapshot_when_crawl_enabled(self) -> None:
        settings = self._settings()
        app_config = {
            "_path": "config/app.json",
            "plugins": {"items": {"demo": {"enabled": True}}},
        }
        plugin_manager = Mock()
        plugin_manager.snapshot.return_value = {
            "enabled": True,
            "count": 1,
            "backend": "sqlite",
            "runtime_live": False,
            "items": [{"plugin_id": "demo", "daily_counts": {"reply": 2}}],
        }
        store = Mock()
        store.get_stats_total.return_value = {"topics_seen": 1}
        store.get_stats_today.return_value = {"topics_seen": 1}
        store.current_path.return_value = "data/test.db"

        with patch("discorsair.runtime.commands.status.open_store", return_value=store):
            with patch("discorsair.runtime.commands.status.PluginManager.from_app_config", return_value=plugin_manager) as factory:
                outcome = handle_status(app_config, settings)

        self.assertEqual(outcome.payload["plugins"]["backend"], "sqlite")
        self.assertEqual(outcome.payload["plugins"]["items"][0]["daily_counts"], {"reply": 2})
        self.assertEqual(factory.call_args.kwargs["store"], store)
        self.assertEqual(factory.call_args.kwargs["instantiate"], False)
        store.close.assert_called_once_with()

    def test_handle_status_does_not_import_plugin_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config").mkdir()
            plugin_dir = root / "plugins" / "no_import"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "manifest.json").write_text(
                textwrap.dedent(
                    """
                    {
                      "id": "no_import",
                      "name": "No Import",
                      "version": "0.1.0",
                      "api_version": 1,
                      "entry": "plugin.py",
                      "hooks": ["topics.fetched"],
                      "permissions": [],
                      "default_priority": 10,
                      "default_config": {}
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (plugin_dir / "plugin.py").write_text(
                "raise RuntimeError('should not import')\n\ndef create_plugin():\n    return object()\n",
                encoding="utf-8",
            )
            config_path = root / "config" / "app.json"
            config_path.write_text("{}", encoding="utf-8")
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"no_import": {"enabled": True}}},
            }
            settings = RuntimeSettings(
                timezone_name="UTC",
                store=StoreSettings(path="data/test.db", timezone_name="UTC", rotate_daily=False),
                watch=WatchSettings(
                    crawl_enabled=False,
                    use_unseen=False,
                    timings_per_topic=30,
                    notify_interval_secs=600,
                    notify_auto_mark_read=False,
                ),
                server=self._settings().server,
            )

            outcome = handle_status(app_config, settings)

        self.assertEqual(outcome.payload["plugins"]["enabled"], True)
        self.assertEqual(outcome.payload["plugins"]["runtime_live"], False)
        self.assertEqual(outcome.payload["plugins"]["items"][0]["plugin_id"], "no_import")
        self.assertEqual(outcome.payload["plugins"]["items"][0]["hook_successes"], None)

    def test_handle_status_rejects_plugin_with_invalid_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config").mkdir()
            plugin_dir = root / "plugins" / "bad_syntax"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "manifest.json").write_text(
                textwrap.dedent(
                    """
                    {
                      "id": "bad_syntax",
                      "name": "Bad Syntax",
                      "version": "0.1.0",
                      "api_version": 1,
                      "entry": "plugin.py",
                      "hooks": ["topics.fetched"],
                      "permissions": [],
                      "default_priority": 10,
                      "default_config": {}
                    }
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (plugin_dir / "plugin.py").write_text("def create_plugin(:\n    pass\n", encoding="utf-8")
            config_path = root / "config" / "app.json"
            config_path.write_text("{}", encoding="utf-8")
            app_config = {
                "_path": str(config_path),
                "plugins": {"items": {"bad_syntax": {"enabled": True}}},
            }
            settings = RuntimeSettings(
                timezone_name="UTC",
                store=StoreSettings(path="data/test.db", timezone_name="UTC", rotate_daily=False),
                watch=WatchSettings(
                    crawl_enabled=False,
                    use_unseen=False,
                    timings_per_topic=30,
                    notify_interval_secs=600,
                    notify_auto_mark_read=False,
                ),
                server=self._settings().server,
            )

            with self.assertRaisesRegex(ValueError, "invalid syntax"):
                handle_status(app_config, settings)

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
        services = types.SimpleNamespace(client=object(), base_client=object(), store=object(), plugin_manager=None)
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
            action_timeout_secs=60,
            on_action_success=serve_kwargs["on_action_success"],
        )

    def test_handle_authenticated_serve_returns_nonzero_on_auth_fatal(self) -> None:
        state = Mock()
        services = types.SimpleNamespace(client=object(), base_client=object(), store=object(), plugin_manager=None)
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
        services = types.SimpleNamespace(client=object(), base_client=object(), store=object(), plugin_manager=None)
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
