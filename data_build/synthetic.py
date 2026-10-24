"""Deterministic synthetic catalog fields (stage `synthetic`). Everything here is synthetic.

Every value is a pure function of the hotel's OSM id (plus its real kind, destination and OSM
stars tag): u = sha256(osm_id | field) mapped to [0, 1). Same input -> same output; different ids
-> different values. Nothing depends on the clock except `fetched_at` (generation time).

Generated per hotel:
- stars: the OSM `stars` tag when it parses to 1..5 (real, NOT listed in synthetic_fields);
  otherwise drawn from STARS_BY_KIND (listed as synthetic).
- rating 6.5..9.6: 6.5 + 3.1 * (0.65 * u + 0.35 * stars / 5); reviews: log-uniform 5..3000
  (hostels and guest houses x0.5).
- rooms: 2..4 room types from ROOM_TEMPLATES[kind] (the first template always included), with
  capacities and base nightly price (data_build/prices.py).
- meals options (same for every room of a hotel) by kind, stars and country (all-inclusive is
  typical for resort hotels in TR/EG/TN and 4-5* resorts elsewhere); price multipliers in
  prices.MEAL_MULTIPLIERS (catalog meta 'synthetic.meal_multipliers').
- policies: check-in/out times, pets (by kind), children (adults-only share), infant cot, extra
  bed price, early check-in availability and price, cancellation (free until N days before
  check-in, or non-refundable) and the penalty after the deadline.
- extras without a schema column (catalog meta 'hotel_extras:<hotel_id>'): pets_fee_rub per
  night (0 = free; null when pets are not allowed), child_free_under_age (children younger
  than this stay free on existing beds), meal_multipliers.
"""
from __future__ import annotations

import math
import re

from .common import h01, hint, hpick
from .prices import MEAL_MULTIPLIERS, base_price_rub, price_level

STARS_BY_KIND = {
    "hotel": ([2, 3, 4, 5], [0.15, 0.4, 0.33, 0.12]),
    "resort": ([3, 4, 5], [0.15, 0.5, 0.35]),
    "motel": ([1, 2, 3], [0.3, 0.5, 0.2]),
    "hostel": ([1, 2], [0.6, 0.4]),
    "guest_house": ([1, 2, 3], [0.25, 0.5, 0.25]),
    "apartment": ([2, 3, 4], [0.25, 0.5, 0.25]),
    "chalet": ([2, 3, 4], [0.2, 0.5, 0.3]),
}

# (name_ru, capacity_adults, capacity_children, bed, price factor)
ROOM_TEMPLATES = {
    "hotel": [("Стандартный двухместный", 2, 1, "double", 1.0), ("Стандартный с двумя кроватями", 2, 1, "twin", 1.0),
              ("Улучшенный", 2, 1, "double", 1.25), ("Семейный номер", 2, 2, "double+sofa", 1.5),
              ("Люкс", 2, 2, "king", 2.1), ("Одноместный", 1, 0, "single", 0.8)],
    "resort": [("Стандартный двухместный", 2, 1, "double", 1.0), ("Номер с видом на море", 2, 1, "double", 1.3),
               ("Семейный номер", 2, 2, "double+sofa", 1.55), ("Бунгало", 2, 2, "king", 1.8),
               ("Люкс", 2, 2, "king", 2.3)],
    "motel": [("Двухместный номер", 2, 1, "double", 1.0), ("Номер с двумя кроватями", 2, 1, "twin", 1.0),
              ("Семейный номер", 2, 2, "double+sofa", 1.4)],
    "hostel": [("Место в общем номере", 1, 0, "bunk", 1.0), ("Двухместный номер с общей ванной", 2, 0, "double", 2.4),
               ("Четырёхместный номер", 4, 0, "bunk", 3.6), ("Женский общий номер", 1, 0, "bunk", 1.1)],
    "guest_house": [("Двухместный номер", 2, 1, "double", 1.0), ("Трёхместный номер", 3, 1, "double+single", 1.3),
                    ("Семейный номер", 2, 2, "double+sofa", 1.5), ("Одноместный", 1, 0, "single", 0.75)],
    "apartment": [("Студия", 2, 1, "double", 1.0), ("Апартаменты с одной спальней", 2, 2, "double+sofa", 1.4),
                  ("Апартаменты с двумя спальнями", 4, 2, "double+twin", 2.0)],
    "chalet": [("Шале целиком", 4, 2, "double+twin", 1.0), ("Номер в шале", 2, 1, "double", 0.55)],
}

