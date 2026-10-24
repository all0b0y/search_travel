"""Stage `climate`: monthly normals per destination from Open-Meteo.

Air (Open-Meteo historical archive, ERA5-based reanalysis), daily values 2015-01-01..2024-12-31:
- air_max_c / air_min_c: mean over all days of the month of the daily 2 m max / min temperature;
- rain_days: mean number of days per month with precipitation_sum >= 1 mm;
- sunny_days: mean number of days per month with sunshine_duration >= 6 h.
Sea (Open-Meteo marine API, daily sea_surface_temperature_mean) for coastal destinations only:
the marine series starts in late 2022, so sea normals use 2023-01-01..2025-12-31 (3 years).
A destination centre is often on land (null SST); the nearest marine grid point with data is
found by probing offsets of 0.05..0.4 degrees in 8 directions (short probe window), then the full
series is fetched for that point. Inland destinations and coastal ones without marine data get
sea_c = NULL. Every response is cached under data_build/cache/open-meteo/.
Caveat: Open-Meteo's sunshine_duration is derived from reanalysis radiation, not measured, and
runs high in humid tropics (e.g. Bali in the wet season still shows ~26 "sunny" days); treat
sunny_days as a relative indicator. Caspian destinations (Baku, Derbent measured) get no
sea temperature: the marine model has no grid points there.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict

from .common import HttpxClient, cache_path, cached_text, haversine_m, log, now_iso, open_db, set_meta, OPENDATA_DB

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
AIR_PERIOD = ("2015-01-01", "2024-12-31")
SEA_PERIOD = ("2023-01-01", "2025-12-31")
PROBE_PERIOD = ("2024-07-01", "2024-07-03")
RAIN_MM = 1.0
SUNNY_S = 6 * 3600
OFFSETS_DEG = (0.0, 0.05, 0.1, 0.2, 0.3, 0.4)
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


class RateLimited(RuntimeError):
    pass


def monthly_air_normals(daily: dict) -> dict[int, dict]:
    """Aggregate an Open-Meteo `daily` block into {month: {air_max_c, air_min_c, rain_days, sunny_days}}."""
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or [None] * len(times)
    tmin = daily.get("temperature_2m_min") or [None] * len(times)
    prcp = daily.get("precipitation_sum") or [None] * len(times)
    sun = daily.get("sunshine_duration") or [None] * len(times)
    acc = defaultdict(lambda: {"tmax": [], "tmin": [], "rain": defaultdict(int), "sunny": defaultdict(int),
                               "prcp_years": set(), "sun_years": set()})
    for i, t in enumerate(times):
        y, m = int(t[:4]), int(t[5:7])
        a = acc[m]
        if tmax[i] is not None:
            a["tmax"].append(tmax[i])
        if tmin[i] is not None:
            a["tmin"].append(tmin[i])
        if prcp[i] is not None:
            a["prcp_years"].add(y)
            if prcp[i] >= RAIN_MM:
                a["rain"][y] += 1
        if sun[i] is not None:
            a["sun_years"].add(y)
            if sun[i] >= SUNNY_S:
                a["sunny"][y] += 1
    out = {}
    for m in range(1, 13):
        a = acc.get(m)
        if not a:
            out[m] = {"air_max_c": None, "air_min_c": None, "rain_days": None, "sunny_days": None}
            continue
        py, sy = a["prcp_years"], a["sun_years"]
        out[m] = {
            "air_max_c": round(sum(a["tmax"]) / len(a["tmax"]), 1) if a["tmax"] else None,
            "air_min_c": round(sum(a["tmin"]) / len(a["tmin"]), 1) if a["tmin"] else None,
            "rain_days": round(sum(a["rain"].values()) / len(py), 1) if py else None,
            "sunny_days": round(sum(a["sunny"].values()) / len(sy), 1) if sy else None,
        }
    return out


def monthly_sea_normals(daily: dict) -> dict[int, float | None]:
    times = daily.get("time") or []
    sst = daily.get("sea_surface_temperature_mean") or [None] * len(times)
    vals = defaultdict(list)
    for t, v in zip(times, sst):
        if v is not None:
            vals[int(t[5:7])].append(v)
    return {m: (round(sum(vals[m]) / len(vals[m]), 1) if vals[m] else None) for m in range(1, 13)}


class OpenMeteo:
    def __init__(self, http=None, sleep=time.sleep, pause_s: float = 1.0):
        self.http = http or HttpxClient()
        self.sleep = sleep
        self.pause_s = pause_s

    def _get(self, url, params, cache_file):
        def fetch():
            for attempt in range(4):
                self.sleep(self.pause_s)
                r = self.http.get(url, params=params, timeout=120)
                if r.status == 200:
                    return r.text
                if r.status == 429:
                    if "daily" in r.text.lower() or "hourly" in r.text.lower():
                        raise RateLimited(r.text[:200])
                    self.sleep(30 * (attempt + 1))
                    continue
                if r.status >= 500:
                    self.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError(f"Open-Meteo HTTP {r.status}: {r.text[:200]}")
            raise RuntimeError("Open-Meteo: retries exhausted")
        return cached_text(cache_file, fetch)

    def archive(self, dest_id, lat, lon):
        params = {"latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": AIR_PERIOD[0],
                  "end_date": AIR_PERIOD[1], "timezone": "auto",
                  "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration"}
        text, fetched_at = self._get(ARCHIVE_URL, params, cache_path("open-meteo", "archive", f"{dest_id}.json"))
        return json.loads(text), fetched_at

    def marine(self, key, lat, lon, period):
        params = {"latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": period[0],
                  "end_date": period[1], "timezone": "GMT", "daily": "sea_surface_temperature_mean"}
        text, fetched_at = self._get(MARINE_URL, params, cache_path("open-meteo", "marine", f"{key}.json"))
        return json.loads(text), fetched_at

    def find_sea_point(self, dest_id, lat, lon):
        """Nearest probe point with SST data, or None. Returns (lat, lon)."""
        cands = []
        for off in OFFSETS_DEG:
            dirs = [(0, 0)] if off == 0 else DIRECTIONS
            for dy, dx in dirs:
                k = 1 / math.sqrt(2) if dy and dx else 1
                plat = lat + dy * off * k
                plon = lon + dx * off * k / max(0.2, math.cos(math.radians(lat)))
                cands.append((round(plat, 3), round(plon, 3)))
        for plat, plon in cands:
            data, _ = self.marine(f"probe-{dest_id}-{plat}-{plon}", plat, plon, PROBE_PERIOD)
            vals = ((data.get("daily") or {}).get("sea_surface_temperature_mean") or [])
            if any(v is not None for v in vals):
                return plat, plon
        return None


def climate_rows(dest_id, air: dict, sea: dict | None, fetched_at: str) -> list[tuple]:
    rows = []
    for m in range(1, 13):
        a = air[m]
        s = sea.get(m) if sea else None
        source = "open-meteo-archive" + ("+open-meteo-marine" if s is not None else "")
        rows.append((dest_id, m, a["air_max_c"], a["air_min_c"], s, a["rain_days"], a["sunny_days"], source, fetched_at))
    return rows


def write_climate(con, dest_id, rows, timezone_name=None, sea_point=None, note=None):
    with con:
        con.execute("DELETE FROM climate WHERE destination_id=?", (dest_id,))
        con.executemany("INSERT INTO climate(destination_id, month, air_max_c, air_min_c, sea_c, rain_days, sunny_days,"
                        " source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        # destinations.timezone comes from the offline table (data_build/timezones.py); the zone
        # Open-Meteo derives from the coordinates is kept here for the cross-check in the report.
        set_meta(con, f"climate_done:{dest_id}", {"sea_point": sea_point, "note": note, "at": now_iso(),
                                                  "open_meteo_timezone": timezone_name})


def build_climate_for(om: OpenMeteo, dest_id, lat, lon, coastal: bool):
    data, fetched_at = om.archive(dest_id, lat, lon)
    air = monthly_air_normals(data.get("daily") or {})
    sea = None
    sea_point = None
    note = None
    if coastal:
        sea_point = om.find_sea_point(dest_id, lat, lon)
        if sea_point:
            sdata, _ = om.marine(f"{dest_id}", sea_point[0], sea_point[1], SEA_PERIOD)
            sea = monthly_sea_normals(sdata.get("daily") or {})
            if all(v is None for v in sea.values()):
                sea, note = None, "marine series empty"
            else:
                note = f"sea point {round(haversine_m(lat, lon, *sea_point) / 1000, 1)} km from centre"
        else:
            note = "no marine grid point with data within ~0.4 deg"
    return climate_rows(dest_id, air, sea, fetched_at), data.get("timezone"), sea_point, note


def rate_limit_wait_s(reason: str, now: float | None = None) -> float:
    """Seconds until Open-Meteo's next quota window (+60 s): the next full UTC hour for the
    hourly limit, the next UTC midnight for the daily limit (the free API counts a 10-year daily
    archive request as ~100 calls: 4 variables x 3653 days / (10 variables x 14 days))."""
    now = time.time() if now is None else now
    if "daily" in reason.lower():
        return 86400 - (now % 86400) + 60
    return 3600 - (now % 3600) + 60


def run_climate(db_path=OPENDATA_DB, http=None, only=None, limit=None, resume=False, sleep=time.sleep,
                wait_rate_limit: bool = False) -> dict:
    from .destinations import BY_ID
    con = open_db(db_path, "opendata")
    om = OpenMeteo(http, sleep=sleep)
    dests = con.execute("SELECT id, lat, lon, tags FROM destinations ORDER BY id").fetchall()
    if only:
        dests = [d for d in dests if d["id"] in only]
    if limit:
        dests = dests[:limit]
    stats = {"done": 0, "skipped": 0, "no_sea": [], "failed": []}
    for d in dests:
        if resume and con.execute("SELECT 1 FROM meta WHERE key=?", (f"climate_done:{d['id']}",)).fetchone():
            stats["skipped"] += 1
            continue
        cur = BY_ID.get(d["id"])
        coastal = cur.is_coastal if cur else ("sea" in json.loads(d["tags"]))
        try:
            rows, tz, sea_point, note = build_climate_for(om, d["id"], d["lat"], d["lon"], coastal)
        except RateLimited as e:
            wait = rate_limit_wait_s(str(e)) if wait_rate_limit else None
            if wait is None:
                log(f"climate: rate limited by Open-Meteo ({e}); stopping, rerun with --resume later")
                stats["failed"].append(d["id"])
                break
            log(f"climate: rate limited ({str(e)[:80]}); sleeping {wait:.0f} s until the next quota window")
            sleep(wait)
            try:
                rows, tz, sea_point, note = build_climate_for(om, d["id"], d["lat"], d["lon"], coastal)
            except Exception as e2:
                log(f"climate: {d['id']} failed after waiting: {e2}; stopping, rerun with --resume")
                stats["failed"].append(d["id"])
                break
        except Exception as e:
            log(f"climate: {d['id']} failed: {e}")
            stats["failed"].append(d["id"])
            continue
        write_climate(con, d["id"], rows, tz, sea_point, note)
        if coastal and rows[0][4] is None and all(r[4] is None for r in rows):
            stats["no_sea"].append(d["id"])
        stats["done"] += 1
        log(f"climate: {d['id']} ok{' (' + note + ')' if note else ''}")
    with con:
        set_meta(con, "source.climate", {"archive": ARCHIVE_URL, "air_period": AIR_PERIOD, "marine": MARINE_URL,
                                         "sea_period": SEA_PERIOD, "rain_day_mm": RAIN_MM, "sunny_day_h": 6})
    con.close()
    log(f"climate: {stats}")
    return stats
