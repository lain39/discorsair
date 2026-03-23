"""Runtime state persistence helpers."""

from __future__ import annotations

import copy
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from discorsair.core.cookies import cookies_to_header, parse_cookie_header
from discorsair.discourse.client import DiscourseClient
from discorsair.utils.config import derive_runtime_state_path, load_raw_runtime_state

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

_CONFIG_PATH_LOCKS: dict[str, threading.Lock] = {}
_CONFIG_PATH_LOCKS_GUARD = threading.Lock()
_MANAGED_AUTH_PATHS: tuple[tuple[str, str], ...] = (
    ("auth", "cookie"),
    ("auth", "last_ok"),
    ("auth", "last_fail"),
    ("auth", "last_error"),
    ("auth", "status"),
    ("auth", "disabled"),
)


class RuntimeStateStore:
    def __init__(self, app_config: dict[str, Any]) -> None:
        self._app_config = app_config
        env_paths = app_config.get("_env_override_paths", [])
        self._env_override_paths = {
            tuple(path)
            for path in env_paths
            if isinstance(path, (list, tuple)) and len(path) == 2
        }
        self._lock = threading.RLock()
        state_path = app_config.get("_state_path") or derive_runtime_state_path(app_config.get("_path", "app.json"))
        self._state_path = str(state_path)

    @property
    def app_config(self) -> dict[str, Any]:
        return self._app_config

    def mark_account_ok(self) -> None:
        with self._lock:
            auth = self._app_config.get("auth", {})
            auth["last_ok"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self._save_runtime_state({("auth", "last_ok")})

    def mark_account_fail(self, exc: Exception, *, mark_invalid: bool, disable: bool) -> None:
        with self._lock:
            auth = self._app_config.get("auth", {})
            auth["last_fail"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            auth["last_error"] = str(exc)
            changed_paths: set[tuple[str, str]] = {
                ("auth", "last_fail"),
                ("auth", "last_error"),
            }
            if mark_invalid:
                auth["status"] = "invalid"
                changed_paths.add(("auth", "status"))
            if disable:
                auth["disabled"] = True
                changed_paths.add(("auth", "disabled"))
            self._save_runtime_state(changed_paths)

    def save_cookies(self, client: DiscourseClient) -> None:
        if client.last_response_ok() is not True:
            return
        cookie_header = _persistent_cookie_header(client.get_cookie_header())
        if not cookie_header:
            return
        with self._lock:
            auth = self._app_config.get("auth", {})
            if auth.get("cookie") == cookie_header:
                return
            auth["cookie"] = cookie_header
            self._save_runtime_state({("auth", "cookie")})

    def _save_runtime_state(self, updated_paths: set[tuple[str, str]]) -> None:
        state_path = self._state_path
        try:
            path = Path(state_path)
            with _lock_for_config_path(path):
                with _lock_config_file(path):
                    base_payload, initialize_all = self._load_runtime_state_base(path)
                    payload = self._build_runtime_state_payload(base_payload, updated_paths, initialize_all=initialize_all)
                    _write_json_atomically(path, payload)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("failed to save runtime state: %s", exc)

    def _load_runtime_state_base(self, state_path: Path) -> tuple[dict[str, Any], bool]:
        try:
            payload = load_raw_runtime_state(state_path)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("failed to reload runtime state, overwriting with current values: %s", exc)
            return {}, True
        return payload, not isinstance(payload.get("auth"), dict)

    def _build_runtime_state_payload(
        self,
        base_payload: dict[str, Any],
        updated_paths: set[tuple[str, str]],
        *,
        initialize_all: bool,
    ) -> dict[str, Any]:
        payload = copy.deepcopy(base_payload)
        for path in self._env_override_paths:
            _delete_nested_value(payload, path)
        paths_to_write = updated_paths
        if initialize_all:
            paths_to_write = set(_MANAGED_AUTH_PATHS)
        for path in paths_to_write:
            if path in self._env_override_paths:
                continue
            exists, value = _get_nested_value(self._app_config, path)
            if not exists:
                continue
            _set_nested_value(payload, path, value)
        return payload


def _persistent_cookie_header(cookie_header: str) -> str:
    cookies = parse_cookie_header(cookie_header.strip())
    token = cookies.get("_t", "").strip()
    if not token:
        return ""
    return cookies_to_header({"_t": token})


def _lock_for_config_path(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _CONFIG_PATH_LOCKS_GUARD:
        lock = _CONFIG_PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CONFIG_PATH_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _lock_config_file(path: Path) -> Iterator[None]:
    parent = path.parent if str(path.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{path.name}.lock"
    with lock_path.open("a+b") as handle:
        _acquire_file_lock(handle)
        try:
            yield
        finally:
            _release_file_lock(handle)


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_text_atomically(path, text)


def _write_text_atomically(path: Path, text: str) -> None:
    parent = path.parent if str(path.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    target_mode = None
    if path.exists():
        target_mode = path.stat().st_mode & 0o777
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if target_mode is not None:
            os.chmod(tmp_name, target_mode)
        os.replace(tmp_name, path)
        _fsync_directory(parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _acquire_file_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:
        _acquire_windows_file_lock(handle)
        return
    raise RuntimeError("no supported file locking backend")


def _release_file_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _acquire_windows_file_lock(handle) -> None:
    handle.seek(0)
    if handle.read(1) == b"":
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    deadline = time.monotonic() + 30.0
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for config file lock")
            time.sleep(0.05)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _get_nested_value(config: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set_nested_value(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current: dict[str, Any] = config
    for key in path[:-1]:
        node = current.setdefault(key, {})
        if not isinstance(node, dict):
            return
        current = node
    current[path[-1]] = copy.deepcopy(value)


def _delete_nested_value(config: dict[str, Any], path: tuple[str, ...]) -> None:
    current: Any = config
    parents: list[tuple[dict[str, Any], str]] = []
    for key in path[:-1]:
        if not isinstance(current, dict):
            return
        node = current.get(key)
        if not isinstance(node, dict):
            return
        parents.append((current, key))
        current = node
    if not isinstance(current, dict):
        return
    current.pop(path[-1], None)
    for parent, key in reversed(parents):
        node = parent.get(key)
        if not isinstance(node, dict) or node:
            break
        parent.pop(key, None)
