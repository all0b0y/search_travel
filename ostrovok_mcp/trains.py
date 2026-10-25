"""Train generator: Russian domestic pairs only, great-circle distance 30..2500 km (ostrovok_mcp/rail.py, the rule
the engine's opendata.train_reachable shares), both cities with stations.

Pairs up to rail.DAY_TRAIN_MAX_KM (1000 km): rail distance = great circle x 1.25; per pair and date 2-5 trains
(seeded). A train is either
- `fast` (day train, <= 750 km rail, 120 km/h, class `seat`) or
- `regular` (70 km/h incl. stops; classes platzkart / coupe / sv).
High-speed corridors (HIGH_SPEED, e.g. Москва — Санкт-Петербург «Сапсан»): 4-7 trains a day, every
even-numbered one a high-speed day train (class `seat`), the others regular overnight trains.
Long-distance pairs (over 1000 km, up to 2500 km): 1-2 FPK trains a day, sleepers only (platzkart / coupe / sv),
rail distance = great circle x 1.3 at 60-70 km/h incl. stops (Москва — Анапа ~1570 km, about a day; up to ~two
days), departures 06:00-23:55, each train's number / time / speed fixed per pair (the same timetable every day);
the offer's `dep` / `arr` carry the real days (arrival one or two days after departure).
Fare per passenger (RUB): seat 500 + 3.2/km, platzkart 400 + 1.6/km, coupe 700 + 2.8/km,
sv 1500 + 5.5/km, x season x lead time (flight table, softened by half) x noise 0.92-1.12.

Every offer has `train_kind` (`high_speed` = a HIGH_SPEED train such as «Сапсан», `day` = another day
train with seats, `long_distance` = an FPK overnight train) and `pets` terms (pet_terms): a cautious
summary of the RZD rules in force since 01.01.2025 — cats and small dogs in a closed carrier whose three
sides sum to <= 180 cm, where hand luggage goes, a travel document per animal, at most two per ticket;
on high-speed / day trains only on the seats for passengers with animals and never a large dog; on FPK
trains a large dog only in a separate compartment (coupe / sv) with every free place bought, muzzled and
on a leash; guide dogs free everywhere. The fee is synthetic like every mock price. A search with
`pets` > 0 prices that many animals into `price_rub` (the offer id keeps the count, so carts agree).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import geo, pricing, rail, rng
from .offer_ids import encode

MAX_KM = rail.TRAIN_MAX_KM
DAY_MAX_KM = rail.DAY_TRAIN_MAX_KM      # beyond it: long-distance sleepers only
RAIL_FACTOR = 1.25
LONG_RAIL_FACTOR = 1.3
LONG_SPEED_KMH = (60, 70)
# pair -> (train name, km/h): the real rail line is shorter than great circle x 1.25, so these trains
# would otherwise never count as `fast` (Москва — Санкт-Петербург: 793 km by the formula)
HIGH_SPEED = {frozenset({"Москва", "Санкт-Петербург"}): ("Сапсан", 200)}
CLASS_TARIFF = {"seat": (500, 3.2), "platzkart": (400, 1.6), "coupe": (700, 2.8), "sv": (1500, 5.5)}


def pair_note(a: geo.City | None, b: geo.City | None) -> str | None:
    """Why there are no trains for this pair (Russian), or None when trains are possible."""
    if a is None or b is None:
        return "Город не найден в демо-каталоге, поезда не ищем"
    if a.country_code != "RU" or b.country_code != "RU":
        return "Поезда в демо — только по России"
    if not a.stations or not b.stations:
        return "В одном из городов нет вокзала в каталоге"
    km = geo.haversine_km(a.lat, a.lon, b.lat, b.lon)
    if km > MAX_KM:
        return f"Поезда в демо — только до ~{MAX_KM} км, здесь около {int(round(km, -1))} км"
    if km < rail.TRAIN_MIN_KM:
        return "Города слишком близко для поезда дальнего следования"
    return None


def _gc_km(a: geo.City, b: geo.City) -> float:
    return geo.haversine_km(a.lat, a.lon, b.lat, b.lon)


def _long_train(a: geo.City, b: geo.City, i: int) -> dict:
    """A long-distance FPK sleeper of a pair over DAY_MAX_KM: number, departure and speed fixed per pair and
    train index (the same timetable every day); 1.3 x great circle at 60-70 km/h incl. stops."""
    pair = f"{a.name_ru}>{b.name_ru}"
    km = _gc_km(a, b) * LONG_RAIL_FACTOR
    speed = rng.between(*LONG_SPEED_KMH, "train", pair, "long_speed", i)
    dep_min = rng.rint(72, 287, "train", pair, "long_dep", i) * 5                 # 06:00 .. 23:55
    number = rng.rint(1, 199, "train", pair, "long_number", i)
    letter = rng.pick("АВГЕЖИМНСУЧЯ", "train", pair, "long_letter", i)
    return {"fast": False, "km": km, "duration": int(round(km / speed * 60)), "dep_min": dep_min,
            "train_no": f"{number:03d}{letter}", "carrier": "ФПК", "kind": "long_distance"}


def _train(a: geo.City, b: geo.City, day: date, i: int) -> dict:
    if _gc_km(a, b) > DAY_MAX_KM:
        return _long_train(a, b, i)
    km = geo.haversine_km(a.lat, a.lon, b.lat, b.lon) * RAIL_FACTOR
    pair = f"{a.name_ru}>{b.name_ru}"
    hsr = HIGH_SPEED.get(frozenset({a.name_ru, b.name_ru}))
    if hsr:
        fast = i % 2 == 0
        speed = hsr[1] if fast else 70
    else:
        fast = km <= 750 and rng.unit("train", pair, "fast", i) < 0.5
        speed = 120 if fast else 70
    if fast:
        dep_min = rng.rint(66, 216, "train", pair, "dep", i) * 5        # 05:30 .. 18:00
    else:
        dep_min = rng.rint(216, 282, "train", pair, "dep", i) * 5       # 18:00 .. 23:30 (overnight)
    number = rng.rint(1, 799, "train", pair, "number", i)
    letter = rng.pick("АВГЕЖИМНСУЧЯ", "train", pair, "letter", i)
    carrier = "РЖД" if fast else "ФПК"
    if hsr and fast:
        # odd numbers one way, even the other (as on the real line)
        number, letter, carrier = 751 + 2 * i + (a.name_ru > b.name_ru), "А", f"РЖД «{hsr[0]}»"
    kind = "high_speed" if hsr and fast else "day" if fast else "long_distance"
    return {"fast": fast, "km": km, "duration": int(round(km / speed * 60)), "dep_min": dep_min,
            "train_no": f"{number:03d}{letter}", "carrier": carrier, "kind": kind}


PET_MAX_PER_TICKET = 2
PET_CARRIER_CM_SUM = 180
COMPARTMENT_PLACES = {"coupe": 4, "sv": 2}
PET_PLACES_SEATS = "места для пассажиров с животными"
LARGE_DOG_HOW = "только в отдельном купе: выкупить все свободные места, собака в наморднике и на поводке"


def pet_terms(tr: dict, cls: str, pax: int, per_pax_fare: float, pets: int, *seed) -> dict:
    """Pet carriage terms of one train and car class (see the module docstring); `seed` makes the
    synthetic fee deterministic. `count` animals are priced into the offer when the terms allow them."""
    fee = pricing.round10((300 + 0.6 * tr["km"]) * rng.between(0.9, 1.1, "train", "petfee", *seed))
    large = cls in COMPARTMENT_PLACES and tr["kind"] == "long_distance"
    terms = {
        "small_in_carrier": True,
        "carrier_max_cm_sum": PET_CARRIER_CM_SUM,
        "carrier_max_kg": None,
        "places": PET_PLACES_SEATS if tr["kind"] != "long_distance" else None,
        "large_dogs": large,
        "large_dogs_how": LARGE_DOG_HOW if large else None,
        "max_animals": PET_MAX_PER_TICKET * pax,
        "fee_rub": fee,
        "large_dog_fee_rub": (pricing.round10(per_pax_fare * max(0, COMPARTMENT_PLACES[cls] - pax))
                              if large else None),
        "guide_dogs_free": True,
        "count": 0,
        "fee_total_rub": 0,
        "synthetic_fee": True,
    }
    if 0 < pets <= terms["max_animals"]:
        terms["count"], terms["fee_total_rub"] = pets, fee * pets
    return terms


def active(a: geo.City, b: geo.City, day: date) -> list[int]:
    pair = f"{a.name_ru}>{b.name_ru}"
    if _gc_km(a, b) > DAY_MAX_KM:
        lo, hi = 1, 2                     # long-distance pairs: one or two sleepers a day
    else:
        lo, hi = (4, 7) if frozenset({a.name_ru, b.name_ru}) in HIGH_SPEED else (2, 5)
    n = rng.rint(lo, hi, "train", pair, "count", day.isoformat())
    return list(range(n))


def classes(train: dict) -> list[str]:
    return ["seat"] if train["fast"] else ["platzkart", "coupe", "sv"]


def build(params: dict) -> dict | None:
    a, b = geo.resolve_city(params["o"]), geo.resolve_city(params["d"])
    if pair_note(a, b) is not None:
        return None
    day, today = date.fromisoformat(params["dt"]), date.fromisoformat(params["t"])
    lead = (day - today).days
    if lead < 0 or lead > pricing.MAX_HORIZON_DAYS:
        return None
    i, cls, pax = int(params["s"]), params["cls"], int(params["p"])
    if i not in active(a, b, day):
        return None
    tr = _train(a, b, day, i)
    if cls not in classes(tr):
        return None
    tz_a = geo.zone(a.tz, a.country_code, a.lon)
    tz_b = geo.zone(b.tz, b.country_code, b.lon)
    dep = geo.local_dt(day, tr["dep_min"], tz_a)
    arr = (dep + timedelta(minutes=tr["duration"])).astimezone(tz_b)
    base, per_km = CLASS_TARIFF[cls]
    fare = (base + per_km * tr["km"])
    prof = pricing.profile(b.kind, b.tags, b.country_code)
    fare *= pricing.season_factor(day, prof)
    fare *= 1 + (pricing.lead_factor(lead, pricing.FLIGHT_LEAD) - 1) / 2
    fare *= rng.between(0.92, 1.12, "train", a.name_ru, b.name_ru, "demand", day.isoformat(), i, cls)
    pets = pet_terms(tr, cls, pax, fare, int(params.get("pt") or 0), a.name_ru, b.name_ru, day.isoformat(), i)
    return {
        "offer_id": encode("train", params),
        "carrier": tr["carrier"], "train_no": tr["train_no"], "train_kind": tr["kind"],
        "from_station": geo.station_for(a, b), "to_station": geo.station_for(b, a),
        "dep": geo.iso(dep), "arr": geo.iso(arr), "duration_min": max(1, tr["duration"]),
        "car_class": cls, "price_rub": pricing.round10(fare * pax) + pets["fee_total_rub"],
        "pets": pets, "date": day.isoformat(), "synthetic": True,
    }


def search(origin_city: str, dest_city: str, day: date, passengers: int, today: date,
           pets: int = 0) -> tuple[list[dict], str | None]:
    a, b = geo.resolve_city(origin_city), geo.resolve_city(dest_city)
    note = pair_note(a, b)
    if note:
        return [], note
    if day < today:
        return [], "Дата в прошлом"
    offers = []
    for i in active(a, b, day):
        for cls in classes(_train(a, b, day, i)):
            params = {"o": a.name_ru, "d": b.name_ru, "dt": day.isoformat(), "s": i, "cls": cls,
                      "p": passengers, "t": today.isoformat(), **({"pt": int(pets)} if pets else {})}
            offer = build(params)
            if offer:
                offers.append(offer)
    offers.sort(key=lambda x: (x["dep"], x["price_rub"]))
    return offers, None if offers else "На эту дату поездов нет"
