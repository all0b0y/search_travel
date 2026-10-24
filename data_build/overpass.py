"""Batched Overpass query per destination (hotels + district POIs) and the Photon fallback.

Overpass is slow and flaky (AGENT_PLAN section 8): one query per destination, mirrors rotated
with backoff, [timeout:180] in the query and a longer HTTP timeout, every successful raw response
cached in data_build/cache/overpass/<dest>.json. Photon (photon.komoot.io) is used only when
Overpass stays down for a destination; its records are filtered by distance to the bbox.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .common import HttpxClient, RateLimiter, cache_path, cached_text, haversine_m, log, now_iso, write_atomic
from .districts import Features

MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
)
PHOTON_URL = "https://photon.komoot.io/api/"
HOTEL_KINDS = ("hotel", "hostel", "apartment", "guest_house", "motel", "resort", "chalet")
AMENITY_CATS = ("bar", "pub", "nightclub", "restaurant", "cafe", "fast_food", "pharmacy")
SHOP_CATS = ("supermarket", "convenience")
LEISURE_CATS = ("playground", "park")
QUERY_TIMEOUT_S = 180
HTTP_TIMEOUT_S = 240


def build_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    kinds = "|".join(HOTEL_KINDS)
    return f"""[out:json][timeout:{QUERY_TIMEOUT_S}][bbox:{s},{w},{n},{e}];
