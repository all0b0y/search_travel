"""Hotel offers over the catalog (real OSM hotels + synthetic rooms/policies/prices).

Room choice: the cheapest room type (up to 3 rooms of one type) that fits the party:
adults use adult places; children (>= 2 y) use free adult places, then child places, then
paid extra beds (`policies.extra_bed_price_rub`, at most one per room); infants (< 2 y) use a
cot and need no place. Hotels that do not take children (or the youngest child is below
`min_child_age`) are skipped when the party has children.

Price per night = rooms.base_price_rub x season(night, destination profile) x weekend
(Fri/Sat nights x1.12) x lead time (pricing.HOTEL_LEAD) x demand noise 0.92-1.10 (seeded by
hotel|room|night) x n_rooms (x0.85 for a single adult) + meals + extra beds.
Meals per person-night = base x MEAL_SHARE[meals] / 2 (children 2-11 at half).
Availability: a room type is sold out when any night draws < 1.2 % (x3 in peak nights, season
factor >= 1.25), seeded by hotel|room|night.
Filters (meals, free_cancellation, stars_min, pets, kinds, max_distance_center_m, max_distance_near_m,
max_price_total) apply before the top-`limit` cut, so a filtered search reaches hotels the unfiltered top would
not show (max_distance_center_m: distance_center_m from the destination's centre point, «в центре»;
`near` {lat, lon}: a point the user wants to stay near — every offer then carries distance_near_m, and
max_distance_near_m keeps only the hotels that close to it, «рядом с Айя-Софией»).
Sorting: stable score = 0.45 rating + 0.20 stars + 0.15 reviews volume + 0.20 price rank.

Synthetic amenities (no column in the catalog has them; deterministic, marked synthetic):
- kitchen (room.kitchen): every room of a hotel of kind apartment, and rooms named «…апартамент…» / «…студи…»
  in any hotel; filters.kitchen picks the cheapest fitting room with a kitchen;
- accessibility {step_free, lift, synthetic}: seeded by hotel_id — step-free access for ~60 % of hotels / resorts
  of 3+ stars, ~20 % of the other kinds, never a hostel; a lift with every step-free hotel and otherwise
  ~70 % / ~20 %; filters.step_free keeps only step-free hotels.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from . import db, geo, pricing, rng
from .db import ServiceError
from .offer_ids import encode

MEAL_ORDER = ["none", "breakfast", "half_board", "full_board", "all_inclusive"]
MEAL_SHARE = {"none": 0.0, "breakfast": 0.12, "half_board": 0.30, "full_board": 0.45, "all_inclusive": 0.65}
MEAL_RU = {"none": "без питания", "breakfast": "завтраки", "half_board": "полупансион",
           "full_board": "полный пансион", "all_inclusive": "всё включено"}
DEFAULT_POLICY = {"check_in_from": "14:00", "check_out_until": "12:00", "pets_allowed": 0, "children_allowed": 1,
                  "min_child_age": None, "infant_cot": "on_request", "extra_bed_price_rub": None,
                  "early_checkin_available": 0, "early_checkin_price_rub": None, "free_cancellation_days": None,
                  "penalty_pct": 100, "source": "default"}
MAX_ROOMS = 3


def hotel(hotel_id: str) -> dict | None:
    rows = db.catalog().query("hotels", "hotel_id = ?", (hotel_id,), key="hotel_id")
    return rows[0] if rows else None


def hotels_of(dest_id: str) -> list[dict]:
    return db.catalog().query("hotels", "destination_id = ?", (dest_id,), key="hotel_id", order="hotel_id")


def rooms_of(hotel_id: str) -> list[dict]:
    rows = db.catalog().query("rooms", "hotel_id = ?", (hotel_id,), key="room_id", order="room_id")
    for r in rows:
        r["meals_options"] = [m for m in db.jload(r.get("meals_options"), ["none"]) if m in MEAL_SHARE] or ["none"]
    return rows


def policy_of(hotel_id: str) -> dict:
    rows = db.catalog().query("policies", "hotel_id = ?", (hotel_id,), key="hotel_id")
    return {**DEFAULT_POLICY, **rows[0]} if rows else dict(DEFAULT_POLICY)


KITCHEN_WORDS = ("апартамент", "студи")
STEP_FREE_SHARE = {"high": 0.6, "other": 0.2}
LIFT_SHARE = {"high": 0.7, "other": 0.2}


def room_kitchen(h: dict, room: dict) -> bool:
    """Synthetic: an apartment's every room, or a room named «апартаменты» / «студия» (a kitchen corner)."""
    name = str(room.get("name") or "").casefold()
    return h.get("kind") == "apartment" or any(w in name for w in KITCHEN_WORDS)


