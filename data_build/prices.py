"""Curated price levels and season factors for the synthetic catalog (all synthetic, documented).

`PRICE_LEVEL_RUB[country_code]` is a curated estimate of a typical nightly rate in RUB (2026) for
a standard double room in a 3-star hotel in an average month; `CITY_PRICE_LEVEL_RUB` overrides it
for destinations that are notably dearer or cheaper than the country average. These are NOT real
prices; they only keep the synthetic catalog plausible (Maldives dearer than Vietnam, Moscow
dearer than Kostroma).

Nightly base price of a room (rooms.base_price_rub, before the season factor):
    level(destination) * STARS_FACTOR[stars] * KIND_FACTOR[kind] * room_factor * jitter,
    jitter = 0.85 .. 1.20 from sha256(osm_id | 'price'), rounded to 50 RUB.
Season factor by month (stored per destination in catalog meta 'synthetic.season_factors'):
    SEASON_PROFILES[season_profile(destination)][month - 1]; the mock multiplies the base price.
Meal price multipliers (catalog meta 'synthetic.meal_multipliers'): MEAL_MULTIPLIERS.
"""
from __future__ import annotations

PRICE_LEVEL_RUB = {
    "RU": 6500, "TR": 7000, "EG": 5500, "AE": 11000, "TH": 5000, "VN": 4000, "LK": 4500, "MV": 25000,
    "ID": 5500, "CN": 6000, "CY": 10000, "GR": 10000, "ES": 11000, "IT": 12000, "PT": 10000, "FR": 14000,
    "TN": 5000, "IN": 4000, "MU": 14000, "SC": 18000, "TZ": 8000, "CU": 6000, "DO": 9000, "MX": 10000,
    "ME": 7000, "RS": 6000, "HR": 10000, "CZ": 8000, "HU": 7500, "AT": 11000, "DE": 11000, "NL": 14000,
    "GB": 16000, "GE": 5000, "AM": 5000, "AZ": 6000, "UZ": 4500, "KZ": 5500, "KG": 4000, "BY": 4500,
    "JP": 11000, "KR": 9000, "MY": 5000, "SG": 15000, "QA": 11000, "OM": 9000, "JO": 8000, "MA": 6500,
    "US": 18000,
}
DEFAULT_PRICE_LEVEL_RUB = 8000

CITY_PRICE_LEVEL_RUB = {
    "moscow": 9500, "saint-petersburg": 8500, "sochi": 9000, "adler": 8000, "krasnaya-polyana": 11000,
    "kazan": 7000, "kaliningrad": 6500, "kamchatka": 9500, "teriberka": 8500, "murmansk": 6500,
    "altai": 6000, "listvyanka": 7000, "olkhon": 6000, "suzdal": 6500, "plyos": 9000, "sheregesh": 7000,
    "elbrus": 6500, "dombay": 6000, "arkhyz": 6500, "anapa": 5500, "gelendzhik": 6500, "kostroma": 5000,
    "dubai": 12000, "abu-dhabi": 11000, "ras-al-khaimah": 10000, "istanbul": 8000, "bodrum": 9000,
    "cappadocia": 8000, "paris": 16000, "nice": 13000, "london": 18000, "venice": 15000, "rome": 12500,
    "santorini": 15000, "maldives-male": 22000, "bali": 6000, "phuket": 5500, "koh-samui": 6500,
    "tbilisi": 5500, "batumi": 5000, "gudauri": 6500, "yerevan": 5500, "samarkand": 5000, "bukhara": 4500,
    "almaty": 6000, "minsk": 5000, "tokyo": 12000, "singapore": 15000, "new-york": 20000,
}

STARS_FACTOR = {0: 0.55, 1: 0.55, 2: 0.72, 3: 1.0, 4: 1.45, 5: 2.2}
KIND_FACTOR = {"hotel": 1.0, "resort": 1.35, "apartment": 0.95, "guest_house": 0.75, "hostel": 0.4,
               "motel": 0.7, "chalet": 1.2}
MEAL_MULTIPLIERS = {"none": 1.0, "breakfast": 1.12, "half_board": 1.3, "full_board": 1.45,
                    "all_inclusive": 1.7}

