"""Session store: one JSON blob per session in SQLite (runtime/sessions.sqlite).

    store = SessionStore()                       # or SessionStore(":memory:") in tests
    store.put(session); store.get(session_id) -> Session | None

Thread-safe: one connection guarded by a lock (sqlite in WAL mode for file databases).
"""
import sqlite3
import threading
from pathlib import Path

from .state import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SESSIONS_DB = REPO_ROOT / "runtime" / "sessions.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


class SessionStore:
    def __init__(self, path=DEFAULT_SESSIONS_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def get(self, session_id):
        with self._lock:
            row = self._conn.execute("SELECT data FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return Session.from_json(row[0]) if row else None

    def put(self, session):
        data = session.to_json()
        stamp = session.clock().isoformat()
        with self._lock:
            self._conn.execute("INSERT INTO sessions(id, updated_at, data) VALUES (?, ?, ?) "
                               "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at, data = excluded.data",
                               (session.id, stamp, data))

    def exists(self, session_id):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None

    def delete(self, session_id):
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def ids(self):
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT id FROM sessions ORDER BY updated_at")]

    def close(self):
        with self._lock:
            self._conn.close()