def accessibility(h: dict) -> dict:
    """Synthetic step-free access and lift, seeded by hotel_id (never a hostel)."""
    if h.get("kind") == "hostel":
        return {"step_free": False, "lift": False, "synthetic": True}
    tier = "high" if h.get("kind") in ("hotel", "resort") and (h.get("stars") or 0) >= 3 else "other"
    step_free = rng.unit("hotel", h["hotel_id"], "step_free") < STEP_FREE_SHARE[tier]
    lift = step_free or rng.unit("hotel", h["hotel_id"], "lift") < LIFT_SHARE[tier]
    return {"step_free": step_free, "lift": lift, "synthetic": True}


def kids_policy(pol: dict) -> dict:
    return {"children_allowed": bool(pol["children_allowed"]), "min_child_age": pol["min_child_age"],
            "infant_cot": pol["infant_cot"], "extra_bed_price_rub": pol["extra_bed_price_rub"]}


def early_checkin(pol: dict) -> dict:
    return {"available": bool(pol["early_checkin_available"]),
            "price_rub": pol["early_checkin_price_rub"] if pol["early_checkin_available"] else None}


def split_party(ages: list[int]) -> tuple[list[int], int]:
    kids = [a for a in ages if a >= 2]
    return kids, len([a for a in ages if a < 2])


def room_fit(room: dict, adults: int, ages: list[int], pol: dict) -> tuple[int, int] | None:
    """(n_rooms, extra_beds) or None when this room type cannot host the party."""
    kids, infants = split_party(ages)
    if ages and not pol["children_allowed"]:
        return None
    if ages and pol["min_child_age"] is not None and min(ages) < pol["min_child_age"]:
        return None
    for n in range(1, MAX_ROOMS + 1):
        if adults < n:
            break
        cap_a, cap_c = room["capacity_adults"] * n, room["capacity_children"] * n
        if adults > cap_a:
            continue
        kids_left = max(0, len(kids) - (cap_a - adults))
        extra = max(0, kids_left - cap_c)
        if extra and (pol["extra_bed_price_rub"] is None or extra > n):
            continue
        return n, extra
    return None


def dest_profile(dest: dict | None) -> str:
    if not dest:
        return "city"
    return pricing.profile(dest.get("kind"), dest.get("tags"), dest.get("country_code"))


def _nights(ci: date, co: date) -> list[date]:
    return [ci + timedelta(days=k) for k in range((co - ci).days)]


def sold_out(h: dict, room: dict, ci: date, co: date, prof: str) -> bool:
    for night in _nights(ci, co):
        p = 0.012 * (3 if pricing.season_factor(night, prof) >= 1.25 else 1)
        if rng.unit("hotel", h["hotel_id"], room["room_id"], "soldout", night.isoformat()) < p:
            return True
    return False


def stay_price(h: dict, room: dict, n_rooms: int, extra: int, meals: str, adults: int, ages: list[int],
               ci: date, co: date, today: date, prof: str, pol: dict) -> int:
    kids, _ = split_party(ages)
    lead = pricing.lead_factor(max(0, (ci - today).days), pricing.HOTEL_LEAD)
    occupancy = 0.85 if adults == 1 and not kids else 1.0
    persons = adults + 0.5 * len([k for k in kids if k < 12]) + len([k for k in kids if k >= 12])
    total = 0.0
    for night in _nights(ci, co):
        f = pricing.season_factor(night, prof) * lead
        f *= 1.12 if night.weekday() in (4, 5) else 1.0
        f *= rng.between(0.92, 1.10, "hotel", h["hotel_id"], room["room_id"], "demand", night.isoformat())
        base = room["base_price_rub"] * f
        total += base * n_rooms * occupancy
        total += base * MEAL_SHARE[meals] * persons / 2
        total += extra * (pol["extra_bed_price_rub"] or 0)
    return pricing.round10(total)


def cancellation(pol: dict, ci: date, tz, today: date) -> dict:
    days = pol["free_cancellation_days"]
    if days is None:
        return {"free_until": None, "penalty_pct": 100}
    until_day = ci - timedelta(days=int(days))
    if until_day < today:
        return {"free_until": None, "penalty_pct": int(pol["penalty_pct"])}
    until = datetime.combine(until_day, time(23, 59), tzinfo=tz)
    return {"free_until": geo.iso(until), "penalty_pct": int(pol["penalty_pct"])}


def _distance_center(h: dict, dest: dict | None) -> int:
    if h.get("distance_center_m") is not None:
        return max(0, int(h["distance_center_m"]))
    if dest:
        return int(round(geo.haversine_km(h["lat"], h["lon"], dest["lat"], dest["lon"]) * 1000))
    return 0


