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

from discorsair.runtime.factory import build_client, build_notifier, build_services, load_runtime_app_config, load_settings, open_store, resolve_storage_path
from discorsair.runtime.settings import RuntimeSettings, ServerSettings, StoreSettings, WatchSettings
from discorsair.storage.postgres_store import PostgresStore
from discorsair.utils.config import derive_runtime_state_path


class _FakePostgresCursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params=()) -> None:
        self._conn.execute_calls.append((sql, params))
        if self._conn.aborted:
            raise RuntimeError("current transaction is aborted")
        if self._conn.fail_next_execute:
            self._conn.fail_next_execute = False
            self._conn.aborted = True
            raise RuntimeError("boom")

    def executemany(self, sql: str, params) -> None:
        self._conn.execute_calls.append((sql, params))
        if self._conn.aborted:
            raise RuntimeError("current transaction is aborted")
        if self._conn.fail_next_execute:
            self._conn.fail_next_execute = False
            self._conn.aborted = True
            raise RuntimeError("boom")

    def fetchone(self):
        return self._conn.fetchone_result

    def fetchall(self):
        return list(self._conn.fetchall_result)


class _FakePostgresConn:
    def __init__(self, *, fetchone_result=None, fetchall_result=None) -> None:
        self.fetchone_result = fetchone_result
        self.fetchall_result = fetchall_result or []
        self.fail_next_execute = False
        self.aborted = False
        self.read_only = False
        self.execute_calls: list[tuple[object, object]] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def cursor(self):
        return _FakePostgresCursor(self)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1
        self.aborted = False

    def close(self) -> None:
        self.closed = True


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
            store=StoreSettings(
                backend="sqlite",
                path="data/test.db",
                lock_dir="data/locks",
                timezone_name="UTC",
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
            ),
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

    def test_resolve_storage_path_uses_site_suffix_when_enabled(self) -> None:
        app_config = {
            "site": {"base_url": "https://meta.example.com/forum"},
            "storage": {"backend": "sqlite", "path": "data/discorsair.db", "auto_per_site": True},
        }
        self.assertEqual(
            resolve_storage_path(app_config),
            "data/discorsair.meta.example.com_forum.db",
        )

    def test_resolve_storage_path_returns_postgres_dsn_for_postgres_backend(self) -> None:
        app_config = {
            "site": {"base_url": "https://meta.example.com/forum"},
            "storage": {
                "backend": "postgres",
                "postgres": {"dsn": "postgresql://user:pass@localhost:5432/discorsair"},
            },
        }
        self.assertEqual(
            resolve_storage_path(app_config),
            "postgresql://user:pass@localhost:5432/discorsair",
        )

    def test_load_runtime_app_config_requires_postgres_dsn_when_backend_is_postgres(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "storage": {"backend": "postgres", "postgres": {"dsn": ""}}
}
"""

        with self.assertRaisesRegex(ValueError, "config.storage.postgres.dsn is required when backend=postgres"):
            self._load_runtime_app_config(config_text)

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

    def test_build_client_ignores_auth_status_when_account_not_disabled(self) -> None:
        app_config = {
            "site": {"base_url": "https://forum.example"},
            "auth": {
                "cookie": "_t=file-token",
                "status": "invalid",
                "disabled": False,
            },
        }

        client = build_client(app_config)

        self.assertEqual(client._requester._session.base_url, "https://forum.example")

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
        self.assertEqual(app_config["_state_path"], str(derive_runtime_state_path(config_path)))
        self.assertIn(("auth", "cookie"), app_config["_env_override_paths"])
        self.assertIn(("server", "api_key"), app_config["_env_override_paths"])

    def test_load_runtime_app_config_supports_postgres_dsn_env_override(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "storage": {
    "backend": "postgres",
    "postgres": {"dsn": ""}
  }
}
"""
        app_config, _ = self._load_runtime_app_config(
            config_text,
            env={"DISCORSAIR_POSTGRES_DSN": "postgresql://env-user:env-pass@127.0.0.1:5432/discorsair"},
        )

        self.assertEqual(
            app_config["storage"]["postgres"]["dsn"],
            "postgresql://env-user:env-pass@127.0.0.1:5432/discorsair",
        )
        self.assertIn(("storage", "postgres", "dsn"), app_config["_env_override_paths"])

    def test_load_runtime_app_config_prefers_state_file_over_app_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = derive_runtime_state_path(config_path)
            config_path.write_text(
                """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token", "status": "active", "disabled": false}
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            state_path.write_text(
                """
{
  "auth": {"cookie": "_t=state-token", "status": "invalid", "disabled": true}
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch("discorsair.runtime.factory.setup_logging"):
                app_config = load_runtime_app_config(str(config_path))

        self.assertEqual(app_config["auth"]["cookie"], "_t=state-token")
        self.assertEqual(app_config["auth"]["status"], "invalid")
        self.assertTrue(app_config["auth"]["disabled"])

    def test_load_runtime_app_config_treats_empty_state_file_as_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "app.json"
            state_path = derive_runtime_state_path(config_path)
            config_path.write_text(
                """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"}
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            state_path.write_text("", encoding="utf-8")
            with patch("discorsair.runtime.factory.setup_logging"):
                app_config = load_runtime_app_config(str(config_path))

        self.assertEqual(app_config["auth"]["cookie"], "_t=file-token")
        self.assertEqual(app_config["auth"]["status"], "active")
        self.assertFalse(app_config["auth"]["disabled"])

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

    def test_load_runtime_app_config_rejects_removed_queue_timeout_secs(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "queue": {"timeout_secs": 60}
}
"""

        with self.assertRaisesRegex(ValueError, "config.queue.timeout_secs has been removed"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_removed_storage_rotate_daily(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "storage": {"rotate_daily": true}
}
"""

        with self.assertRaisesRegex(ValueError, "config.storage.rotate_daily has been removed"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_object_site(self) -> None:
        config_text = """
{
  "site": "https://forum.example",
  "auth": {"cookie": "_t=file-token"}
}
"""

        with self.assertRaisesRegex(ValueError, "config.site must be an object"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_string_base_url(self) -> None:
        config_text = """
{
  "site": {"base_url": 123},
  "auth": {"cookie": "_t=file-token"}
}
"""

        with self.assertRaisesRegex(ValueError, "config.site.base_url must be a string"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_object_plugin_item(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "plugins": {
    "items": {
      "demo": "bad"
    }
  }
}
"""

        with self.assertRaisesRegex(ValueError, "config.plugins.items.demo must be an object"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_integer_plugin_priority(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "plugins": {
    "items": {
      "demo": {"enabled": true, "priority": "high"}
    }
  }
}
"""

        with self.assertRaisesRegex(ValueError, "config.plugins.items.demo.priority must be an integer"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_boolean_plugin_enabled(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "plugins": {
    "items": {
      "demo": {"enabled": "false"}
    }
  }
}
"""

        with self.assertRaisesRegex(ValueError, "config.plugins.items.demo.enabled must be a boolean"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_integer_server_max_posts(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "server": {"max_posts_per_interval": "10"}
}
"""

        with self.assertRaisesRegex(ValueError, "config.server.max_posts_per_interval must be an integer or null"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_string_schedule_items(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "server": {"schedule": [1]}
}
"""

        with self.assertRaisesRegex(ValueError, "config.server.schedule items must be strings"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_invalid_schedule_window_format(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "server": {"schedule": ["25:00-26:00"]}
}
"""

        with self.assertRaisesRegex(ValueError, "config.server.schedule\\[0\\] must use valid 24-hour times"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_string_server_host(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "server": {"host": 123}
}
"""

        with self.assertRaisesRegex(ValueError, "config.server.host must be a string"):
            self._load_runtime_app_config(config_text)

    def test_load_runtime_app_config_rejects_non_string_server_api_key(self) -> None:
        config_text = """
{
  "site": {"base_url": "https://forum.example"},
  "auth": {"cookie": "_t=file-token"},
  "server": {"api_key": 123}
}
"""

        with self.assertRaisesRegex(ValueError, "config.server.api_key must be a string"):
            self._load_runtime_app_config(config_text)

    def test_load_settings_builds_structured_values(self) -> None:
        app_config = {
            "site": {"base_url": "https://meta.example.com/forum"},
            "time": {"timezone": "UTC"},
            "auth": {"name": "main"},
            "storage": {"path": "data/discorsair.db", "auto_per_site": True},
            "crawl": {"enabled": False},
            "watch": {"use_unseen": True, "timings_per_topic": 9},
            "notify": {"interval_secs": 321, "auto_mark_read": True},
            "server": {
                "host": "127.0.0.1",
                "port": 9090,
                "schedule": ["08:00-10:00"],
                "api_key": "k",
                "action_timeout_secs": 0.5,
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
        self.assertEqual(settings.store.backend, "sqlite")
        self.assertEqual(settings.store.lock_dir, "data/locks")
        self.assertEqual(settings.store.timezone_name, "UTC")
        self.assertEqual(settings.store.site_key, "meta.example.com_forum")
        self.assertEqual(settings.store.account_name, "main")
        self.assertEqual(settings.store.base_url, "https://meta.example.com/forum")
        self.assertEqual(settings.watch.crawl_enabled, False)
        self.assertEqual(settings.watch.use_unseen, True)
        self.assertEqual(settings.watch.timings_per_topic, 9)
        self.assertEqual(settings.watch.notify_interval_secs, 321)
        self.assertEqual(settings.watch.notify_auto_mark_read, True)
        self.assertEqual(settings.server.port, 9090)
        self.assertEqual(settings.server.action_timeout_secs, 0.5)
        self.assertEqual(settings.server.max_posts_per_interval, 77)

    def test_load_settings_keeps_postgres_dsn_and_lock_dir(self) -> None:
        app_config = {
            "site": {"base_url": "https://meta.example.com/forum"},
            "time": {"timezone": "UTC"},
            "auth": {"name": "main"},
            "storage": {
                "backend": "postgres",
                "lock_dir": "/tmp/discorsair-locks",
                "postgres": {"dsn": "postgresql://user:pass@localhost:5432/discorsair"},
            },
            "crawl": {"enabled": True},
            "watch": {"use_unseen": False, "timings_per_topic": 9},
            "notify": {"interval_secs": 321, "auto_mark_read": False},
            "server": {
                "host": "127.0.0.1",
                "port": 9090,
                "schedule": [],
                "api_key": "k",
                "action_timeout_secs": 0.5,
                "interval_secs": 15,
                "max_posts_per_interval": 77,
                "auto_restart": False,
                "restart_backoff_secs": 22,
                "max_restarts": 3,
                "same_error_stop_threshold": 4,
            },
        }

        settings = load_settings(app_config)

        self.assertEqual(settings.store.backend, "postgres")
        self.assertEqual(settings.store.path, "postgresql://user:pass@localhost:5432/discorsair")
        self.assertEqual(settings.store.lock_dir, "/tmp/discorsair-locks")

    def test_build_services_closes_store_when_client_setup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app_config = {
                "_path": str(Path(tmpdir) / "config" / "app.json"),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {"cookie": "_t=1", "proxy": "", "disabled": False},
                "request": {"impersonate_target": "chrome110", "user_agent": "", "max_retries": 2, "min_interval_secs": 1},
                "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
                "queue": {"maxsize": 0},
            }
            settings = RuntimeSettings(
                timezone_name="UTC",
                store=StoreSettings(
                    backend="sqlite",
                    path=str(Path(tmpdir) / "discorsair.db"),
                    lock_dir=str(Path(tmpdir) / "locks"),
                    timezone_name="UTC",
                    site_key="forum.example",
                    account_name="main",
                    base_url="https://forum.example",
                ),
                watch=self._settings().watch,
                server=self._settings().server,
            )
            store = Mock()

            with patch("discorsair.runtime.factory.open_store", return_value=store):
                with patch("discorsair.runtime.factory.build_client", side_effect=RuntimeError("boom")):
                    with self.assertRaisesRegex(RuntimeError, "boom"):
                        build_services(app_config, settings)

        store.close.assert_called_once_with()

    def test_build_services_skips_store_when_crawl_disabled(self) -> None:
        app_config = {
            "site": {"base_url": "https://forum.example", "timeout_secs": 20},
            "auth": {"cookie": "_t=1", "proxy": "", "disabled": False},
            "request": {"impersonate_target": "chrome110", "user_agent": "", "max_retries": 2, "min_interval_secs": 1},
            "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
            "queue": {"maxsize": 0},
        }
        settings = RuntimeSettings(
            timezone_name="UTC",
            store=StoreSettings(
                backend="sqlite",
                path="data/test.db",
                lock_dir="data/locks",
                timezone_name="UTC",
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
            ),
            watch=WatchSettings(
                crawl_enabled=False,
                use_unseen=False,
                timings_per_topic=30,
                notify_interval_secs=600,
                notify_auto_mark_read=False,
            ),
            server=self._settings().server,
        )

        with patch("discorsair.runtime.factory.open_store") as open_store:
            services = build_services(app_config, settings)

        try:
            self.assertIsNone(services.store)
            open_store.assert_not_called()
        finally:
            services.close()

    def test_build_services_skips_store_and_lock_when_crawl_resources_not_requested(self) -> None:
        app_config = {
            "site": {"base_url": "https://forum.example", "timeout_secs": 20},
            "auth": {"cookie": "_t=1", "proxy": "", "disabled": False},
            "request": {"impersonate_target": "chrome110", "user_agent": "", "max_retries": 2, "min_interval_secs": 1},
            "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
            "queue": {"maxsize": 0},
        }

        with patch("discorsair.runtime.factory.open_store") as open_store:
            services = build_services(app_config, self._settings(), with_crawl_resources=False)

        try:
            self.assertIsNone(services.store)
            self.assertIsNone(services.crawl_lock)
            open_store.assert_not_called()
        finally:
            services.close()

    def test_build_services_skips_plugin_manager_when_not_requested(self) -> None:
        app_config = {
            "site": {"base_url": "https://forum.example", "timeout_secs": 20},
            "auth": {"cookie": "_t=1", "proxy": "", "disabled": False},
            "request": {"impersonate_target": "chrome110", "user_agent": "", "max_retries": 2, "min_interval_secs": 1},
            "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
            "queue": {"maxsize": 0},
            "plugins": {"items": {"demo": {"enabled": True}}},
        }

        with patch("discorsair.runtime.factory.PluginManager.from_app_config") as plugin_factory:
            services = build_services(app_config, self._settings(), with_crawl_resources=False, with_plugins=False)

        try:
            self.assertIsNone(services.plugin_manager)
            plugin_factory.assert_not_called()
        finally:
            services.close()

    def test_build_services_rejects_second_crawl_for_same_site_until_first_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "discorsair.db")
            app_config = {
                "_path": str(Path(tmpdir) / "config" / "app.json"),
                "site": {"base_url": "https://forum.example", "timeout_secs": 20},
                "auth": {"name": "main", "cookie": "_t=1", "proxy": "", "disabled": False},
                "request": {"impersonate_target": "chrome110", "user_agent": "", "max_retries": 2, "min_interval_secs": 1},
                "flaresolverr": {"enabled": False, "base_url": "", "request_timeout_secs": 60, "ua_probe_url": ""},
                "queue": {"maxsize": 0},
                "storage": {"path": db_path, "auto_per_site": False},
            }
            settings = RuntimeSettings(
                timezone_name="UTC",
                store=StoreSettings(
                    backend="sqlite",
                    path=db_path,
                    lock_dir=str(Path(tmpdir) / "locks"),
                    timezone_name="UTC",
                    site_key="forum.example",
                    account_name="main",
                    base_url="https://forum.example",
                ),
                watch=WatchSettings(
                    crawl_enabled=True,
                    use_unseen=False,
                    timings_per_topic=30,
                    notify_interval_secs=600,
                    notify_auto_mark_read=False,
                ),
                server=self._settings().server,
            )

            first = build_services(app_config, settings)
            try:
                with self.assertRaisesRegex(RuntimeError, "crawl already running for site=forum.example"):
                    build_services(app_config, settings)
            finally:
                first.close()

            second = build_services(app_config, settings)
            try:
                self.assertIsNotNone(second.crawl_lock)
            finally:
                second.close()

    def test_open_store_uses_postgres_backend_when_configured(self) -> None:
        settings = RuntimeSettings(
            timezone_name="UTC",
            store=StoreSettings(
                backend="postgres",
                path="postgresql://user:pass@localhost:5432/discorsair",
                lock_dir="data/locks",
                timezone_name="UTC",
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
            ),
            watch=WatchSettings(
                crawl_enabled=True,
                use_unseen=False,
                timings_per_topic=30,
                notify_interval_secs=600,
                notify_auto_mark_read=False,
            ),
            server=self._settings().server,
        )

        with patch("discorsair.runtime.factory.PostgresStore", return_value=Mock()) as pg_cls:
            result = open_store(settings)

        self.assertIs(result, pg_cls.return_value)
        pg_cls.assert_called_once()

    def test_open_store_passes_read_only_flags_to_postgres_backend(self) -> None:
        settings = RuntimeSettings(
            timezone_name="UTC",
            store=StoreSettings(
                backend="postgres",
                path="postgresql://user:pass@localhost:5432/discorsair",
                lock_dir="data/locks",
                timezone_name="UTC",
                site_key="forum.example",
                account_name="main",
                base_url="https://forum.example",
            ),
            watch=WatchSettings(
                crawl_enabled=True,
                use_unseen=False,
                timings_per_topic=30,
                notify_interval_secs=600,
                notify_auto_mark_read=False,
            ),
            server=self._settings().server,
        )

        with patch("discorsair.runtime.factory.PostgresStore", return_value=Mock()) as pg_cls:
            open_store(settings, initialize=False, ensure_metadata=False, read_only=True)

        pg_cls.assert_called_once_with(
            "postgresql://user:pass@localhost:5432/discorsair",
            site_key="forum.example",
            account_name="main",
            base_url="https://forum.example",
            timezone_name="UTC",
            initialize=False,
            ensure_metadata=False,
            read_only=True,
        )

    def test_postgres_store_rolls_back_failed_query_before_reuse(self) -> None:
        store = object.__new__(PostgresStore)
        store._conn = _FakePostgresConn(fetchone_result=(1, 2, 3, 4))
        store._site_key = "forum.example"
        store._account_name = "main"
        store._conn.fail_next_execute = True

        with self.assertRaisesRegex(RuntimeError, "boom"):
            store.get_stats_total()

        self.assertEqual(store._conn.rollback_calls, 1)
        self.assertEqual(
            store.get_stats_total(),
            {
                "topics_seen": 1,
                "posts_fetched": 2,
                "timings_sent": 3,
                "notifications_sent": 4,
            },
        )

    def test_postgres_store_rolls_back_failed_write_before_reuse(self) -> None:
        store = object.__new__(PostgresStore)
        store._conn = _FakePostgresConn()
        store._conn.fail_next_execute = True

        with self.assertRaisesRegex(RuntimeError, "boom"):
            store._execute("UPDATE stats_total SET topics_seen = topics_seen + %s", (1,))

        self.assertEqual(store._conn.rollback_calls, 1)
        store._execute("UPDATE stats_total SET topics_seen = topics_seen + %s", (1,))
        self.assertEqual(store._conn.commit_calls, 1)

    def test_postgres_store_sets_session_read_only_when_requested(self) -> None:
        conn = _FakePostgresConn()
        fake_psycopg = types.SimpleNamespace(connect=lambda dsn: conn)

        with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            with patch("discorsair.storage.postgres_store.postgres_schema_exists", return_value=True):
                with patch("discorsair.storage.postgres_store.assert_postgres_schema"):
                    store = PostgresStore(
                        "postgresql://user:pass@localhost:5432/discorsair",
                        site_key="forum.example",
                        account_name="main",
                        base_url="https://forum.example",
                        timezone_name="UTC",
                        initialize=False,
                        ensure_metadata=False,
                        read_only=True,
                    )

        try:
            self.assertTrue(conn.read_only)
        finally:
            store.close()

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
