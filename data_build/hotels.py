"""Stages `hotels`, `districts`, `synthetic`, run per destination in one pipeline.

For each destination (priority list first, then by popularity):
  1. raw data: cached Overpass response, else one batched Overpass query (mirrors + backoff),
     else the Photon fallback (source 'photon'); every raw response is cached;
  2. hotels: up to MAX_HOTELS named hotels, chosen deterministically for diversity (see
     `select_hotels`), written with their real OSM fields;
  3. districts: POI counts / nearest distances / scores per hotel (data_build/districts.py);
  4. synthetic: stars (when not tagged), rating, reviews, rooms, policies, extras.
The destination is marked done in the catalog meta (`hotels_done:<id>`) only after all requested
steps committed, so an interrupted run is resumed with `--resume` without half-built cities.
A hotel OSM object already assigned to another destination (overlapping areas) is skipped.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict

from .common import (CATALOG_DB, OPENDATA_DB, HttpxClient, RateLimiter, bbox_around, haversine_m, jdump, log,
                     now_iso, open_db, set_meta, stable_key)
from .districts import Features, district_for
from .overpass import (HotelCandidate, OverpassDown, fetch_overpass_cached, load_overpass_cache, parse_overpass,
                       parse_photon, photon_fetch_all)
from .prices import MEAL_MULTIPLIERS, season_factors, season_profile
from .synthetic import parse_osm_stars, synth_hotel

MAX_HOTELS = 60
GRID = 4
KIND_WEIGHT = {"hotel": 3, "apartment": 2, "guest_house": 2, "resort": 2, "hostel": 1, "motel": 1, "chalet": 1}
PRIORITY = ("antalya", "sochi", "istanbul", "dubai", "kazan", "saint-petersburg", "tbilisi", "hurghada", "phuket",
            "kaliningrad", "moscow", "nha-trang", "batumi", "yerevan", "altai", "paris", "rome", "barcelona",
            "prague", "bali")


def hotel_id_for(osm_id: str) -> str:
    typ, num = osm_id.split("/", 1)
    return f"h_{typ[0]}{num}"


def select_hotels(cands: list[HotelCandidate], bbox, max_n: int = MAX_HOTELS) -> list[HotelCandidate]:
    """Deterministic, diverse selection.

    The bbox is split into GRID x GRID cells; candidates are bucketed by (cell, kind) and ordered
    inside a bucket by sha256(osm_id). Selection proceeds in rounds; in every round each bucket
    gives its next w = KIND_WEIGHT[kind] items (hotels 3, apartments / guest houses / resorts 2,
    hostels / motels / chalets 1). Buckets are visited in a fixed
    pseudo-random order (sha256 of the bucket key), so truncation at `max_n` does not favour one
    corner of the area. The result is returned sorted by osm_id. Same input -> same output.
    """
    s, w, n, e = bbox
    buckets: dict[tuple[int, int, str], list[HotelCandidate]] = defaultdict(list)
    for c in cands:
        i = min(GRID - 1, max(0, int((c.lat - s) / max(1e-9, n - s) * GRID)))
        j = min(GRID - 1, max(0, int((c.lon - w) / max(1e-9, e - w) * GRID)))
        buckets[(i, j, c.kind)].append(c)
    for b in buckets.values():
        b.sort(key=lambda c: stable_key(c.osm_id))
    order = sorted(buckets, key=lambda k: stable_key(f"{k[0]},{k[1]},{k[2]}"))
    pos = {k: 0 for k in order}
    picked: list[HotelCandidate] = []
    while len(picked) < max_n:
        added = False
        for key in order:
            for _ in range(KIND_WEIGHT.get(key[2], 1)):
                if pos[key] < len(buckets[key]) and len(picked) < max_n:
                    picked.append(buckets[key][pos[key]])
                    pos[key] += 1
                    added = True
        if not added:
            break
    return sorted(picked, key=lambda c: c.osm_id)


def _address(tags: dict) -> str | None:
    street = " ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x)
    parts = [p for p in (street, tags.get("addr:city")) if p]
    return ", ".join(parts) or None


def _photos(tags: dict) -> list[dict]:
    out = []
    wc = (tags.get("wikimedia_commons") or "").strip()
    if wc.startswith("File:"):
        out.append({"url": "https://commons.wikimedia.org/wiki/" + wc.replace(" ", "_"), "source": "wikimedia",
                    "license": "see Wikimedia Commons page"})
    img = (tags.get("image") or "").strip()
    if img.startswith("http"):
        src = "wikimedia" if "wikimedia.org" in img else "osm"
        out.append({"url": img, "source": src, "license": None})
    return out


def obtain_raw(dest, http, network: bool, sleep=time.sleep, deadline=None, overpass_attempts: int = 6):
    """(candidates, features, source, fetched_at) or None when nothing is available."""
    bbox = bbox_around(dest["lat"], dest["lon"], dest["half_km"])
    cached = load_overpass_cache(dest["id"])
    if cached:
        data, fetched_at = cached
        c, f = parse_overpass(data)
        return c, f, "overpass", fetched_at, bbox
    from .common import cache_path, cached_text
    ph = cache_path("photon", f"{dest['id']}.json")
    if not network:
        if ph.exists():
            text, fetched_at = cached_text(ph, lambda: "")
            c, f = parse_photon(json.loads(text), bbox)
            return c, f, "photon", fetched_at, bbox
        return None
    try:
        data, fetched_at = fetch_overpass_cached(http, dest["id"], bbox, sleep=sleep, deadline=deadline,
                                                 attempts=overpass_attempts)
        c, f = parse_overpass(data)
        return c, f, "overpass", fetched_at, bbox
    except OverpassDown as e:
        log(f"hotels: Overpass down for {dest['id']} ({e}); Photon fallback")
    res = photon_fetch_all(http, dest["id"], dest["lat"], dest["lon"], limiter=RateLimiter(1.0, sleep=sleep))
    fetched_at = now_iso()
    c, f = parse_photon(res, bbox)
    return c, f, "photon", fetched_at, bbox


def write_destination(dest, cands, feats: Features, source: str, fetched_at: str, bbox, ocon, ccon,
                      stages=("hotels", "districts", "synthetic")) -> dict:
    """Write hotels/districts/synthetic rows of one destination into the given connections."""
    did = dest["id"]
    gen_at = now_iso()
    stats = {"candidates": len(cands), "hotels": 0, "skipped_dup": 0, "source": source}
    if "hotels" in stages:
        selected_all = select_hotels(cands, bbox, MAX_HOTELS * 2)
        ids = [c.osm_id for c in selected_all]
        taken = set()
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            q = ",".join("?" * len(chunk))
            taken |= {r[0] for r in ccon.execute(
                f"SELECT osm_id FROM hotels WHERE destination_id != ? AND osm_id IN ({q})", [did, *chunk])}
        stats["skipped_dup"] = sum(1 for c in selected_all if c.osm_id in taken)
        pool = [c for c in cands if c.osm_id not in taken]
        selected = select_hotels(pool, bbox, MAX_HOTELS)
        old = [r[0] for r in ccon.execute("SELECT hotel_id FROM hotels WHERE destination_id=?", (did,))]
        for hid in old:
            ccon.execute("DELETE FROM rooms WHERE hotel_id=?", (hid,))
            ccon.execute("DELETE FROM policies WHERE hotel_id=?", (hid,))
            ccon.execute("DELETE FROM meta WHERE key=?", (f"hotel_extras:{hid}",))
        ccon.execute("DELETE FROM hotels WHERE destination_id=?", (did,))
        beachy = bool(set(dest["tags"]) & {"sea", "beach"})
        grids: dict = {}
        hsource = "osm" if source == "overpass" else "photon"
        for c in selected:
            dist = district_for(c.lat, c.lon, feats, (dest["lat"], dest["lon"]), grids)
            beach = dist["nearest_m"].get("beach") if beachy else None
            ccon.execute(
                "INSERT INTO hotels(hotel_id, osm_id, destination_id, name, kind, lat, lon, address, stars, rating,"
                " reviews, distance_beach_m, distance_center_m, photos, synthetic_fields, source, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hotel_id_for(c.osm_id), c.osm_id, did, c.name, c.kind, round(c.lat, 7), round(c.lon, 7),
                 _address(c.tags), parse_osm_stars(c.tags.get("stars")), None, 0, beach,
                 int(round(haversine_m(c.lat, c.lon, dest["lat"], dest["lon"]))), jdump(_photos(c.tags)), "[]",
                 hsource, fetched_at))
        stats["hotels"] = len(selected)
    hotels = ccon.execute("SELECT hotel_id, osm_id, kind, lat, lon, stars, synthetic_fields FROM hotels"
                          " WHERE destination_id=? ORDER BY hotel_id", (did,)).fetchall()
    if "districts" in stages:
        ocon.execute("DELETE FROM districts WHERE destination_id=?", (did,))
        grids = {}
        dsource = "overpass" if source == "overpass" else "photon"
        for h in hotels:
            d = district_for(h["lat"], h["lon"], feats, (dest["lat"], dest["lon"]), grids)
            ocon.execute(
                "INSERT OR REPLACE INTO districts(osm_id, destination_id, lat, lon, poi_counts, nearest_m, lively,"
                " quiet, family, walkable, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (h["osm_id"], did, h["lat"], h["lon"], jdump(d["poi_counts"]), jdump(d["nearest_m"]), d["lively"],
                 d["quiet"], d["family"], d["walkable"], dsource, fetched_at))
        stats["districts"] = len(hotels)
    if "synthetic" in stages:
        stats["synthetic"] = write_synthetic(ccon, dest, hotels, gen_at)
    return stats


def write_synthetic(ccon, dest, hotels, gen_at: str) -> int:
    for h in hotels:
        prev = json.loads(h["synthetic_fields"] or "[]")
        osm_stars = None if "stars" in prev else h["stars"]
        s = synth_hotel({"hotel_id": h["hotel_id"], "osm_id": h["osm_id"], "kind": h["kind"], "stars": osm_stars},
                        {"id": dest["id"], "country_code": dest["country_code"]})
        ccon.execute("DELETE FROM rooms WHERE hotel_id=?", (h["hotel_id"],))
        ccon.executemany(
            "INSERT INTO rooms(room_id, hotel_id, name, capacity_adults, capacity_children, bed, base_price_rub,"
            " meals_options, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r["room_id"], r["hotel_id"], r["name"], r["capacity_adults"], r["capacity_children"], r["bed"],
              r["base_price_rub"], jdump(r["meals_options"]), "synthetic", gen_at) for r in s["rooms"]])
        p = s["policy"]
        ccon.execute(
            "INSERT OR REPLACE INTO policies(hotel_id, check_in_from, check_out_until, pets_allowed, children_allowed,"
            " min_child_age, infant_cot, extra_bed_price_rub, early_checkin_available, early_checkin_price_rub,"
            " free_cancellation_days, penalty_pct, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["hotel_id"], p["check_in_from"], p["check_out_until"], p["pets_allowed"], p["children_allowed"],
             p["min_child_age"], p["infant_cot"], p["extra_bed_price_rub"], p["early_checkin_available"],
             p["early_checkin_price_rub"], p["free_cancellation_days"], p["penalty_pct"], "synthetic", gen_at))
        ccon.execute("UPDATE hotels SET stars=?, rating=?, reviews=?, synthetic_fields=? WHERE hotel_id=?",
                     (s["stars"], s["rating"], s["reviews"], jdump(s["synthetic_fields"]), h["hotel_id"]))
        set_meta(ccon, f"hotel_extras:{h['hotel_id']}", {**s["extras"], "source": "synthetic"})
    set_meta(ccon, f"synthetic.season_factors:{dest['id']}", {
        "profile": season_profile(dest["id"], dest["country_code"], dest["kind"], dest["tags"]),
        "factors": season_factors(dest["id"], dest["country_code"], dest["kind"], dest["tags"]),
        "source": "synthetic"})
    set_meta(ccon, "synthetic.meal_multipliers", {**MEAL_MULTIPLIERS})
    return len(hotels)


def _dest_rows(ocon, only=None):
    from .destinations import BY_ID
    rows = ocon.execute("SELECT id, lat, lon, country_code, kind, tags, popularity FROM destinations").fetchall()
    out = []
    for r in rows:
        cur = BY_ID.get(r["id"])
        out.append({"id": r["id"], "lat": r["lat"], "lon": r["lon"], "country_code": r["country_code"],
                    "kind": r["kind"], "tags": json.loads(r["tags"]), "popularity": r["popularity"],
                    "half_km": cur.half_km if cur else 4.0})
    if only:
        by = {d["id"]: d for d in out}
        return [by[i] for i in only if i in by]
    prio = {d: i for i, d in enumerate(PRIORITY)}
    return sorted(out, key=lambda d: (prio.get(d["id"], len(prio)), -d["popularity"], d["id"]))


def run_hotel_pipeline(stages, only=None, limit=None, resume=False, network=True, http=None,
                       opendata_path=OPENDATA_DB, catalog_path=CATALOG_DB, sleep=time.sleep) -> dict:
    stages = tuple(stages)
    ocon = open_db(opendata_path, "opendata")
    ccon = open_db(catalog_path, "ostrovok_catalog")
    if network and http is None:
        http = HttpxClient()
    dests = _dest_rows(ocon, only)
    done_key = "hotels_done" if "hotels" in stages else ("districts_done" if "districts" in stages else "synthetic_done")
    summary = {"done": [], "skipped": [], "no_data": [], "photon": [], "failed": []}
    processed = 0
    for dest in dests:
        if limit is not None and processed >= limit:
            break
        if resume and ccon.execute("SELECT 1 FROM meta WHERE key=?", (f"{done_key}:{dest['id']}",)).fetchone():
            summary["skipped"].append(dest["id"])
            continue
        if "hotels" not in stages and not ccon.execute("SELECT 1 FROM hotels WHERE destination_id=? LIMIT 1",
                                                      (dest["id"],)).fetchone():
            continue
        processed += 1
        t0 = time.monotonic()
        try:
            if stages == ("synthetic",):
                hotels = ccon.execute("SELECT hotel_id, osm_id, kind, lat, lon, stars, synthetic_fields FROM hotels"
                                      " WHERE destination_id=? ORDER BY hotel_id", (dest["id"],)).fetchall()
                with ccon:
                    n = write_synthetic(ccon, dest, hotels, now_iso())
                    set_meta(ccon, f"synthetic_done:{dest['id']}", {"at": now_iso(), "hotels": n})
                summary["done"].append(dest["id"])
                continue
            raw = obtain_raw(dest, http, network, sleep=sleep)
            if raw is None:
                summary["no_data"].append(dest["id"])
                continue
            cands, feats, source, fetched_at, bbox = raw
            with ocon, ccon:
                st = write_destination(dest, cands, feats, source, fetched_at, bbox, ocon, ccon, stages)
                info = {**st, "at": now_iso(), "stages": list(stages)}
                set_meta(ccon, f"{done_key}:{dest['id']}", info)
                if "hotels" in stages:
                    for k in ("districts_done", "synthetic_done"):
                        if k.split("_")[0] in stages:
                            set_meta(ccon, f"{k}:{dest['id']}", info)
            if source == "photon":
                summary["photon"].append(dest["id"])
            summary["done"].append(dest["id"])
            log(f"hotels: {dest['id']}: {st.get('hotels', '-')} hotels of {st['candidates']} candidates "
                f"({source}, {time.monotonic() - t0:.0f} s)")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"hotels: {dest['id']} failed: {type(e).__name__}: {e}")
            summary["failed"].append(dest["id"])
    with ccon:
        set_meta(ccon, "source.hotels", {"osm": "Overpass API (OSM, ODbL) tourism=* objects with a readable name",
                                         "fallback": "Photon (photon.komoot.io) when Overpass is down",
                                         "max_per_destination": MAX_HOTELS,
                                         "synthetic": "data_build/synthetic.py + data_build/prices.py"})
    ocon.close()
    ccon.close()
    nd = summary["no_data"]
    nd_txt = f"{len(nd)}" + (f" ({', '.join(nd[:8])}{', ...' if len(nd) > 8 else ''})" if nd else "")
    log(f"hotels pipeline: done {len(summary['done'])}, skipped {len(summary['skipped'])}, "
        f"photon {summary['photon']}, no data {nd_txt}, failed {summary['failed']}")
    return summary
