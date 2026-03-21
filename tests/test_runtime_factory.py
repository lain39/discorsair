"""Runtime factory tests."""

from __future__ import annotations

import subprocess
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

from discorsair.runtime.factory import build_client, build_notifier, build_services, load_runtime_app_config, load_settings, resolve_storage_path
from discorsair.runtime.settings import RuntimeSettings, ServerSettings, StoreSettings, WatchSettings


class RuntimeFactoryTests(unittest.TestCase):
    def _load_runtime_app_config(
        self,
        config_text: str,
        *,
        env: dict[str, str] | None = None,
    ) -> tuple[dict[str, object], Path]:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            config_path.write_text(config_text, encoding="utf-8")
            with patch.dict("os.environ", env or {}, clear=False):
                with patch("discorsair.runtime.factory.setup_logging"):
                    app_config = load_runtime_app_config(str(config_path))
            return app_config, config_path

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

    def test_resolve_storage_path_uses_site_suffix_when_enabled(self) -> None:
        app_config = {
            "site": {"base_url": "https://meta.example.com/forum"},
            "storage": {"path": "data/discorsair.db", "auto_per_site": True},
        }
        self.assertEqual(
            resolve_storage_path(app_config),
            "data/discorsair.meta.example.com_forum.db",
        )

    def test_build_notifier_prefixes_account_name(self) -> None:
        app_config = {
            "auth": {"name": "main"},
            "notify": {
                "enabled": True,
                "url": "https://notify.example",
                "chat_id": "123",
                "prefix": "[Discorsair]",
                "error_prefix": "[Discorsair][error]",
                "headers": {"Content-Type": "application/json"},
                "timeout_secs": 10,
            },
        }
        notifier = build_notifier(app_config)
        self.assertIsNotNone(notifier)
        self.assertEqual(notifier._prefix, "[Discorsair][main]")
        self.assertEqual(notifier._error_prefix, "[Discorsair][error][main]")

    def test_load_runtime_app_config_supports_auth_env_overrides(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"name": "file-name", "cookie": ""},
  "server": {"api_key": ""}
}
"""
        app_config, config_path = self._load_runtime_app_config(
            config_text,
            env={
                "DISCORSAIR_AUTH_NAME": "env-name",
                "DISCORSAIR_AUTH_COOKIE": "_t=env-token",
                "DISCORSAIR_AUTH_KEY": "env-key",
                "DISCORSAIR_NOTIFY_URL": "https://env-notify.example",
            },
        )

        self.assertEqual(app_config["auth"]["name"], "env-name")
        self.assertEqual(app_config["auth"]["cookie"], "_t=env-token")
        self.assertEqual(app_config["server"]["api_key"], "env-key")
        self.assertEqual(app_config["notify"]["url"], "https://env-notify.example")
        self.assertEqual(app_config["_path"], str(config_path))

    def test_build_notifier_uses_env_overridden_account_name(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"name": "file-name", "cookie": "_t=file-token"},
  "notify": {
    "enabled": true,
    "url": "https://notify.example",
    "chat_id": "123"
  }
}
"""
        app_config, _ = self._load_runtime_app_config(
            config_text,
            env={"DISCORSAIR_AUTH_NAME": "env-name"},
        )

        notifier = build_notifier(app_config)
        self.assertIsNotNone(notifier)
        self.assertEqual(notifier._prefix, "[Discorsair][env-name]")

    def test_build_notifier_uses_env_overridden_notify_url(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"name": "file-name", "cookie": "_t=file-token"},
  "notify": {
    "enabled": true,
    "url": "https://file-notify.example",
    "chat_id": "123"
  }
}
"""
        app_config, _ = self._load_runtime_app_config(
            config_text,
            env={"DISCORSAIR_NOTIFY_URL": "https://env-notify.example"},
        )

        notifier = build_notifier(app_config)
        self.assertIsNotNone(notifier)
        self.assertEqual(notifier._url, "https://env-notify.example")

    def test_load_settings_builds_structured_values(self) -> None:
        app_config = {
            "site": {"base_url": "https://meta.example.com/forum"},
            "time": {"timezone": "UTC"},
            "storage": {"path": "data/discorsair.db", "auto_per_site": True, "rotate_daily": True},
            "crawl": {"enabled": False},
            "watch": {"use_unseen": True, "timings_per_topic": 9},
            "notify": {"interval_secs": 321},
            "server": {
                "host": "127.0.0.1",
                "port": 9090,
                "schedule": ["08:00-10:00"],
                "api_key": "k",
                "interval_secs": 15,
                "max_posts_per_interval": 77,
                "auto_restart": False,
                "restart_backoff_secs": 22,
                "max_restarts": 3,
                "same_error_stop_threshold": 4,
            },
        }
        settings = load_settings(app_config)
        self.assertEqual(settings.store.path, "data/discorsair.meta.example.com_forum.db")
        self.assertEqual(settings.store.timezone_name, "UTC")
        self.assertEqual(settings.store.rotate_daily, True)
        self.assertEqual(settings.watch.crawl_enabled, False)
        self.assertEqual(settings.watch.use_unseen, True)
        self.assertEqual(settings.watch.timings_per_topic, 9)
        self.assertEqual(settings.watch.notify_interval_secs, 321)
        self.assertEqual(settings.server.port, 9090)
        self.assertEqual(settings.server.max_posts_per_interval, 77)

    def test_build_services_closes_store_when_client_setup_fails(self) -> None:
        app_config = {
            "site": {"base_url": "https://forum.example", "timeout_secs": 20},
            "auth": {"cookie": "_t=1", "proxy": "", "disabled": False},
            "request": {"impersonate_target": "chrome110", "user_agent": "", "max_retries": 2, "min_interval_secs": 1},
            "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
            "queue": {"maxsize": 0, "timeout_secs": 60},
        }
        store = Mock()

        with patch("discorsair.runtime.factory.open_store", return_value=store):
            with patch("discorsair.runtime.factory.build_client", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    build_services(app_config, self._settings())

        store.close.assert_called_once_with()

    def test_build_client_treats_missing_impersonate_target_as_empty(self) -> None:
        app_config = {
            "site": {"base_url": "https://forum.example", "timeout_secs": 20},
            "auth": {"cookie": "_t=1", "proxy": "", "disabled": False},
            "request": {"user_agent": "", "max_retries": 2, "min_interval_secs": 1},
            "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
        }

        client = build_client(app_config)

        self.assertEqual(client._requester._session.impersonate_target, "")

    def test_runtime_factory_import_does_not_eagerly_load_command_stack(self) -> None:
        script = """
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path('src').resolve()))
fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault('curl_cffi', types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault('curl_cffi.requests', fake_requests)
sys.modules.setdefault('curl_cffi.requests.exceptions', fake_requests_exceptions)

before = set(sys.modules)
import discorsair.runtime.factory  # noqa: F401
loaded = sorted(name for name in set(sys.modules) - before if name.startswith('discorsair.runtime'))
print('\\n'.join(loaded))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        loaded = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        self.assertIn("discorsair.runtime", loaded)
        self.assertIn("discorsair.runtime.factory", loaded)
        self.assertNotIn("discorsair.runtime.commands", loaded)
        self.assertNotIn("discorsair.runtime.runner", loaded)


if __name__ == "__main__":
    unittest.main()