AI_COUNTRIES = {"TR", "EG", "TN"}
SYNTHETIC_FIELDS = ["rating", "reviews", "rooms", "policies", "prices", "meals", "availability"]


def parse_osm_stars(v) -> int | None:
    if v is None:
        return None
    m = re.match(r"^\s*(\d)", str(v))
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 5 else None


def synth_stars(osm_id: str, kind: str) -> int:
    opts, w = STARS_BY_KIND.get(kind, STARS_BY_KIND["hotel"])
    return hpick(opts, osm_id, "stars", weights=w)


def synth_rating(osm_id: str, stars: int) -> float:
    u = h01(osm_id, "rating")
    return round(6.5 + 3.1 * (0.65 * u + 0.35 * (stars or 0) / 5), 1)


def synth_reviews(osm_id: str, kind: str) -> int:
    u = h01(osm_id, "reviews")
    n = math.exp(math.log(5) + u * (math.log(3000) - math.log(5)))
    if kind in ("hostel", "guest_house"):
        n *= 0.5
    return max(1, int(n))


def meals_options(osm_id: str, kind: str, stars: int, country_code: str) -> list[str]:
    if kind == "hostel":
        return ["none", "breakfast"] if h01(osm_id, "meals") < 0.4 else ["none"]
    if kind in ("apartment", "chalet"):
        return ["none", "breakfast"] if h01(osm_id, "meals") < 0.2 else ["none"]
    if kind == "guest_house":
        return ["none", "breakfast"] if h01(osm_id, "meals") < 0.7 else ["breakfast"]
    resortish = kind == "resort" or (country_code in AI_COUNTRIES and stars >= 4)
    if resortish:
        opts = ["breakfast", "half_board", "all_inclusive"]
        if h01(osm_id, "meals_fb") < 0.3:
            opts.insert(2, "full_board")
        if h01(osm_id, "meals_ai_only") < 0.25 and country_code in AI_COUNTRIES:
            opts = ["all_inclusive"]
        return opts
    opts = ["none", "breakfast"]
    if stars >= 3 and h01(osm_id, "meals_hb") < 0.35:
        opts.append("half_board")
    if h01(osm_id, "meals_bo") < 0.3:
        opts = [o for o in opts if o != "none"]
    return opts


def synth_rooms(osm_id: str, hotel_id: str, kind: str, stars: int, level: int, meals: list[str]) -> list[dict]:
    tmpl = ROOM_TEMPLATES.get(kind, ROOM_TEMPLATES["hotel"])
    n = min(len(tmpl), hint(2, 4, osm_id, "rooms_n"))
    rest = sorted(range(1, len(tmpl)), key=lambda i: h01(osm_id, "room_order", i))
    chosen = sorted([0] + rest[:n - 1])
    jitter = 0.85 + 0.35 * h01(osm_id, "price")
    rooms = []
    for i in chosen:
        name, ad, ch, bed, f = tmpl[i]
        rooms.append({
            "room_id": f"{hotel_id}_r{i}", "hotel_id": hotel_id, "name": name, "capacity_adults": ad,
            "capacity_children": ch, "bed": bed, "base_price_rub": base_price_rub(level, stars, kind, f, jitter),
            "meals_options": list(meals),
        })
    return rooms


PETS_P = {"hotel": 0.25, "resort": 0.1, "motel": 0.4, "hostel": 0.05, "guest_house": 0.3, "apartment": 0.35,
          "chalet": 0.4}


