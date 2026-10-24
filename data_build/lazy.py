"""Lazy city: build one destination outside the catalog at runtime (AGENT_PLAN 'Module interfaces').

    build_city(query, *, timeout_s=60) -> destination id | None

One attempt within `timeout_s`: one Nominatim search, then one batched Overpass query (same
query as the offline build) in parallel with one Open-Meteo archive call for the climate. On
success it writes the destination, its climate, hotels, districts and synthetic catalog fields
into the overlays runtime/opendata_extra.sqlite and runtime/catalog_extra.sqlite (created from
data/schema/*.sql). It never writes the committed databases. When the place already exists
(same name, or a destination within 10 km) the existing id is returned and nothing is written.
On any failure (no geocoder hit, Overpass error/timeout, no named hotels) nothing is written and
None is returned, so the engine can say honestly that the city is not in the demo.

Rows written here carry source 'nominatim+lazy' (destination), 'open-meteo-archive' (climate,
no sea temperature: the marine probing needs too many calls for a live request), 'osm' hotels,
'overpass' districts and 'synthetic' catalog fields.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from .common import (CATALOG_DB, CATALOG_OVERLAY, OPENDATA_DB, OPENDATA_OVERLAY, HttpxClient, bbox_around,
                     haversine_m, jdump, log, now_iso, open_db, set_meta)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
SAME_PLACE_KM = 10.0
_LOCK = threading.Lock()

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))


def slugify(name: str) -> str:
    s = "".join(_TRANSLIT.get(ch, ch) for ch in name.lower())
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "place"


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace("ё", "е")


def _existing(paths: list[Path], query: str) -> tuple[str | None, list[tuple]]:
    """(id of a destination named like the query, all destination (id, lat, lon) rows)."""
    q = _norm(query)
    rows = []
    hit = None
    for p in paths:
        if not Path(p).exists():
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            for did, ru, en, lat, lon in con.execute("SELECT id, name_ru, name_en, lat, lon FROM destinations"):
                rows.append((did, lat, lon))
                if hit is None and q in (_norm(ru), _norm(en), _norm(did)):
                    hit = did
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return hit, rows


def _place_hit(results: list[dict]) -> dict | None:
    for r in results:
        if r.get("category") == "place" or (r.get("category") == "boundary" and r.get("type") == "administrative"
                                            and r.get("addresstype") not in ("state", "province", "country", "region")):
            return r
    return None


def _kind(addresstype: str | None) -> str:
    if addresstype in ("city", "town", "suburb", "city_district", "borough", "municipality"):
        return "city"
    if addresstype in ("village", "hamlet", "isolated_dwelling"):
        return "village"
    return "nature"


def build_city(query: str, *, timeout_s: float = 60, http=None,
               opendata_overlay: Path = OPENDATA_OVERLAY, catalog_overlay: Path = CATALOG_OVERLAY,
               committed_opendata: Path = OPENDATA_DB, clock=time.monotonic) -> str | None:
    start = clock()
    deadline = start + timeout_s
    query = (query or "").strip()
    if not query:
        return None
    hit_id, known = _existing([committed_opendata, opendata_overlay], query)
    if hit_id:
        return hit_id
    http = http or HttpxClient()
    try:
        r = http.get(NOMINATIM_URL, params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1,
                                            "namedetails": 1, "accept-language": "ru"},
                     timeout=min(15, max(1, deadline - clock())))
        if r.status != 200:
            return None
        hit = _place_hit(json.loads(r.text))
    except Exception as e:
        log(f"lazy: nominatim failed for {query!r}: {e}")
        return None
    if not hit:
        return None
    lat, lon = float(hit["lat"]), float(hit["lon"])
    for did, dlat, dlon in known:
        if haversine_m(lat, lon, dlat, dlon) / 1000 <= SAME_PLACE_KM:
            return did
    addr = hit.get("address") or {}
    names = hit.get("namedetails") or {}
    name_ru = names.get("name:ru") or hit.get("name") or query
    name_en = names.get("name:en") or names.get("int_name") or names.get("name") or name_ru
    cc = (addr.get("country_code") or "").upper()
    country = addr.get("country") or cc
    did = slugify(name_en if re.search(r"[a-zA-Z]", name_en) else name_ru)
    if any(k[0] == did for k in known):
        did = f"{did}-{cc.lower()}"
    kind = _kind(hit.get("addresstype"))

    # one Overpass attempt and one Open-Meteo call in parallel
    from .climate import ARCHIVE_URL, AIR_PERIOD, climate_rows, monthly_air_normals
    from .overpass import build_query, overpass_fetch, parse_overpass
    bbox = bbox_around(lat, lon, 4.0)
    clim: dict = {}

    def get_climate():
        try:
            rr = http.get(ARCHIVE_URL, params={
                "latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": AIR_PERIOD[0],
                "end_date": AIR_PERIOD[1], "timezone": "auto",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration"},
                timeout=min(30, max(1, deadline - clock())))
            if rr.status == 200:
                clim["data"] = json.loads(rr.text)
                clim["fetched_at"] = now_iso()
        except Exception as e:  # climate is optional; the destination still gets hotels
            log(f"lazy: climate failed for {query!r}: {e}")

    th = threading.Thread(target=get_climate, daemon=True)
    th.start()
    try:
        raw = overpass_fetch(http, build_query(bbox), attempts=1, deadline=deadline, clock=clock,
                             sleep=lambda s: None)
        data = json.loads(raw)
    except Exception as e:
        log(f"lazy: overpass failed for {query!r}: {e}")
        return None
    th.join(timeout=max(0.0, deadline - clock()))
    fetched_at = now_iso()
    cands, feats = parse_overpass(data)
    if not cands:
        return None
    tags = {"city": ["city"], "village": ["village"], "nature": ["nature"]}[kind]
    if feats.has_beach():
        tags = tags + ["beach"]
    from .destinations import nearest_airports
    from .timezones import tz_for
    tz, _method = tz_for(did, cc, lon)
    if tz is None and clim.get("data"):
        tz = clim["data"].get("timezone")
    airports: list[str] = []
    if Path(committed_opendata).exists():
        acon = sqlite3.connect(f"file:{committed_opendata}?mode=ro", uri=True)
        try:
            airports = nearest_airports(acon, lat, lon, None, country_code=cc)
        finally:
            acon.close()

    from .hotels import write_destination
    dest = {"id": did, "lat": lat, "lon": lon, "country_code": cc, "kind": kind, "tags": tags, "half_km": 4.0}
    with _LOCK:
        ocon = open_db(Path(opendata_overlay), "opendata")
        ccon = open_db(Path(catalog_overlay), "ostrovok_catalog")
        try:
            with ocon, ccon:
                ocon.execute(
                    "INSERT OR REPLACE INTO destinations(id, name_ru, name_en, country, country_code, lat, lon, kind,"
                    " tags, airports, stations, timezone, description_ru, popularity, source, fetched_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, name_ru, name_en, country, cc, round(lat, 6), round(lon, 6), kind, jdump(tags),
                     jdump(airports), "[]", tz, None, 0.0, "nominatim+lazy", fetched_at))
                if clim.get("data"):
                    air = monthly_air_normals(clim["data"].get("daily") or {})
                    ocon.execute("DELETE FROM climate WHERE destination_id=?", (did,))
                    ocon.executemany(
                        "INSERT INTO climate(destination_id, month, air_max_c, air_min_c, sea_c, rain_days,"
                        " sunny_days, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        climate_rows(did, air, None, clim["fetched_at"]))
                st = write_destination(dest, cands, feats, "overpass", fetched_at, bbox, ocon, ccon)
                set_meta(ccon, f"hotels_done:{did}", {**st, "at": now_iso(), "lazy": True, "query": query})
        finally:
            ocon.close()
            ccon.close()
    log(f"lazy: built {did} ({st.get('hotels')} hotels) in {clock() - start:.1f} s")
    return did