nwr["tourism"~"^({kinds})$"];
out center qt;
(
  nwr["amenity"~"^({'|'.join(AMENITY_CATS)})$"];
  nwr["shop"~"^({'|'.join(SHOP_CATS)})$"];
  nwr["leisure"~"^({'|'.join(LEISURE_CATS)})$"];
  node["railway"~"^(station|halt)$"];
  node["public_transport"~"^(stop_position|platform)$"];
);
out center qt;
(
  nwr["natural"="beach"];
  way["highway"~"^(motorway|trunk|primary|pedestrian)$"];
  way["railway"="rail"];
);
out geom qt;
"""


class OverpassDown(RuntimeError):
    pass


def overpass_fetch(http, query: str, *, attempts: int = 6, sleep=time.sleep, deadline: float | None = None,
                   http_timeout: float = HTTP_TIMEOUT_S, clock=time.monotonic) -> str:
    """POST the query, rotating mirrors with exponential backoff. Returns the raw JSON text."""
    last = None
    for i in range(attempts):
        url = MIRRORS[i % len(MIRRORS)]
        timeout = http_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - clock())
            if timeout <= 1:
                break
        try:
            r = http.post(url, data={"data": query}, timeout=timeout)
            if r.status == 200:
                try:
                    data = json.loads(r.text)
                except ValueError:
                    last = f"{url}: invalid JSON"
                else:
                    remark = str(data.get("remark") or "")
                    if "runtime error" in remark.lower() or "timed out" in remark.lower():
                        last = f"{url}: {remark[:160]}"
                    else:
                        return r.text
            else:
                last = f"{url}: HTTP {r.status}"
        except Exception as e:  # network errors and timeouts
            last = f"{url}: {type(e).__name__}: {str(e)[:120]}"
        log(f"overpass: attempt {i + 1}/{attempts} failed ({last})")
        if i + 1 < attempts:
            pause = min(120, 10 * 2 ** (i // len(MIRRORS))) if deadline is None else 1
            sleep(pause)
    raise OverpassDown(last or "overpass unavailable")


# ------------------------------------------------------------------ parsing

@dataclass
class HotelCandidate:
    osm_id: str
    name: str
    kind: str
    lat: float
    lon: float
    tags: dict = field(default_factory=dict)


_READABLE = re.compile(r"[A-Za-zА-Яа-яЁё]")
_OTHER_LETTER = re.compile(r"[^\W\d_A-Za-zА-Яа-яЁё]", re.UNICODE)


def readable(s: str) -> bool:
    """True when the name is mostly Latin/Cyrillic (readable for a Russian user)."""
    if not s or not _READABLE.search(s):
        return False
    return len(_READABLE.findall(s)) >= len(_OTHER_LETTER.findall(s))


def pick_name(tags: dict) -> str | None:
    for k in ("name", "name:en", "name:ru", "int_name"):
        v = (tags.get(k) or "").strip()
        if v and readable(v):
            return v
    return None


def _coords(el: dict) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    c = el.get("center")
    if c:
        return float(c["lat"]), float(c["lon"])
    g = el.get("geometry")
    if g:
        pts = [(p["lat"], p["lon"]) for p in g if p]
        if pts:
            return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    b = el.get("bounds")
    if b:
        return (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
    return None


def _geom_lines(el: dict) -> list[list[tuple[float, float]]]:
    if el.get("geometry"):
        return [[(p["lat"], p["lon"]) for p in el["geometry"] if p]]
    out = []
    for m in el.get("members") or []:
        if m.get("geometry"):
            out.append([(p["lat"], p["lon"]) for p in m["geometry"] if p])
    return out


def parse_overpass(data: dict) -> tuple[list[HotelCandidate], Features]:
    hotels: dict[str, HotelCandidate] = {}
    feats = Features()
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        typ = el.get("type")
        osm_id = f"{typ}/{el.get('id')}"
        xy = _coords(el)
        if xy is None:
            continue
        lat, lon = xy
        tourism = tags.get("tourism")
        if tourism in HOTEL_KINDS and osm_id not in hotels:
            name = pick_name(tags)
            if name:
                hotels[osm_id] = HotelCandidate(osm_id, name, tourism, lat, lon, tags)
        amen = tags.get("amenity")
        if amen in AMENITY_CATS:
            feats.add_point(amen, lat, lon)
        if tags.get("shop") in SHOP_CATS:
            feats.add_point(tags["shop"], lat, lon)
        if tags.get("leisure") in LEISURE_CATS:
            feats.add_point(tags["leisure"], lat, lon)
        if tags.get("railway") in ("station", "halt") and typ == "node":
            feats.add_point("station", lat, lon)
        if tags.get("public_transport") in ("stop_position", "platform") and typ == "node":
            feats.add_point("stop", lat, lon)
        if tags.get("natural") == "beach":
            lines = _geom_lines(el)
            if lines:
                for ln in lines:
                    feats.add_line("beach", ln)
            else:
                feats.add_point("beach", lat, lon)
        hw = tags.get("highway")
        if typ == "way" and hw in ("motorway", "trunk", "primary"):
            for ln in _geom_lines(el):
                feats.add_line("major_road", ln)
        if typ == "way" and hw == "pedestrian":
            for ln in _geom_lines(el):
                feats.add_line("pedestrian", ln)
        if typ == "way" and tags.get("railway") == "rail":
            for ln in _geom_lines(el):
                feats.add_line("railway", ln)
    return sorted(hotels.values(), key=lambda h: h.osm_id), feats


# ------------------------------------------------------------------ Photon fallback

PHOTON_HOTEL_QUERIES = {k: k.replace("_", " ") for k in HOTEL_KINDS}
PHOTON_POI_QUERIES = {
    **{c: ("amenity", c) for c in AMENITY_CATS},
    "supermarket": ("shop", "supermarket"), "convenience": ("shop", "convenience"),
    "playground": ("leisure", "playground"), "park": ("leisure", "park"),
    "beach": ("natural", "beach"), "station": ("railway", "station"),
    "stop": ("highway", "bus_stop"),
}
_PHOTON_TYPES = {"N": "node", "W": "way", "R": "relation"}


def photon_fetch_all(http, dest_id: str, lat: float, lon: float, *, limiter: RateLimiter | None = None,
                     limit: int = 50) -> dict:
    """All Photon queries for one destination, cached in one file. Returns {key: geojson}."""
    path = cache_path("photon", f"{dest_id}.json")

    def fetch():
        out = {}
        jobs = [(f"tourism:{k}", q, f"tourism:{k}") for k, q in PHOTON_HOTEL_QUERIES.items()]
        jobs += [(f"poi:{cat}", val.replace("_", " "), f"{key}:{val}") for cat, (key, val) in PHOTON_POI_QUERIES.items()]
        for name, q, tag in jobs:
            if limiter:
                limiter.wait()
            r = http.get(PHOTON_URL, params={"q": q, "lat": lat, "lon": lon, "osm_tag": tag, "limit": limit},
                         timeout=30)
            if r.status != 200:
                raise RuntimeError(f"Photon HTTP {r.status} for {name}")
            out[name] = json.loads(r.text)
        return json.dumps(out, ensure_ascii=False)
    text, _ = cached_text(path, fetch)
    return json.loads(text)


def parse_photon(results: dict, bbox: tuple[float, float, float, float]) -> tuple[list[HotelCandidate], Features]:
    s, w, n, e = bbox
    hotels: dict[str, HotelCandidate] = {}
    feats = Features()
    for key, fc in results.items():
        for f in (fc or {}).get("features") or []:
            p = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (s <= lat <= n and w <= lon <= e):
                continue
            if key.startswith("tourism:"):
                kind = p.get("osm_value")
                if kind not in HOTEL_KINDS:
                    continue
                name = (p.get("name") or "").strip()
                if not readable(name):
                    continue
                osm_id = f"{_PHOTON_TYPES.get(p.get('osm_type'), 'node')}/{p.get('osm_id')}"
                tags = {"addr:street": p.get("street"), "addr:housenumber": p.get("housenumber"),
                        "addr:city": p.get("city")}
                hotels.setdefault(osm_id, HotelCandidate(osm_id, name, kind, lat, lon,
                                                         {k: v for k, v in tags.items() if v}))
            else:
                cat = key.split(":", 1)[1]
                feats.add_point(cat, lat, lon)
    return sorted(hotels.values(), key=lambda h: h.osm_id), feats


def load_overpass_cache(dest_id: str) -> tuple[dict, str] | None:
    path = cache_path("overpass", f"{dest_id}.json")
    if not path.exists():
        return None
    text, fetched_at = cached_text(path, lambda: "")
    return json.loads(text), fetched_at


def fetch_overpass_cached(http, dest_id: str, bbox, **kw) -> tuple[dict, str]:
    path = cache_path("overpass", f"{dest_id}.json")
    query = build_query(bbox)
    text, fetched_at = cached_text(path, lambda: overpass_fetch(http, query, **kw))
    write_atomic(cache_path("overpass", f"{dest_id}.query.txt"), query)
    return json.loads(text), fetched_at