def synth_policy(osm_id: str, hotel_id: str, kind: str, stars: int, nightly: int) -> tuple[dict, dict]:
    u = lambda f: h01(osm_id, f)  # noqa: E731
    check_in = hpick(["14:00", "15:00", "13:00"], osm_id, "check_in", weights=[0.55, 0.35, 0.1])
    if kind in ("apartment", "chalet"):
        check_in = hpick(["15:00", "16:00", "14:00"], osm_id, "check_in", weights=[0.5, 0.3, 0.2])
    check_out = hpick(["12:00", "11:00", "10:00"], osm_id, "check_out", weights=[0.6, 0.32, 0.08])
    pets = u("pets") < PETS_P.get(kind, 0.2)
    if kind == "hostel":
        children_allowed = u("children") >= 0.4
    elif kind in ("resort", "hotel") and stars >= 4:
        children_allowed = u("children") >= 0.04      # adults-only share
    else:
        children_allowed = True
    min_child_age = None
    if children_allowed and u("min_child_age") < 0.05:
        min_child_age = hpick([6, 12], osm_id, "min_child_age_v")
    if kind in ("hostel",):
        cot = "none"
    else:
        cot = hpick(["free", "paid", "on_request", "none"], osm_id, "cot", weights=[0.5, 0.15, 0.3, 0.05])
    extra_bed = None
    if kind not in ("hostel", "apartment", "chalet") and children_allowed:
        extra_bed = int(round(nightly * (0.15 + 0.15 * u("extra_bed")) / 100.0)) * 100
    early = u("early") < (0.7 if kind == "hostel" else 0.55)
    early_price = int(round(nightly * (0.3 + 0.3 * u("early_price")) / 100.0)) * 100 if early else None
    free_days = hpick([None, 1, 2, 3, 7, 14], osm_id, "cancel", weights=[0.25, 0.2, 0.1, 0.2, 0.15, 0.1])
    penalty = 100 if free_days is None else hpick([100, 50, 30], osm_id, "penalty", weights=[0.6, 0.25, 0.15])
    policy = {
        "hotel_id": hotel_id, "check_in_from": check_in, "check_out_until": check_out, "pets_allowed": int(pets),
        "children_allowed": int(children_allowed), "min_child_age": min_child_age, "infant_cot": cot,
        "extra_bed_price_rub": extra_bed, "early_checkin_available": int(early),
        "early_checkin_price_rub": max(500, early_price) if early_price is not None else None,
        "free_cancellation_days": free_days, "penalty_pct": penalty,
    }
    pets_fee = None
    if pets:
        pets_fee = 0 if u("pets_fee") < 0.3 else int(round(max(500, nightly * (0.1 + 0.2 * u("pets_fee_v"))) / 100)) * 100
    extras = {
        "pets_fee_rub": pets_fee,
        "child_free_under_age": hpick([2, 4, 6, 7, 12], osm_id, "child_free") if children_allowed else None,
        "meal_multipliers": dict(MEAL_MULTIPLIERS),
    }
    return policy, extras


def synth_hotel(h: dict, dest: dict) -> dict:
    """h: {hotel_id, osm_id, kind, stars (OSM or None)}; dest: {id, country_code}.
    Returns {stars, stars_real, rating, reviews, rooms, policy, extras, synthetic_fields}."""
    osm_id, kind = h["osm_id"], h["kind"]
    stars_real = h.get("stars") is not None
    stars = h["stars"] if stars_real else synth_stars(osm_id, kind)
    level = price_level(dest["id"], dest["country_code"])
    meals = meals_options(osm_id, kind, stars, dest["country_code"])
    rooms = synth_rooms(osm_id, h["hotel_id"], kind, stars, level, meals)
    policy, extras = synth_policy(osm_id, h["hotel_id"], kind, stars, rooms[0]["base_price_rub"])
    fields = list(SYNTHETIC_FIELDS) + ([] if stars_real else ["stars"])
    return {"stars": stars, "stars_real": stars_real, "rating": synth_rating(osm_id, stars),
            "reviews": synth_reviews(osm_id, kind), "rooms": rooms, "policy": policy, "extras": extras,
            "synthetic_fields": sorted(fields)}