# Month factors Jan..Dec. Curated shapes of demand seasons; mean is close to 1.
SEASON_PROFILES = {
    "med_beach":   [0.70, 0.70, 0.75, 0.85, 1.00, 1.25, 1.45, 1.50, 1.20, 0.95, 0.75, 0.80],
    "ru_sea":      [0.80, 0.75, 0.75, 0.80, 0.95, 1.25, 1.55, 1.60, 1.20, 0.85, 0.75, 0.95],
    "red_sea":     [1.15, 1.05, 1.10, 1.15, 1.00, 0.90, 0.95, 1.00, 0.95, 1.10, 1.15, 1.30],
    "gulf":        [1.30, 1.25, 1.20, 1.05, 0.85, 0.70, 0.65, 0.70, 0.85, 1.05, 1.25, 1.40],
    "tropical":    [1.35, 1.30, 1.15, 1.05, 0.85, 0.80, 0.85, 0.85, 0.80, 0.90, 1.10, 1.40],
    "bali":        [0.90, 0.85, 0.90, 1.00, 1.05, 1.15, 1.35, 1.35, 1.15, 1.00, 0.90, 1.30],
    "ski":         [1.50, 1.45, 1.25, 0.85, 0.75, 0.85, 0.95, 0.95, 0.80, 0.75, 0.80, 1.35],
    "city":        [0.85, 0.85, 0.95, 1.00, 1.10, 1.10, 1.05, 1.00, 1.05, 1.00, 0.90, 1.15],
    "white_nights": [0.80, 0.80, 0.85, 0.95, 1.15, 1.40, 1.30, 1.15, 1.00, 0.90, 0.85, 1.15],
    "ru_nature":   [0.80, 0.85, 0.80, 0.80, 0.95, 1.25, 1.45, 1.40, 1.10, 0.85, 0.75, 0.95],
    "arctic":      [1.30, 1.30, 1.20, 0.90, 0.85, 1.00, 1.10, 1.00, 1.00, 0.90, 1.10, 1.30],
    "spa":         [0.95, 0.95, 0.95, 1.00, 1.05, 1.10, 1.15, 1.15, 1.10, 1.00, 0.95, 1.10],
}

_PROFILE_BY_ID = {
    "saint-petersburg": "white_nights", "murmansk": "arctic", "teriberka": "arctic", "kirovsk": "ski",
    "bali": "bali", "salalah": "ru_nature", "kislovodsk": "spa", "pyatigorsk": "spa", "belokurikha": "spa",
    "karlovy-vary": "spa", "issyk-kul": "ru_nature", "sevan": "ru_nature",
}
_TROPICAL = {"TH", "VN", "LK", "MV", "MY", "IN", "CU", "DO", "MX", "MU", "SC", "TZ", "SG", "CN"}
_GULF = {"AE", "QA", "OM"}
_RED_SEA = {"EG", "JO"}
_MED = {"TR", "CY", "GR", "ES", "IT", "ME", "HR", "TN", "PT", "FR", "GE", "AZ"}


def price_level(dest_id: str, country_code: str) -> int:
    if dest_id in CITY_PRICE_LEVEL_RUB:
        return CITY_PRICE_LEVEL_RUB[dest_id]
    return PRICE_LEVEL_RUB.get(country_code, DEFAULT_PRICE_LEVEL_RUB)


def season_profile(dest_id: str, country_code: str, kind: str, tags: list[str] | tuple[str, ...]) -> str:
    if dest_id in _PROFILE_BY_ID:
        return _PROFILE_BY_ID[dest_id]
    tags = set(tags)
    if "ski" in tags:
        return "ski"
    beachy = "sea" in tags and ("beach" in tags or kind == "resort")
    if country_code == "RU":
        if beachy:
            return "ru_sea"
        if kind in ("nature", "village") or "lake" in tags or "mountains" in tags:
            return "ru_nature"
        return "city"
    if country_code in _GULF:
        return "gulf"
    if country_code in _RED_SEA:
        return "red_sea"
    if country_code in _TROPICAL and ("tropical" in tags or beachy):
        return "tropical"
    if beachy and (country_code in _MED or kind == "resort"):
        return "med_beach"
    if "mountains" in tags and kind in ("nature", "village"):
        return "ru_nature"
    return "city"


def season_factors(dest_id: str, country_code: str, kind: str, tags) -> list[float]:
    return list(SEASON_PROFILES[season_profile(dest_id, country_code, kind, tags)])


def base_price_rub(level: int, stars: int | None, kind: str, room_factor: float, jitter: float) -> int:
    s = STARS_FACTOR.get(stars if stars is not None else 3, 1.0)
    k = KIND_FACTOR.get(kind, 1.0)
    raw = level * s * k * room_factor * jitter
    return max(300, int(round(raw / 50.0)) * 50)
