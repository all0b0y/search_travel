"""Stage `pois`: notable sights and named districts around each destination (OSM + Wikidata).

Why: users ask for a hotel «рядом с Айя-Софией», «в районе Султанахмет» or send a photo of a sight;
the engine needs that point to measure hotel distances from. Offline like the rest of opendata.

Per destination (resumable: meta `pois_done:<id>` is written in the same transaction as its rows):
1. Overpass, one batched query in a square of half side max(POI_MIN_HALF_KM, POI_HALF_X x the
   hotel bbox half side) around the destination centre: sights that carry a `wikidata` tag (tourism
   attraction/museum/gallery/viewpoint/zoo/aquarium/theme_park, historic=* except tombs, graves and
   memorials (HISTORIC_SIGHTS), places of worship and church/mosque/cathedral buildings, theatres,
   parks, gardens, stadiums, towers, lighthouses,
   bridges, squares, pedestrian streets, beaches/peaks/bays/capes) and named districts
   (place=suburb/quarter/neighbourhood). Raw JSON cached in data_build/cache/overpass_pois/<id>.json.
2. Wikidata wbgetentities for the sights' items: ru/en labels, ru/en aliases and the number of
   sitelinks (notability). Cached per batch in data_build/cache/wikidata/<key>.json. One SPARQL query
   per destination finds items that are humans (P31 Q5): OSM often tags a tomb or a statue with the
   person's item (Istanbul: «Мехмед II», 122 sitelinks, on a türbe); such a sight keeps its OSM names
   only, without the item, its labels or its sitelinks.
3. Sights: one row per Wikidata item (a node and a way of the same sight collapse to the way),
   ranked by sitelinks, then a Russian name present, then name; the top SIGHT_KEEP kept.
   Districts: every one with a readable name (Latin/Cyrillic), at most DISTRICT_KEEP, Wikidata first,
   then nearest to the centre (Istanbul alone has ~400 named suburbs and quarters in the box).

`names` holds every OSM name and Wikidata label/alias (ru, en, local) for matching a user's words;
`name` is the display name for a Russian user (name:ru, the Wikidata ru label, then en/local).

    python -m data_build.pois [--out DB] [--only id1,id2] [--limit N] [--resume] [--no-network]
    python -m data_build.pois --merge-from data_build/cache/pois_stage.sqlite   # into data/opendata.sqlite

Building into a staging DB (--out) keeps data/opendata.sqlite untouched while the engine reads it;
--merge-from then replaces the staged destinations' rows in one transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from .common import (OPENDATA_DB, HttpxClient, RateLimiter, bbox_around, cache_path, cached_text, chunks,
                     haversine_m, log, open_db, set_meta)
from .overpass import OverpassDown, overpass_fetch, readable

POI_MIN_HALF_KM = 8.0
POI_HALF_X = 2.5          # x the hotel bbox half side (data_build.destinations half_km, 4 km by default)
SIGHT_KEEP = 120
DISTRICT_KEEP = 400
QUERY_TIMEOUT_S = 180
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_BATCH = 50
WIKIDATA_INTERVAL_S = 0.5
SPARQL_URL = "https://query.wikidata.org/sparql"
DISTRICT_PLACES = ("suburb", "quarter", "neighbourhood")
# historic=* values that are sights a traveller names; tombs, graves and memorial plaques are left out
# (their wikidata is often the buried or remembered person, and nobody books «рядом с мемориальной доской»)
HISTORIC_SIGHTS = ("castle", "fort", "fortress", "city_gate", "citywalls", "palace", "manor", "monument", "ruins",
                   "archaeological_site", "building", "church", "mosque", "monastery", "temple", "tower", "aqueduct",
                   "bridge", "ship", "district", "heritage", "fountain", "square", "yes")
NAME_KEYS = ("name:ru", "name", "name:en", "int_name", "official_name:ru", "official_name", "official_name:en",
             "alt_name:ru", "alt_name", "alt_name:en", "old_name:ru", "old_name", "short_name:ru", "short_name",
             "loc_name")
_QID = re.compile(r"Q\d+")


def build_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return f"""[out:json][timeout:{QUERY_TIMEOUT_S}][bbox:{s},{w},{n},{e}];
(
  nwr["tourism"~"^(attraction|museum|gallery|viewpoint|zoo|aquarium|theme_park)$"]["wikidata"];
  nwr["historic"~"^({'|'.join(HISTORIC_SIGHTS)})$"]["wikidata"];
  nwr["amenity"~"^(place_of_worship|theatre)$"]["wikidata"];
  nwr["building"~"^(cathedral|church|mosque|temple|chapel|synagogue)$"]["wikidata"];
  nwr["leisure"~"^(park|garden|stadium)$"]["wikidata"];
  nwr["man_made"~"^(tower|lighthouse|bridge)$"]["wikidata"];
  nwr["place"="square"]["wikidata"];
  way["highway"="pedestrian"]["wikidata"];
  nwr["natural"~"^(beach|peak|bay|cape)$"]["wikidata"];
);
out center tags qt;
nwr["place"~"^({'|'.join(DISTRICT_PLACES)})$"]["name"];
out center tags qt;
"""


def sight_kind(tags: dict) -> str:
    t = tags.get
    if t("tourism") in ("museum", "gallery"):
        return "museum"
    if t("amenity") == "place_of_worship" or t("building") in ("cathedral", "church", "mosque", "temple", "chapel",
                                                               "synagogue"):
        return "worship"
    if t("amenity") == "theatre":
        return "theatre"
    if t("tourism") in ("zoo", "aquarium", "theme_park"):
        return "entertainment"
    if t("leisure") in ("park", "garden"):
        return "park"
    if t("leisure") == "stadium":
        return "stadium"
    if t("place") == "square":
        return "square"
    if t("highway") == "pedestrian":
        return "street"
    if t("natural") == "beach":
        return "beach"
    if t("natural") in ("peak", "bay", "cape"):
        return "nature"
    if t("tourism") == "viewpoint":
        return "viewpoint"
    if t("man_made") in ("tower", "lighthouse", "bridge"):
        return "landmark"
    if t("historic") in ("castle", "fort", "fortress", "city_gate", "citywalls", "palace", "manor"):
        return "castle"
    if t("historic"):
        return "monument"
    return "attraction"


def osm_names(tags: dict) -> list[str]:
    out: list[str] = []
    for k in NAME_KEYS:
        for v in str(tags.get(k) or "").split(";"):
            v = v.strip()
            if v and v not in out:
                out.append(v)
    return out


def _point(el: dict):
    if el.get("lat") is not None and el.get("lon") is not None:
        return float(el["lat"]), float(el["lon"])
    c = el.get("center") or {}
    if c.get("lat") is not None and c.get("lon") is not None:
        return float(c["lat"]), float(c["lon"])
    return None


def parse_elements(data: dict) -> tuple[dict, dict]:
    """({qid: sight}, {key: district}) from an Overpass answer; sights collapse per Wikidata item
    (a way or relation wins over a node of the same item), districts per readable display name."""
    sights: dict[str, dict] = {}
    districts: dict[str, dict] = {}
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        pt = _point(el)
        if pt is None or not el.get("type") or el.get("id") is None:
            continue
        osm_id = f"{el['type'][0]}{el['id']}"
        rec = {"osm_id": osm_id, "lat": pt[0], "lon": pt[1], "tags": tags, "names": osm_names(tags)}
        if tags.get("place") in DISTRICT_PLACES:
            name = next((n for n in (tags.get("name:ru"), tags.get("name:en"), tags.get("name"))
                         if n and readable(n)), None)
            if not name:
                continue
            key = name.casefold()
            prev = districts.get(key)
            if prev is None or (not prev["tags"].get("wikidata") and tags.get("wikidata")):
                districts[key] = rec
            continue
        qid = str(tags.get("wikidata") or "").split(";")[0].strip()
        if not _QID.fullmatch(qid):
            continue
        prev = sights.get(qid)
        if prev is None or (prev["osm_id"][0] == "n" and osm_id[0] != "n"):
            if prev is not None:
                rec["names"] = list(dict.fromkeys(rec["names"] + prev["names"]))
            sights[qid] = rec
        else:
            prev["names"] = list(dict.fromkeys(prev["names"] + rec["names"]))
    return sights, districts


def _wikidata_batch(http, batch: list[str], limiter: RateLimiter, sleep, attempts: int = 5) -> str:
    last = None
    for i in range(attempts):
        limiter.wait()
        try:
            r = http.get(WIKIDATA_URL, params={"action": "wbgetentities", "ids": "|".join(batch),
                                               "props": "labels|aliases|sitelinks", "languages": "ru|en",
                                               "format": "json"}, timeout=60)   # reads: no maxlag
            if r.status == 200:
                data = json.loads(r.text)
                if "entities" in data:
                    return r.text
                last = str((data.get("error") or {}).get("code") or "no entities")
            else:
                last = f"HTTP {r.status}"
        except Exception as e:  # network errors and timeouts
            last = f"{type(e).__name__}: {str(e)[:120]}"
        log(f"pois: wikidata attempt {i + 1}/{attempts} failed ({last})")
        sleep(5 * (i + 1))
    raise RuntimeError(f"wikidata unavailable: {last}")


def wikidata_info(http, qids, limiter: RateLimiter, sleep=time.sleep, network: bool = True) -> dict:
    """{qid: {ru, en, aliases, sitelinks}}; batches cached by their id list. Without network only
    cached batches are read (the rest is simply missing)."""
    out: dict[str, dict] = {}
    for batch in chunks(sorted(set(qids), key=lambda q: int(q[1:])), WIKIDATA_BATCH):
        key = hashlib.sha1("|".join(batch).encode()).hexdigest()[:20]
        path = cache_path("wikidata", f"{key}.json")
        if not network and not path.exists():
            continue
        text, _ = cached_text(path, lambda b=batch: _wikidata_batch(http, b, limiter, sleep))
        for req_id, ent in (json.loads(text).get("entities") or {}).items():
            if "missing" in ent:
                continue
            labels, aliases = ent.get("labels") or {}, ent.get("aliases") or {}
            info = {"ru": (labels.get("ru") or {}).get("value"), "en": (labels.get("en") or {}).get("value"),
                    "aliases": [a.get("value") for lang in ("ru", "en") for a in aliases.get(lang) or []
                                if a.get("value")],
                    "sitelinks": len(ent.get("sitelinks") or {})}
            for q in {req_id, ent.get("id"), (ent.get("redirects") or {}).get("from")}:
                if q:
                    out[q] = info
    return out


def wikidata_humans(http, qids, sleep=time.sleep, network: bool = True, attempts: int = 4) -> set:
    """Items among `qids` that are humans (P31 Q5), one SPARQL query, cached by the id list."""
    ids = sorted(set(qids), key=lambda q: int(q[1:]))
    if not ids:
        return set()
    path = cache_path("wikidata_humans", hashlib.sha1("|".join(ids).encode()).hexdigest()[:20] + ".json")
    if not network and not path.exists():
        return set()

    def fetch():
        query = "SELECT ?item WHERE { VALUES ?item { %s } ?item wdt:P31 wd:Q5 . }" % " ".join("wd:" + q for q in ids)
        last = None
        for i in range(attempts):
            try:
                r = http.post(SPARQL_URL, data={"query": query, "format": "json"},
                              headers={"Accept": "application/sparql-results+json"}, timeout=120)
                if r.status == 200:
                    json.loads(r.text)
                    return r.text
                last = f"HTTP {r.status}"
            except Exception as e:  # network errors, timeouts, invalid JSON
                last = f"{type(e).__name__}: {str(e)[:120]}"
            log(f"pois: wikidata sparql attempt {i + 1}/{attempts} failed ({last})")
            sleep(10 * (i + 1))
        raise RuntimeError(f"wikidata sparql unavailable: {last}")

    text, _ = cached_text(path, fetch)
    bindings = (json.loads(text).get("results") or {}).get("bindings") or []
    return {b["item"]["value"].rsplit("/", 1)[-1] for b in bindings if (b.get("item") or {}).get("value")}


def select_rows(dest: dict, sights: dict, districts: dict, wd: dict, fetched_at: str,
                humans: frozenset | set = frozenset(), max_m: float | None = None) -> list[tuple]:
    """Rows for the pois table (column order of INSERT_SQL). Items in `humans` are a person tagged on
    the object: the sight keeps its OSM names only, without the item. Objects whose centre lies farther
    than `max_m` from the destination centre are dropped (a gulf or a park relation reaching into the
    box has its centre far away: «Финский залив», 220 km from Saint Petersburg's centre)."""
    def dist(r):
        return int(round(haversine_m(dest["lat"], dest["lon"], r["lat"], r["lon"])))

    if max_m is not None:
        sights = {q: r for q, r in sights.items() if dist(r) <= max_m}
        districts = {k: r for k, r in districts.items() if dist(r) <= max_m}
    picked = []
    for qid, r in sights.items():
        tags = r["tags"]
        if qid in humans:
            qid, info = None, {}
        else:
            info = wd.get(qid) or {}
        name_ru = tags.get("name:ru") or info.get("ru")
        name_en = tags.get("name:en") or info.get("en")
        display = next((n for n in (name_ru, name_en, tags.get("name")) if n and readable(n)), None)
        if display is None:
            continue
        names = list(dict.fromkeys([n for n in [name_ru, name_en, info.get("ru"), info.get("en")] if n]
                                   + r["names"] + list(info.get("aliases") or [])))
        picked.append({"r": r, "kind": sight_kind(tags), "name": display, "name_ru": name_ru, "name_en": name_en,
                       "names": names, "qid": qid, "sitelinks": info.get("sitelinks"),
                       "source": "overpass+wikidata" if info else "overpass"})
    picked.sort(key=lambda p: (-(p["sitelinks"] or 0), 0 if p["name_ru"] else 1, p["name"].casefold(),
                               p["r"]["osm_id"]))
    rows = []
    for rank, p in enumerate(picked[:SIGHT_KEEP], 1):
        r = p["r"]
        rows.append((dest["id"], r["osm_id"], p["kind"], p["name"], p["name_ru"], p["name_en"],
                     json.dumps(p["names"], ensure_ascii=False), r["lat"], r["lon"], dist(r), p["qid"],
                     p["sitelinks"], rank, p["source"], fetched_at))
    ds = []
    for r in districts.values():
        tags = r["tags"]
        qid = str(tags.get("wikidata") or "").split(";")[0].strip() or None
        info = (wd.get(qid) or {}) if qid else {}
        name_ru = tags.get("name:ru") or info.get("ru")
        name_en = tags.get("name:en") or info.get("en")
        display = next((n for n in (name_ru, name_en, tags.get("name")) if n and readable(n)), None)
        if display is None:
            continue
        names = list(dict.fromkeys([n for n in [name_ru, name_en] if n] + r["names"] + list(info.get("aliases") or [])))
        ds.append((r, display, name_ru, name_en, names, qid, info.get("sitelinks")))
    ds.sort(key=lambda d: (0 if d[5] else 1, -(d[6] or 0), dist(d[0]), d[1].casefold(), d[0]["osm_id"]))
    for rank, (r, display, name_ru, name_en, names, qid, sl) in enumerate(ds[:DISTRICT_KEEP], 1):
        rows.append((dest["id"], r["osm_id"], "district", display, name_ru, name_en,
                     json.dumps(names, ensure_ascii=False), r["lat"], r["lon"], dist(r), qid, sl, rank,
                     "overpass+wikidata" if sl is not None else "overpass", fetched_at))
    return rows


INSERT_SQL = ("INSERT OR REPLACE INTO pois (destination_id, osm_id, kind, name, name_ru, name_en, names, lat, lon, "
              "dist_center_m, wikidata, sitelinks, rank, source, fetched_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
COLUMNS = ("destination_id, osm_id, kind, name, name_ru, name_en, names, lat, lon, dist_center_m, wikidata, "
           "sitelinks, rank, source, fetched_at")


def _half_km(dest_id: str) -> float:
    from .destinations import BY_ID
    d = BY_ID.get(dest_id)
    return max(POI_MIN_HALF_KM, POI_HALF_X * (d.half_km if d is not None else 4.0))


def run_pois(source_db=OPENDATA_DB, out_db=None, http=None, only=None, limit=None, resume=False,
             network=True, sleep=time.sleep) -> dict:
    out_db = Path(out_db or source_db)
    src = sqlite3.connect(str(source_db))
    src.row_factory = sqlite3.Row
    dests = [dict(r) for r in src.execute("SELECT * FROM destinations ORDER BY id")]
    src.close()
    if only:
        dests = [d for d in dests if d["id"] in only]
    if limit:
        dests = dests[:limit]
    con = open_db(out_db, "opendata")
    if Path(out_db).resolve() != Path(source_db).resolve():
        cols = [r[1] for r in con.execute("PRAGMA table_info(destinations)")]
        with con:   # the staging DB needs the destination rows (foreign key), never merged back
            con.executemany(f"INSERT OR IGNORE INTO destinations ({', '.join(cols)}) "
                            f"VALUES ({', '.join('?' for _ in cols)})",
                            [tuple(d.get(c) for c in cols) for d in dests])
    if network and http is None:
        http = HttpxClient()
    limiter = RateLimiter(WIKIDATA_INTERVAL_S, sleep=sleep)
    stats = {"done": 0, "skipped": 0, "sights": 0, "districts": 0, "no_cache": [], "failed": [], "empty": []}
    for d in dests:
        if resume and con.execute("SELECT 1 FROM meta WHERE key=?", (f"pois_done:{d['id']}",)).fetchone():
            stats["skipped"] += 1
            continue
        path = cache_path("overpass_pois", f"{d['id']}.json")
        if not network and not path.exists():
            stats["no_cache"].append(d["id"])
            continue
        bbox = bbox_around(d["lat"], d["lon"], _half_km(d["id"]))
        try:
            text, fetched_at = cached_text(path, lambda b=bbox: overpass_fetch(http, build_query(b), sleep=sleep))
            sights, districts = parse_elements(json.loads(text))
            wd = wikidata_info(http, list(sights) + [str(r["tags"].get("wikidata")).split(";")[0].strip()
                                                     for r in districts.values()
                                                     if _QID.fullmatch(str(r["tags"].get("wikidata") or "")
                                                                       .split(";")[0].strip())],
                               limiter, sleep=sleep, network=network)
            try:
                humans = wikidata_humans(http, list(sights), sleep=sleep, network=network)
            except RuntimeError as e:   # no person filter rather than no sights
                log(f"pois: {d['id']}: {e}; persons not filtered")
                humans = set()
        except (OverpassDown, RuntimeError, ValueError) as e:
            log(f"pois: {d['id']} failed: {e}")
            stats["failed"].append(d["id"])
            continue
        rows = select_rows(d, sights, districts, wd, fetched_at, humans, max_m=_half_km(d["id"]) * 1000 * 1.45)
        n_s = sum(1 for r in rows if r[2] != "district")
        with con:
            con.execute("DELETE FROM pois WHERE destination_id=?", (d["id"],))
            con.executemany(INSERT_SQL, rows)
            set_meta(con, f"pois_done:{d['id']}", {"sights": n_s, "districts": len(rows) - n_s, "at": fetched_at})
        stats["done"] += 1
        stats["sights"] += n_s
        stats["districts"] += len(rows) - n_s
        if not rows:
            stats["empty"].append(d["id"])
        top = ", ".join(r[3] for r in rows[:3] if r[2] != "district")
        log(f"pois: {d['id']} ok: {n_s} sights, {len(rows) - n_s} districts{' (' + top + ')' if top else ''}")
        if network:
            sleep(1.0)    # be gentle with the public Overpass instances
    with con:
        set_meta(con, "source.pois", {"osm": "Overpass API (OSM, ODbL): sights with a wikidata tag, named "
                                              "place=suburb/quarter/neighbourhood",
                                      "wikidata": "wbgetentities labels/aliases (ru, en), sitelinks (CC0)",
                                      "sight_keep": SIGHT_KEEP, "district_keep": DISTRICT_KEEP,
                                      "half_km": f"max({POI_MIN_HALF_KM}, {POI_HALF_X} x hotel half_km)"})
    con.close()
    log(f"pois: {stats}")
    return stats


def merge(stage_db, into_db=OPENDATA_DB) -> int:
    """Replace the staged destinations' rows of `into_db` with the staging DB's rows (one transaction)."""
    con = open_db(Path(into_db), "opendata")
    con.execute("ATTACH DATABASE ? AS stage", (str(stage_db),))
    with con:
        ids = [r[0] for r in con.execute("SELECT DISTINCT destination_id FROM stage.pois")]
        con.executemany("DELETE FROM main.pois WHERE destination_id=?", [(i,) for i in ids])
        con.execute(f"INSERT INTO main.pois ({COLUMNS}) SELECT {COLUMNS} FROM stage.pois")
        con.execute("INSERT OR REPLACE INTO main.meta (key, value) SELECT key, value FROM stage.meta "
                    "WHERE key LIKE 'pois_done:%' OR key = 'source.pois'")
        n = con.execute("SELECT COUNT(*) FROM main.pois").fetchone()[0]
    con.execute("DETACH DATABASE stage")
    con.close()
    log(f"pois: merged {len(ids)} destinations into {into_db} ({n} rows)")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--out", default=None, help="DB to write (default data/opendata.sqlite)")
    ap.add_argument("--only", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--merge-from", default=None, help="staging DB to merge into data/opendata.sqlite")
    args = ap.parse_args(argv)
    if args.merge_from:
        merge(args.merge_from)
        return 0
    try:
        run_pois(out_db=args.out, only=set(args.only.split(",")) if args.only else None, limit=args.limit,
                 resume=args.resume, network=not args.no_network)
    except KeyboardInterrupt:
        log("pois: interrupted; finished destinations are committed, rerun with --resume")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
