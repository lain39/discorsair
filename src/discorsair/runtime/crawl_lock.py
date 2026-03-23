"""Site crawl lock helpers."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

_LOCK_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


@dataclass
class CrawlSiteLock:
    path: Path
    _local_lock: threading.Lock
    _handle: Any
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            _release_file_lock(self._handle)
        finally:
            try:
                self._handle.close()
            finally:
                self._local_lock.release()


def acquire_site_crawl_lock(
    lock_dir: str | Path,
    *,
    site_key: str,
    account_name: str,
    config_path: str,
) -> CrawlSiteLock:
    lock_path = _lock_path_for_site(lock_dir, site_key)
    local_lock = _local_lock_for_path(lock_path)
    if not local_lock.acquire(blocking=False):
        raise RuntimeError(f"crawl already running for site={site_key} in current process")
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if not _try_acquire_file_lock(handle):
            holder = _read_lock_payload(handle)
            holder_desc = _format_holder(holder)
            raise RuntimeError(f"crawl already running for site={site_key}{holder_desc}")
        payload = {
            "site_key": str(site_key or ""),
            "account_name": str(account_name or ""),
            "config_path": str(config_path or ""),
            "pid": os.getpid(),
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_lock_payload(handle, payload)
        return CrawlSiteLock(path=lock_path, _local_lock=local_lock, _handle=handle)
    except Exception:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        local_lock.release()
        raise


def _lock_path_for_site(lock_dir: str | Path, site_key: str) -> Path:
    root = Path(lock_dir)
    return root / f"{site_key}.crawl.lock"


def _local_lock_for_path(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCK_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _read_lock_payload(handle) -> dict[str, Any]:
    try:
        handle.seek(0)
        raw = handle.read().decode("utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_lock_payload(handle, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    handle.seek(0)
    handle.truncate()
    handle.write(text.encode("utf-8"))
    handle.flush()
    os.fsync(handle.fileno())


def _format_holder(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    account = str(payload.get("account_name", "") or "")
    config_path = str(payload.get("config_path", "") or "")
    pid = payload.get("pid")
    parts = []
    if account:
        parts.append(f"account={account}")
    if config_path:
        parts.append(f"config={config_path}")
    if pid:
        parts.append(f"pid={pid}")
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


def _try_acquire_file_lock(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    if msvcrt is not None:  # pragma: no cover
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise RuntimeError("no supported file locking backend")


def _release_file_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    raise RuntimeError("no supported file locking backend")