def _distance_near(h: dict, near: dict | None) -> int | None:
    if not near:
        return None
    return int(round(geo.haversine_km(h["lat"], h["lon"], float(near["lat"]), float(near["lon"])) * 1000))


def _room_out(room: dict, n: int, h: dict | None = None) -> dict:
    name = room["name"] if n == 1 else f"{n} номера: {room['name']}"
    return {"room_id": room["room_id"], "name": name, "capacity_adults": room["capacity_adults"] * n,
            "capacity_children": room["capacity_children"] * n, "bed": room.get("bed"),
            "kitchen": room_kitchen(h or {}, room)}


def _offer(h: dict, room: dict, n: int, extra: int, meals: str, adults: int, ages: list[int], ci: date, co: date,
           today: date, dest: dict | None, pol: dict, price: int) -> dict:
    tz = geo.destination_tz(dest) if dest else geo.zone(None, None, h["lon"])
    params = {"h": h["hotel_id"], "r": room["room_id"], "n": n, "x": extra, "m": meals, "ci": ci.isoformat(),
              "co": co.isoformat(), "a": adults, "ages": list(ages), "t": today.isoformat()}
    return {
        "offer_id": encode("hotel", params),
        "hotel_id": h["hotel_id"], "osm_id": h["osm_id"], "name": h["name"], "kind": h["kind"],
        "stars": h.get("stars"), "rating": h.get("rating"), "reviews": int(h.get("reviews") or 0),
        "lat": h["lat"], "lon": h["lon"], "room": _room_out(room, n, h), "meals": meals,
        "cancellation": cancellation(pol, ci, tz, today), "price_total_rub": price,
        "pets_allowed": bool(pol["pets_allowed"]), "kids_policy": kids_policy(pol), "early_checkin": early_checkin(pol),
        "check_in": geo.iso(datetime.combine(ci, time(0), tzinfo=tz) + timedelta(minutes=geo.hhmm_to_min(pol["check_in_from"]))),
        "check_out": geo.iso(datetime.combine(co, time(0), tzinfo=tz) + timedelta(minutes=geo.hhmm_to_min(pol["check_out_until"]))),
        "distance_beach_m": h.get("distance_beach_m"), "distance_center_m": _distance_center(h, dest),
        "accessibility": accessibility(h), "synthetic": True,
    }


def build(params: dict) -> dict | None:
    """Rebuild a hotel offer from its id params; None when sold out / not bookable any more."""
    h = hotel(params["h"])
    if not h:
        return None
    room = next((r for r in rooms_of(h["hotel_id"]) if r["room_id"] == params["r"]), None)
    if not room or params["m"] not in room["meals_options"]:
        return None
    ci, co, today = date.fromisoformat(params["ci"]), date.fromisoformat(params["co"]), date.fromisoformat(params["t"])
    if co <= ci or ci < today or (ci - today).days > pricing.MAX_HORIZON_DAYS:
        return None
    adults, ages = int(params["a"]), [int(a) for a in params["ages"]]
    pol = policy_of(h["hotel_id"])
    fit = room_fit(room, adults, ages, pol)
    if fit != (int(params["n"]), int(params["x"])):
        return None
    dest = geo.destination(h["destination_id"])
    prof = dest_profile(dest)
    if sold_out(h, room, ci, co, prof):
        return None
    price = stay_price(h, room, fit[0], fit[1], params["m"], adults, ages, ci, co, today, prof, pol)
    return _offer(h, room, fit[0], fit[1], params["m"], adults, ages, ci, co, today, dest, pol, price)


