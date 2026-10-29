"""Door-to-door variants, fit and sensitivity (AGENT_PLAN.md decisions 6, 8, 9; sections 3 and 5).

    variants = build_variants(brief, results, districts, rules, today, segments=skill.segments)
    fit(brief, variant, rules, segments=skill.segments) -> Fit
    questions = sensitivity(brief, variants, rerun, skill)

`results` (assembled by the engine from the mock's answers; every value may also be the raw tool
output `{"offers": [...]}`):

    {"flights_out": [FlightOffer], "flights_back": [FlightOffer],
     "trains_out": [TrainOffer], "trains_back": [TrainOffer],
     "hotels": [HotelOffer],
     "transfers_out": {hotel_id: [TransferOffer]},    # arrival point -> hotel
     "transfers_back": {hotel_id: [TransferOffer]},   # hotel -> departure point
     "destination": {"id", "name", "country"}}        # optional, for titles

A TransferOffer may carry an extra `point` (IATA or station name) added by the engine; when present
it must equal the transport's arrival/departure point. `districts` = {osm_id: OpenData.district_profile()}.

Everything the variants say is computed here: totals, facts (every number a card may cite), sacrifices
(diffs vs the other variants), risks, not_for (district scores, hotel policies) and fit with a
per-criterion breakdown. No margin weight anywhere (decision 8).

Thresholds come from `rules["ranking"]` with the defaults in RANKING_DEFAULTS (documented there); the
skill should copy them into rules.json. Segment district weights come from segments.json (passed as
`segments=`, or `rules["segments"]`, else loaded from the default skill package).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from .checks import (DEFAULT_BUFFERS, DEFAULT_LATE_CHECKIN, DEFAULT_TAXI_FACTOR, check_party, connection_risks,
                     fmt_hours, fmt_int, fmt_rub, in_clock_window, nights_text, plural, ts, wants_pet)
from .payloads import MONTHS_GEN
from .night_arrival import arrival_penalty
from .weekend import add_weekend_choice, destination_hours, quoted_weekend_choices, short_break

# ---------------------------------------------------------------- tunables

RANKING_DEFAULTS = {
    "max_legs": 6,                    # transport offers kept per direction (cheapest + fastest union)
    "max_hotels": 15,                 # hotel offers kept (cheapest + most comfortable union)
    "fastest_min_gain_h": 0.25,
    "fastest_hours_tolerance": 0.25,  # combos this close to the minimum time count as "fastest"
    "fastest_price_weight": 0.5,      # among them: comfort minus this x relative extra price
    "comfort_max_extra_hours": 3,     # comfort variant: at most this many hours longer than the fastest journey       # a separate "fastest" must save at least this vs the cheapest
    "comfort_min_gain": 0.03,         # a separate "comfort" must beat the others' comfort score by this
    "comfort_price_cap_ratio": 2.0,   # without a budget, comfort costs at most this x the cheapest
    "after_train_min": 10,            # leaving the platform after a train
    "night_window": ["23:00", "06:00"],   # night departure / arrival (local clock)
    "night_relax_penalty": 0.15,       # fit reduction for a night arrival on a quiet holiday
    "night_family_penalty": 0.2,
    "night_senior_penalty": 0.2,
    "night_senior_age": 65,
    "night_move_at": "03:00",             # a train still on the move at this local time is an overnight one
    "early_departure_before": "07:00",    # departures before this are "early"
    "tight_layover_min": 60,          # a flight's own connection shorter than this is tight
    "long_transfer_min": 90,          # airport -> hotel longer than this is long
    "wait_before_checkin_min": 180,   # arriving this long before check-in is a risk
    "wait_after_checkout_min": 240,   # leaving the hotel this long before the transport is a risk
    "max_wait_after_checkout_h": 14,  # beyond this the return does not fit the stay at all
    "district_label_min": 0.6,        # district score from which a label is shown
    "lively_noisy_min": 0.6,          # not_for: noisy in the evening
    "quiet_min": 0.7,                 # not_for: quiet, no nightlife (with lively <= quiet_lively_max)
    "quiet_lively_max": 0.2,
    "not_walkable_max": 0.3,          # not_for: not walkable
    "beach_far_m": 1000,              # not_for (sea trips): far from the beach
    "beach_near_m": 300,              # fit: full beach score up to this distance (no explicit beach_max_m)
    "beach_zero_m": 2000,             # fit: zero beach score from this distance
    "sacrifice_beach_delta_m": 300,
    "sacrifice_rating_delta": 0.5,
    "budget_zero_over_pct": 50,       # fit: budget score reaches 0 at +50 % over the budget
    "max_notes": 4,                   # per list (sacrifices / risks / not_for)
    "fit_weights": {"budget": 0.25, "time": 0.15, "district": 0.15, "meals": 0.1, "cancellation": 0.07,
                    "stars": 0.08, "beach": 0.1, "constraints": 0.05, "rating": 0.05, "center": 0.15, "near": 0.2},
    "comfort_weights": {"stars": 0.2, "rating": 0.25, "meals": 0.15, "cancellation": 0.1, "direct": 0.1,
                        "fare": 0.05, "transfer": 0.05, "district": 0.1, "car_class": 0.1, "cot": 0.15,
                        "center": 0.3, "near": 0.35},
    "comfort_legs": 2,                # extra transport offers kept per direction for comfort (fare, class)
    "unused_night_penalty": 0.05,     # comfort minus this when arriving after midnight (first night unused)
    "max_hours_relax_tolerance_h": 1.0,   # nothing within max_travel_hours: keep journeys this close to the best
    "flex_shift_min_saving_pct": 10,  # flexible_days: another start day only when this much cheaper overall
    "max_dorm_hotels": 3,             # dorm-bed offers kept when private rooms exist (never crowd them out)
    "dorm_fit_factor": 0.5,           # fit x this for a dorm bed the user did not ask for (never the pick)
    # city centre (center_applies): full score up to center_full_m, zero from center_zero_m; with a user
    # prefs.center_max_m full up to it, zero at max + max(max, center_full_m)
    "center_kinds": ["city"],         # destination kinds whose stays are scored by the distance to the centre
    "center_full_m": 1000,
    "center_zero_m": 5000,
    "center_search_m": 1500,          # steps: the extra search_hotels(max_distance_center_m) for city stays
    "center_relax_tolerance_m": 1000, # prefs.center_max_m relaxed: hotels this close to the nearest one kept
    "sacrifice_center_delta_m": 700,
    "center_card_min_score": 0.9,     # «Удачнее по району» for a city stay: a hotel this central ...
    "center_card_price_ratio": 1.3,   # ... within this x the cheapest private-room trip ...
    "center_card_max_gain_h": 1.0,    # ... replaces «Быстрее» when that saves less than this door to door
    # a named place (prefs.near resolved in the destination's pois, results["near"]): the `near` component
    # replaces `center` — full up to near_full_m, zero from near_zero_m; near_max_m is the relaxable hard radius
    "near_full_m": 500,
    "near_zero_m": 3000,
    "near_max_m": 1500,
    "near_wide_m": 6000,              # steps: no hotel within near_max_m -> one wider search to find the nearest
    "near_relax_tolerance_m": 700,    # prefs.near relaxed: hotels this close to the nearest one kept
    "sacrifice_near_delta_m": 400,
}
SENSITIVITY_DEFAULTS = {"price_delta_pct": 10, "top_changes": True, "max_reruns": 12}

MEALS_RANK = {"none": 0, "breakfast": 1, "half_board": 2, "full_board": 3, "all_inclusive": 4}
MEALS_RU = {"none": "без питания", "breakfast": "завтраки", "half_board": "полупансион",
            "full_board": "полный пансион", "all_inclusive": "всё включено", "any": "любое"}
TITLES = {"cheapest": "Дешевле", "fastest": "Быстрее", "comfort": "Комфортнее", "custom-1": "Удачнее по району",
          "custom-2": "Свой номер подешевле", "custom-3": "Больше времени на месте"}
SEA_TRAITS = {"sea", "beach", "island"}
CAR_CLASS_COMFORT = {"sv": 1.0, "coupe": 0.6, "seat": 0.4, "platzkart": 0.2}
COMFORT_TRANSFER = {"private": 1.0, "taxi": 0.8, "shuttle": 0.4, "public": 0.1}
TRANSFER_RU = {"private": "индивидуальный трансфер", "taxi": "такси", "shuttle": "шаттл", "public": "общественный транспорт"}


def ranking_rules(rules) -> dict:
    merged = dict(RANKING_DEFAULTS)
    merged.update((rules or {}).get("ranking") or {})
    return merged


def _segments(rules, segments):
    if segments is not None:
        return segments.get("segments", segments)
    if isinstance(rules, dict) and "segments" in rules:
        return rules["segments"]
    try:
        from .skill_loader import load_skill
        return load_skill().segments
    except Exception:
        return {}


# ---------------------------------------------------------------- normalised offers

def _offers(value) -> list:
    if value is None:
        return []
    if isinstance(value, dict) and "offers" in value:
        return list(value["offers"])
    return list(value)


def _dump(obj):
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


@dataclass
class Leg:
    kind: str            # flight | train
    offer: dict
    offer_id: str
    dep: datetime
    arr: datetime
    origin: str          # IATA or station
    dest: str
    duration_min: int
    stops: int
    price_rub: int
    carrier: Optional[str]
    international: bool
    baggage: bool
    refundable: Optional[bool]
    changeable: Optional[bool]
    min_layover: Optional[int]
    number: Optional[str]
    car_class: Optional[str] = None
    fare_name: Optional[str] = None

    @classmethod
    def flight(cls, o):
        segs = o.get("segments") or []
        lays = [round((ts(b["dep"]) - ts(a["arr"])).total_seconds() / 60) for a, b in zip(segs, segs[1:])]
        bag = (o.get("baggage") or {}).get("checked_kg")
        fare = o.get("fare") or {}
        return cls("flight", o, o["offer_id"], ts(o["dep"]), ts(o["arr"]), o["origin_iata"], o["dest_iata"],
                   int(o["duration_min"]), int(o.get("stops", 0)), int(o["price_rub"]), o.get("carrier"),
                   bool(o.get("international")), bool(bag), fare.get("refundable"),
                   None if not fare else fare.get("change_fee_rub") is not None,
                   min(lays) if lays else None, o.get("flight_no"), None, fare.get("name"))

    @classmethod
    def train(cls, o):
        return cls("train", o, o["offer_id"], ts(o["dep"]), ts(o["arr"]), o["from_station"], o["to_station"],
                   int(o["duration_min"]), 0, int(o["price_rub"]), o.get("carrier"), False, True, None, None,
                   None, o.get("train_no"), o.get("car_class"))


@dataclass
class Combo:
    out: Optional[Leg]
    back: Optional[Leg]
    hotel: Optional[dict]          # None: transport-only scope (tickets without a stay)
    tr_out: Optional[dict]
    tr_back: Optional[dict]
    # computed
    rub: int = 0
    hours: float = 0.0
    out_hours: float = 0.0
    back_hours: float = 0.0
    arrival: Optional[datetime] = None      # at the hotel (or at the arrival point without a transfer)
    leave: Optional[datetime] = None        # leaving the hotel (or being at the departure point)
    stay_start: Optional[datetime] = None
    stay_end: Optional[datetime] = None
    comfort: float = 0.0
    unused_night: bool = False             # arrives after midnight: the first booked night is not used
    relaxed: tuple = ()
    key: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------- filters

def _night(dt, rr):
    return in_clock_window(dt.strftime("%H:%M"), rr["night_window"])


def _user(brief, path):
    return brief.source_of(path) == "user"


def _leg_ok(leg, brief, rr, today):
    prefs = brief.prefs.value
    constraints = set(brief.constraints.value or [])
    if today and leg.dep.date() < today:
        return False
    if "no_night_flights" in constraints and night_leg(leg, rr):
        return False
    if prefs is not None:
        if leg.kind == "flight" and prefs.direct_only and _user(brief, "prefs.direct_only") and leg.stops:
            return False
        if _user(brief, "prefs.transport") and prefs.transport in ("plane", "train"):
            if (prefs.transport == "plane") != (leg.kind == "flight"):
                return False
    return True


def _on_move_at(leg, hhmm) -> bool:
    """The leg is travelling at the local clock time `hhmm` (of its departure time zone) on some night."""
    t = leg.dep.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0)
    if t <= leg.dep:
        t += timedelta(days=1)
    return t < leg.arr


def night_leg(leg, rr) -> bool:
    """«Без ночных рейсов / поездов» (prefs.no_night_flights covers every leg): a flight or a train leaving or
    arriving inside ranking.night_window, or a train still on the move at ranking.night_move_at (03:00 local:
    an overnight sleeper leaving at 21:30 and arriving at 07:40 is a night train)."""
    if _night(leg.dep, rr) or _night(leg.arr, rr):
        return True
    return leg.kind == "train" and _on_move_at(leg, rr.get("night_move_at") or "03:00")


def _night_flight(leg, rr) -> bool:
    return night_leg(leg, rr)


def _soft_leg_filters(brief, direction, rr):
    """User prefs on tickets, as (relaxed path, keep(leg)) in relaxation order. Each is a hard filter
    unless it would leave no offer in this direction: then it is dropped and the path is reported
    (risk `relaxed_pref`, or `non_refundable_only` for tickets under free cancellation).
    A pet in the brief keeps tickets whose `pets` terms take it (pet_ok); constraint train:sapsan /
    train:high_speed (steps.train_cues) keeps only high-speed trains — per direction, path HIGH_SPEED.<dir>."""
    prefs = brief.prefs.value
    out = []
    if wants_pet(brief):
        out.append((PETS_CARRIAGE, lambda l: pet_ok(l, brief)))
    if wants_high_speed(brief):
        out.append((f"{HIGH_SPEED}.{direction}", lambda l: l.kind != "train" or is_high_speed(l)))
    if prefs is None:
        return out
    if direction == "out" and prefs.arrive_before and _user(brief, "prefs.arrive_before"):
        limit = prefs.arrive_before
        dates = brief.dates.value
        start = dates.start if dates is not None else None
        # «прилететь до 14:00» = by 14:00 local on the travel day: a 01:53 landing the next night is not
        # «before 14:00» (the clock alone said it was, s11)
        out.append(("prefs.arrive_before",
                    lambda l: (l.arr.date(), l.arr.strftime("%H:%M")) <= (start or l.dep.date(), limit)))
    if prefs.no_night_flights and _user(brief, "prefs.no_night_flights"):
        out.append(("prefs.no_night_flights", lambda l: not _night_flight(l, rr)))
    if prefs.free_cancellation and _user(brief, "prefs.free_cancellation"):
        # trains carry no fare flag in the mock (refundable by law); flights need a refundable fare
        out.append((TICKETS_REFUNDABLE, lambda l: l.kind != "flight" or l.refundable is True))
    return out


TICKETS_REFUNDABLE = "tickets.refundable"   # relaxed marker: no refundable fare under free cancellation
HIGH_SPEED = "train.high_speed"     # relaxed marker (+ .out / .back): no high-speed train that way
PETS_CARRIAGE = "pets.carriage"     # relaxed marker: no ticket whose pet terms take the party's animals
HIGH_SPEED_TOKENS = ("train:sapsan", "train:high_speed")   # constraints set by steps.train_cues


def wants_high_speed(brief) -> bool:
    return any(t in (brief.constraints.value or []) for t in HIGH_SPEED_TOKENS)


def is_high_speed(leg) -> bool:
    """A high-speed train (the mock's train_kind; offers without it: a «Сапсан» carrier)."""
    kind = (leg.offer or {}).get("train_kind")
    return kind == "high_speed" if kind else "сапсан" in str(leg.carrier or "").casefold()


def pet_count(brief) -> int:
    """Animals travelling (party.pets, else 1 for a pet:* constraint), at most 4 — the mock prices them."""
    party = brief.party.value
    n = len(party.pets) if party is not None and party.pets else 0
    return min(4, n or (1 if wants_pet(brief) else 0))


def pet_ok(leg, brief) -> bool:
    """The ticket's pet terms take the party's animals in a carrier (a dog of unknown size counts as one in a
    carrier). An offer without terms is unknown, never dropped on a guess."""
    terms = (leg.offer or {}).get("pets")
    if not terms:
        return True
    return bool(terms.get("small_in_carrier")) and pet_count(brief) <= int(terms.get("max_animals") or 0)


TRAIN_PLACE_RU = {"platzkart": "В плацкарте поезда", "coupe": "В купе поезда", "sv": "В СВ поезда"}


def pet_rule_line(kind, offer) -> Optional[str]:
    """A ready Russian line with the pet terms of one ticket (offer `pets`), or None without terms:
    «В «Сапсане» — только в переноске до 180 см по сумме трёх сторон, на местах для пассажиров с животными;
    провоз 720 ₽; крупных собак не возят, кроме собак-проводников». Sizes are the rule's, never the pet's."""
    o = offer or {}
    t = o.get("pets") or {}
    if not t:
        return None
    count, fee = int(t.get("count") or 0), t.get("fee_rub")
    price = (f"провоз {fmt_rub(fee)}" + (f" за животное, за {count} — {fmt_rub(t.get('fee_total_rub'))}" if count > 1 else "")
             if fee is not None else None)
    if kind == "flight":
        head = f"На рейсе {o.get('flight_no')}" if o.get("flight_no") else "В самолёте"
        if not t.get("small_in_carrier"):
            return f"{head} животных в салоне не возят"
        cabin = "в салоне в переноске" + (f", если вместе с ней до {t['carrier_max_kg']} кг" if t.get("carrier_max_kg") else "")
        line = f"{head} — {cabin}" + (f", {price}" if price else "")
        if t.get("large_dogs"):
            line += f"; {t.get('large_dogs_how') or 'крупных — в багажном отсеке'}" + (
                f", {fmt_rub(t['large_dog_fee_rub'])}" if t.get("large_dog_fee_rub") is not None else "")
        return line
    carrier, no, cls = str(o.get("carrier") or ""), o.get("train_no") or "", o.get("car_class")
    if o.get("train_kind") == "high_speed" or "сапсан" in carrier.casefold():
        head = "В «Сапсане»" if "сапсан" in carrier.casefold() else f"В скоростном поезде {no}".strip()
    elif o.get("train_kind") == "day" or cls == "seat":
        head = f"В дневном поезде {no}".strip()
    else:
        head = f"{TRAIN_PLACE_RU.get(cls, 'В поезде')} {no}".strip()
    if not t.get("small_in_carrier"):
        return f"{head} животных не возят"
    line = f"{head} — только в переноске"
    if t.get("carrier_max_cm_sum"):
        line += f" до {t['carrier_max_cm_sum']} см по сумме трёх сторон"
    if t.get("places"):
        line += f", на местах для пассажиров с животными" if "животн" in str(t["places"]) else f", {t['places']}"
    if price:
        line += f"; {price}"
    if t.get("large_dogs"):
        line += f"; крупную собаку — {t.get('large_dogs_how')}" if t.get("large_dogs_how") else "; крупную собаку можно"
        if t.get("large_dog_fee_rub"):
            line += f" (ещё {fmt_rub(t['large_dog_fee_rub'])})"
    elif o.get("train_kind") in ("high_speed", "day") or cls == "seat":
        line += "; крупных собак не возят, кроме собак-проводников"
    else:
        line += "; крупную собаку — только в отдельном купе, в плацкарте нельзя"
    return line


def _party_ok(h, brief):
    party = brief.party.value
    room = h.get("room") or {}
    kids = h.get("kids_policy") or {}
    if party is not None:
        cap_a, cap_c = room.get("capacity_adults"), room.get("capacity_children", 0) or 0
        if isinstance(cap_a, int) and (party.adults > cap_a or party.adults + party.children > cap_a + cap_c):
            return False
        if party.children:
            if kids.get("children_allowed") is False:
                return False
            min_age = kids.get("min_child_age")
            if min_age is not None and party.children_ages and min(party.children_ages) < min_age:
                return False
    if wants_pet(brief) and h.get("pets_allowed") is False:
        return False
    if party is not None and _is_dorm(room) and (party.segment in ("couple", "family") or party.children):
        return False   # a bed in a shared dorm is not a stay for a couple or a family
    if _is_dorm(room) and DORM_NEVER in (brief.constraints.value or []):
        return False   # «отдельный номер», «не хостел» (steps.room_cues)
    if refuses_hostel(brief) and (h.get("kind") == "hostel" or _is_dorm(room)):
        return False   # «хостел нам не подходит»: no hostel at all (private rooms too), no dorm bed anywhere
    if is_gendered_room(room):
        return False   # men-/women-only room: the traveller's gender is never known
    return True


DORM_OK = "hostel"       # constraint: the user asked for a hostel / a dorm bed (steps.room_cues)
DORM_NEVER = "no_dorm"   # constraint: the user asked for a private room
NO_HOSTEL = "no_hostel"  # constraint: the user refused hostels (implies no_dorm; steps.room_cues / understand)


def refuses_hostel(brief) -> bool:
    return NO_HOSTEL in (brief.constraints.value or [])


def wants_dorm(brief) -> bool:
    return DORM_OK in (brief.constraints.value or []) and not refuses_hostel(brief)


def _hotel_is_dorm(h) -> bool:
    return h is not None and _is_dorm(h.get("room") or {})


def _private_first(combos, brief):
    """Combos without a dorm bed when there are any (unless the user asked for a hostel): «Комфортнее»,
    «Быстрее», the location pick and the recommended variant are never a bed in a shared room when a
    private room exists; only «Дешевле» may be one."""
    if wants_dorm(brief):
        return combos
    private = [c for c in combos if not _hotel_is_dorm(c.hotel)]
    return private or combos


def _is_dorm(room) -> bool:
    name = str(room.get("name") or "").casefold()
    return room.get("bed") == "bunk" or "общем номере" in name or "общий номер" in name or "dorm" in name


# the traveller's gender is never known: a men-/women-only room is never offered (the catalog names
# them «Женский общий номер»; the other spellings guard data from other sources)
GENDERED_ROOM_WORDS = ("женск", "мужск", "female", "male", "women", "ladies", "girls", "boys")


def is_gendered_room(room) -> bool:
    name = str((room or {}).get("name") or "").casefold()
    return any(w in name for w in GENDERED_ROOM_WORDS)


def _penalty_rub(h, canc):
    pct, price = canc.get("penalty_pct"), h.get("price_total_rub")
    if not pct or not price:
        return None
    return int(round(price * pct / 100 / 10.0)) * 10


def _free_cancel(h) -> bool:
    c = h.get("cancellation") or {}
    return bool(c.get("free_until")) or c.get("penalty_pct", 100) == 0


def _pref_fails(h, brief, near=None) -> list[str]:
    """User-given prefs this hotel does not meet (hard filters, relaxed only when nothing is left). `near`: the
    resolved place of prefs.near (results["near"] with radius_m): farther hotels fail "prefs.near". Constraints
    kitchen / accessible need the mock's synthetic room.kitchen / accessibility.step_free (unknown fails)."""
    prefs = brief.prefs.value
    bad = []
    if wants_crib(brief) and _infant_cot(h) not in GUARANTEED_COT:
        bad.append(CRIB)
    if wants_kitchen(brief) and not has_kitchen(h):
        bad.append(KITCHEN)
    if wants_step_free(brief) and not step_free(h):
        bad.append(ACCESSIBLE)
    if near is not None and near.get("radius_m") is not None:
        d = h.get("distance_near_m")
        if d is None or d > int(near["radius_m"]):
            bad.append("prefs.near")
    if prefs is None:
        return bad
    if _user(brief, "prefs.stars_min") and prefs.stars_min and (h.get("stars") or 0) < prefs.stars_min:
        bad.append("prefs.stars_min")
    if _user(brief, "prefs.meals") and prefs.meals != "any" and MEALS_RANK.get(h.get("meals"), 0) < MEALS_RANK[prefs.meals]:
        bad.append("prefs.meals")
    if _user(brief, "prefs.free_cancellation") and prefs.free_cancellation and not _free_cancel(h):
        bad.append("prefs.free_cancellation")
    if _user(brief, "prefs.beach_max_m") and prefs.beach_max_m is not None:
        d = h.get("distance_beach_m")
        if d is None or d > prefs.beach_max_m:
            bad.append("prefs.beach_max_m")
    if _user(brief, "prefs.center_max_m") and prefs.center_max_m is not None:
        d = h.get("distance_center_m")
        if d is None or d > prefs.center_max_m:
            bad.append("prefs.center_max_m")
    return bad


# ---------------------------------------------------------------- city centre

def user_center_max(brief):
    prefs = brief.prefs.value
    if prefs is None or prefs.center_max_m is None or not _user(brief, "prefs.center_max_m"):
        return None
    return int(prefs.center_max_m)


def center_applies(brief, dest, rr) -> bool:
    """The hotel's distance to the city centre is a fit / comfort component: always with a user-set
    prefs.center_max_m; otherwise for a destination whose kind is in ranking.center_kinds (city — not a
    resort, nature or village stay) when the trip has no sea / beach / island wish and no prefs.beach_max_m
    (a beach stay is scored by the beach, never pulled to the centre)."""
    if user_center_max(brief) is not None:
        return True
    prefs = brief.prefs.value
    if prefs is not None and prefs.beach_max_m is not None:
        return False
    if set(brief.traits.value or []) & SEA_TRAITS:
        return False
    return (dest or {}).get("kind") in tuple(rr.get("center_kinds") or ())


def center_score(brief, distance_m, rr) -> Optional[float]:
    """0..1 by the distance to the centre (None without a distance)."""
    if distance_m is None:
        return None
    d = float(distance_m)
    mx = user_center_max(brief)
    if mx is not None:
        return round(1.0 if d <= mx else max(0.0, 1 - (d - mx) / max(mx, rr["center_full_m"])), 3)
    full, zero = rr["center_full_m"], rr["center_zero_m"]
    return round(1.0 if d <= full else max(0.0, 1 - (d - full) / max(1.0, zero - full)), 3)


def near_score(distance_m, rr) -> Optional[float]:
    """0..1 by the distance to the named place (None without a distance): 1 up to near_full_m, 0 from near_zero_m."""
    if distance_m is None:
        return None
    d, full, zero = float(distance_m), rr["near_full_m"], rr["near_zero_m"]
    return round(1.0 if d <= full else max(0.0, 1 - (d - full) / max(1.0, zero - full)), 3)


def near_distance_m(h, near) -> Optional[int]:
    """Metres from a hotel offer to the named place: the mock's distance_near_m, else from the offer's lat/lon."""
    if h is None or not near:
        return None
    if h.get("lat") is not None and h.get("lon") is not None:
        return int(round(_haversine_km(float(h["lat"]), float(h["lon"]), float(near["lat"]), float(near["lon"])) * 1000))
    d = h.get("distance_near_m")
    return int(d) if d is not None else None


def _km_text(metres) -> str:
    """«1,5 км» / «800 м» for a radius."""
    m = int(metres)
    return f"{fmt_int(m)} м" if m < 1000 else f"{m / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " км"


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _loc_score(h, brief, rr) -> Optional[float]:
    """The location score of a hotel: by the distance to the named place when one is resolved (rr["near"]),
    else by the distance to the centre."""
    if rr.get("near"):
        return near_score(h.get("distance_near_m"), rr)
    return center_score(brief, h.get("distance_center_m"), rr)


def _center_of(h, brief, rr) -> float:
    """The location score of a hotel offer when the location counts for this trip (rr["center_on"]: a city
    stay, prefs.center_max_m or a resolved prefs.near), else 0."""
    if not rr.get("center_on") or h is None:
        return 0.0
    return _loc_score(h, brief, rr) or 0.0


def _center_card(picked, private, ref, brief, rr, limit):
    """City stays (door to door): when no affordable card is central (only «Комфортнее» may be) and a central
    hotel (centre score >= center_card_min_score) exists within center_card_price_ratio x the cheapest
    private-room trip (x «Быстрее» when it replaces that card), «Удачнее по району» (custom-1) shows it (the best comfort for its price, like
    «Быстрее»). With a full set it takes the place of «Быстрее» when that one is less than
    center_card_max_gain_h faster door to door (a shorter airport transfer is not worth a card in a city;
    s04: Galata 2.5 km out was «Быстрее» and the recommendation). Changes `picked` in place."""
    good = rr["center_card_min_score"]
    if any(_center_of(c.hotel, brief, rr) >= good for vid, c in picked if vid != "comfort"):
        return
    taken = {c.key for _, c in picked}
    full = len(picked) >= limit
    fast = next((c for vid, c in picked if vid == "fastest"), None) if full else None
    if full and fast is None:
        return
    # the price is compared with the card it joins (the cheapest private room) or replaces («Быстрее»)
    cap = (fast.rub if full else ref.rub) * rr["center_card_price_ratio"]
    rest = [c for c in private if c.key not in taken and c.rub <= cap and _center_of(c.hotel, brief, rr) >= good]
    if full:
        rest = [c for c in rest if c.hours - fast.hours < rr["center_card_max_gain_h"]]
    if not rest:
        return
    base = max(1, ref.rub)
    best = max(rest, key=lambda c: (round(c.comfort - rr["fastest_price_weight"] * (c.rub / base - 1), 6),
                                    -c.hours, -c.rub))
    if full:
        picked[:] = [pc for pc in picked if pc[0] != "fastest"]
    picked.append(("custom-1", best))


def _nearest_hotels(hotels, rr, key="distance_center_m", tolerance="center_relax_tolerance_m"):
    """prefs.center_max_m (prefs.near: key distance_near_m) relaxed: only the hotels within the tolerance of the
    nearest one («показываем ближайшие»)."""
    ds = [h.get(key) for h in hotels if h.get(key) is not None]
    if not ds:
        return hotels
    cap = min(ds) + rr[tolerance]
    return [h for h in hotels if h.get(key) is not None and h[key] <= cap]


CRIB = "constraints.crib"          # relaxed marker: no hotel with a guaranteed cot
GUARANTEED_COT = ("free", "paid")  # ostrovok KidsPolicy.infant_cot; on_request / none are not a promise
COT_COMFORT = {"free": 1.0, "paid": 0.6, "on_request": 0.2, "none": 0.0}
# hotel prefs in relaxation order: when no hotel meets them all, the smallest set is dropped, the
# earliest paths in this list first (a cot for a toddler is dropped last)
KITCHEN = "constraints.kitchen"         # relaxed marker: no room with a kitchen
ACCESSIBLE = "constraints.accessible"   # relaxed marker: no hotel with step-free access
RELAX_ORDER = ("prefs.center_max_m", "prefs.near", "prefs.beach_max_m", "prefs.stars_min", "prefs.meals",
               "prefs.free_cancellation", CRIB, KITCHEN, ACCESSIBLE)


def wants_crib(brief) -> bool:
    return "crib" in (brief.constraints.value or [])


def wants_kitchen(brief) -> bool:
    return "kitchen" in (brief.constraints.value or [])


def wants_step_free(brief) -> bool:
    return "accessible" in (brief.constraints.value or [])


def has_kitchen(h) -> bool:
    """The offer's room has a kitchen (the mock's synthetic room.kitchen)."""
    return bool(((h or {}).get("room") or {}).get("kitchen"))


def step_free(h) -> Optional[bool]:
    """Step-free access of the offer's hotel (the mock's synthetic accessibility.step_free), None when unknown."""
    acc = (h or {}).get("accessibility")
    return None if not isinstance(acc, dict) else bool(acc.get("step_free"))


def lift(h) -> Optional[bool]:
    acc = (h or {}).get("accessibility")
    return None if not isinstance(acc, dict) else bool(acc.get("lift"))


def _infant_cot(h):
    return (h.get("kids_policy") or {}).get("infant_cot")


def _relax_hotels(hotels, brief, near=None):
    """Hotels that meet the user's hotel prefs -> (hotels, relaxed paths). When none meets them all, the
    smallest set of prefs is dropped (ties: earlier in RELAX_ORDER first); the paths reported are those
    the kept hotels actually miss."""
    from itertools import combinations
    fails = [(h, set(_pref_fails(h, brief, near))) for h in hotels]
    strict = [h for h, f in fails if not f]
    if strict or not hotels:
        return strict, ()
    paths = sorted({p for _, f in fails for p in f},
                   key=lambda p: RELAX_ORDER.index(p) if p in RELAX_ORDER else -1)
    for n in range(1, len(paths) + 1):
        for drop in combinations(paths, n):
            kept = [h for h, f in fails if f <= set(drop)]
            if kept:
                return kept, tuple(sorted({p for h, f in fails if f <= set(drop) for p in f}))
    return hotels, tuple(paths)


RELAXED_RU = {"prefs.stars_min": "с нужной звёздностью", "prefs.meals": "с нужным питанием",
              "prefs.free_cancellation": "с бесплатной отменой", "prefs.beach_max_m": "так близко к пляжу",
              "prefs.center_max_m": "так близко к центру", "prefs.near": "так близко к нужному месту",
              "prefs.max_travel_hours": "с дорогой в нужное время", "prefs.arrive_before": "с прибытием к нужному времени",
              "prefs.no_night_flights": "без ночной дороги", CRIB: "с гарантированной детской кроваткой",
              KITCHEN: "с кухней в номере", ACCESSIBLE: "без ступенек"}


# ---------------------------------------------------------------- district

def district_of(h, districts):
    if not districts:
        return None
    return districts.get(h.get("osm_id")) or None


def district_fit(brief, profile, segments) -> Optional[float]:
    if not profile or not profile.get("scores"):
        return None
    scores = profile["scores"]
    prefs = brief.prefs.value
    if prefs is not None and prefs.district != "any" and _user(brief, "prefs.district"):
        weights = {prefs.district: 1.0}
    else:
        party = brief.party.value
        seg = (segments or {}).get(party.segment if party else "other") or {}
        weights = seg.get("district_weights") or {k: 0.25 for k in ("lively", "quiet", "family", "walkable")}
        if prefs is not None and prefs.district != "any":  # assumed district pref leans the segment weights
            weights = {k: v * 0.5 for k, v in weights.items()}
            weights[prefs.district] = weights.get(prefs.district, 0) + 0.5
    total = sum(weights.values()) or 1.0
    return round(sum(w * float(scores.get(k, 0)) for k, w in weights.items()) / total, 3)


def district_labels(profile, rr):
    if not profile:
        return []
    if profile.get("labels"):
        return list(profile["labels"])
    return [k for k in ("lively", "quiet", "family", "walkable") if (profile.get("scores") or {}).get(k, 0) >= rr["district_label_min"]]


# ---------------------------------------------------------------- combos

def _buffers(rules):
    return {**DEFAULT_BUFFERS, **(rules.get("buffers_min") or {})}


def _before(leg, buffers):
    if leg.kind == "train":
        return buffers["before_train"]
    return buffers["before_flight_international" if leg.international else "before_flight_domestic"]


def _after(leg, buffers, rr):
    if leg.kind == "train":
        return rr["after_train_min"]
    return buffers["after_flight_with_baggage" if leg.baggage else "after_flight"]


def _planned(duration, factor):
    return int(math.ceil(duration * factor / 5.0) * 5)  # round up to 5 minutes


def _transfer_point_ok(t, point):
    p = t.get("point")
    return p is None or str(p).casefold() == str(point).casefold()


def _pick_transfers(options, point):
    """Cheapest and fastest transfer for the point (one or two offers)."""
    opts = [t for t in options if _transfer_point_ok(t, point)]
    if not opts:
        return [None]
    picks = {min(opts, key=lambda t: (t["price_rub"], t["duration_min"]))["offer_id"]: None,
             min(opts, key=lambda t: (t["duration_min"], t["price_rub"]))["offer_id"]: None,
             max(opts, key=lambda t: (COMFORT_TRANSFER.get(t.get("kind"), 0), -t["price_rub"]))["offer_id"]: None}
    by_id = {t["offer_id"]: t for t in opts}
    return [by_id[i] for i in picks]


RED_EYE_UNTIL_HOUR = 6   # a return leaving before this hour on the day after check-out is a night flight


def _evaluate(c: Combo, rules, rr, factor) -> bool:
    """Compute times and totals; False when the pieces do not fit together."""
    buffers = _buffers(rules)
    h = c.hotel
    check_in, check_out = ts(h["check_in"]), ts(h["check_out"])
    out_span = back_span = 0.0
    if c.out is not None:
        t0 = c.out.arr + timedelta(minutes=_after(c.out, buffers, rr))
        ride = _planned(c.tr_out["duration_min"], factor) if c.tr_out else 0
        arrival = t0 + timedelta(minutes=ride) if c.tr_out else t0
        c.arrival = arrival
        out_span = ((c.out.arr - c.out.dep).total_seconds() / 60 + ride) / 60
        day0 = datetime.combine(check_in.date(), datetime.min.time(), tzinfo=check_in.tzinfo)
        late_end = (rules.get("late_checkin") or DEFAULT_LATE_CHECKIN)[1]
        if arrival >= day0 + timedelta(days=1, hours=int(late_end[:2]), minutes=int(late_end[3:5])):
            return False  # the first night is lost
        if arrival.date() < check_in.date():
            return False  # arrives a day before the stay: a night without a bed
        c.unused_night = arrival.date() > check_in.date()   # after midnight: the first night is paid, not used
        c.stay_start = max(check_in, arrival)
    else:
        c.stay_start = check_in
    if c.back is not None:
        at_point = c.back.dep - timedelta(minutes=_before(c.back, buffers))
        ride = _planned(c.tr_back["duration_min"], factor) if c.tr_back else 0
        leave = at_point - timedelta(minutes=ride)
        c.leave = leave
        back_span = ((c.back.arr - c.back.dep).total_seconds() / 60 + ride) / 60
        last_day = datetime.combine(check_out.date(), datetime.min.time(), tzinfo=check_out.tzinfo)
        if leave < last_day:
            return False  # leaves before the last night is over
        if leave - check_out > timedelta(hours=rr["max_wait_after_checkout_h"]):
            return False
        if c.back.dep.date() > check_out.date() and c.back.dep.hour >= RED_EYE_UNTIL_HOUR:
            return False  # leaves the next morning or later: a night without a bed
        # a red-eye (00:00-05:59 the night after check-out) is fine: the evening goes to the airport, the
        # night is spent travelling, and max_wait_after_checkout_h above already bounds the wait
        c.stay_end = min(check_out, leave)
    else:
        c.stay_end = check_out
    if c.stay_end <= c.stay_start:
        return False
    if c.out is not None and c.back is not None and c.back.dep <= c.out.arr:
        return False
    c.out_hours, c.back_hours = round(out_span, 2), round(back_span, 2)
    c.hours = round(out_span + back_span, 1)
    c.rub = int(sum(x for x in (c.out.price_rub if c.out else 0, c.back.price_rub if c.back else 0,
                                h["price_total_rub"], c.tr_out["price_rub"] if c.tr_out else 0,
                                c.tr_back["price_rub"] if c.tr_back else 0)))
    c.key = tuple(x for x in (c.out.offer_id if c.out else None, c.back.offer_id if c.back else None,
                              h["offer_id"], c.tr_out["offer_id"] if c.tr_out else None,
                              c.tr_back["offer_id"] if c.tr_back else None))
    return True


def _evaluate_transport(c: Combo, rules, rr) -> bool:
    """Tickets only (no stay): the return must leave after the outbound arrives, with the
    after-arrival and before-departure buffers between them. No transfers."""
    buffers = _buffers(rules)
    if c.out is None or c.back is None:
        return False
    if c.back.dep < c.out.arr + timedelta(minutes=_after(c.out, buffers, rr) + _before(c.back, buffers)):
        return False
    c.arrival, c.leave = c.out.arr, c.back.dep
    c.out_hours = round((c.out.arr - c.out.dep).total_seconds() / 3600, 2)
    c.back_hours = round((c.back.arr - c.back.dep).total_seconds() / 3600, 2)
    c.hours = round(c.out_hours + c.back_hours, 1)
    c.rub = int(c.out.price_rub + c.back.price_rub)
    c.key = (c.out.offer_id, c.back.offer_id)
    return True


def _comfort(c: Combo, brief, districts, segments, rr) -> float:
    w = rr["comfort_weights"]
    h = c.hotel
    legs = [x for x in (c.out, c.back) if x is not None]
    parts = {}
    if h is not None:
        parts.update({
            "stars": (h.get("stars") or 0) / 5,
            "rating": (h.get("rating") or 0) / 10,
            "meals": MEALS_RANK.get(h.get("meals"), 0) / 4,
            "cancellation": 1.0 if _free_cancel(h) else 0.0,
        })
        if wants_crib(brief):
            parts["cot"] = COT_COMFORT.get(_infant_cot(h), 0.0)
    if legs:
        parts["direct"] = 1.0 if all(l.stops == 0 for l in legs) else 0.0
        parts["fare"] = sum((1.0 if l.baggage else 0) * 0.5 + (0.5 if l.refundable else 0) for l in legs) / len(legs)
        if h is not None:
            trs = [t for t in (c.tr_out, c.tr_back)]
            parts["transfer"] = sum(COMFORT_TRANSFER.get(t.get("kind"), 0) if t else 0 for t in trs) / len(trs)
        trains = [l for l in legs if l.kind == "train"]
        if trains:
            parts["car_class"] = sum(CAR_CLASS_COMFORT.get(l.car_class, 0.3) for l in trains) / len(trains)
    d = district_fit(brief, district_of(h, districts), segments) if h is not None else None
    if d is not None:
        parts["district"] = d
    if h is not None and rr.get("center_on"):
        cs = _loc_score(h, brief, rr)
        if cs is not None:
            parts["near" if rr.get("near") else "center"] = cs
    total = sum(w.get(k, 0) for k in parts) or 1.0
    return round(sum(w.get(k, 0) * v for k, v in parts.items()) / total, 4)


def _leg_comfort(leg) -> float:
    if leg.kind == "train":
        return CAR_CLASS_COMFORT.get(leg.car_class, 0.3)
    return (0.4 if leg.stops == 0 else 0) + (0.3 if leg.baggage else 0) + (0.3 if leg.refundable else 0)


def _prune_legs(legs, n, extra=0):
    """Cheapest half + fastest up to n, plus `extra` most comfortable (fare, car class) for the comfort variant."""
    if len(legs) <= n:
        return legs
    keep = {}
    for leg in sorted(legs, key=lambda l: (l.price_rub, l.duration_min))[: (n + 1) // 2]:
        keep[leg.offer_id] = leg
    for leg in sorted(legs, key=lambda l: (l.duration_min, l.price_rub)):
        if len(keep) >= n:
            break
        keep.setdefault(leg.offer_id, leg)
    for leg in sorted(legs, key=lambda l: (-_leg_comfort(l), l.duration_min, l.price_rub)):
        if len(keep) >= n + extra:
            break
        keep.setdefault(leg.offer_id, leg)
    return list(keep.values())


def _prune_hotels(hotels, n, brief, districts, segments, max_dorms=None, rr=None):
    """Cheapest half + best quality up to n. Dorm beds never crowd out private rooms: unless the user
    asked for a hostel, at most `max_dorms` cheapest dorm offers stay next to the private ones."""
    if max_dorms is not None and not wants_dorm(brief):
        dorms = [h for h in hotels if _hotel_is_dorm(h)]
        private = [h for h in hotels if not _hotel_is_dorm(h)]
        if dorms and private:
            keep = sorted(dorms, key=lambda h: (h["price_total_rub"], h["offer_id"]))[:max(0, int(max_dorms))]
            return keep + _prune_hotels(private, max(1, n - len(keep)), brief, districts, segments, rr=rr)
    if len(hotels) <= n:
        return hotels
    def quality(h):
        d = district_fit(brief, district_of(h, districts), segments) or 0
        return ((h.get("stars") or 0) / 5 + (h.get("rating") or 0) / 10 + MEALS_RANK.get(h.get("meals"), 0) / 8 + d
                + (_center_of(h, brief, rr) if rr else 0.0))
    keep = {}
    for h in sorted(hotels, key=lambda h: h["price_total_rub"])[: (n + 1) // 2]:
        keep[h["offer_id"]] = h
    for h in sorted(hotels, key=lambda h: -quality(h)):
        if len(keep) >= n:
            break
        keep.setdefault(h["offer_id"], h)
    return list(keep.values())


def _scope(brief):
    scope = brief.scope.value
    return set(scope) if scope else {"flights", "trains", "stay", "transfers"}


def _legs(results, direction, brief, rr, today, soft=True, buffers=None):
    """Transport offers for one direction -> (legs, relaxed paths). Hard filters first (_leg_ok), then
    the user's ticket prefs (_soft_leg_filters), each dropped only when it would leave nothing."""
    scope = _scope(brief)
    legs = []
    if "flights" in scope or "trains" not in scope:
        legs += [Leg.flight(_dump(o)) for o in _offers(results.get(f"flights_{direction}"))]
    if "trains" in scope or "flights" not in scope:
        legs += [Leg.train(_dump(o)) for o in _offers(results.get(f"trains_{direction}"))]
    legs = [l for l in legs if _leg_ok(l, brief, rr, today)]
    relaxed = []
    for path, keep in _soft_leg_filters(brief, direction, rr):
        kept = [l for l in legs if keep(l)]
        if kept and soft:
            legs = kept
        elif legs and not (kept and kept == legs):
            relaxed.append(path)
    kept = _prune_legs(legs, rr["max_legs"], int(rr.get("comfort_legs", 0)))
    if short_break(brief) and legs:
        buffers = buffers or DEFAULT_BUFFERS
        def schedule_key(leg):
            time = (leg.arr.timestamp() + 60 * _after(leg, buffers, rr) if direction == "out" else
                    -leg.dep.timestamp() + 60 * _before(leg, buffers))
            return time, leg.duration_min, leg.price_rub
        best = min(legs, key=schedule_key)
        if all(leg.offer_id != best.offer_id for leg in kept):
            kept.append(best)
    return kept, tuple(relaxed)


def _one_way_hours(c) -> float:
    return max(c.out_hours, c.back_hours)


def _max_hours_ok(c, brief):
    """prefs.max_travel_hours is a ONE-WAY limit («не дольше 5 часов в дороге» = each direction):
    the longer of the two door-to-door directions (transport + planned transfer) must fit it; the
    round-trip sum (door_to_door_hours) is never compared with it."""
    prefs = brief.prefs.value
    if prefs is None or not prefs.max_travel_hours or not _user(brief, "prefs.max_travel_hours"):
        return True
    return _one_way_hours(c) <= prefs.max_travel_hours


# ---------------------------------------------------------------- variant assembly

def _place(name, fallback):
    return name or fallback


def _step(id_, type_, frm, to, start, end, price, today, **extra):
    from .state import Price, Step
    link = f"ostrovok-mock:{type_}/{extra.get('offer_id') or id_}"
    return Step(id=id_, type=type_, **{"from": frm}, to=to, start=start.isoformat(), end=end.isoformat(),
                price=Price(amount=int(price), currency="RUB", checked_at=today.isoformat(), rub=int(price)),
                link=link, **{k: v for k, v in extra.items() if v is not None})


def _leg_title(leg, frm_city, to_city):
    word = "Перелёт" if leg.kind == "flight" else "Поезд"
    return f"{word} {frm_city or leg.origin} → {to_city or leg.dest}"


def _steps_for(c: Combo, brief, rules, today, dest_name):
    factor = rules.get("taxi_time_factor", DEFAULT_TAXI_FACTOR)
    buffers = _buffers(rules)
    rr = ranking_rules(rules)
    origin = brief.origin.value.city if brief.origin.value else None
    h = c.hotel
    hotel_name = h["name"] if h is not None else None
    steps = []
    n = 0

    def nid():
        nonlocal n
        n += 1
        return f"s{n}"

    if c.out is not None:
        steps.append(_step(nid(), c.out.kind, c.out.origin, c.out.dest, c.out.dep, c.out.arr, c.out.price_rub, today,
                           carrier=c.out.carrier, international=c.out.international if c.out.kind == "flight" else None,
                           checked_baggage=c.out.baggage if c.out.kind == "flight" else None,
                           offer_id=c.out.offer_id, title=_leg_title(c.out, origin, dest_name)))
        if c.tr_out:
            t0 = c.out.arr + timedelta(minutes=_after(c.out, buffers, rr))
            t1 = t0 + timedelta(minutes=_planned(c.tr_out["duration_min"], factor))
            steps.append(_step(nid(), "transfer", c.out.dest, hotel_name, t0, t1, c.tr_out["price_rub"], today,
                               duration_estimate_min=int(c.tr_out["duration_min"]), offer_id=c.tr_out["offer_id"],
                               title=f"{TRANSFER_RU.get(c.tr_out.get('kind'), 'Трансфер').capitalize()} до отеля"))
    if h is not None:
        nights = (ts(h["check_out"]).date() - ts(h["check_in"]).date()).days
        steps.append(_step(nid(), "stay", hotel_name, hotel_name, c.stay_start, c.stay_end, h["price_total_rub"], today,
                           offer_id=h["offer_id"], title=f"{nights_text(nights)}, {MEALS_RU.get(h.get('meals'), '')}"))
    if c.back is not None:
        if c.tr_back:
            t1 = c.back.dep - timedelta(minutes=_before(c.back, buffers))
            t0 = t1 - timedelta(minutes=_planned(c.tr_back["duration_min"], factor))
            steps.append(_step(nid(), "transfer", hotel_name, c.back.origin, t0, t1, c.tr_back["price_rub"], today,
                               duration_estimate_min=int(c.tr_back["duration_min"]), offer_id=c.tr_back["offer_id"],
                               title=f"{TRANSFER_RU.get(c.tr_back.get('kind'), 'Трансфер').capitalize()} в {'аэропорт' if c.back.kind == 'flight' else 'на вокзал'}"))
        steps.append(_step(nid(), c.back.kind, c.back.origin, c.back.dest, c.back.dep, c.back.arr, c.back.price_rub, today,
                           carrier=c.back.carrier, international=c.back.international if c.back.kind == "flight" else None,
                           checked_baggage=c.back.baggage if c.back.kind == "flight" else None,
                           offer_id=c.back.offer_id, title=_leg_title(c.back, dest_name, origin)))
    if h is None:   # tickets only: the days at the destination are not a connection
        return steps
    risks = connection_risks([s.to_check_dict() for s in steps], rules)
    for s in steps:
        if s.id in risks:
            s.risk = risks[s.id]
    return steps


def _facts(c: Combo, brief, districts, segments, rr):
    h = c.hotel
    if h is None:
        return _transport_facts(c, rr)
    room = h.get("room") or {}
    kids = h.get("kids_policy") or {}
    canc = h.get("cancellation") or {}
    early = h.get("early_checkin") or {}
    check_in, check_out = ts(h["check_in"]), ts(h["check_out"])
    f: dict[str, Any] = {
        "total_rub": c.rub,
        "door_to_door_hours": c.hours,
        "price_stay_rub": int(h["price_total_rub"]),
        "nights": (check_out.date() - check_in.date()).days,
        "check_in_date": check_in.date().isoformat(),
        "check_out_date": check_out.date().isoformat(),
        "check_in_time": check_in.strftime("%H:%M"),
        "check_out_time": check_out.strftime("%H:%M"),
        "hotel_name": h["name"],
        "hotel_kind": h.get("kind"),
        "hotel_stars": h.get("stars"),
        "hotel_rating": h.get("rating"),
        "hotel_reviews": h.get("reviews"),
        "meals": h.get("meals"),
        "meals_ru": MEALS_RU.get(h.get("meals")),
        "free_cancellation": _free_cancel(h),
        "free_cancellation_until": (canc.get("free_until") or "")[:10] or None,
        "cancellation_penalty_pct": canc.get("penalty_pct"),
        "cancellation_penalty_rub": _penalty_rub(h, canc),   # code computes it: models must not take percents
        "distance_beach_m": h.get("distance_beach_m"),
        "distance_center_m": h.get("distance_center_m"),
        "room_name": room.get("name"),
        "dorm_bed": True if _is_dorm(room) else None,
        "room_capacity_adults": room.get("capacity_adults"),
        "room_capacity_children": room.get("capacity_children"),
        "pets_allowed": h.get("pets_allowed"),
        "children_allowed": kids.get("children_allowed"),
        "min_child_age": kids.get("min_child_age"),
        "infant_cot": kids.get("infant_cot"),
        "early_checkin_available": early.get("available"),
        "early_checkin_price_rub": early.get("price_rub"),
        "kitchen": has_kitchen(h) if "room" in h else None,   # synthetic (the mock): a kitchen in the room
        "step_free": step_free(h),                            # synthetic: step-free access
        "lift": lift(h),
    }
    _leg_facts(f, c, rr)
    _long_leg_facts(f, c, check_in, check_out)
    if short_break(brief) and destination_hours(c) is not None:
        f["destination_hours"] = destination_hours(c)
    for direction, tr in (("out", c.tr_out), ("back", c.tr_back)):
        if tr is not None:
            f[f"transfer_{direction}_min"] = int(tr["duration_min"])
            f[f"transfer_{direction}_price_rub"] = int(tr["price_rub"])
            f[f"transfer_{direction}_kind"] = tr.get("kind")
    if c.tr_out or c.tr_back:
        f["price_transfers_rub"] = sum(int(t["price_rub"]) for t in (c.tr_out, c.tr_back) if t)
    if c.out or c.back:
        f["price_transport_rub"] = sum(l.price_rub for l in (c.out, c.back) if l)
    if c.out is not None and c.arrival is not None:
        f["arrival_at_hotel_time"] = c.arrival.strftime("%H:%M")
        f["wait_before_checkin_min"] = max(0, round((check_in - c.arrival).total_seconds() / 60))
    if c.back is not None and c.leave is not None:
        f["leave_hotel_time"] = c.leave.strftime("%H:%M")
        f["wait_after_checkout_min"] = max(0, round((c.leave - check_out).total_seconds() / 60))
    profile = district_of(h, districts)
    if profile:
        for k, v in (profile.get("scores") or {}).items():
            f[f"district_{k}"] = round(float(v), 2)
        for k, v in (profile.get("counts") or profile.get("poi_counts") or {}).items():
            f[f"poi_{k}"] = int(v)
        for k, v in (profile.get("nearest_m") or {}).items():
            if v is not None:
                f[f"nearest_{k}_m"] = int(v)
        d = district_fit(brief, profile, segments)
        if d is not None:
            f["district_fit"] = d
    near = rr.get("near")
    if near:   # a named place: its distance replaces the centre's in fit and comfort
        f["near_name"] = near.get("name")
        f["distance_near_m"] = h.get("distance_near_m")
        ns = near_score(h.get("distance_near_m"), rr)
        if ns is not None:
            f["near_fit"] = ns
    elif rr.get("center_on"):
        cs = center_score(brief, h.get("distance_center_m"), rr)
        if cs is not None:
            f["center_fit"] = cs   # the centre counts for this trip (city stay or prefs.center_max_m)
    f["comfort_score"] = c.comfort
    if c.relaxed:
        f["relaxed"] = ",".join(c.relaxed)
    return {k: v for k, v in f.items() if v is not None}


def _leg_facts(f, c: Combo, rr=None):
    for direction, leg in (("out", c.out), ("back", c.back)):
        if leg is None:
            continue
        if rr is not None and night_leg(leg, rr):
            f[f"{direction}_night"] = True   # leaves / arrives at night or an overnight train
        f.update({
            f"{direction}_kind": leg.kind,
            f"{direction}_price_rub": leg.price_rub,
            f"{direction}_from": leg.origin,
            f"{direction}_to": leg.dest,
            f"{direction}_date": leg.dep.date().isoformat(),
            f"{direction}_dep_time": leg.dep.strftime("%H:%M"),
            f"{direction}_arr_time": leg.arr.strftime("%H:%M"),
            f"{direction}_arr_date": leg.arr.date().isoformat(),
            f"{direction}_duration_min": leg.duration_min,
            f"{direction}_duration_hours": round(leg.duration_min / 60, 1),
            f"{direction}_stops": leg.stops,
            f"{direction}_carrier": leg.carrier,
            f"{direction}_number": leg.number,
            f"{direction}_baggage": leg.baggage,
            f"{direction}_refundable": leg.refundable,
            f"{direction}_fare": leg.fare_name,
            f"{direction}_car_class": leg.car_class,
            f"{direction}_layover_min": leg.min_layover,
            f"{direction}_door_hours": round(getattr(c, f"{direction}_hours"), 1),
        })
        if leg.kind == "train" and is_high_speed(leg):
            f[f"{direction}_high_speed"] = True
        terms = (leg.offer or {}).get("pets") or {}
        if terms.get("count"):   # searched with the party's animals: the fee is inside the leg price
            f[f"{direction}_pets_rule"] = pet_rule_line(leg.kind, leg.offer)
            f[f"{direction}_pet_fee_rub"] = int(terms.get("fee_total_rub") or 0)
    fees = [f[k] for k in ("out_pet_fee_rub", "back_pet_fee_rub") if f.get(k)]
    if fees:   # its own line of the total (already inside the ticket prices and totals.rub)
        f["price_pets_rub"] = sum(fees)


# A train this long is a long-distance sleeper of a pair over 1000 km (ostrovok_mcp/rail.py: 1.3 x great circle at
# 60-70 km/h, at least ~18.6 h); the mock's trains of pairs up to 1000 km stay under ~17.9 h.
LONG_LEG_MIN = 18 * 60


def road_time_ru(minutes) -> str:
    """Time on board as people say it: «23 ч», «1 сутки 3 ч», «2 суток» (rounded to the hour)."""
    total = int(round(int(minutes) / 60))
    days, hours = divmod(total, 24)
    if not days:
        return f"{hours} ч"
    text = f"{days} {plural(days, 'сутки', 'суток', 'суток')}"
    return f"{text} {hours} ч" if hours else text


def day_month_ru(dt) -> str:
    return f"{dt.day} {MONTHS_GEN[dt.month - 1]}"


def _days_word(n) -> str:
    return "день" if n == 1 else f"{n} {plural(n, 'день', 'дня', 'дней')}"


def is_long_leg(leg) -> bool:
    return leg is not None and leg.kind == "train" and leg.duration_min >= LONG_LEG_MIN


def _long_leg_facts(f, c: Combo, check_in=None, check_out=None):
    """Ready Russian facts of a long-distance train leg (the card says them as written, never computes them):
    `{dir}_long_train`, `{dir}_on_road_ru` («1 сутки 3 ч»), `{dir}_road_ru` (departure and arrival days and
    times, the time on board and — with a stay — how many days before check-in the trip starts /
    after check-out it ends), `out_days_before_checkin`, `back_days_after_checkout`."""
    for direction, leg in (("out", c.out), ("back", c.back)):
        if not is_long_leg(leg):
            continue
        road = road_time_ru(leg.duration_min)
        line = (f"{'туда' if direction == 'out' else 'обратно'} поездом: отправление {day_month_ru(leg.dep)} в "
                f"{leg.dep:%H:%M}, прибытие {day_month_ru(leg.arr)} в {leg.arr:%H:%M}, в пути {road}")
        if direction == "out" and check_in is not None and leg.dep.date() < check_in.date():
            n = (check_in.date() - leg.dep.date()).days
            f["out_days_before_checkin"] = n
            # short ready clause the card copies as is (a long line gets paraphrased and loses the day)
            f["out_leave_ru"] = f"выезд {day_month_ru(leg.dep)} — за {_days_word(n)} до заезда"
            line += f" — выезжаете за {_days_word(n)} до заезда в отель"
        if direction == "back" and check_out is not None and leg.arr.date() > check_out.date():
            n = (leg.arr.date() - check_out.date()).days
            f["back_days_after_checkout"] = n
            f["back_home_ru"] = f"дома {day_month_ru(leg.arr)}"
            line += (" — дома на следующий день после выезда из отеля" if n == 1
                     else f" — дома через {_days_word(n)} после выезда из отеля")
        f[f"{direction}_long_train"] = True
        f[f"{direction}_on_road_ru"] = road
        f[f"{direction}_road_ru"] = line


def _transport_facts(c: Combo, rr=None):
    """Facts of a tickets-only variant (no stay, no transfers)."""
    f: dict[str, Any] = {"total_rub": c.rub, "door_to_door_hours": c.hours, "transport_only": True}
    _leg_facts(f, c, rr)
    _long_leg_facts(f, c)
    f["price_transport_rub"] = c.rub
    if c.out is not None and c.back is not None:
        f["days_at_destination"] = (c.back.dep.date() - c.out.arr.date()).days
    f["comfort_score"] = c.comfort
    if c.relaxed:
        f["relaxed"] = ",".join(c.relaxed)
    return {k: v for k, v in f.items() if v is not None}


def _note(code, text, **data):
    from .state import Note
    return Note(code=code, text=text, data={k: v for k, v in data.items()})


def _risks(c: Combo, facts, brief, rules, rr, steps):
    out = []
    late_window = rules.get("late_checkin", DEFAULT_LATE_CHECKIN)
    for direction, leg in (("out", c.out), ("back", c.back)):
        if leg is None:
            continue
        if leg.min_layover is not None and leg.min_layover < rr["tight_layover_min"]:
            out.append(_note("tight_layover", f"Пересадка {leg.min_layover} мин — впритык", layover_min=leg.min_layover,
                             direction=direction))
        when = "Вылет" if leg.kind == "flight" else "Отправление"
        if _night(leg.dep, rr):
            out.append(_note("night_departure", f"{when} ночью, в {leg.dep:%H:%M}", time=leg.dep.strftime("%H:%M"),
                             direction=direction))
        elif leg.dep.strftime("%H:%M") < rr["early_departure_before"]:
            out.append(_note("early_departure", f"Ранний {'вылет' if leg.kind == 'flight' else 'поезд'} — в {leg.dep:%H:%M}",
                             time=leg.dep.strftime("%H:%M"), direction=direction))
        if direction == "out" and _night(leg.arr, rr):
            out.append(_note("night_arrival", f"Прибытие ночью, в {leg.arr:%H:%M}", time=leg.arr.strftime("%H:%M")))

    nonref = [(d, l) for d, l in (("out", c.out), ("back", c.back)) if l is not None and l.kind == "flight" and l.refundable is False]
    if nonref:
        fixed = all(l.changeable is False for _, l in nonref)
        if len(nonref) == 2:
            text = "Невозвратные билеты" + (" без обмена" if fixed else "")
        else:
            text = ("Невозвратный билет" + (" без обмена" if fixed else "")
                    + (" туда" if nonref[0][0] == "out" else " обратно"))
        out.append(_note("non_refundable", text, directions=",".join(d for d, _ in nonref)))
    for sid, risk in connection_risks([s.to_check_dict() for s in steps], rules).items():
        if risk["risky"]:
            out.append(_note("tight_connection", f"Короткая стыковка: запас {risk['slack_min']} мин",
                             slack_min=risk["slack_min"], step=sid))
    if c.hotel is not None and not facts.get("free_cancellation"):
        pct, rub = facts.get("cancellation_penalty_pct"), facts.get("cancellation_penalty_rub")
        text = "Отель без бесплатной отмены" + (f": штраф {pct}% — {fmt_rub(rub)}" if pct and rub else
                                               (f": штраф {pct}%" if pct else ""))
        out.append(_note("no_free_cancel", text, penalty_pct=pct, penalty_rub=rub))
    for direction, tr in (("out", c.tr_out), ("back", c.tr_back)):
        if tr is not None and tr["duration_min"] > rr["long_transfer_min"]:
            out.append(_note("long_transfer", f"Дорога {'до отеля' if direction == 'out' else 'в аэропорт'} {tr['duration_min']} мин",
                             minutes=int(tr["duration_min"]), direction=direction))
    if c.hotel is not None and c.out is not None and c.tr_out is None:
        out.append(_note("no_transfer", "Трансфер не нашли — до отеля добираться самим"))
    wait = facts.get("wait_before_checkin_min")
    if wait and wait > rr["wait_before_checkin_min"]:
        out.append(_note("wait_before_checkin",
                         f"Приезд в {facts['arrival_at_hotel_time']}, а заселение с {facts['check_in_time']} — ждать {fmt_hours(wait / 60)} ч",
                         minutes=wait, arrival=facts["arrival_at_hotel_time"], check_in=facts["check_in_time"]))
    if c.hotel is not None and c.out is not None and c.arrival is not None and in_clock_window(c.arrival.strftime("%H:%M"), late_window):
        out.append(_note("late_checkin", f"Заселение поздно, в {c.arrival:%H:%M} — отель стоит предупредить",
                         time=c.arrival.strftime("%H:%M")))
    if c.unused_night and c.arrival is not None:
        out.append(_note("unused_first_night",
                         f"Приезд {c.arrival:%d.%m} в {c.arrival:%H:%M}, а заезд с {facts['check_in_date'][8:10]}."
                         f"{facts['check_in_date'][5:7]} — первая ночь оплачена, но не используется",
                         arrival=c.arrival.strftime("%H:%M"), check_in_date=facts["check_in_date"]))
    wait = facts.get("wait_after_checkout_min")
    if wait and wait > rr["wait_after_checkout_min"]:
        out.append(_note("wait_after_checkout", f"После выезда из отеля {fmt_hours(wait / 60)} ч до отправления",
                         minutes=wait))
    if wants_step_free(brief) and c.hotel is not None and step_free(c.hotel) is None:
        out.append(_note("accessibility_unknown", "Доступность для коляски в демо-данных не проверена"))
    # what the user asked for and did not get goes first: the list is capped
    prefs = brief.prefs.value
    head = []
    if _hotel_is_dorm(c.hotel):   # never hidden by the cap: a bed in a shared room, not a private room
        head.append(_note("dorm_bed", "Это койка в общем номере хостела, а не отдельный номер",
                          room=((c.hotel or {}).get("room") or {}).get("name")))
    fast = [p for p in c.relaxed if p.startswith(HIGH_SPEED + ".")]
    if fast:   # «на Сапсане» and no high-speed train that way on these dates
        name = "«Сапсана»" if "train:sapsan" in (brief.constraints.value or []) else "Скоростного поезда"
        way = "" if len(fast) > 1 else (" туда" if fast[0].endswith(".out") else " обратно")
        head.append(_note("relaxed_pref", f"{name}{way} на эти даты нет — показываем другие поезда", field=HIGH_SPEED,
                          directions=",".join(p.rsplit(".", 1)[1] for p in fast)))
    for path in c.relaxed:
        if path.startswith(HIGH_SPEED + "."):
            continue
        if path == PETS_CARRIAGE:
            head.append(_note("relaxed_pref", "Билетов, где можно ехать с животным, на эти даты не нашли — "
                                              "условия провоза уточните у перевозчика", field=path))
            continue
        if path == TICKETS_REFUNDABLE:
            head.append(_note("non_refundable_only", "Возвратных билетов на эти даты нет — в варианте невозвратные",
                              field="prefs.free_cancellation"))
            continue
        if path == CRIB:
            head.append(_note("relaxed_pref", "Отелей с гарантированной кроваткой не нашли — кроватка только по запросу",
                              field=path, infant_cot=_infant_cot(c.hotel or {})))
            continue
        if path == KITCHEN:
            head.append(_note("relaxed_pref", "С кухней в номере на эти даты ничего нет — показываю без неё", field=path))
            continue
        if path == ACCESSIBLE:
            head.append(_note("relaxed_pref", "Отелей без ступенек на эти даты нет — показываю без этого условия",
                              field=path))
            continue
        if path == "prefs.no_night_flights":
            legs = [l for l in (c.out, c.back) if l is not None and night_leg(l, rr)]
            if any(l.kind == "train" for l in legs):
                text = "Дневных поездов на эти даты нет — в варианте ночной поезд"
            elif legs:
                text = "Дневных рейсов на эти даты нет — в варианте ночной рейс"
            else:
                continue   # this variant's legs are all by day: nothing to say
            head.append(_note("relaxed_pref", text, field=path))
            continue
        if path == "prefs.arrive_before" and prefs is not None and prefs.arrive_before:
            text = f"Рейсов с прибытием до {prefs.arrive_before} не нашли — показываем ближайшие"
        elif path == "prefs.center_max_m":
            text = "Отелей так близко к центру на эти даты нет — показываем ближайшие"
        elif path == "prefs.near" and rr.get("near"):
            near = rr["near"]
            text = (f"В {_km_text(near.get('radius_m') or rr['near_max_m'])} от места «{near.get('name')}» отелей на "
                    f"эти даты нет — ближайший в {fmt_int(near.get('nearest_m'))} м, показываем ближайшие"
                    if near.get("nearest_m") is not None else
                    f"Отелей рядом с местом «{near.get('name')}» на эти даты нет — показываем ближайшие")
        else:
            text = f"Вариантов {RELAXED_RU.get(path, path)} не нашли — показываем ближайшие"
        head.append(_note("relaxed_pref", text, field=path))
    return (head + out)[: rr["max_notes"] + 2]


def _not_for(c: Combo, facts, brief, districts, rr):
    if c.hotel is None:
        return []
    out = _not_for_all(c, facts, brief, districts, rr)
    party = brief.party.value
    traits = set(brief.traits.value or [])
    drop = set()
    if party is not None and not party.children:
        drop |= {"no_playground", "no_children", "min_child_age"}   # no kids on this trip
    if party is not None and party.children:
        drop.add("quiet_no_nightlife")                              # quiet is a plus for a family
    if "nightlife" in traits:
        drop.add("noisy_evening")                                   # lively is what they asked for
    return [n for n in out if n.code not in drop]


def _not_for_all(c: Combo, facts, brief, districts, rr):
    out = []
    h = c.hotel
    profile = district_of(h, districts) or {}
    scores = profile.get("scores") or {}
    counts = profile.get("counts") or profile.get("poi_counts") or {}
    if scores.get("lively", 0) >= rr["lively_noisy_min"]:
        out.append(_note("noisy_evening", "Шумно вечером — не для тех, кто рано ложится", lively=scores["lively"]))
    if scores.get("quiet", 0) >= rr["quiet_min"] and scores.get("lively", 1) <= rr["quiet_lively_max"]:
        out.append(_note("quiet_no_nightlife", "Тихо и почти нет баров — не для тех, кто хочет вечерней движухи",
                         quiet=scores["quiet"]))
    if "playground" in counts and counts["playground"] == 0:
        out.append(_note("no_playground", "Нет детской площадки рядом — с маленькими детьми неудобно"))
    if scores and scores.get("walkable", 1) <= rr["not_walkable_max"]:
        out.append(_note("not_walkable", "До кафе и магазинов не пешком — без такси неудобно", walkable=scores["walkable"]))
    kids = h.get("kids_policy") or {}
    if kids.get("children_allowed") is False:
        out.append(_note("no_children", "Не для поездки с детьми: отель без детей"))
    elif kids.get("min_child_age"):
        out.append(_note("min_child_age", f"Детям младше {kids['min_child_age']} лет нельзя", age=kids["min_child_age"]))
    traits = set(brief.traits.value or [])
    d = h.get("distance_beach_m")
    if traits & SEA_TRAITS and d is not None and d > rr["beach_far_m"]:
        out.append(_note("beach_far", f"До пляжа {fmt_int(d)} м — не для тех, кто хочет жить у моря", distance_m=d))
    if h.get("kind") == "hostel":
        out.append(_note("hostel", "Хостел — не для тех, кому важны тишина и своё пространство"))
    return out[: rr["max_notes"]]


def _sacrifices(v, others, rr, brief):
    """Diffs of variant `v` (a facts dict + id) against the other selected variants."""
    out = []
    if not others:
        return out
    f = v["facts"]
    all_ = [v] + others
    min_rub = min(x["facts"]["total_rub"] for x in all_)
    if f["total_rub"] > min_rub:
        delta = f["total_rub"] - min_rub
        out.append(_note("price_more", f"+{fmt_rub(delta)} к самому дешёвому", delta_rub=delta))
    min_h = min(x["facts"]["door_to_door_hours"] for x in all_)
    if f["door_to_door_hours"] - min_h >= rr["fastest_min_gain_h"]:
        delta = round(f["door_to_door_hours"] - min_h, 1)
        out.append(_note("slower", f"+{fmt_hours(delta)} ч в пути", delta_hours=delta))
    stops = f.get("out_stops", 0) + f.get("back_stops", 0)
    if stops and any(x["facts"].get("out_stops", 0) + x["facts"].get("back_stops", 0) == 0 for x in others):
        out.append(_note("stops", "С пересадкой", stops=stops))
    if not f.get("free_cancellation") and any(x["facts"].get("free_cancellation") for x in others):
        out.append(_note("no_free_cancel", "Нет бесплатной отмены"))
    best_meal = max(MEALS_RANK.get(x["facts"].get("meals"), 0) for x in all_)
    mine = MEALS_RANK.get(f.get("meals"), 0)
    if mine < best_meal:
        text = "Без питания" if mine == 0 else f"Только {MEALS_RU[f['meals']]}"
        out.append(_note("meals_less", text, meals=f.get("meals")))
    if ("out_baggage" in f and not f.get("out_baggage")) and any(x["facts"].get("out_baggage") for x in others):
        out.append(_note("no_baggage", "Без багажа в тарифе"))
    stars = [x["facts"].get("hotel_stars") or 0 for x in all_]
    if (f.get("hotel_stars") or 0) < max(stars) and f.get("hotel_stars"):
        out.append(_note("fewer_stars", f"Отель попроще: {f['hotel_stars']}★", stars=f["hotel_stars"]))
    traits = set(brief.traits.value or [])
    beaches = [x["facts"].get("distance_beach_m") for x in all_ if x["facts"].get("distance_beach_m") is not None]
    d = f.get("distance_beach_m")
    if traits & SEA_TRAITS and d is not None and beaches and d - min(beaches) >= rr["sacrifice_beach_delta_m"]:
        out.append(_note("beach_farther", f"До пляжа {fmt_int(d)} м", distance_m=d))
    centers = [x["facts"].get("distance_center_m") for x in all_ if x["facts"].get("center_fit") is not None
               and x["facts"].get("distance_center_m") is not None]
    dc = f.get("distance_center_m")
    if f.get("center_fit") is not None and dc is not None and centers and \
            dc - min(centers) >= rr.get("sacrifice_center_delta_m", 700):
        out.append(_note("center_farther", f"До центра {fmt_int(dc)} м", distance_m=dc))
    nears = [x["facts"].get("distance_near_m") for x in all_ if x["facts"].get("distance_near_m") is not None]
    dn = f.get("distance_near_m")
    if dn is not None and nears and dn - min(nears) >= rr.get("sacrifice_near_delta_m", 400):
        # the place the user wants to stay near comes first among the sacrifices (never cut by max_notes)
        out.insert(0, _note("near_farther", f"До места «{f.get('near_name')}» {fmt_int(dn)} м", distance_m=dn))
    ratings = [x["facts"].get("hotel_rating") for x in all_ if x["facts"].get("hotel_rating") is not None]
    r = f.get("hotel_rating")
    if r is not None and ratings and max(ratings) - r >= rr["sacrifice_rating_delta"]:
        out.append(_note("lower_rating", f"Рейтинг {fmt_hours(r)} — ниже остальных", rating=r))
    return out[: rr["max_notes"]]


# ---------------------------------------------------------------- fit

def fit(brief, variant, rules, segments=None):
    """Fit of a variant to the brief, 0..1, with a per-criterion breakdown (each 0..1).

    Criteria present only when they can be computed: budget (vs budget max, else vs the cheapest shown:
    facts.ref_min_rub), time (vs prefs.max_travel_hours, else vs the fastest shown), district (OSM
    scores weighted by the user's district pref or the segment weights of segments.json), meals,
    cancellation, stars, beach, constraints (party/pet policies), rating, center (facts.center_fit: the hotel's
    distance to the city centre, only when ranking.center_applies). Weights: rules.ranking.fit_weights.
    """
    from .state import Fit
    rr = ranking_rules(rules)
    segs = _segments(rules, segments)
    f = variant.facts
    prefs = brief.prefs.value
    b: dict[str, float] = {}
    rub = variant.totals.rub
    budget = brief.budget_rub.value
    if budget is not None and budget.max:
        r = rub / budget.max
        b["budget"] = 1.0 if r <= 1 else max(0.0, 1 - (r - 1) / (rr["budget_zero_over_pct"] / 100))
    elif f.get("ref_min_rub") and rub:
        b["budget"] = min(1.0, f["ref_min_rub"] / rub)
    hours = variant.totals.door_to_door_hours
    if hours > 0:
        one_way = max(f.get("out_door_hours", 0), f.get("back_door_hours", 0)) or hours / 2
        if prefs is not None and prefs.max_travel_hours:
            mx = prefs.max_travel_hours
            b["time"] = 1.0 if one_way <= mx else max(0.0, 1 - (one_way - mx) / mx)
        elif f.get("ref_min_hours"):
            b["time"] = min(1.0, f["ref_min_hours"] / hours)
    scores = {k: f[f"district_{k}"] for k in ("lively", "quiet", "family", "walkable") if f"district_{k}" in f}
    if scores:
        d = district_fit(brief, {"scores": scores}, segs)
        if d is not None:
            b["district"] = d
    meals = f.get("meals")
    if prefs is not None and prefs.meals != "any" and meals is not None:
        diff = MEALS_RANK[prefs.meals] - MEALS_RANK.get(meals, 0)
        b["meals"] = 1.0 if diff <= 0 else (0.5 if diff == 1 else 0.0)
    if "free_cancellation" in f:
        free = bool(f["free_cancellation"])
        if prefs is not None and prefs.free_cancellation:
            b["cancellation"] = 1.0 if free else 0.0
        elif prefs is None or prefs.free_cancellation is None:
            b["cancellation"] = 1.0 if free else 0.5
    stars = f.get("hotel_stars")
    if stars is not None:
        if prefs is not None and prefs.stars_min:
            b["stars"] = 1.0 if stars >= prefs.stars_min else round(stars / prefs.stars_min, 3)
        else:
            b["stars"] = round(stars / 5, 3)
    dist = f.get("distance_beach_m")
    sea_wanted = bool(set(brief.traits.value or []) & SEA_TRAITS)
    if prefs is not None and prefs.beach_max_m is not None:
        mx = prefs.beach_max_m
        b["beach"] = 0.0 if dist is None else (1.0 if dist <= mx else max(0.0, 1 - (dist - mx) / max(mx, 500)))
    elif sea_wanted:
        near, zero = rr["beach_near_m"], rr["beach_zero_m"]
        b["beach"] = 0.0 if dist is None else (1.0 if dist <= near else max(0.0, 1 - (dist - near) / (zero - near)))
    if f.get("near_fit") is not None:     # a named place (prefs.near): replaces the centre
        b["near"] = float(f["near_fit"])
    elif f.get("center_fit") is not None:   # set by _facts only when the centre counts for this trip
        b["center"] = float(f["center_fit"])
    party = brief.party.value
    if brief.constraints.value or wants_pet(brief) or (party and party.children):
        b["constraints"] = 0.0 if any(i["severity"] == "block" for i in check_party(variant, brief)) else 1.0
    if f.get("hotel_rating") is not None:
        b["rating"] = round(min(1.0, f["hotel_rating"] / 10), 3)
    b = {k: round(max(0.0, min(1.0, v)), 3) for k, v in b.items()}
    w = rr["fit_weights"]
    total = sum(w.get(k, 0) for k in b)
    score = sum(w.get(k, 0) * v for k, v in b.items()) / total if total else 0.0
    if f.get("dorm_bed") and not wants_dorm(brief):
        # a bed in a shared room is never the code's pick over a private room (only «Дешевле» may be one)
        b["room"] = 0.0
        score *= rr["dorm_fit_factor"]
    penalty = arrival_penalty(brief, variant, rules, rr)
    if penalty:
        b["arrival_time"] = 0.0
        score *= 1 - penalty
    return Fit(score=round(max(0.0, min(1.0, score)), 3), breakdown=b)


def recommended_hint(variants):
    """The code's pick: highest fit (ties: cheapest, fastest, comfort order). None for no variants."""
    if not variants:
        return None
    private = [v for v in variants if not (v.facts or {}).get("dorm_bed")]
    variants = private or list(variants)   # a bed in a shared room is never the pick when a room exists
    best = max(enumerate(variants), key=lambda iv: ((iv[1].fit.score if iv[1].fit else 0), -iv[0]))
    return best[1].id


# ---------------------------------------------------------------- build

def build_variants(brief, results: dict, districts: dict, rules: dict, today, segments=None):
    """Combine transport, hotels and transfers into cheapest / fastest / comfort variants.

    - Door-to-door when the scope has transport and offers exist; stay-only (scope D, or no transport
      offers: risk note `no_transport`) otherwise: then cheapest / comfort / custom-1 (best location).
    - Hard filters: dates before `today`, `no_night_flights`, user-set direct_only / transport,
      party fit (room capacity, children policy, pets). User-set stars/meals/cancellation/beach and
      max_travel_hours filter too, but are relaxed (risk note `relaxed_pref`) when nothing is left.
    - A variant that coincides with another is not duplicated: facts `also_fastest` / `also_comfort`
      and `coincide_note` on the variant that won both.
    - totals.door_to_door_hours = sum of the non-stay step durations both ways (trip.py travel_hours:
      flights/trains + planned transfers; airport buffers and waits are not counted, they are checked).
    - Transfers are timed by code: pickup = arrival + after-flight buffer (75 min with checked baggage,
      45 without; trains rules.ranking.after_train_min), length = duration x taxi_time_factor rounded up
      to 5 min; the return transfer ends at departure - before-flight/train buffer. The stay step runs
      from max(check-in, arrival at the hotel) to min(check-out, leaving the hotel).
    - Tickets only (scope without "stay": flights and/or trains): out + back pairs, no hotel, no transfers;
      cheapest / fastest / comfort (fare tier, baggage, car class) among them; facts `transport_only`.
    - Ticket prefs (user-set): prefs.arrive_before (outbound arrival, local clock at the destination),
      prefs.no_night_flights (no night leg: a flight or train leaving or arriving in ranking.night_window, or a
      train on the move at ranking.night_move_at 03:00 — night_leg; facts out_night / back_night), and refundable fares
      under prefs.free_cancellation. Each is relaxed per direction when nothing passes: risk
      `relaxed_pref` (or `non_refundable_only` for tickets).
    - Dates never mix: an outbound reaching the hotel on a day before the check-in date (a night without
      a bed) or a return leaving on a day after the check-out date is never paired (an overnight leg
      arriving on the check-in morning is fine); arriving after midnight (first night paid, not used)
      is allowed with risk `unused_first_night` and loses to an equal variant (cheapest tie-break,
      comfort minus ranking.unused_night_penalty). results["date_shift"] (steps.run_search moved the
      whole trip inside flexible_days) -> facts.dates_shifted_days + risk `dates_shifted`.
    - prefs.max_travel_hours is ONE-WAY: each direction's door-to-door hours (totals.out_hours /
      back_hours) must fit; when none fits, the journeys within ranking.max_hours_relax_tolerance_h of
      the best one-way time are kept with `relaxed_pref`.
    - Rooms: men-/women-only rooms are never offered (is_gendered_room; the gender is never known);
      shared dorms never for couples/families. Dorm beds (unless the user asked for a hostel, constraint
      `hostel`) never crowd out private rooms (ranking.max_dorm_hotels), only «Дешевле» may be one (risk
      `dorm_bed`, facts.dorm_bed, fit x ranking.dorm_fit_factor); constraint `no_dorm` drops them, constraint
      `no_hostel` drops every hotel of kind hostel and every dorm bed.
      Constraint `crib`: hotels with a guaranteed cot
      (kids_policy.infant_cot free/paid; free scores higher in comfort) — a relaxable hotel pref like
      stars/meals/cancellation/beach; when no hotel meets them all, the fewest prefs are dropped
      (RELAX_ORDER, the cot last) with a `relaxed_pref` note each.
    Returns [] when there are no hotel offers for a scope with "stay", or no out + back pair for a
    tickets-only scope (honest "no data"; the engine says so).
    """
    from .state import Brief, DistrictProfile, Totals, Variant, VariantHotel
    brief = brief if isinstance(brief, Brief) else Brief.model_validate(brief)
    today = today.date() if isinstance(today, datetime) else (date.fromisoformat(today) if isinstance(today, str) else today)
    rr = ranking_rules(rules)
    segs = _segments(rules, segments)
    factor = rules.get("taxi_time_factor", DEFAULT_TAXI_FACTOR)
    districts = districts or {}
    results = results or {}
    scope = _scope(brief)
    dest = results.get("destination") or {}
    shift = results.get("date_shift") or None   # flexible_days: the whole trip moved (steps.run_search)
    dest_name = dest.get("name") or (brief.destination.value.name if brief.destination.value else None)

    want_stay = "stay" in scope
    hotels_all = [_dump(h) for h in _offers(results.get("hotels"))] if want_stay else []
    hotels_all = [h for h in hotels_all if _party_ok(h, brief)]
    if want_stay and not hotels_all:
        return []
    near = dict(results.get("near")) if want_stay and results.get("near") else None
    if near:   # prefs.near resolved by steps: every hotel gets its distance to that place
        near.setdefault("radius_m", rr["near_max_m"])
        hotels_all = [dict(h, distance_near_m=near_distance_m(h, near)) for h in hotels_all]
        ds = [h["distance_near_m"] for h in hotels_all if h.get("distance_near_m") is not None]
        near["nearest_m"] = min(ds) if ds else None
    rr["near"] = near
    rr["center_on"] = want_stay and (bool(near) or center_applies(brief, dest, rr))
    hotels, relaxed_paths = _relax_hotels(hotels_all, brief, near)
    if "prefs.center_max_m" in relaxed_paths:   # none so close to the centre: the nearest ones, said so
        hotels = _nearest_hotels(hotels, rr) or hotels
    if "prefs.near" in relaxed_paths:           # none within the radius of the place: the nearest ones
        hotels = _nearest_hotels(hotels, rr, "distance_near_m", "near_relax_tolerance_m") or hotels
    hotels = _prune_hotels(hotels, rr["max_hotels"], brief, districts, segs, rr["max_dorm_hotels"], rr=rr)

    want_transport = bool(scope & {"flights", "trains"})
    if not (want_stay or want_transport):
        return []
    tr_out_all = results.get("transfers_out") or {}
    tr_back_all = results.get("transfers_back") or {}
    hotel_relaxed = relaxed_paths
    # second pass without the ticket prefs when the legs they keep fit no stay / no return at all
    for soft in (True, False):
        outs, out_relaxed = _legs(results, "out", brief, rr, today, soft, _buffers(rules)) if want_transport else ([], ())
        backs, back_relaxed = _legs(results, "back", brief, rr, today, soft, _buffers(rules)) if want_transport else ([], ())
        no_transport = want_transport and not (outs and backs)
        if not want_stay and no_transport:
            return []
        door = want_transport and not no_transport
        relaxed_paths = hotel_relaxed
        if door:
            relaxed_paths = tuple(sorted(set(hotel_relaxed) | set(out_relaxed) | set(back_relaxed)))
        combos: list[Combo] = []
        if not want_stay:   # tickets only
            for o in outs:
                for bk in backs:
                    c = Combo(o, bk, None, None, None, relaxed=relaxed_paths)
                    if _evaluate_transport(c, rules, rr):
                        combos.append(c)
        for h in hotels:
            if not door:
                c = Combo(None, None, h, None, None, relaxed=relaxed_paths)
                if _evaluate(c, rules, rr, factor):
                    combos.append(c)
                continue
            hid = h["hotel_id"]
            for o in outs:
                t_outs = _pick_transfers([_dump(t) for t in _offers(tr_out_all.get(hid))], o.dest) if "transfers" in scope else [None]
                for bk in backs:
                    t_backs = _pick_transfers([_dump(t) for t in _offers(tr_back_all.get(hid))], bk.origin) if "transfers" in scope else [None]
                    for to in t_outs:
                        for tb in t_backs:
                            c = Combo(o, bk, h, to, tb, relaxed=relaxed_paths)
                            if _evaluate(c, rules, rr, factor):
                                combos.append(c)
        if combos or not door or not (_soft_leg_filters(brief, "out", rr) or _soft_leg_filters(brief, "back", rr)):
            break
    if door:
        timed = [c for c in combos if _max_hours_ok(c, brief)]
        if timed:
            combos = timed
        elif combos:   # nothing within the one-way limit: the journeys nearest to it, said so
            best = min(_one_way_hours(c) for c in combos)
            combos = [c for c in combos if _one_way_hours(c) <= best + rr["max_hours_relax_tolerance_h"]]
            for c in combos:
                c.relaxed = tuple(sorted(set(c.relaxed) | {"prefs.max_travel_hours"}))
    if not combos:
        return []
    for c in combos:
        c.comfort = _comfort(c, brief, districts, segs, rr)
        if c.unused_night:   # loses to an equal variant that uses every booked night
            c.comfort = round(c.comfort - rr["unused_night_penalty"], 4)

    picked: list[tuple[str, Combo]] = []
    notes: dict[str, list[str]] = {}
    cheapest = min(combos, key=lambda c: (c.rub, c.unused_night, c.hours, -c.comfort))
    picked.append(("cheapest", cheapest))
    # only «Дешевле» may be a dorm bed (unless the user asked for a hostel): the rest come from private rooms
    private = _private_first(combos, brief)
    dorm_cheapest = _hotel_is_dorm(cheapest.hotel) and not _hotel_is_dorm(private[0].hotel)
    if dorm_cheapest:   # the cheapest private room right next to the dorm bed
        picked.append(("custom-2", min(private, key=lambda c: (c.rub, c.unused_night, c.hours, -c.comfort))))
    low_id, ref = picked[-1]   # the cheapest variant with a private room (or «Дешевле» itself)
    if door:
        min_h = min(c.hours for c in private)
        tolerance = 0 if short_break(brief) else rr["fastest_hours_tolerance"]
        near = [c for c in private if c.hours <= min_h + tolerance]
        base = min(c.rub for c in near) or 1
        fastest = max(near, key=lambda c: (round(c.comfort - rr["fastest_price_weight"] * (c.rub / base - 1), 6),
                                           -c.hours, -c.rub))
        if fastest.key != ref.key and ref.hours - fastest.hours >= rr["fastest_min_gain_h"]:
            picked.append(("fastest", fastest))
        else:
            notes.setdefault(low_id, []).append("fastest")
    taken = {c.key for _, c in picked}
    budget = brief.budget_rub.value.max if brief.budget_rub.value else None
    cap = budget if budget else ref.rub * rr["comfort_price_cap_ratio"]
    pool = [c for c in private if c.key not in taken and c.rub <= max(cap, ref.rub)]
    if door:   # comfort is about the stay, not about a long detour: keep its journey near the fastest one
        min_hours = min(c.hours for c in combos)
        near = [c for c in pool if c.hours <= min_hours + rr["comfort_max_extra_hours"]]
        pool = near or pool
    rooms = [c for _, c in picked if not (dorm_cheapest and _hotel_is_dorm(c.hotel))]
    best_other = max((c.comfort for c in rooms), default=None)
    comfort = max(pool, key=lambda c: (c.comfort, -c.rub)) if pool else None
    if comfort is not None and (best_other is None or comfort.comfort >= best_other + rr["comfort_min_gain"]):
        picked.append(("comfort", comfort))
    else:
        winner = max([pc for pc in picked if any(pc[1] is r for r in rooms)] or picked,
                     key=lambda pc: pc[1].comfort)[0]
        notes.setdefault(winner, []).append("comfort")
    if not door and want_stay:
        taken = {c.key for _, c in picked}
        rest = [c for c in private if c.key not in taken and c.rub <= max(cap, ref.rub)]
        def loc(c):
            d = district_fit(brief, district_of(c.hotel, districts), segs)
            beach = c.hotel.get("distance_beach_m")
            near = 0.0 if beach is None or not (set(brief.traits.value or []) & SEA_TRAITS) else max(0.0, 1 - beach / rr["beach_zero_m"])
            return (d or 0) + near + _center_of(c.hotel, brief, rr)
        if rest:
            best_loc = max(rest, key=lambda c: (loc(c), -c.rub))
            if loc(best_loc) > max(loc(c) for _, c in picked) + 0.05:
                picked.append(("custom-1", best_loc))
    limit = rules.get("max_variants", 3)
    if door and rr.get("center_on"):
        _center_card(picked, private, ref, brief, rr, limit)
    if dorm_cheapest and len(picked) > limit:   # the private-room cards go first; the fastest may give way
        picked = [pc for pc in picked if pc[0] != "fastest"] or picked
    add_weekend_choice(picked, private, brief, rr, limit)
    picked = picked[:limit]

    # assemble
    drafts = []
    for vid, c in picked:
        facts = _facts(c, brief, districts, segs, rr)
        if no_transport:
            facts["no_transport"] = True
        drafts.append({"id": vid, "combo": c, "facts": facts})
    min_rub = min(d["facts"]["total_rub"] for d in drafts)
    min_h = min(d["facts"]["door_to_door_hours"] for d in drafts)
    variants = []
    for d in drafts:
        c, f, vid = d["combo"], d["facts"], d["id"]
        f["ref_min_rub"] = min_rub
        f["delta_rub_vs_cheapest"] = f["total_rub"] - min_rub
        if door:
            f["ref_min_hours"] = min_h
            f["delta_hours_vs_fastest"] = round(f["door_to_door_hours"] - min_h, 1)
        also = notes.get(vid, [])
        if also:
            for a in also:
                f[f"also_{a}"] = True
            f["coincide_note"] = _coincide_text(vid, also)
        steps = _steps_for(c, brief, rules, today, dest_name)
        hotel = None
        if c.hotel is not None:
            profile = district_of(c.hotel, districts)
            dp = DistrictProfile(labels=district_labels(profile, rr),
                                 scores={k: round(float(v), 3) for k, v in ((profile or {}).get("scores") or {}).items()})
            hotel = VariantHotel(hotel_id=c.hotel["hotel_id"], offer_id=c.hotel["offer_id"], name=c.hotel["name"],
                                 stars=c.hotel.get("stars"), rating=c.hotel.get("rating"), district=dp,
                                 lat=c.hotel.get("lat"), lon=c.hotel.get("lon"))
        risks = _risks(c, f, brief, rules, rr, steps)
        if shift and shift.get("days"):
            f["dates_shifted_days"] = int(shift["days"])
            f["requested_check_in_date"] = shift.get("requested_check_in")
            risks.insert(0, _shift_note(shift, f))
        if no_transport:
            from .state import Note
            risks.insert(0, Note(code="no_transport", text="Билеты не нашлись — показываем только проживание"))
        v = Variant(id=vid, title=TITLES.get(vid, vid), destination_id=dest.get("id") or (brief.destination.value.id if brief.destination.value else None),
                    steps=steps, totals=Totals(rub=c.rub, door_to_door_hours=c.hours,
                                               out_hours=round(c.out_hours, 1) if c.out is not None else None,
                                               back_hours=round(c.back_hours, 1) if c.back is not None else None),
                    hotel=hotel, facts=f,
                    risks=risks, not_for=_not_for(c, f, brief, districts, rr))
        variants.append(v)
    return refresh_variant_comparison(variants, brief, rules, segs)


def refresh_variant_comparison(variants, brief, rules, segments=None, original_ids=None):
    """Refresh comparisons after a timed quote changed a variant's price or journey."""
    if not variants:
        return variants
    if original_ids is not None:
        winners = {}
        for label, metric in (("cheapest", "rub"), ("fastest", "door_to_door_hours")):
            if label not in original_ids:
                continue
            named = next((v for v in variants if v.id == label), None)
            best = min(variants, key=lambda v: getattr(v.totals, metric))
            winners[label] = named if named is not None and getattr(named.totals, metric) == getattr(
                best.totals, metric) else best
        variants = [v for v in variants if v.id not in winners or any(v is winner for winner in winners.values())]
        for label, winner in winners.items():
            if label == "fastest" and winner is winners.get("cheapest"):
                winner.facts["also_fastest"] = True
            else:
                winner.id, winner.title = label, TITLES[label]
    rr = ranking_rules(rules)
    segs = _segments(rules, segments)
    if original_ids is not None:
        variants = quoted_weekend_choices(variants, brief, rr)
        if not variants:
            return variants
    min_rub = min(v.totals.rub for v in variants)
    min_hours = min(v.totals.door_to_door_hours for v in variants)
    for v in variants:
        v.facts.update(total_rub=v.totals.rub, ref_min_rub=min_rub,
                       delta_rub_vs_cheapest=v.totals.rub - min_rub)
        if original_ids is not None:
            if v.facts.get("also_fastest") and v.totals.door_to_door_hours > min_hours:
                v.facts.pop("also_fastest")
            also = [label for label in ("fastest", "comfort") if v.facts.get(f"also_{label}")]
            if also:
                v.facts["coincide_note"] = _coincide_text(v.id, also)
            else:
                v.facts.pop("coincide_note", None)
        if "ref_min_hours" in v.facts:
            v.facts.update(ref_min_hours=min_hours,
                           delta_hours_vs_fastest=round(v.totals.door_to_door_hours - min_hours, 1))
    for v in variants:
        others = [{"id": o.id, "facts": o.facts} for o in variants if o.id != v.id]
        v.sacrifices = _sacrifices({"id": v.id, "facts": v.facts}, others, rr, brief)
        v.fit = fit(brief, v, rules, segs)
    return variants


def _shift_note(shift, facts):
    """The trip was moved inside the user's flexible days (check-in, check-out and both legs together)."""
    def dm(iso):
        return f"{iso[8:10]}.{iso[5:7]}" if iso else ""
    why = {"no_options": "на исходные даты собрать поездку не получилось",
           "budget": "так укладываемся в бюджет", "cheaper": "так дешевле",
           "other_dates": "вы просили другие числа"}.get(shift.get("reason"), "так заметно дешевле")
    start = facts.get("check_in_date") or facts.get("out_date")
    head = ("Сдвинул даты" if shift.get("auto")    # moved by the engine (rescue / budget), not the user's ± days
            else f"Даты сдвинуты в пределах ±{shift.get('flex_days')} дн.")
    return _note("dates_shifted", f"{head}: заезд {dm(start)} "
                                  f"вместо {dm(shift.get('requested_check_in'))} — {why}",
                 days=int(shift["days"]), requested=shift.get("requested_check_in"), reason=shift.get("reason"))


def _coincide_text(vid, also):
    names = {"fastest": "самым быстрым", "comfort": "самым комфортным"}
    base = {"cheapest": "Самый дешёвый вариант", "fastest": "Самый быстрый вариант", "comfort": "Комфортный вариант",
            "custom-1": "Этот вариант", "custom-2": "Этот вариант"}.get(vid, "Этот вариант")
    return f"{base} оказался и {' и '.join(names[a] for a in also)} — отдельной карточки нет."


# ---------------------------------------------------------------- sensitivity

def _identity(item):
    if hasattr(item, "steps"):
        ids = tuple(sorted(s.offer_id or s.id for s in item.steps))
        return ids or (item.id,)
    if isinstance(item, dict):
        return (item.get("id") or item.get("destination_id"),)
    return (getattr(item, "id", None),)


def _total(item):
    if hasattr(item, "totals"):
        return item.totals.rub
    if isinstance(item, dict):
        for k in ("total_rub", "price_from_rub", "rub"):
            if item.get(k) is not None:
                return item[k]
        t = item.get("totals") or {}
        return t.get("rub")
    return None


def top_of(items):
    """The item a reply leads with: highest fit for variants, the first item otherwise."""
    if not items:
        return None
    if hasattr(items[0], "fit"):
        return max(enumerate(items), key=lambda iv: ((iv[1].fit.score if iv[1].fit else 0), -iv[0]))[1]
    return items[0]


def _recommended_value(brief, path, spec):
    rec = spec.get("recommend") or {}
    src = brief.source_of(path)
    top, _, sub = path.partition(".")
    current = (brief.values().get(top) or {}).get(sub) if sub else brief.values().get(top)
    if top == "origin" and isinstance(brief.values().get("origin"), dict):
        current = brief.values()["origin"].get("city")
    if src in (rec.get("from") or []) and current is not None:
        return current
    return rec.get("default")


def _current_value(brief, path):
    top, _, sub = path.partition(".")
    value = brief.values().get(top)
    if top == "origin":
        return (value or {}).get("city")
    if top == "destination":
        return (value or {}).get("name")
    if sub:
        return (value or {}).get(sub) if isinstance(value, dict) else None
    return value


def _askable(brief, path):
    """Assumed (source != user) and relevant. Children ages only matter when there are children."""
    top = path.partition(".")[0]
    try:
        if brief.source_of(path) == "user":
            return False
    except KeyError:
        return False
    if path == "party.children_ages":
        party = brief.party.value
        return bool(party and party.children and not party.children_ages)
    if top == "party" and brief.source_of("party.segment") in ("profile", "inferred"):
        return False   # saved travellers or the user's own words (a business trip): a chip, not a question
    if top == "destination":
        return False
    return True


def sensitivity(brief, variants, rerun: Callable, skill):
    """Questions worth asking (≤ rules.max_questions), most impactful first.

    For every assumed brief field that questions.json can ask, rerun with each alternative choice
    value; the field matters when the top item changes (rules.sensitivity.top_changes) or the top
    total moves more than rules.sensitivity.price_delta_pct. `variants` may be Variants (top = best
    fit) or destination items (dicts with id + total_rub/price_from_rub; top = first). `rerun(brief)`
    returns the same kind of list; exceptions skip that alternative. `skill`: SkillPackage or a dict
    {"questions": ..., "rules": ...}. At most rules.sensitivity.max_reruns reruns (default 12).
    """
    from .state import Choice, PatchError, Question, apply_patch
    questions = getattr(skill, "questions", None) if not isinstance(skill, dict) else skill.get("questions")
    rules = getattr(skill, "rules", None) if not isinstance(skill, dict) else skill.get("rules")
    questions, rules = questions or {}, rules or {}
    sens = {**SENSITIVITY_DEFAULTS, **(rules.get("sensitivity") or {})}
    limit = rules.get("max_questions", 2)
    if not variants or limit <= 0:
        return []
    base_top = top_of(variants)
    base_id, base_total = _identity(base_top), _total(base_top)
    reruns = 0
    found = []
    for path, spec in questions.items():
        if not _askable(brief, path):
            continue
        current = _current_value(brief, path)
        impact = 0.0
        why = None
        for choice in spec.get("choices", []):
            value = choice.get("value")
            if value == current or reruns >= sens["max_reruns"]:
                continue
            try:
                patched, _ = apply_patch(brief, {"set": {path: value}} if value is not None else {"unset": [path]})
            except PatchError:
                continue
            reruns += 1
            try:
                alt = rerun(patched)
            except Exception:
                continue
            alt_top = top_of(alt or [])
            if alt_top is None:
                score, reason = 1.0, f"{path}={value!r}: no result"
            else:
                changed = _identity(alt_top) != base_id
                total = _total(alt_top)
                pct = abs(total - base_total) / base_total * 100 if base_total and total is not None else 0.0
                score = 0.0
                if changed and sens.get("top_changes", True):
                    score = 1.0 + pct / 100
                elif pct > sens["price_delta_pct"]:
                    score = pct / 100
                reason = f"{path}={value!r}: top {'changes' if changed else 'same'}, total {pct:.0f}%"
            if score > impact:
                impact, why = score, reason
        if impact > 0:
            rec = _recommended_value(brief, path, spec)
            choices = [Choice(label=c["label"], value=c.get("value"), recommended=(rec is not None and c.get("value") == rec))
                       for c in spec.get("choices", [])]
            found.append((impact, Question(id=spec["id"], field=path, text=spec["text"], choices=choices, reason=why)))
    found.sort(key=lambda iq: -iq[0])
    return [q for _, q in found[:limit]]
