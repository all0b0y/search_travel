"""Open-data tools of the agent (AGENT_PLAN.md section 8, decision 13).

    od = OpenData()                                   # data/opendata.sqlite + runtime/opendata_extra.sqlite
    od.find_destinations(["sea", "warm"], 11, "Москва", segment="family")
    od.climate("antalya", 11); od.district_profile("node/123"); od.airports_near(36.9, 30.8)
    od.geocode("Стамбуле"); od.travel_time("Москва", "antalya"); od.cbr_rates()

Readers always union the committed database and the overlay written by data_build/lazy.py (the
committed row wins on an id conflict). Missing files read as empty: every method then returns its
"no data" value (None / []), never a guess.

Network: only through the injected `http` client (httpx-like: `get(url, params=, headers=, timeout=)`
returning an object with `status_code`, `json()`, `content`), used by `geocode` (Nominatim) and
`cbr_rates` (CBR XML_daily). `lazy_builder(query) -> destination_id | None` is called at most once
per name per OpenData instance.

Scoring constants (documented; override with `OpenData(..., weights=..., climate=...)`):
- destination score = 0.4 trait match + 0.3 climate fit + 0.2 travel time + 0.1 popularity
  (components without data are dropped and the rest renormalised; family weighs travel x1.5;
  friends get +0.05 for a `nightlife` tag). Budget is not scored here: prices come from the mock's
  quote_destinations afterwards.
- warm sea: air max 20 °C -> 0 .. 26 °C -> 1, sea 19 °C -> 0 .. 24 °C -> 1 (no marine data -> 0);
  warm without sea: air only; cold/snow/ski: air max 8 °C -> 0 .. 0 °C -> 1; otherwise comfortable
  15..28 °C -> 1, 0 at 0 °C and 38 °C; rain: x (1 - min(rain_days, 20) / 40).
- climate traits derived for matching: warm >= 24 °C air max, hot >= 30 °C, cold <= 8 °C, snow <= 0 °C;
  with climate data for the month they replace the static warm/hot/cold/snow tags; without it the
  static tags count and the climate component is a neutral 0.5 (item `climate_known: false`), so a
  place with a known good month outranks one without data. hot and
  tropical imply warm.
- travel time: great circle / 750 km/h + 0.5 h per flight, + 2 h per connection; direct routes or one
  stop via a hub from the OpenFlights graph; score 1 up to 4 h, 0 at 14 h.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
USER_AGENT = "trip-agent-demo/0.1 (Ostrovok track; offline-first)"

WEIGHTS = {"trait": 0.4, "climate": 0.3, "travel": 0.2, "popularity": 0.1}
CLIMATE = {"warm_air": (20.0, 26.0), "warm_sea": (19.0, 24.0), "cold_air": (8.0, 0.0), "comfort": (15.0, 28.0),
           "comfort_zero": (0.0, 38.0), "rain_cap_days": 20, "rain_halving_days": 40,
           "trait_warm": 24.0, "trait_hot": 30.0, "trait_cold": 8.0, "trait_snow": 0.0}
CRUISE_KMH = 750.0
FLIGHT_OVERHEAD_H = 0.5
CONNECTION_H = 2.0
TRAVEL_FULL_H, TRAVEL_ZERO_H = 4.0, 14.0
NEAR_CATALOG_KM = 40.0
DISTRICT_LABEL_MIN = 0.6
WARM_WISH = {"warm", "hot", "tropical", "beach"}
SEA_WISH = {"sea", "beach", "island"}
COLD_WISH = {"cold", "snow", "ski"}
CLIMATE_TAGS = {"warm", "hot", "cold", "snow"}
UNKNOWN_CLIMATE = 0.5
# Coordinates of the origin cities offered in questions.json, used only when the catalog has no row.
ORIGIN_FALLBACK = {"москва": (55.7558, 37.6173), "санкт-петербург": (59.9343, 30.3351),
                   "екатеринбург": (56.8389, 60.6057), "казань": (55.7963, 49.1088),
                   "новосибирск": (55.0084, 82.9357)}
# Colloquial names -> catalog names (normalised). Only unambiguous, well-known ones.
ALIASES = {"питер": "санкт петербург", "спб": "санкт петербург", "петербург": "санкт петербург",
           "мск": "москва", "екб": "екатеринбург", "ебург": "екатеринбург", "нск": "новосибирск",
           "новосиб": "новосибирск", "нижний": "нижний новгород", "калининград": "калининград"}
_PREPOSITIONS = ("в ", "во ", "на ", "из ", "до ")
_ENDINGS = ("ами", "ями", "ого", "ему", "ой", "ей", "ом", "ем", "ах", "ях", "ам", "ям", "е", "у", "ю", "и", "ы", "а", "я", "ь")


def _resolve(path):
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return REPO / p


def gc_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ramp(x, zero, full):
    """0 at `zero`, 1 at `full` (either direction), clamped."""
    if x is None:
        return 0.0
    if full == zero:
        return 1.0 if x >= full else 0.0
    return max(0.0, min(1.0, (x - zero) / (full - zero)))


def norm_name(text):
    s = str(text or "").replace("ё", "е").replace("Ё", "Е").casefold()
    s = re.sub(r"[\-‐–—_/]", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = " ".join(s.split())
    for p in _PREPOSITIONS:
        if s.startswith(p) and len(s) > len(p) + 2:
            s = s[len(p):]
            break
    return s


def _stems(s):
    """The name and its forms without one or two Russian case endings ("анталию" ~ "анталья")."""
    out = {s}
    for _ in range(2):
        for w in list(out):
            for end in _ENDINGS:
                if w.endswith(end) and len(w) - len(end) >= (3 if w == s else 4):
                    out.add(w[: -len(end)])
    return out


def name_score(query, name):
    q, n = norm_name(query), norm_name(name)
    q = ALIASES.get(q, q)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if _stems(q) & _stems(n):
        return 0.92
    if len(q) >= 5 and (n.startswith(q) or q.startswith(n)) and abs(len(n) - len(q)) <= 3:
        return 0.85
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    return round(ratio * 0.95, 3) if ratio >= 0.82 else 0.0


def _json(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class OpenData:
    def __init__(self, path="data/opendata.sqlite", overlay="runtime/opendata_extra.sqlite", http=None,
                 lazy_builder=None, cache_dir="runtime", today=None, weights=None, climate=None):
        self.path = _resolve(path)
        self.overlay = _resolve(overlay) if overlay else None
        self.http = http
        self.lazy_builder = lazy_builder
        self.cache_dir = _resolve(cache_dir)
        self._today = today
        self.weights = {**WEIGHTS, **(weights or {})}
        self.climate_rules = {**CLIMATE, **(climate or {})}
        self._lazy_tried: set[str] = set()

    # ------------------------------------------------------------ low level

    def _dbs(self):
        for p in (self.path, self.overlay):
            if p is not None and Path(p).is_file():
                yield p

    def _query(self, sql, params=()):
        rows = []
        for p in self._dbs():
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                rows += [dict(r) for r in con.execute(sql, params)]
            except sqlite3.OperationalError:
                pass  # a table missing in the overlay
            finally:
                con.close()
        return rows

    @staticmethod
    def _dedupe(rows, key):
        seen, out = set(), []
        for r in rows:
            k = tuple(r[c] for c in key) if isinstance(key, tuple) else r[key]
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    @property
    def available(self):
        return Path(self.path).is_file()

    # ------------------------------------------------------------ destinations

    def _dest(self, row):
        d = dict(row)
        for k in ("tags", "airports", "stations"):
            d[k] = _json(d.get(k), [])
        if "aliases" in d:
            d["aliases"] = _json(d.get("aliases"), [])
        return d

    def destinations(self):
        return [self._dest(r) for r in self._dedupe(self._query("SELECT * FROM destinations"), "id")]

    def destination(self, dest_id):
        rows = self._query("SELECT * FROM destinations WHERE id = ?", (dest_id,))
        return self._dest(rows[0]) if rows else None

    def climate(self, dest_id, month):
        rows = self._query("SELECT * FROM climate WHERE destination_id = ? AND month = ?", (dest_id, int(month)))
        if not rows:
            return None
        r = rows[0]
        return {k: r.get(k) for k in ("destination_id", "month", "air_max_c", "air_min_c", "sea_c", "rain_days",
                                      "sunny_days", "source", "fetched_at")}

    def district_profile(self, osm_id):
        rows = self._query("SELECT * FROM districts WHERE osm_id = ?", (osm_id,))
        if not rows:
            return None
        r = rows[0]
        scores = {k: float(r[k]) for k in ("lively", "quiet", "family", "walkable")}
        return {"osm_id": r["osm_id"], "destination_id": r.get("destination_id"),
                "labels": [k for k, v in scores.items() if v >= DISTRICT_LABEL_MIN], "scores": scores,
                "counts": _json(r.get("poi_counts"), {}), "nearest_m": _json(r.get("nearest_m"), {}),
                "source": r.get("source"), "fetched_at": r.get("fetched_at")}

    def districts_for(self, osm_ids):
        return {o: p for o in osm_ids if (p := self.district_profile(o)) is not None}

    # ------------------------------------------------------------ airports and routes

    def airport(self, iata):
        rows = self._query("SELECT * FROM airports WHERE iata = ?", (iata,))
        return rows[0] if rows else None

    def airports_near(self, lat, lon, radius_km=150, limit=5, types=("large_airport", "medium_airport")):
        dlat = radius_km / 111.0
        dlon = radius_km / max(1e-6, 111.0 * math.cos(math.radians(lat)))
        rows = self._query("SELECT * FROM airports WHERE iata IS NOT NULL AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                           (lat - dlat, lat + dlat, lon - dlon, lon + dlon))
        out = []
        for r in self._dedupe(rows, "iata"):
            if types and r["type"] not in types:
                continue
            d = gc_km(lat, lon, r["lat"], r["lon"])
            if d <= radius_km:
                out.append({"iata": r["iata"], "name": r["name"], "city": r.get("city"),
                            "country_code": r["country_code"], "lat": r["lat"], "lon": r["lon"], "type": r["type"],
                            "distance_km": round(d, 1)})
        out.sort(key=lambda a: (a["type"] != "large_airport", a["distance_km"]))
        return out[:limit]

    def _place_airports(self, dest):
        codes = [c for c in dest.get("airports") or [] if self.airport(c)]
        if codes:
            return codes
        return [a["iata"] for a in self.airports_near(dest["lat"], dest["lon"], 150, 3)]

    def origin_airports(self, origin_city):
        """IATA codes for an origin city: an IATA code itself, the catalog row, airports.city, then coordinates."""
        text = str(origin_city or "").strip()
        if re.fullmatch(r"[A-Z]{3}", text) and self.airport(text):
            return [text]
        hit = self._catalog_match(text)
        if hit is not None:
            codes = self._place_airports(hit[0])
            if codes:
                return codes
        key = norm_name(text)
        rows = [r for r in self._query("SELECT * FROM airports WHERE iata IS NOT NULL AND type IN ('large_airport','medium_airport')")
                if r.get("city") and norm_name(r["city"]) == key]
        if rows:
            return [r["iata"] for r in self._dedupe(rows, "iata")]
        if key in ORIGIN_FALLBACK:
            lat, lon = ORIGIN_FALLBACK[key]
            return [a["iata"] for a in self.airports_near(lat, lon, 80, 4)]
        return []

    def _routes_from(self, origins):
        if not origins:
            return []
        q = ",".join("?" * len(origins))
        return self._query(f"SELECT origin_iata, dest_iata, stops FROM routes WHERE origin_iata IN ({q})", tuple(origins))

    def _routes_to(self, dests):
        if not dests:
            return []
        q = ",".join("?" * len(dests))
        return self._query(f"SELECT origin_iata, dest_iata, stops FROM routes WHERE dest_iata IN ({q})", tuple(dests))

    def _coords(self, iata):
        a = self.airport(iata)
        return (a["lat"], a["lon"]) if a else None

    def _leg_h(self, a, b):
        ca, cb = self._coords(a), self._coords(b)
        if not ca or not cb:
            return None, None
        km = gc_km(*ca, *cb)
        return km / CRUISE_KMH + FLIGHT_OVERHEAD_H, km

    def travel_time(self, origin_city, dest_id, _origins=None):
        """Flight time estimate {hours, stops, via, from_iata, to_iata, distance_km, source} or None.

        Direct routes first, else one stop via a hub (routes graph); great-circle speed plus overheads.
        """
        dest = self.destination(dest_id)
        if dest is None:
            return None
        origins = _origins if _origins is not None else self.origin_airports(origin_city)
        dests = self._place_airports(dest)
        if not origins or not dests:
            return None
        if set(origins) & set(dests):
            return {"hours": 0.0, "stops": 0, "via": None, "from_iata": origins[0], "to_iata": origins[0],
                    "distance_km": 0.0, "source": "same_airport"}
        best = None
        out_routes = self._routes_from(origins)
        dset = set(dests)
        for r in out_routes:
            if r["dest_iata"] in dset:
                h, km = self._leg_h(r["origin_iata"], r["dest_iata"])
                if h is None:
                    continue
                stops = int(r.get("stops") or 0)
                h += stops * CONNECTION_H
                cand = {"hours": round(h, 1), "stops": stops, "via": None, "from_iata": r["origin_iata"],
                        "to_iata": r["dest_iata"], "distance_km": round(km), "source": "routes+great_circle"}
                if best is None or (cand["stops"], cand["hours"]) < (best["stops"], best["hours"]):
                    best = cand
        if best is not None and best["stops"] == 0:
            return best
        first = {}
        for r in out_routes:
            if int(r.get("stops") or 0) == 0:
                first.setdefault(r["dest_iata"], set()).add(r["origin_iata"])
        second = {}
        for r in self._routes_to(dests):
            if int(r.get("stops") or 0) == 0:
                second.setdefault(r["origin_iata"], set()).add(r["dest_iata"])
        for hub in set(first) & set(second):
            for o in first[hub]:
                for d in second[hub]:
                    h1, km1 = self._leg_h(o, hub)
                    h2, km2 = self._leg_h(hub, d)
                    if h1 is None or h2 is None:
                        continue
                    h = h1 + h2 + CONNECTION_H
                    cand = {"hours": round(h, 1), "stops": 1, "via": hub, "from_iata": o, "to_iata": d,
                            "distance_km": round(km1 + km2), "source": "routes+great_circle"}
                    if best is None or cand["hours"] < best["hours"]:
                        best = cand
        return best

    # ------------------------------------------------------------ find destinations

    def _climate_traits(self, clim):
        c = self.climate_rules
        if not clim or clim.get("air_max_c") is None:
            return set()
        t = clim["air_max_c"]
        out = set()
        if t >= c["trait_warm"]:
            out.add("warm")
        if t >= c["trait_hot"]:
            out.add("hot")
        if t <= c["trait_cold"]:
            out.add("cold")
        if t <= c["trait_snow"]:
            out.add("snow")
        return out

    def climate_fit(self, traits, clim):
        """0..1 fit of the month's climate to the wishes (thresholds in the module docstring)."""
        if not clim:
            return 0.0
        c = self.climate_rules
        wishes = set(traits or [])
        air = clim.get("air_max_c")
        if wishes & COLD_WISH:
            score = ramp(air, *c["cold_air"])
        elif wishes & (WARM_WISH | SEA_WISH):
            parts = [ramp(air, *c["warm_air"])]
            if wishes & SEA_WISH:
                parts.append(ramp(clim.get("sea_c"), *c["warm_sea"]) if clim.get("sea_c") is not None else 0.0)
            score = sum(parts) / len(parts)
        else:
            lo, hi = c["comfort"]
            z_lo, z_hi = c["comfort_zero"]
            if air is None:
                score = 0.0
            elif lo <= air <= hi:
                score = 1.0
            elif air < lo:
                score = ramp(air, z_lo, lo)
            else:
                score = ramp(air, z_hi, hi)
        rain = clim.get("rain_days")
        if rain is not None:
            score *= 1 - min(rain, c["rain_cap_days"]) / c["rain_halving_days"]
        return round(score, 3)

    def find_destinations(self, traits, month, origin_city, budget=None, max_travel_hours=None, segment=None,
                          limit=8, exclude=()):
        """Ranked destinations for the wishes. Items: id, name, country, kind, tags, match, weather,
        travel_time_h, stops, via, popularity, score, breakdown. `budget` is not scored (see module doc)."""
        traits = list(traits or [])
        origin_key = norm_name(origin_city)
        origins = self.origin_airports(origin_city) if origin_city else []
        out = []
        for d in self.destinations():
            if d["id"] in exclude or (origin_key and origin_key in (norm_name(d["name_ru"]), norm_name(d["name_en"]))):
                continue
            clim = self.climate(d["id"], month) if month else None
            have = set(d["tags"]) | ({d["kind"]} if d.get("kind") else set())
            if clim and clim.get("air_max_c") is not None:   # the month's climate replaces static climate tags
                have = (have - CLIMATE_TAGS) | self._climate_traits(clim)
            if have & {"hot", "tropical"}:
                have.add("warm")
            match = [t for t in traits if t in have]
            tt = self.travel_time(origin_city, d["id"], _origins=origins) if origin_city else None
            if max_travel_hours and (tt is None or tt["hours"] > max_travel_hours):
                continue
            parts = {"trait": len(match) / len(traits) if traits else 0.5,
                     "popularity": float(d.get("popularity") or 0)}
            if month:   # unknown climate: neutral prior, flagged climate_known=False (not a fact)
                parts["climate"] = self.climate_fit(traits, clim) if clim else UNKNOWN_CLIMATE
            if origin_city:
                parts["travel"] = 0.0 if tt is None else ramp(tt["hours"], TRAVEL_ZERO_H, TRAVEL_FULL_H)
            w = dict(self.weights)
            if segment == "family":
                w["travel"] = w["travel"] * 1.5
            total = sum(w[k] for k in parts)
            score = sum(w[k] * v for k, v in parts.items()) / total
            if segment == "friends" and "nightlife" in d["tags"]:
                score += 0.05
            weather = None
            if clim:
                weather = {k: clim.get(k) for k in ("air_max_c", "air_min_c", "sea_c", "rain_days", "sunny_days")}
            out.append({"id": d["id"], "name": d["name_ru"], "name_en": d["name_en"], "country": d["country"],
                        "country_code": d["country_code"], "kind": d["kind"], "tags": d["tags"],
                        "lat": d["lat"], "lon": d["lon"], "airports": d["airports"],
                        "description_ru": d.get("description_ru"), "popularity": d.get("popularity"),
                        "match": match, "weather": weather, "climate_known": bool(clim),
                        "travel_time_h": tt["hours"] if tt else None, "stops": tt["stops"] if tt else None,
                        "via": tt["via"] if tt else None, "score": round(min(1.0, score), 3),
                        "breakdown": {k: round(v, 3) for k, v in parts.items()}})
        out.sort(key=lambda x: (-x["score"], x["id"]))
        return out[:limit]

    # ------------------------------------------------------------ geocode

    def _catalog_match(self, name):
        best = None
        for d in self.destinations():
            names = [d["name_ru"], d["name_en"]] + list(d.get("aliases") or [])
            s = max(name_score(name, n) for n in names if n)
            if s and (best is None or s > best[1]):
                best = (d, s)
        if best and best[1] >= 0.8:
            return best
        return None

    def _hit(self, d, source, confidence):
        return {"id": d["id"], "name": d["name_ru"], "name_en": d["name_en"], "country": d["country"],
                "country_code": d["country_code"], "lat": d["lat"], "lon": d["lon"], "in_catalog": True,
                "source": source, "confidence": round(confidence, 3)}

    def _nominatim(self, name):
        if self.http is None:
            return None
        try:
            resp = self.http.get(NOMINATIM_URL, params={"q": name, "format": "jsonv2", "limit": 1,
                                                        "addressdetails": 1, "accept-language": "ru"},
                                 headers={"User-Agent": USER_AGENT}, timeout=10)
            if getattr(resp, "status_code", 200) != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        if not data:
            return None
        top = data[0]
        try:
            lat, lon = float(top["lat"]), float(top["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        address = top.get("address") or {}
        return {"name": top.get("name") or str(top.get("display_name", name)).split(",")[0], "lat": lat, "lon": lon,
                "country": address.get("country"), "country_code": (address.get("country_code") or "").upper() or None,
                "display_name": top.get("display_name")}

    def geocode(self, name):
        """Catalog fuzzy match (ru/en names, aliases; case, ё/е, prepositions and case endings ignored),
        then Nominatim (snapped to a catalog place within 40 km), then lazy_builder once per name.
        Returns a place dict (`in_catalog`, `source`, `confidence`) or None."""
        if not str(name or "").strip():
            return None
        hit = self._catalog_match(name)
        if hit is not None:
            return self._hit(hit[0], "catalog", hit[1])
        place = self._nominatim(name)
        if place is not None:
            near = min(((d, gc_km(place["lat"], place["lon"], d["lat"], d["lon"])) for d in self.destinations()),
                       key=lambda x: x[1], default=None)
            if near and near[1] <= NEAR_CATALOG_KM:
                return self._hit(near[0], "nominatim+catalog", 0.8)
        key = norm_name(name)
        if self.lazy_builder is not None and key not in self._lazy_tried:
            self._lazy_tried.add(key)
            try:
                new_id = self.lazy_builder(name)
            except Exception:
                new_id = None
            if new_id:
                d = self.destination(new_id)
                if d is not None:
                    return self._hit(d, "lazy", 0.8)
        if place is not None:
            return {"id": None, "name": place["name"], "name_en": None, "country": place["country"],
                    "country_code": place["country_code"], "lat": place["lat"], "lon": place["lon"],
                    "in_catalog": False, "source": "nominatim", "confidence": 0.6}
        return None

    # ------------------------------------------------------------ CBR rates

    def _today_date(self):
        if self._today is None:
            return date.today()
        return self._today() if callable(self._today) else self._today

    def cbr_rates(self):
        """{"source": "CBR", "date", "rub_per_unit", "stale": bool, "available": bool}.

        Uses the file cache (runtime/cbr_rates.json) when fetched today; else live via `http`; on any
        failure the cached rates with their date and stale=True; with no cache available=False."""
        from .checks import parse_cbr_xml
        cache = Path(self.cache_dir) / "cbr_rates.json"
        cached = None
        if cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
        today = self._today_date().isoformat()
        if cached and cached.get("fetched_on") == today:
            return {**{k: cached[k] for k in ("source", "date", "rub_per_unit")}, "stale": False, "available": True}
        if self.http is not None:
            try:
                resp = self.http.get(CBR_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
                if getattr(resp, "status_code", 200) == 200:
                    parsed = parse_cbr_xml(resp.content)
                    record = {**parsed, "fetched_on": today,
                              "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                    return {**parsed, "stale": False, "available": True}
            except Exception:
                pass
        if cached:
            return {**{k: cached[k] for k in ("source", "date", "rub_per_unit")}, "stale": True, "available": True}
        return {"source": "CBR", "date": None, "rub_per_unit": {}, "stale": True, "available": False}
