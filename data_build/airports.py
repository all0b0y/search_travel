"""Stages `airports` (OurAirports) and `routes` (OpenFlights).

Sources:
- OurAirports `airports.csv` (public domain, updated nightly):
  https://davidmegginson.github.io/ourairports-data/airports.csv
  Kept: type large_airport | medium_airport with a 3-letter IATA code. When two rows share an
  IATA code, the large one, then the one with scheduled service, then the lowest ident wins.
- OpenFlights `routes.dat` (ODbL). NOTE: the dataset was last updated in June 2014, so carriers
  and routes are dated (e.g. pre-2022 flights between Russia and the EU). Rows are aggregated by
  (origin, destination, stops) with the list of carrier codes; only routes whose both ends are in
  the airports table are kept. The great-circle distance is computed by the readers from the
  airports' coordinates (the `routes` schema has no distance column; see report Contract questions).
"""
from __future__ import annotations

import csv
import io
import sqlite3
from typing import Iterable

from .common import (HttpxClient, cache_path, cached_text, haversine_m, log, open_db, set_meta,
                     jdump, OPENDATA_DB)

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
ROUTES_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat"
KEEP_TYPES = ("large_airport", "medium_airport")
# ISO regions of politically contested territories: their airports are never offered as the
# nearest airport of a destination (they stay in the airports table, which mirrors OurAirports).
CONTESTED_REGIONS = ("GE-AB", "UA-43", "UA-40")
# Curated: airports of southern and central Russia closed to civil flights since 2022-02-24 (Rosaviatsia
# restriction, still in force as of 2025). OurAirports keeps some of them as scheduled (GDZ, KRR), so the
# flag is corrected here: they are never a destination's or an origin's airport (meta airports.closed_iata).
CLOSED_AIRPORTS = frozenset({"AAQ", "EGO", "BZK", "VOZ", "GDZ", "KRR", "URS", "LPK", "ROV", "SIP", "ESL"})


def parse_airports_csv(text: str) -> list[dict]:
    """Parse OurAirports CSV text into rows for the `airports` table (plus `scheduled` flag)."""
    best: dict[str, dict] = {}
    for r in csv.DictReader(io.StringIO(text)):
        typ = (r.get("type") or "").strip()
        iata = (r.get("iata_code") or "").strip().upper()
        if typ not in KEEP_TYPES or len(iata) != 3 or not iata.isalpha():
            continue
        try:
            lat, lon = float(r["latitude_deg"]), float(r["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        row = {
            "ident": r["ident"].strip(),
            "iata": iata,
            "name": (r.get("name") or "").strip() or iata,
            "city": (r.get("municipality") or "").strip() or None,
            "country_code": (r.get("iso_country") or "").strip().upper(),
            "lat": lat,
            "lon": lon,
            "type": typ,
            "scheduled": (r.get("scheduled_service") or "").strip().lower() == "yes" and iata not in CLOSED_AIRPORTS,
            "contested": (r.get("iso_region") or "").strip().upper() in CONTESTED_REGIONS,
        }
        rank = (row["type"] != "large_airport", not row["scheduled"], row["ident"])
        cur = best.get(iata)
        if cur is None or rank < (cur["type"] != "large_airport", not cur["scheduled"], cur["ident"]):
            best[iata] = row
    return sorted(best.values(), key=lambda x: x["iata"])


def parse_routes_dat(text: str, known_iata: Iterable[str] | None = None) -> list[dict]:
    """Aggregate OpenFlights routes.dat lines into {origin_iata, dest_iata, stops, carriers}."""
    known = set(known_iata) if known_iata is not None else None
    agg: dict[tuple[str, str, int], set[str]] = {}
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 8:
            continue
        airline, src, dst, stops = rec[0].strip(), rec[2].strip().upper(), rec[4].strip().upper(), rec[7].strip()
        if len(src) != 3 or len(dst) != 3 or src == dst:
            continue
        if known is not None and (src not in known or dst not in known):
            continue
        try:
            n_stops = int(stops)
        except ValueError:
            n_stops = 0
        carriers = agg.setdefault((src, dst, n_stops), set())
        if airline and airline != "\\N":
            carriers.add(airline)
    return [{"origin_iata": o, "dest_iata": d, "stops": s, "carriers": sorted(c)}
            for (o, d, s), c in sorted(agg.items())]


def route_distance_km(con: sqlite3.Connection, origin: str, dest: str) -> float | None:
    a = con.execute("SELECT lat, lon FROM airports WHERE iata=?", (origin,)).fetchone()
    b = con.execute("SELECT lat, lon FROM airports WHERE iata=?", (dest,)).fetchone()
    if not a or not b:
        return None
    return round(haversine_m(a[0], a[1], b[0], b[1]) / 1000, 1)


def fetch_airports_text(http=None, refresh=False) -> tuple[str, str]:
    http = http or HttpxClient()

    def fetch():
        r = http.get(AIRPORTS_URL, timeout=120)
        if r.status != 200:
            raise RuntimeError(f"OurAirports HTTP {r.status}")
        return r.text
    return cached_text(cache_path("ourairports", "airports.csv"), fetch, refresh)


def run_airports(db_path=OPENDATA_DB, http=None, refresh=False) -> int:
    text, fetched_at = fetch_airports_text(http, refresh)
    rows = parse_airports_csv(text)
    con = open_db(db_path, "opendata")
    with con:
        con.execute("DELETE FROM airports")
        con.executemany(
            "INSERT INTO airports(ident, iata, name, city, country_code, lat, lon, type, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r["ident"], r["iata"], r["name"], r["city"], r["country_code"], r["lat"], r["lon"], r["type"],
              "ourairports", fetched_at) for r in rows])
        set_meta(con, "source.airports", {"url": AIRPORTS_URL, "fetched_at": fetched_at, "rows": len(rows),
                                          "filter": "large/medium airports with IATA"})
        set_meta(con, "airports.scheduled_iata", sorted(r["iata"] for r in rows if r["scheduled"]))
        set_meta(con, "airports.closed_iata", sorted(CLOSED_AIRPORTS))
        set_meta(con, "airports.contested_iata", sorted(r["iata"] for r in rows if r["contested"]))
    con.close()
    log(f"airports: {len(rows)} rows")
    return len(rows)


def run_routes(db_path=OPENDATA_DB, http=None, refresh=False) -> int:
    http = http or HttpxClient()

    def fetch():
        r = http.get(ROUTES_URL, timeout=120)
        if r.status != 200:
            raise RuntimeError(f"OpenFlights HTTP {r.status}")
        return r.text
    text, fetched_at = cached_text(cache_path("openflights", "routes.dat"), fetch, refresh)
    con = open_db(db_path, "opendata")
    known = [r[0] for r in con.execute("SELECT iata FROM airports WHERE iata IS NOT NULL")]
    if not known:
        raise RuntimeError("routes stage needs the airports stage first")
    rows = parse_routes_dat(text, known)
    with con:
        con.execute("DELETE FROM routes")
        con.executemany(
            "INSERT INTO routes(origin_iata, dest_iata, stops, carriers, source, fetched_at) VALUES (?,?,?,?,?,?)",
            [(r["origin_iata"], r["dest_iata"], r["stops"], jdump(r["carriers"]), "openflights", fetched_at)
             for r in rows])
        set_meta(con, "source.routes", {"url": ROUTES_URL, "fetched_at": fetched_at, "rows": len(rows),
                                        "dataset_date": "2014-06 (OpenFlights last update; routes are dated)",
                                        "distance": "not stored; compute great-circle from airports lat/lon"})
    con.close()
    log(f"routes: {len(rows)} rows")
    return len(rows)
