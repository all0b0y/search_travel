"""Places, distances and local time for the mock.

Time zones (all times the mock emits are local with the UTC offset of the place):
1. `destinations.timezone` (IANA) from opendata, when the place is a catalog destination or an
   airport listed in a destination's `airports`;
2. RU_AIRPORT_TZ for common Russian airports (Russia spans 11 zones);
3. COUNTRY_TZ for single-zone (or practically single-zone for tourism) countries;
4. Russia by longitude bands (RU_LON_BANDS, an approximation of the zone borders);
5. otherwise a fixed offset round(lon / 15) hours (documented fallback, no DST).
Real IANA names go through `zoneinfo` (stdlib), so DST is applied where the zone has it.

KNOWN_CITIES: real coordinates / IATA codes / station names of common origin cities that may
not be catalog destinations (e.g. Moscow). Catalog destinations always win over this table.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db

COUNTRY_TZ = {
    "TR": "Europe/Istanbul", "AE": "Asia/Dubai", "EG": "Africa/Cairo", "TH": "Asia/Bangkok",
    "GE": "Asia/Tbilisi", "AM": "Asia/Yerevan", "AZ": "Asia/Baku", "UZ": "Asia/Tashkent",
    "KZ": "Asia/Almaty", "KG": "Asia/Bishkek", "TJ": "Asia/Dushanbe", "BY": "Europe/Minsk",
    "CY": "Asia/Nicosia", "GR": "Europe/Athens", "IT": "Europe/Rome", "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon", "FR": "Europe/Paris", "DE": "Europe/Berlin", "AT": "Europe/Vienna",
    "CZ": "Europe/Prague", "HU": "Europe/Budapest", "RS": "Europe/Belgrade", "ME": "Europe/Podgorica",
    "HR": "Europe/Zagreb", "BG": "Europe/Sofia", "AL": "Europe/Tirane", "MV": "Indian/Maldives",
    "LK": "Asia/Colombo", "IN": "Asia/Kolkata", "VN": "Asia/Ho_Chi_Minh", "CN": "Asia/Shanghai",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "QA": "Asia/Qatar", "OM": "Asia/Muscat",
    "SA": "Asia/Riyadh", "BH": "Asia/Bahrain", "KW": "Asia/Kuwait", "IL": "Asia/Jerusalem",
    "JO": "Asia/Amman", "TN": "Africa/Tunis", "MA": "Africa/Casablanca", "SC": "Indian/Mahe",
    "MU": "Indian/Mauritius", "CU": "America/Havana", "DO": "America/Santo_Domingo",
    "MY": "Asia/Kuala_Lumpur", "SG": "Asia/Singapore", "PH": "Asia/Manila", "MN": "Asia/Ulaanbaatar",
    "TZ": "Africa/Dar_es_Salaam", "KE": "Africa/Nairobi", "AB": "Europe/Moscow", "GB": "Europe/London",
    "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "CH": "Europe/Zurich", "PL": "Europe/Warsaw",
    "FI": "Europe/Helsinki", "EE": "Europe/Tallinn", "LV": "Europe/Riga", "LT": "Europe/Vilnius",
    "MT": "Europe/Malta", "SI": "Europe/Ljubljana", "SK": "Europe/Bratislava", "BA": "Europe/Sarajevo",
    "MK": "Europe/Skopje", "MD": "Europe/Chisinau", "IR": "Asia/Tehran", "NP": "Asia/Kathmandu",
}
RU_AIRPORT_TZ = {
    "SVO": "Europe/Moscow", "DME": "Europe/Moscow", "VKO": "Europe/Moscow", "ZIA": "Europe/Moscow",
    "LED": "Europe/Moscow", "AER": "Europe/Moscow", "KZN": "Europe/Moscow", "KRR": "Europe/Moscow",
    "MRV": "Europe/Moscow", "ROV": "Europe/Moscow", "MCX": "Europe/Moscow", "GOJ": "Europe/Moscow",
    "VOZ": "Europe/Moscow", "VOG": "Europe/Volgograd", "AAQ": "Europe/Moscow", "GDZ": "Europe/Moscow",
    "SIP": "Europe/Simferopol", "MMK": "Europe/Moscow", "ARH": "Europe/Moscow", "KGD": "Europe/Kaliningrad",
    "KUF": "Europe/Samara", "ULV": "Europe/Ulyanovsk", "ASF": "Europe/Astrakhan", "GSV": "Europe/Saratov",
    "UFA": "Asia/Yekaterinburg", "SVX": "Asia/Yekaterinburg", "PEE": "Asia/Yekaterinburg",
    "CEK": "Asia/Yekaterinburg", "TJM": "Asia/Yekaterinburg", "SGC": "Asia/Yekaterinburg",
    "OMS": "Asia/Omsk", "OVB": "Asia/Novosibirsk", "BAX": "Asia/Barnaul", "KJA": "Asia/Krasnoyarsk",
    "IKT": "Asia/Irkutsk", "UUD": "Asia/Irkutsk", "YKS": "Asia/Yakutsk", "VVO": "Asia/Vladivostok",
    "KHV": "Asia/Vladivostok", "UUS": "Asia/Sakhalin", "GDX": "Asia/Magadan", "PKC": "Asia/Kamchatka",
    "IAR": "Europe/Moscow", "PKV": "Europe/Moscow", "IJK": "Europe/Samara",
}
RU_LON_BANDS = [  # (max longitude, zone) — approximation of the Russian zone borders
    (22.9, "Europe/Kaliningrad"), (47.5, "Europe/Moscow"), (55.0, "Europe/Samara"),
    (68.0, "Asia/Yekaterinburg"), (79.0, "Asia/Omsk"), (97.0, "Asia/Novosibirsk"),
    (114.0, "Asia/Irkutsk"), (130.0, "Asia/Yakutsk"), (147.0, "Asia/Vladivostok"),
    (160.0, "Asia/Magadan"), (181.0, "Asia/Kamchatka"),
]
METRO_CODES = {"MOW": ["SVO", "DME", "VKO", "ZIA"], "IST": ["IST", "SAW"], "LED": ["LED"]}
HUBS = ["IST", "DXB", "SVO", "VKO", "DME", "DOH", "AUH", "LED", "SAW", "TAS", "ALA", "EVN", "TBS", "BAH"]


def norm(text: str) -> str:
    return " ".join(str(text).casefold().replace("ё", "е").split())


@dataclass
class City:
    key: str
    name_ru: str
    country_code: str
    lat: float
    lon: float
    iata: list[str] = field(default_factory=list)
    stations: list[str] = field(default_factory=list)
    tz: str | None = None
    destination_id: str | None = None
    kind: str = "city"
    tags: list[str] = field(default_factory=list)


def _c(name, cc, lat, lon, iata, stations=None, tz=None, aliases=()):
    return name, aliases, City(key=name.casefold(), name_ru=name, country_code=cc, lat=lat, lon=lon, iata=iata,
                               stations=stations if stations is not None else [name], tz=tz)


_KNOWN = [
    _c("Москва", "RU", 55.7558, 37.6173, ["SVO", "DME", "VKO", "ZIA"], ["Москва"], "Europe/Moscow",
       ("moscow", "мск", "moskva")),
    _c("Санкт-Петербург", "RU", 59.9386, 30.3141, ["LED"], ["Санкт-Петербург"], "Europe/Moscow",
       ("питер", "спб", "петербург", "saint petersburg", "st petersburg", "st. petersburg", "sankt-peterburg")),
    _c("Казань", "RU", 55.7963, 49.1088, ["KZN"], None, "Europe/Moscow", ("kazan",)),
    _c("Нижний Новгород", "RU", 56.3269, 44.0059, ["GOJ"], None, "Europe/Moscow", ("nizhny novgorod",)),
    _c("Екатеринбург", "RU", 56.8389, 60.6057, ["SVX"], None, "Asia/Yekaterinburg", ("yekaterinburg",)),
    _c("Новосибирск", "RU", 55.0084, 82.9357, ["OVB"], None, "Asia/Novosibirsk", ("novosibirsk",)),
    _c("Самара", "RU", 53.1959, 50.1002, ["KUF"], None, "Europe/Samara", ("samara",)),
    _c("Краснодар", "RU", 45.0355, 38.9753, ["KRR"], None, "Europe/Moscow", ("krasnodar",)),
    _c("Ростов-на-Дону", "RU", 47.2357, 39.7015, ["ROV"], None, "Europe/Moscow", ("rostov-on-don", "ростов")),
    _c("Сочи", "RU", 43.5855, 39.7231, ["AER"], None, "Europe/Moscow", ("sochi",)),
    _c("Уфа", "RU", 54.7388, 55.9721, ["UFA"], None, "Asia/Yekaterinburg", ("ufa",)),
    _c("Пермь", "RU", 58.0105, 56.2502, ["PEE"], None, "Asia/Yekaterinburg", ("perm",)),
    _c("Воронеж", "RU", 51.6720, 39.1843, ["VOZ"], None, "Europe/Moscow", ("voronezh",)),
    _c("Волгоград", "RU", 48.7080, 44.5133, ["VOG"], None, "Europe/Volgograd", ("volgograd",)),
    _c("Калининград", "RU", 54.7104, 20.4522, ["KGD"], None, "Europe/Kaliningrad", ("kaliningrad",)),
    _c("Красноярск", "RU", 56.0153, 92.8932, ["KJA"], None, "Asia/Krasnoyarsk", ("krasnoyarsk",)),
    _c("Владивосток", "RU", 43.1155, 131.8855, ["VVO"], None, "Asia/Vladivostok", ("vladivostok",)),
    _c("Иркутск", "RU", 52.2870, 104.3050, ["IKT"], None, "Asia/Irkutsk", ("irkutsk",)),
    _c("Тюмень", "RU", 57.1530, 65.5343, ["TJM"], None, "Asia/Yekaterinburg", ("tyumen",)),
    _c("Челябинск", "RU", 55.1644, 61.4368, ["CEK"], None, "Asia/Yekaterinburg", ("chelyabinsk",)),
    _c("Омск", "RU", 54.9885, 73.3242, ["OMS"], None, "Asia/Omsk", ("omsk",)),
    _c("Ярославль", "RU", 57.6261, 39.8845, ["IAR"], None, "Europe/Moscow", ("yaroslavl",)),
    _c("Тула", "RU", 54.1931, 37.6173, [], None, "Europe/Moscow", ("tula",)),
    _c("Владимир", "RU", 56.1290, 40.4066, [], None, "Europe/Moscow", ("vladimir",)),
    _c("Тверь", "RU", 56.8587, 35.9176, [], None, "Europe/Moscow", ("tver",)),
    _c("Великий Новгород", "RU", 58.5215, 31.2755, [], None, "Europe/Moscow", ("veliky novgorod",)),
    _c("Псков", "RU", 57.8136, 28.3496, ["PKV"], None, "Europe/Moscow", ("pskov",)),
    _c("Минск", "BY", 53.9006, 27.5590, ["MSQ"], None, "Europe/Minsk", ("minsk",)),
]
KNOWN_CITIES: dict[str, City] = {}
for _name, _aliases, _city in _KNOWN:
    for _k in (_name, *_aliases):
        KNOWN_CITIES[norm(_k)] = _city

# Moscow has one terminal per direction; trains leave from the station of their direction.
MOSCOW_STATIONS = [  # (bearing from, bearing to, station, keyword in catalog station names)
    (0, 20, "Москва Ленинградская", "Ленинград"), (20, 65, "Москва Ярославская", "Ярослав"),
    (65, 125, "Москва Казанская", "Казан"), (125, 170, "Москва Павелецкая", "Павелец"),
    (170, 215, "Москва Курская", "Курск"), (215, 250, "Москва Киевская", "Киев"),
    (250, 290, "Москва Белорусская", "Белорус"), (290, 360, "Москва Ленинградская", "Ленинград"),
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


@lru_cache(maxsize=512)
def zone(name: str | None, country_code: str | None = None, lon: float | None = None,
         iata: str | None = None) -> tzinfo:
    """Resolve a tzinfo by the documented priority (see module docstring)."""
    candidates = [name, RU_AIRPORT_TZ.get(iata or "")]
    cc = (country_code or "").upper()
    if cc == "RU" and lon is not None:
        candidates.append(next(z for max_lon, z in RU_LON_BANDS if lon <= max_lon))
    candidates.append(COUNTRY_TZ.get(cc))
    for cand in candidates:
        if cand:
            try:
                return ZoneInfo(cand)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    hours = round((lon or 0.0) / 15)
    return timezone(timedelta(hours=hours))


def local_dt(day: date, minutes: int, tz: tzinfo) -> datetime:
    """`minutes` after local midnight of `day` (may exceed a day)."""
    return datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(minutes=minutes)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hhmm_to_min(value: str) -> int:
    hh, _, mm = value.partition(":")
    return int(hh) * 60 + int(mm)


# ------------------------------------------------------------------ catalog lookups

def destination(dest_id: str) -> dict | None:
    rows = db.opendata().query("destinations", "id = ?", (dest_id,), key="id")
    if not rows:
        return None
    d = rows[0]
    d["tags"] = db.jload(d.get("tags"), [])
    d["airports"] = db.jload(d.get("airports"), [])
    d["stations"] = db.jload(d.get("stations"), [])
    return d


def destination_tz(dest: dict) -> tzinfo:
    first_airport = (dest.get("airports") or [None])[0]
    return zone(dest.get("timezone"), dest.get("country_code"), dest.get("lon"), first_airport)


def _dest_city(d: dict) -> City:
    return City(key=d["id"], name_ru=d["name_ru"], country_code=d["country_code"], lat=d["lat"], lon=d["lon"],
                iata=list(d["airports"]), stations=list(d["stations"]), tz=d.get("timezone"),
                destination_id=d["id"], kind=d.get("kind") or "city", tags=list(d["tags"]))


def resolve_city(name: str) -> City | None:
    """Catalog destination (id, name_ru, name_en) first, then KNOWN_CITIES, then airports.city."""
    if not name:
        return None
    n = norm(name)
    od = db.opendata()
    rows = od.query("destinations", "lower(id) = ? OR lower(name_ru) = ? OR lower(name_en) = ?",
                    (n, n, n), key="id")
    if not rows:  # sqlite lower() is ASCII-only: compare Cyrillic names in Python
        rows = [r for r in od.query("destinations", cols="id, name_ru, name_en", key="id")
                if n in (norm(r["id"]), norm(r["name_ru"]), norm(r["name_en"]))]
        rows = [destination(r["id"]) for r in rows[:1]]
    else:
        rows = [destination(rows[0]["id"])]
    known = KNOWN_CITIES.get(n)
    if rows and rows[0]:
        city = _dest_city(rows[0])
        if known:  # fill gaps the catalog leaves (e.g. no stations recorded)
            city.iata = city.iata or list(known.iata)
            city.stations = city.stations or list(known.stations)
            city.tz = city.tz or known.tz
        return city
    if known:
        return City(**{**known.__dict__, "iata": list(known.iata), "stations": list(known.stations),
                       "tags": list(known.tags)})
    ap = [a for a in od.query("airports", "iata IS NOT NULL", key="ident") if a.get("city") and norm(a["city"]) == n]
    if ap:
        ap.sort(key=lambda a: (a["type"] != "large_airport", a["iata"]))
        lat = sum(a["lat"] for a in ap) / len(ap)
        lon = sum(a["lon"] for a in ap) / len(ap)
        return City(key=n, name_ru=ap[0]["city"], country_code=ap[0]["country_code"], lat=lat, lon=lon,
                    iata=[a["iata"] for a in ap], stations=[], tz=None)
    return None


@lru_cache(maxsize=4096)
def _airport_cached(cache_key: tuple, iata: str) -> dict | None:
    rows = db.opendata().query("airports", "iata = ?", (iata,), key="ident")
    if not rows:
        return None
    a = dict(rows[0])
    dests = [d for d in db.opendata().query("destinations", "airports LIKE ?", (f'%"{iata}"%',), key="id")]
    a["destination_id"] = dests[0]["id"] if dests else None
    a["tz_name"] = dests[0].get("timezone") if dests else None
    a["dest_kind"] = dests[0].get("kind") if dests else None
    a["dest_tags"] = db.jload(dests[0].get("tags"), []) if dests else []
    return a


def airport(iata: str) -> dict | None:
    return _airport_cached(db._cache_key(db.opendata_paths()), iata.upper())


def airport_tz(ap: dict) -> tzinfo:
    return zone(ap.get("tz_name"), ap.get("country_code"), ap.get("lon"), ap.get("iata"))


def expand_iata(codes: list[str]) -> list[str]:
    out: list[str] = []
    for c in codes:
        for x in METRO_CODES.get(c.upper(), [c.upper()]):
            if x not in out:
                out.append(x)
    return out


def station_for(city: City, towards: City | None) -> str | None:
    """The departure/arrival station; Moscow has one terminal per direction (by bearing)."""
    if not city.stations:
        return None
    if city.name_ru == "Москва" and towards is not None:
        b = bearing_deg(city.lat, city.lon, towards.lat, towards.lon)
        for lo, hi, default, keyword in MOSCOW_STATIONS:
            if lo <= b < hi:
                named = [st for st in city.stations if keyword in st]
                return named[0] if named else default
    return city.stations[0]