def search(dest_id: str, ci: date, co: date, adults: int, ages: list[int], filters: dict, limit: int,
           today: date, hotel_ids: set[str] | None = None, all_rooms: bool = False) -> tuple[list[dict], int]:
    if ci < today or (ci - today).days > pricing.MAX_HORIZON_DAYS:
        return [], 0
    dest = geo.destination(dest_id)
    prof = dest_profile(dest)
    tz = geo.destination_tz(dest) if dest else None
    meals_f = filters.get("meals")
    near = filters.get("near") or None
    found = []
    for h in hotels_of(dest_id):
        if hotel_ids is not None and h["hotel_id"] not in hotel_ids:
            continue
        pol = policy_of(h["hotel_id"])
        if filters.get("pets") and not pol["pets_allowed"]:
            continue
        if filters.get("stars_min") and (h.get("stars") is None or h["stars"] < filters["stars_min"]):
            continue
        if filters.get("kinds") and h["kind"] not in filters["kinds"]:
            continue
        if filters.get("step_free") and not accessibility(h)["step_free"]:
            continue
        if filters.get("max_distance_center_m") is not None and \
                _distance_center(h, dest) > int(filters["max_distance_center_m"]):
            continue
        near_m = _distance_near(h, near)
        if near_m is not None and filters.get("max_distance_near_m") is not None and \
                near_m > int(filters["max_distance_near_m"]):
            continue
        if filters.get("free_cancellation") and cancellation(pol, ci, tz or geo.zone(None, None, h["lon"]),
                                                             today)["free_until"] is None:
            continue
        best = None
        every = []
        for room in rooms_of(h["hotel_id"]):
            fit = room_fit(room, adults, ages, pol)
            if not fit:
                continue
            if filters.get("kitchen") and not room_kitchen(h, room):
                continue
            if meals_f:
                if meals_f not in room["meals_options"]:
                    continue
                meals = meals_f
            else:
                meals = next(m for m in MEAL_ORDER if m in room["meals_options"])
            if sold_out(h, room, ci, co, prof):
                continue
            price = stay_price(h, room, fit[0], fit[1], meals, adults, ages, ci, co, today, prof, pol)
            every.append((price, room, fit, meals))
            if best is None or (price, room["room_id"]) < (best[0], best[1]["room_id"]):
                best = (price, room, fit, meals)
        if best is None:
            continue
        for price, room, fit, meals in (sorted(every, key=lambda e: (e[0], e[1]["room_id"])) if all_rooms else [best]):
            if filters.get("max_price_total") is not None and price > filters["max_price_total"]:
                continue
            offer = _offer(h, room, fit[0], fit[1], meals, adults, ages, ci, co, today, dest, pol, price)
            if near_m is not None:
                offer["distance_near_m"] = near_m
            found.append(offer)
    if not found:
        return [], 0
    prices = sorted(o["price_total_rub"] for o in found)

    def score(o: dict) -> float:
        rank = prices.index(o["price_total_rub"]) / max(1, len(prices) - 1)
        return (0.45 * (o["rating"] if o["rating"] is not None else 6.0) / 10
                + 0.20 * (o["stars"] if o["stars"] is not None else 2) / 5
                + 0.15 * min(1.0, math.log10(o["reviews"] + 1) / 3)
                + 0.20 * (1 - rank))

    found.sort(key=lambda o: (-round(score(o), 9), o["hotel_id"], o["price_total_rub"], o["offer_id"]))
    return found[:limit], len(found)


def details(hotel_id: str) -> dict:
    h = hotel(hotel_id)
    if not h:
        raise ServiceError("hotel_not_found", f"no hotel {hotel_id!r} in the catalog")
    pol = policy_of(hotel_id)
    dest = geo.destination(h["destination_id"])
    photos = [p for p in db.jload(h.get("photos"), []) if isinstance(p, dict) and p.get("url")
              and str(p["url"]).startswith(("http://", "https://"))]
    photos = [{"url": p["url"], "source": p.get("source") if p.get("source") in ("wikimedia", "osm") else "other",
               "license": p.get("license")} for p in photos]
    days = pol["free_cancellation_days"]
    if days is None:
        note = "Невозвратный тариф: при отмене стоимость не возвращается"
    else:
        note = f"Бесплатная отмена до {int(days)} дн. до заезда, позже штраф {int(pol['penalty_pct'])} %"
    synthetic_fields = db.jload(h.get("synthetic_fields"), None)
    if not isinstance(synthetic_fields, list):
        synthetic_fields = ["stars", "rating", "reviews", "policies", "rooms"]
    for f in ("policies", "rooms", "accessibility"):
        if f not in synthetic_fields:
            synthetic_fields.append(f)
    return {
        "hotel_id": h["hotel_id"], "osm_id": h["osm_id"], "destination_id": h["destination_id"], "name": h["name"],
        "kind": h["kind"], "stars": h.get("stars"), "rating": h.get("rating"), "reviews": int(h.get("reviews") or 0),
        "lat": h["lat"], "lon": h["lon"], "address": h.get("address"), "distance_beach_m": h.get("distance_beach_m"),
        "distance_center_m": _distance_center(h, dest),
        "policies": {"check_in_from": pol["check_in_from"], "check_out_until": pol["check_out_until"],
                     "pets_allowed": bool(pol["pets_allowed"]), "kids_policy": kids_policy(pol),
                     "early_checkin": early_checkin(pol), "cancellation_note": note},
        "rooms": [_room_out(r, 1, h) for r in rooms_of(hotel_id)],
        "photos": photos, "accessibility": accessibility(h), "source": h.get("source") or "osm", "synthetic": True, "synthetic_fields": synthetic_fields,
    }
