"""Database access for the Ostrovok mock.

Read-only sources (each is the committed database UNION its runtime overlay):
- catalog:  env OSTROVOK_CATALOG_DB (default data/ostrovok_catalog.sqlite)
            + env OSTROVOK_CATALOG_EXTRA_DB (default runtime/catalog_extra.sqlite) when present;
- opendata: env OPENDATA_DB (default data/opendata.sqlite)
            + env OPENDATA_EXTRA_DB (default runtime/opendata_extra.sqlite) when present.
Both are attached with `mode=ro`; rows of the committed database win on key collisions.

Runtime state (carts, orders, live insights): env OSTROVOK_STATE_DB (default
runtime/ostrovok_state.sqlite), created from data/schema/ostrovok_state.sql on first use.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
STATE_SCHEMA = ROOT / "data" / "schema" / "ostrovok_state.sql"


class ServiceError(Exception):
    """A problem the caller can fix (bad id, unknown entity, wrong state). `code` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _env_path(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else ROOT / default


def catalog_paths() -> list[Path]:
    return [_env_path("OSTROVOK_CATALOG_DB", "data/ostrovok_catalog.sqlite"),
            _env_path("OSTROVOK_CATALOG_EXTRA_DB", "runtime/catalog_extra.sqlite")]


def opendata_paths() -> list[Path]:
    return [_env_path("OPENDATA_DB", "data/opendata.sqlite"),
            _env_path("OPENDATA_EXTRA_DB", "runtime/opendata_extra.sqlite")]


def state_path() -> Path:
    return _env_path("OSTROVOK_STATE_DB", "runtime/ostrovok_state.sqlite")


class UnionDB:
    """Read-only union over a committed database and its optional overlay."""

    def __init__(self, paths: Iterable[Path]):
        self.paths = [p for p in paths if p.exists()]
        self._lock = threading.Lock()
        self._conn = sqlite3.connect("file::memory:", uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.schemas: list[str] = []
        for i, path in enumerate(self.paths):
            name = f"db{i}"
            self._conn.execute(f"ATTACH DATABASE ? AS {name}", (path.resolve().as_uri() + "?mode=ro",))
            self.schemas.append(name)
        self._tables: dict[str, set[str]] = {}
        for name in self.schemas:
            rows = self._conn.execute(f"SELECT name FROM {name}.sqlite_master WHERE type='table'").fetchall()
            self._tables[name] = {r[0] for r in rows}

    def has_table(self, table: str) -> bool:
        return any(table in t for t in self._tables.values())

    def query(self, table: str, where: str = "1=1", params: tuple = (), key: str | tuple | None = None,
              cols: str = "*", order: str | None = None) -> list[dict[str, Any]]:
        """SELECT from every attached copy of `table`; dedupe by `key` (first database wins)."""
        out: list[dict[str, Any]] = []
        seen: set = set()
        with self._lock:
            for name in self.schemas:
                if table not in self._tables[name]:
                    continue
                sql = f"SELECT {cols} FROM {name}.{table} WHERE {where}"
                if order:
                    sql += f" ORDER BY {order}"
                for row in self._conn.execute(sql, params).fetchall():
                    d = dict(row)
                    if key is not None:
                        k = tuple(d[c] for c in key) if isinstance(key, tuple) else d[key]
                        if k in seen:
                            continue
                        seen.add(k)
                    out.append(d)
        return out

    def close(self) -> None:
        self._conn.close()


_CACHE: dict[tuple, UnionDB] = {}
_CACHE_LOCK = threading.Lock()
_SOURCE_KEYS: ContextVar[dict | None] = ContextVar("source_keys", default=None)


@contextmanager
def cache_source_keys():
    """Reuse file signatures within one tool call; the next call checks live sources again."""
    token = _SOURCE_KEYS.set({})
    try:
        yield
    finally:
        _SOURCE_KEYS.reset(token)


def _cache_key(paths: list[Path]) -> tuple:
    cached = _SOURCE_KEYS.get()
    path_key = tuple(paths)
    if cached is not None and path_key in cached:
        return cached[path_key]
    key = []
    for p in paths:
        try:
            st = p.stat()
            key.append((str(p), st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            key.append((str(p), None, None))
    result = tuple(key)
    if cached is not None:
        cached[path_key] = result
    return result


def _union(paths: list[Path]) -> UnionDB:
    key = _cache_key(paths)
    with _CACHE_LOCK:
        db = _CACHE.get(key)
        if db is None:
            db = UnionDB(paths)
            _CACHE[key] = db
        return db


def catalog() -> UnionDB:
    return _union(catalog_paths())


def opendata() -> UnionDB:
    return _union(opendata_paths())


def reset_caches() -> None:
    """Close cached read-only connections (tests switch databases through env)."""
    with _CACHE_LOCK:
        for db in _CACHE.values():
            db.close()
        _CACHE.clear()
        _STATE_READY.clear()


_STATE_READY: set[str] = set()


@contextmanager
def state() -> Iterator[sqlite3.Connection]:
    """A write connection to the runtime state DB (schema applied on first use); commits on success."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if str(path) not in _STATE_READY or not _has_state_tables(conn):
            conn.executescript(STATE_SCHEMA.read_text(encoding="utf-8"))
            _STATE_READY.add(str(path))
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _has_state_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='carts'").fetchone() is not None


def jload(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
