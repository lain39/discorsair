"""NDJSON export/import helpers for storage backends."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from discorsair.storage.postgres_store import initialize_postgres_schema
from discorsair.storage.postgres_store import assert_postgres_schema
from discorsair.storage.sqlite_store import assert_sqlite_schema
from discorsair.storage.sqlite_store import initialize_sqlite_schema


_DSN_PASSWORD_RE = re.compile(r":([^:@/]+)@")
_EXPORT_FORMAT = "discorsair-ndjson-v1"


@dataclass(frozen=True)
class TableSpec:
    name: str
    export_columns: tuple[str, ...]
    filter_columns: tuple[str, ...] = ()
    import_columns: tuple[str, ...] | None = None
    conflict_columns: tuple[str, ...] | None = None
    mode: str = "upsert"
    dedupe_columns: tuple[str, ...] = ()


_TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("sites", ("site_key", "base_url", "created_at", "updated_at"), filter_columns=("site_key",), conflict_columns=("site_key",)),
    TableSpec(
        "accounts",
        ("site_key", "account_name", "created_at", "updated_at"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name"),
    ),
    TableSpec(
        "topic_crawl_state",
        ("site_key", "topic_id", "last_synced_post_number", "last_stream_len", "updated_at"),
        filter_columns=("site_key",),
        conflict_columns=("site_key", "topic_id"),
    ),
    TableSpec(
        "topics",
        (
            "site_key",
            "topic_id",
            "category_id",
            "title",
            "slug",
            "tags_json",
            "reply_count",
            "views",
            "like_count",
            "highest_post_number",
            "unseen",
            "last_read_post_number",
            "created_at",
            "bumped_at",
            "last_posted_at",
            "first_post_updated_at",
            "first_seen_at",
            "synced_at",
        ),
        filter_columns=("site_key",),
        conflict_columns=("site_key", "topic_id"),
    ),
    TableSpec(
        "topic_snapshots",
        (
            "site_key",
            "topic_id",
            "captured_at",
            "first_post_updated_at",
            "title",
            "category_id",
            "tags_json",
            "raw_json",
        ),
        filter_columns=("site_key",),
        conflict_columns=("site_key", "topic_id", "captured_at"),
        mode="ignore",
    ),
    TableSpec(
        "posts",
        (
            "site_key",
            "post_id",
            "topic_id",
            "post_number",
            "reply_to_post_number",
            "username",
            "created_at",
            "updated_at",
            "fetched_at",
            "like_count",
            "reply_count",
            "reads",
            "score",
            "incoming_link_count",
            "current_user_reaction",
            "cooked",
            "raw_json",
        ),
        filter_columns=("site_key",),
        conflict_columns=("site_key", "post_id"),
    ),
    TableSpec(
        "notification_dedupe",
        ("site_key", "account_name", "notification_id", "created_at"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name", "notification_id"),
        mode="ignore",
    ),
    TableSpec(
        "plugin_daily_counters",
        ("site_key", "account_name", "plugin_id", "action", "day", "count"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name", "plugin_id", "action", "day"),
    ),
    TableSpec(
        "plugin_once_marks",
        ("site_key", "account_name", "plugin_id", "key", "created_at"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name", "plugin_id", "key"),
        mode="ignore",
    ),
    TableSpec(
        "plugin_kv",
        ("site_key", "account_name", "plugin_id", "key", "value_json", "updated_at"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name", "plugin_id", "key"),
    ),
    TableSpec(
        "watch_cycles",
        (
            "cycle_id",
            "site_key",
            "account_name",
            "started_at",
            "ended_at",
            "topics_fetched",
            "topics_entered",
            "posts_fetched",
            "notifications_sent",
            "success",
            "error_text",
        ),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("cycle_id",),
    ),
    TableSpec(
        "plugin_action_logs",
        (
            "id",
            "cycle_id",
            "site_key",
            "account_name",
            "plugin_id",
            "hook_name",
            "action",
            "topic_id",
            "post_id",
            "status",
            "reason",
            "created_at",
            "extra_json",
        ),
        filter_columns=("site_key", "account_name"),
        import_columns=(
            "cycle_id",
            "site_key",
            "account_name",
            "plugin_id",
            "hook_name",
            "action",
            "topic_id",
            "post_id",
            "status",
            "reason",
            "created_at",
            "extra_json",
        ),
        mode="dedupe_insert",
        dedupe_columns=(
            "cycle_id",
            "site_key",
            "account_name",
            "plugin_id",
            "hook_name",
            "action",
            "topic_id",
            "post_id",
            "status",
            "reason",
            "created_at",
            "extra_json",
        ),
    ),
    TableSpec(
        "stats_total",
        ("site_key", "account_name", "topics_seen", "posts_fetched", "timings_sent", "notifications_sent"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name"),
    ),
    TableSpec(
        "stats_daily",
        ("site_key", "account_name", "day", "topics_seen", "posts_fetched", "timings_sent", "notifications_sent"),
        filter_columns=("site_key", "account_name"),
        conflict_columns=("site_key", "account_name", "day"),
    ),
)


def export_backend(
    *,
    backend: str,
    path: str,
    output_dir: str | Path,
    site_key: str,
    account_name: str,
) -> dict[str, Any]:
    out_dir = Path(output_dir)
    if out_dir.exists() and not out_dir.is_dir():
        raise ValueError(f"export output must be a directory: {out_dir}")
    if backend == "sqlite" and not Path(path).exists():
        raise ValueError(f"sqlite database not found: {path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    scope = {"site_key": site_key, "account_name": account_name}
    if backend == "sqlite":
        _assert_sqlite_schema(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN")
    else:
        conn = _postgres_connect(path)
        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        counts: dict[str, int] = {}
        for spec in _TABLE_SPECS:
            rows = _fetch_rows_with_conn(conn=conn, backend=backend, spec=spec, scope=scope)
            counts[spec.name] = len(rows)
            _write_ndjson(out_dir / f"{spec.name}.ndjson", rows)
    finally:
        if backend == "sqlite":
            conn.rollback()
        else:
            conn.rollback()
        conn.close()

    meta = {
        "format": _EXPORT_FORMAT,
        "exported_at": _utc_now_iso(),
        "source_backend": backend,
        "source_path": _display_storage_path(backend, path),
        "source_site_key": site_key,
        "source_account_name": account_name,
        "tables": counts,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "action": "export",
        "format": _EXPORT_FORMAT,
        "backend": backend,
        "storage_path": _display_storage_path(backend, path),
        "output_dir": str(out_dir),
        "site_key": site_key,
        "account_name": account_name,
        "tables": counts,
    }


def import_backend(*, backend: str, path: str, input_dir: str | Path) -> dict[str, Any]:
    in_dir, meta = validate_import_bundle(input_dir)
    scope = {
        "site_key": str(meta.get("source_site_key", "") or ""),
        "account_name": str(meta.get("source_account_name", "") or ""),
    }

    if backend == "sqlite":
        conn, final_path, temp_path = _open_sqlite_import_connection(path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        initialize_sqlite_schema(conn)
        assert_sqlite_schema(conn)
    else:
        conn = _postgres_connect(path)
        final_path = None
        temp_path = None
        with conn.cursor() as cur:
            cur.execute("BEGIN")
        initialize_postgres_schema(conn)
        assert_postgres_schema(conn)
    try:
        counts: dict[str, int] = {}
        if backend == "sqlite":
            for spec in _TABLE_SPECS:
                rows = _read_ndjson(in_dir / f"{spec.name}.ndjson")
                _validate_rows_against_scope(spec, rows, scope)
                _import_rows_with_executor(
                    spec=spec,
                    rows=rows,
                    execute=lambda sql, params=(): conn.execute(sql, params),
                    executemany=lambda sql, params: conn.executemany(sql, params),
                    paramstyle="qmark",
                )
                counts[spec.name] = len(rows)
        else:
            with conn.cursor() as cur:
                for spec in _TABLE_SPECS:
                    rows = _read_ndjson(in_dir / f"{spec.name}.ndjson")
                    _validate_rows_against_scope(spec, rows, scope)
                    _import_rows_with_executor(
                        spec=spec,
                        rows=rows,
                        execute=lambda sql, params=(), cur=cur: _cursor_execute(cur, sql, params),
                        executemany=lambda sql, params, cur=cur: _cursor_executemany(cur, sql, params),
                        paramstyle="format",
                    )
                    counts[spec.name] = len(rows)
        conn.commit()
    except Exception:
        conn.rollback()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    if temp_path is not None and final_path is not None:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(final_path)

    return {
        "ok": True,
        "action": "import",
        "format": _EXPORT_FORMAT,
        "backend": backend,
        "storage_path": _display_storage_path(backend, path),
        "input_dir": str(in_dir),
        "tables": counts,
    }


def validate_import_bundle(
    input_dir: str | Path,
    *,
    expected_site_key: str | None = None,
    expected_account_name: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    in_dir = Path(input_dir)
    if not in_dir.exists() or not in_dir.is_dir():
        raise ValueError(f"import input directory not found: {in_dir}")

    meta_path = in_dir / "meta.json"
    if not meta_path.exists():
        raise ValueError("import input missing meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or meta.get("format") != _EXPORT_FORMAT:
        raise ValueError("unsupported import format")
    source_site_key = str(meta.get("source_site_key", "") or "")
    source_account_name = str(meta.get("source_account_name", "") or "")
    if not source_site_key:
        raise ValueError("import meta missing source_site_key")
    if not source_account_name:
        raise ValueError("import meta missing source_account_name")
    if expected_site_key is not None and source_site_key != str(expected_site_key):
        raise ValueError(
            f"import site mismatch: export is for site={source_site_key}, current config is site={expected_site_key}"
        )
    if expected_account_name is not None and source_account_name != str(expected_account_name):
        raise ValueError(
            "import account mismatch: "
            f"export is for account={source_account_name}, current config is account={expected_account_name}"
        )
    for spec in _TABLE_SPECS:
        if not (in_dir / f"{spec.name}.ndjson").exists():
            raise ValueError(f"import input missing table file: {spec.name}.ndjson")
    return in_dir, meta


def _fetch_rows_with_conn(*, conn, backend: str, spec: TableSpec, scope: dict[str, str]) -> list[dict[str, Any]]:
    sql = f"SELECT {', '.join(spec.export_columns)} FROM {spec.name}"
    params: list[Any] = []
    if spec.filter_columns:
        placeholder = "?" if backend == "sqlite" else "%s"
        sql += " WHERE " + " AND ".join(f"{column} = {placeholder}" for column in spec.filter_columns)
        params.extend(scope[column] for column in spec.filter_columns)
    if backend == "sqlite":
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [dict(zip(spec.export_columns, row, strict=False)) for row in rows]


def _validate_rows_against_scope(spec: TableSpec, rows: list[dict[str, Any]], scope: dict[str, str]) -> None:
    if not spec.filter_columns:
        return
    for row in rows:
        for column in spec.filter_columns:
            if str(row.get(column, "") or "") != scope[column]:
                raise ValueError(
                    f"import row scope mismatch in {spec.name}: expected {column}={scope[column]!r}"
                )


def _import_rows_with_executor(*, spec: TableSpec, rows: list[dict[str, Any]], execute, executemany, paramstyle: str) -> None:
    import_columns = spec.import_columns or spec.export_columns
    if spec.mode == "dedupe_insert":
        insert_sql = _build_insert_sql(spec.name, import_columns, paramstyle)
        for row in rows:
            insert_values = tuple(row.get(column) for column in import_columns)
            if _row_exists(
                spec.name,
                spec.dedupe_columns,
                tuple(row.get(column) for column in spec.dedupe_columns),
                execute=execute,
                paramstyle=paramstyle,
            ):
                continue
            execute(insert_sql, insert_values)
        return
    sql = _build_mutation_sql(spec=spec, paramstyle=paramstyle)
    executemany(sql, [tuple(row.get(column) for column in import_columns) for row in rows])


def _row_exists(table: str, columns: tuple[str, ...], values: tuple[Any, ...], *, execute, paramstyle: str) -> bool:
    where_parts: list[str] = []
    params: list[Any] = []
    placeholder = _placeholder(paramstyle)
    for column, value in zip(columns, values, strict=False):
        if value is None:
            where_parts.append(f"{column} IS NULL")
        else:
            where_parts.append(f"{column} = {placeholder}")
            params.append(value)
    sql = f"SELECT 1 FROM {table} WHERE {' AND '.join(where_parts)} LIMIT 1"
    return execute(sql, tuple(params)).fetchone() is not None


def _build_mutation_sql(*, spec: TableSpec, paramstyle: str) -> str:
    columns = spec.import_columns or spec.export_columns
    insert_sql = _build_insert_sql(spec.name, columns, paramstyle)
    if spec.mode == "ignore":
        if spec.conflict_columns is None:
            raise ValueError(f"ignore mode requires conflict columns: {spec.name}")
        return f"{insert_sql} ON CONFLICT({', '.join(spec.conflict_columns)}) DO NOTHING"
    if spec.mode != "upsert":
        raise ValueError(f"unsupported import mode: {spec.mode}")
    if spec.conflict_columns is None:
        raise ValueError(f"upsert mode requires conflict columns: {spec.name}")
    update_columns = [column for column in columns if column not in spec.conflict_columns]
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
    return f"{insert_sql} ON CONFLICT({', '.join(spec.conflict_columns)}) DO UPDATE SET {assignments}"


def _build_insert_sql(table: str, columns: tuple[str, ...], paramstyle: str) -> str:
    placeholders = ", ".join(_placeholder(paramstyle) for _ in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        row = json.loads(text)
        if not isinstance(row, dict):
            raise ValueError(f"ndjson row must be an object: {path}")
        rows.append(row)
    return rows


def _write_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _placeholder(paramstyle: str) -> str:
    if paramstyle == "qmark":
        return "?"
    if paramstyle == "format":
        return "%s"
    raise ValueError(f"unsupported paramstyle: {paramstyle}")


def _display_storage_path(backend: str, path: str) -> str:
    if backend == "postgres":
        return _DSN_PASSWORD_RE.sub(":***@", str(path or ""))
    return str(path or "")


def _postgres_connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("postgres backend requires psycopg; install it before use") from exc
    return psycopg.connect(dsn)


def _cursor_execute(cur, sql: str, params: tuple[Any, ...] = ()):
    cur.execute(sql, params)
    return cur


def _cursor_executemany(cur, sql: str, params: list[tuple[Any, ...]]):
    cur.executemany(sql, params)
    return cur


def _assert_sqlite_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        assert_sqlite_schema(conn)
    finally:
        conn.close()


def _open_sqlite_import_connection(path: str) -> tuple[sqlite3.Connection, Path, Path | None]:
    final_path = Path(path)
    temp_path: Path | None = None
    connect_path = final_path
    if not final_path.exists():
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = final_path.with_name(f".{final_path.name}.importing")
        temp_path.unlink(missing_ok=True)
        connect_path = temp_path
    conn = sqlite3.connect(connect_path)
    return conn, final_path, temp_path


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
