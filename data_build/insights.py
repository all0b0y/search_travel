"""Stage `insights`: synthetic event log from a hidden preference model (source = 'synthetic').

No personal data: an event is (origin city, travel month, segment, destination, choices).
The log feeds the mock's insights_* tools, which only return groups with n >= 20 (K_MIN).

Allocation (k-anonymity friendly): for every origin x segment x month cell the generator first
places FLOOR_PER_CELL = 24 events, then distributes the rest of TOTAL_EVENTS proportionally to
ORIGIN_WEIGHT x SEGMENT_WEIGHT x MONTH_WEIGHT (largest remainder, deterministic). So every
origin x segment x month group has >= 24 events.

Hidden preference model (per event, seeded RNG; same inputs -> same log):
- budget band: SEGMENT_BUDGET[segment] probabilities over low/mid/high.
- destination: P(dest) ~ popularity * season_fit(dest, month) * KIND_AFFINITY[segment][kind]
  * budget_fit(price level, band) * home_bias(origin, dest)
    season_fit: for sea destinations, 1.6 when air_max >= 27 and sea >= 24 °C, 1.0 when
      air_max >= 23, 0.25 otherwise (climate table; 0.6 without data); ski destinations 1.8 in
      Dec-Mar and 0.3 otherwise; other places 1.0, 0.6 when air_max < -5.
    budget_fit: low band prefers price level <= 6000 RUB (x1.6, dearer x0.4); high band prefers
      >= 10000 (x1.6); mid x1.0.
    home_bias: Russian destinations x1.3; destinations whose name equals the origin x0.
- nights: segment median (solo 4, couple 7, family 9, friends 5) +- 0..3, resort kinds +2.
- meals: family/resort -> all_inclusive 55 %, else breakfast-heavy shares by segment.
- free_cancellation: family 0.75, couple 0.55, solo 0.35, friends 0.45 (+0.1 in high band).
- direct_flight: family 0.8, couple 0.6, solo 0.4, friends 0.5.
- district: SEGMENT_DISTRICT[segment] probabilities over lively/quiet/family/walkable.
- budget_rub: price level x nights x party size factor x band multiplier x lognormal noise.
- kind: 'booking' with probability 0.3, else 'search'.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

from .common import CATALOG_DB, OPENDATA_DB, jdump, log, now_iso, open_db, set_meta
from .prices import price_level

ORIGINS = {  # Russian city name -> weight
    "Москва": 0.34, "Санкт-Петербург": 0.18, "Казань": 0.09, "Екатеринбург": 0.1, "Новосибирск": 0.09,
    "Краснодар": 0.1, "Нижний Новгород": 0.1,
}
ORIGIN_DEST_ID = {"Москва": "moscow", "Санкт-Петербург": "saint-petersburg", "Казань": "kazan",
                  "Екатеринбург": "yekaterinburg", "Новосибирск": "novosibirsk", "Краснодар": "krasnodar",
                  "Нижний Новгород": "nizhny-novgorod"}
SEGMENTS = {"solo": 0.18, "couple": 0.38, "family": 0.3, "friends": 0.14}
MONTH_WEIGHT = [0.9, 0.8, 0.8, 0.8, 1.1, 1.2, 1.4, 1.4, 1.1, 0.9, 0.8, 1.0]
TOTAL_EVENTS = 24000
FLOOR_PER_CELL = 24
SEED = 20261025
TZ = timezone(timedelta(hours=3))

KIND_AFFINITY = {
    "solo": {"city": 1.5, "resort": 0.8, "nature": 1.3, "village": 1.0},
    "couple": {"city": 1.1, "resort": 1.2, "nature": 1.0, "village": 1.2},
    "family": {"city": 0.6, "resort": 1.8, "nature": 0.9, "village": 0.7},
    "friends": {"city": 1.3, "resort": 1.2, "nature": 1.1, "village": 0.8},
}
SEGMENT_BUDGET = {"solo": (0.5, 0.4, 0.1), "couple": (0.3, 0.5, 0.2), "family": (0.35, 0.5, 0.15),
                  "friends": (0.45, 0.45, 0.1)}
BANDS = ("low", "mid", "high")
BAND_MULT = {"low": 0.75, "mid": 1.0, "high": 1.6}
NIGHTS = {"solo": 4, "couple": 7, "family": 9, "friends": 5}
FREE_CANCEL = {"solo": 0.35, "couple": 0.55, "family": 0.75, "friends": 0.45}
DIRECT = {"solo": 0.4, "couple": 0.6, "family": 0.8, "friends": 0.5}
SEGMENT_DISTRICT = {"solo": (0.35, 0.15, 0.05, 0.45), "couple": (0.2, 0.4, 0.05, 0.35),
                    "family": (0.03, 0.35, 0.5, 0.12), "friends": (0.6, 0.05, 0.05, 0.3)}
DISTRICTS = ("lively", "quiet", "family", "walkable")
MEALS_BY_SEGMENT = {  # non-resort trips
    "solo": (("none", 0.45), ("breakfast", 0.5), ("half_board", 0.05)),
    "couple": (("none", 0.2), ("breakfast", 0.65), ("half_board", 0.15)),
    "family": (("none", 0.15), ("breakfast", 0.5), ("half_board", 0.25), ("full_board", 0.1)),
    "friends": (("none", 0.45), ("breakfast", 0.5), ("half_board", 0.05)),
}
PARTY = {"solo": (1, 0), "couple": (2, 0), "family": (2, 1), "friends": (3, 0)}


def allocate(total: int = TOTAL_EVENTS, floor: int = FLOOR_PER_CELL) -> dict[tuple[str, str, int], int]:
    cells = [(o, s, m) for o in ORIGINS for s in SEGMENTS for m in range(1, 13)]
    base = {c: floor for c in cells}
    rest = total - floor * len(cells)
    if rest < 0:
        raise ValueError("total too small for the floor")
    w = {c: ORIGINS[c[0]] * SEGMENTS[c[1]] * MONTH_WEIGHT[c[2] - 1] for c in cells}
    tw = sum(w.values())
    raw = {c: rest * w[c] / tw for c in cells}
    for c in cells:
        base[c] += int(raw[c])
    left = total - sum(base.values())
    for c in sorted(cells, key=lambda c: (-(raw[c] - int(raw[c])), c))[:left]:
        base[c] += 1
    return base


def season_fit(d: dict, month: int) -> float:
    c = (d.get("climate") or {}).get(month)
    tags = set(d["tags"])
    if "ski" in tags:
        return 1.8 if month in (12, 1, 2, 3) else 0.3
    if "sea" in tags and ("beach" in tags or d["kind"] == "resort"):
        if not c or c.get("air_max_c") is None:
            return 0.6
        sea = c.get("sea_c")
        if c["air_max_c"] >= 27 and (sea is None or sea >= 24):
            return 1.6
        if c["air_max_c"] >= 23:
            return 1.0
        return 0.25
    if c and c.get("air_max_c") is not None and c["air_max_c"] < -5:
        return 0.6
    return 1.0


def budget_fit(level: int, band: str) -> float:
    if band == "low":
        return 1.6 if level <= 6000 else 0.4
    if band == "high":
        return 1.6 if level >= 10000 else 0.7
    return 1.0


def _weights(dests, levels, home, seg, month, band) -> list[float]:
    out = []
    for d in dests:
        w = max(0.01, d["popularity"]) * season_fit(d, month) * KIND_AFFINITY[seg].get(d["kind"], 1.0)
        w *= budget_fit(levels[d["id"]], band)
        if d["country_code"] == "RU":
            w *= 1.3
        if d["id"] == home:
            w = 0.0
        out.append(w)
    return out


def generate(dests: list[dict], total: int = TOTAL_EVENTS, floor: int = FLOOR_PER_CELL, seed: int = SEED,
             fetched_at: str = "1970-01-01T00:00:00Z") -> list[dict]:
    """dests: [{id, kind, tags, popularity, country_code, climate: {month: {air_max_c, sea_c}}}]."""
    rng = random.Random(seed)
    dests = sorted(dests, key=lambda d: d["id"])
    levels = {d["id"]: price_level(d["id"], d["country_code"]) for d in dests}
    events = []
    eid = 0
    base_day = datetime(2025, 1, 1, tzinfo=TZ)
    wcache: dict = {}
    for (origin, seg, month), n in sorted(allocate(total, floor).items()):
        home = ORIGIN_DEST_ID.get(origin)
        for _ in range(n):
            band = rng.choices(BANDS, weights=SEGMENT_BUDGET[seg])[0]
            key = (home, seg, month, band)
            weights = wcache.get(key)
            if weights is None:
                weights = wcache[key] = _weights(dests, levels, home, seg, month, band)
            d = rng.choices(dests, weights=weights)[0]
            resortish = d["kind"] == "resort"
            nights = max(1, NIGHTS[seg] + rng.randint(-2, 3) + (2 if resortish else 0))
            if resortish and (seg == "family" or rng.random() < 0.35) and rng.random() < 0.55:
                meals = "all_inclusive"
            else:
                opts = MEALS_BY_SEGMENT[seg]
                meals = rng.choices([o for o, _ in opts], weights=[p for _, p in opts])[0]
            fc = rng.random() < min(0.95, FREE_CANCEL[seg] + (0.1 if band == "high" else 0))
            direct = rng.random() < DIRECT[seg]
            district = rng.choices(DISTRICTS, weights=SEGMENT_DISTRICT[seg])[0]
            adults, children = PARTY[seg]
            if seg == "family" and rng.random() < 0.4:
                children = 2
            if seg == "friends":
                adults = rng.choice((2, 3, 4))
            party_factor = 1.0 + 0.25 * (adults + children - 2)
            budget = levels[d["id"]] * nights * max(0.6, party_factor) * BAND_MULT[band] * math.exp(rng.gauss(0, 0.2))
            budget += 18000 * (adults + 0.75 * children) * (0.0 if d["country_code"] == "RU" and rng.random() < 0.4 else 1.0)
            day = base_day + timedelta(days=rng.randint(0, 600), minutes=rng.randint(0, 1439))
            eid += 1
            events.append({
                "id": eid, "at": day.isoformat(), "kind": "booking" if rng.random() < 0.3 else "search",
                "origin_city": origin, "destination_id": d["id"], "destination_kind": d["kind"], "month": month,
                "segment": seg, "nights": nights, "budget_rub": int(round(budget / 1000.0)) * 1000, "meals": meals,
                "free_cancellation": int(fc), "direct_flight": int(direct), "district": district,
                "data": {"budget_band": band, "adults": adults, "children": children}, "source": "synthetic",
                "fetched_at": fetched_at,
            })
    return events


def load_dests(ocon) -> list[dict]:
    rows = ocon.execute("SELECT id, kind, tags, popularity, country_code FROM destinations").fetchall()
    clim: dict = {}
    for r in ocon.execute("SELECT destination_id, month, air_max_c, sea_c FROM climate"):
        clim.setdefault(r[0], {})[r[1]] = {"air_max_c": r[2], "sea_c": r[3]}
    return [{"id": r[0], "kind": r[1], "tags": json.loads(r[2]), "popularity": r[3], "country_code": r[4],
             "climate": clim.get(r[0], {})} for r in rows]


def run_insights(opendata_path=OPENDATA_DB, catalog_path=CATALOG_DB, total: int = TOTAL_EVENTS) -> int:
    ocon = open_db(opendata_path, "opendata")
    dests = load_dests(ocon)
    ocon.close()
    if not dests:
        raise RuntimeError("insights stage needs destinations first")
    gen_at = now_iso()
    events = generate(dests, total=total, fetched_at=gen_at)
    ccon = open_db(catalog_path, "ostrovok_catalog")
    with ccon:
        ccon.execute("DELETE FROM insights_events")
        ccon.executemany(
            "INSERT INTO insights_events(id, at, kind, origin_city, destination_id, destination_kind, month, segment,"
            " nights, budget_rub, meals, free_cancellation, direct_flight, district, data, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(e["id"], e["at"], e["kind"], e["origin_city"], e["destination_id"], e["destination_kind"], e["month"],
              e["segment"], e["nights"], e["budget_rub"], e["meals"], e["free_cancellation"], e["direct_flight"],
              e["district"], jdump(e["data"]), "synthetic", gen_at) for e in events])
        set_meta(ccon, "source.insights", {"generator": "data_build/insights.py (hidden preference model)",
                                           "seed": SEED, "events": len(events), "floor_per_cell": FLOOR_PER_CELL,
                                           "origin_city_format": "Russian city name, e.g. 'Москва'",
                                           "synthetic": True, "generated_at": gen_at})
    ccon.close()
    log(f"insights: {len(events)} events")
    return len(events)
