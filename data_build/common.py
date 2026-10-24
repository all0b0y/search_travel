"""Shared helpers of the offline data builders: paths, HTTP with cache, geometry, DB setup.

Network is used only by data_build (AGENT_PLAN.md section 1). Every HTTP call goes through an
`Http`-like object with `get(url, params=None, headers=None, timeout=...)` and
`post(url, data=None, headers=None, timeout=...)` returning `HttpResponse`, so tests inject a fake.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCHEMA_DIR = DATA_DIR / "schema"
CACHE_DIR = ROOT / "data_build" / "cache"
RUNTIME_DIR = ROOT / "runtime"

OPENDATA_DB = DATA_DIR / "opendata.sqlite"
CATALOG_DB = DATA_DIR / "ostrovok_catalog.sqlite"
OPENDATA_OVERLAY = RUNTIME_DIR / "opendata_extra.sqlite"
CATALOG_OVERLAY = RUNTIME_DIR / "catalog_extra.sqlite"

USER_AGENT = "SearchTravelHackathon/0.1 (https://github.com/all0b0y)"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- HTTP

@dataclass
class HttpResponse:
    status: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


class Http(Protocol):
    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            timeout: float = 60) -> HttpResponse: ...

    def post(self, url: str, data: dict | None = None, headers: dict | None = None,
             timeout: float = 60) -> HttpResponse: ...


class HttpxClient:
    """Real client (httpx). Raises nothing for HTTP statuses; network errors raise httpx errors."""

    def __init__(self):
        import httpx  # local import: tests never construct the real client
        self._httpx = httpx
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    def get(self, url, params=None, headers=None, timeout=60):
        r = self._client.get(url, params=params, headers=headers, timeout=timeout)
        return HttpResponse(r.status_code, r.text)

    def post(self, url, data=None, headers=None, timeout=60):
        r = self._client.post(url, data=data, headers=headers, timeout=timeout)
        return HttpResponse(r.status_code, r.text)


class RateLimiter:
    """Minimum interval between calls per key (e.g. Nominatim 1 req/s)."""

    def __init__(self, min_interval_s: float, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.min_interval_s = min_interval_s
        self._last = None
        self._sleep = sleep
        self._clock = clock

    def wait(self) -> None:
        if self._last is not None:
            delta = self._clock() - self._last
            if delta < self.min_interval_s:
                self._sleep(self.min_interval_s - delta)
        self._last = self._clock()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def cache_path(*parts: str) -> Path:
    return CACHE_DIR.joinpath(*parts)


def cached_text(path: Path, fetch: Callable[[], str], refresh: bool = False) -> tuple[str, str]:
    """Return (text, fetched_at). The fetch time is stored next to the cache file."""
    meta = path.with_suffix(path.suffix + ".meta.json")
    if path.exists() and not refresh:
        fetched_at = None
        if meta.exists():
            fetched_at = json.loads(meta.read_text(encoding="utf-8")).get("fetched_at")
        if not fetched_at:
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(
                microsecond=0).isoformat().replace("+00:00", "Z")
        return path.read_text(encoding="utf-8"), fetched_at
    text = fetch()
    fetched_at = now_iso()
    write_atomic(path, text)
    write_atomic(meta, json.dumps({"fetched_at": fetched_at}))
    return text, fetched_at


# --------------------------------------------------------------------------- deterministic hashing

def h01(*parts: Any) -> float:
    """Deterministic uniform number in [0, 1) from sha256 of the joined parts."""
    s = "|".join(str(p) for p in parts)
    d = hashlib.sha256(s.encode("utf-8")).digest()
    return int.from_bytes(d[:8], "big") / 2 ** 64


def hint(lo: int, hi: int, *parts: Any) -> int:
    """Deterministic integer in [lo, hi]."""
    return lo + int(h01(*parts) * (hi - lo + 1))


def hpick(options: list, *parts: Any, weights: list[float] | None = None):
    """Deterministic weighted choice."""
    u = h01(*parts)
    if not weights:
        return options[int(u * len(options))]
    total = float(sum(weights))
    acc = 0.0
    for opt, w in zip(options, weights):
        acc += w / total
        if u < acc:
            return opt
    return options[-1]


def stable_key(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- geometry

EARTH_R = 6371008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def point_segment_m(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from a point to segment a-b (lat, lon pairs), local equirectangular projection."""
    k = math.cos(math.radians(lat))
    ax, ay = (a[1] - lon) * k, a[0] - lat
    bx, by = (b[1] - lon) * k, b[0] - lat
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py) * (math.pi / 180) * EARTH_R


def point_polyline_m(lat: float, lon: float, pts: list[tuple[float, float]]) -> float:
    if not pts:
        return math.inf
    if len(pts) == 1:
        return haversine_m(lat, lon, pts[0][0], pts[0][1])
    return min(point_segment_m(lat, lon, pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def bbox_around(lat: float, lon: float, half_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a square with half side `half_km`."""
    dlat = half_km / 111.32
    dlon = half_km / (111.32 * max(0.1, math.cos(math.radians(lat))))
    return (round(lat - dlat, 5), round(lon - dlon, 5), round(lat + dlat, 5), round(lon + dlon, 5))


# --------------------------------------------------------------------------- DB

def schema_sql(name: str) -> str:
    return (SCHEMA_DIR / f"{name}.sql").read_text(encoding="utf-8")


def open_db(path: Path, schema: str) -> sqlite3.Connection:
    """Open (creating from the schema SQL if needed) a database built from data/schema/<schema>.sql.
    Columns added to the schema after the DB was built are added with ALTER TABLE (`migrate`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60)
    con.row_factory = sqlite3.Row
    sql = schema_sql(schema)
    con.executescript(sql)
    migrate(con, sql)
    return con


def migrate(con: sqlite3.Connection, sql: str) -> list[str]:
    """Add columns present in the schema SQL but missing in `con` (same type/default)."""
    ref = sqlite3.connect(":memory:")
    ref.executescript(sql)
    added = []
    for (table,) in ref.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for cid, name, typ, notnull, dflt, pk in ref.execute(f"PRAGMA table_info({table})"):
            if name in have:
                continue
            ddl = f"ALTER TABLE {table} ADD COLUMN {name} {typ}"
            if dflt is not None:
                ddl += f" DEFAULT {dflt}"
                if notnull:
                    ddl += " NOT NULL"
            con.execute(ddl)
            added.append(f"{table}.{name}")
    ref.close()
    con.commit()
    return added


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, v))


def get_meta(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return row[0]


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
