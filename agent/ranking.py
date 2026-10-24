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
                     fmt_hours, fmt_int, fmt_rub, in_clock_window, nights_text, ts, wants_pet)

# ---------------------------------------------------------------- tunables

RANKING_DEFAULTS = {
    "max_legs": 6,                    # transport offers kept per direction (cheapest + fastest union)
    "max_hotels": 15,                 # hotel offers kept (cheapest + most comfortable union)
    "fastest_min_gain_h": 0.25,       # a separate "fastest" must save at least this vs the cheapest
    "comfort_min_gain": 0.03,         # a separate "comfort" must beat the others' comfort score by this
    "comfort_price_cap_ratio": 2.0,   # without a budget, comfort costs at most this x the cheapest
    "after_train_min": 10,            # leaving the platform after a train
    "night_window": ["23:00", "06:00"],   # night departure / arrival (local clock)
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
                    "stars": 0.08, "beach": 0.1, "constraints": 0.05, "rating": 0.05},
    "comfort_weights": {"stars": 0.2, "rating": 0.25, "meals": 0.15, "cancellation": 0.1, "direct": 0.1,
                        "fare": 0.05, "transfer": 0.05, "district": 0.1},
}
SENSITIVITY_DEFAULTS = {"price_delta_pct": 10, "top_changes": True, "max_reruns": 12}

MEALS_RANK = {"none": 0, "breakfast": 1, "half_board": 2, "full_board": 3, "all_inclusive": 4}
MEALS_RU = {"none": "без питания", "breakfast": "завтраки", "half_board": "полупансион",
            "full_board": "полный пансион", "all_inclusive": "всё включено", "any": "любое"}
TITLES = {"cheapest": "Дешевле", "fastest": "Быстрее", "comfort": "Комфортнее", "custom-1": "Удачнее по району"}
SEA_TRAITS = {"sea", "beach", "island"}
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
                   min(lays) if lays else None, o.get("flight_no"))

    @classmethod
    def train(cls, o):
        return cls("train", o, o["offer_id"], ts(o["dep"]), ts(o["arr"]), o["from_station"], o["to_station"],
                   int(o["duration_min"]), 0, int(o["price_rub"]), o.get("carrier"), False, True, None, None,
                   None, o.get("train_no"))


@dataclass
class Combo:
    out: Optional[Leg]
    back: Optional[Leg]
    hotel: dict
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
    if leg.kind == "flight" and "no_night_flights" in constraints and (_night(leg.dep, rr) or _night(leg.arr, rr)):
        return False
    if prefs is not None:
        if leg.kind == "flight" and prefs.direct_only and _user(brief, "prefs.direct_only") and leg.stops:
            return False
        if _user(brief, "prefs.transport") and prefs.transport in ("plane", "train"):
            if (prefs.transport == "plane") != (leg.kind == "flight"):
                return False
    return True


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
    return True


def _free_cancel(h) -> bool:
    c = h.get("cancellation") or {}
    return bool(c.get("free_until")) or c.get("penalty_pct", 100) == 0


def _pref_fails(h, brief) -> list[str]:
    """User-given prefs this hotel does not meet (hard filters, relaxed only when nothing is left)."""
    prefs = brief.prefs.value
    if prefs is None:
        return []
    bad = []
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
    return bad


RELAXED_RU = {"prefs.stars_min": "с нужной звёздностью", "prefs.meals": "с нужным питанием",
              "prefs.free_cancellation": "с бесплатной отменой", "prefs.beach_max_m": "так близко к пляжу",
              "prefs.max_travel_hours": "с дорогой в нужное время"}


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
        if (check_in - arrival) > timedelta(hours=24):
            return False  # arrives a day before the stay
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


def _comfort(c: Combo, brief, districts, segments, rr) -> float:
    w = rr["comfort_weights"]
    h = c.hotel
    legs = [x for x in (c.out, c.back) if x is not None]
    parts = {
        "stars": (h.get("stars") or 0) / 5,
        "rating": (h.get("rating") or 0) / 10,
        "meals": MEALS_RANK.get(h.get("meals"), 0) / 4,
        "cancellation": 1.0 if _free_cancel(h) else 0.0,
    }
    if legs:
        parts["direct"] = 1.0 if all(l.stops == 0 for l in legs) else 0.0
        parts["fare"] = sum((1.0 if l.baggage else 0) * 0.5 + (0.5 if l.refundable else 0) for l in legs) / len(legs)
        trs = [t for t in (c.tr_out, c.tr_back)]
        parts["transfer"] = sum(COMFORT_TRANSFER.get(t.get("kind"), 0) if t else 0 for t in trs) / len(trs)
    d = district_fit(brief, district_of(h, districts), segments)
    if d is not None:
        parts["district"] = d
    total = sum(w.get(k, 0) for k in parts) or 1.0
    return round(sum(w.get(k, 0) * v for k, v in parts.items()) / total, 4)


def _prune_legs(legs, n):
    if len(legs) <= n:
        return legs
    keep = {}
    for leg in sorted(legs, key=lambda l: (l.price_rub, l.duration_min))[: (n + 1) // 2]:
        keep[leg.offer_id] = leg
    for leg in sorted(legs, key=lambda l: (l.duration_min, l.price_rub)):
        if len(keep) >= n:
            break
        keep.setdefault(leg.offer_id, leg)
    return list(keep.values())


def _prune_hotels(hotels, n, brief, districts, segments):
    if len(hotels) <= n:
        return hotels
    def quality(h):
        d = district_fit(brief, district_of(h, districts), segments) or 0
        return (h.get("stars") or 0) / 5 + (h.get("rating") or 0) / 10 + MEALS_RANK.get(h.get("meals"), 0) / 8 + d
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


def _legs(results, direction, brief, rr, today):
    scope = _scope(brief)
    legs = []
    if "flights" in scope or "trains" not in scope:
        legs += [Leg.flight(_dump(o)) for o in _offers(results.get(f"flights_{direction}"))]
    if "trains" in scope or "flights" not in scope:
        legs += [Leg.train(_dump(o)) for o in _offers(results.get(f"trains_{direction}"))]
    legs = [l for l in legs if _leg_ok(l, brief, rr, today)]
    return _prune_legs(legs, rr["max_legs"])


def _max_hours_ok(c, brief):
    prefs = brief.prefs.value
    if prefs is None or not prefs.max_travel_hours or not _user(brief, "prefs.max_travel_hours"):
        return True
    return max(c.out_hours, c.back_hours) <= prefs.max_travel_hours


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
    hotel_name = h["name"]
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
    risks = connection_risks([s.to_check_dict() for s in steps], rules)
    for s in steps:
        if s.id in risks:
            s.risk = risks[s.id]
    return steps


def _facts(c: Combo, brief, districts, segments, rr):
    h = c.hotel
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
        "distance_beach_m": h.get("distance_beach_m"),
        "distance_center_m": h.get("distance_center_m"),
        "room_name": room.get("name"),
        "room_capacity_adults": room.get("capacity_adults"),
        "room_capacity_children": room.get("capacity_children"),
        "pets_allowed": h.get("pets_allowed"),
        "children_allowed": kids.get("children_allowed"),
        "min_child_age": kids.get("min_child_age"),
        "early_checkin_available": early.get("available"),
        "early_checkin_price_rub": early.get("price_rub"),
    }
    for direction, leg in (("out", c.out), ("back", c.back)):
        if leg is None:
            continue
        f.update({
            f"{direction}_kind": leg.kind,
            f"{direction}_price_rub": leg.price_rub,
            f"{direction}_from": leg.origin,
            f"{direction}_to": leg.dest,
            f"{direction}_date": leg.dep.date().isoformat(),
            f"{direction}_dep_time": leg.dep.strftime("%H:%M"),
            f"{direction}_arr_time": leg.arr.strftime("%H:%M"),
            f"{direction}_duration_min": leg.duration_min,
            f"{direction}_duration_hours": round(leg.duration_min / 60, 1),
            f"{direction}_stops": leg.stops,
            f"{direction}_carrier": leg.carrier,
            f"{direction}_number": leg.number,
            f"{direction}_baggage": leg.baggage,
            f"{direction}_refundable": leg.refundable,
            f"{direction}_layover_min": leg.min_layover,
            f"{direction}_door_hours": round(getattr(c, f"{direction}_hours"), 1),
        })
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
    if not facts.get("free_cancellation"):
        pct = facts.get("cancellation_penalty_pct")
        out.append(_note("no_free_cancel", "Отель без бесплатной отмены" + (f": штраф {pct}%" if pct else ""),
                         penalty_pct=pct))
    for direction, tr in (("out", c.tr_out), ("back", c.tr_back)):
        if tr is not None and tr["duration_min"] > rr["long_transfer_min"]:
            out.append(_note("long_transfer", f"Дорога {'до отеля' if direction == 'out' else 'в аэропорт'} {tr['duration_min']} мин",
                             minutes=int(tr["duration_min"]), direction=direction))
    if c.out is not None and c.tr_out is None:
        out.append(_note("no_transfer", "Трансфер не нашли — до отеля добираться самим"))
    wait = facts.get("wait_before_checkin_min")
    if wait and wait > rr["wait_before_checkin_min"]:
        out.append(_note("wait_before_checkin",
                         f"Приезд в {facts['arrival_at_hotel_time']}, а заселение с {facts['check_in_time']} — ждать {fmt_hours(wait / 60)} ч",
                         minutes=wait, arrival=facts["arrival_at_hotel_time"], check_in=facts["check_in_time"]))
    if c.out is not None and c.arrival is not None and in_clock_window(c.arrival.strftime("%H:%M"), late_window):
        out.append(_note("late_checkin", f"Заселение поздно, в {c.arrival:%H:%M} — отель стоит предупредить",
                         time=c.arrival.strftime("%H:%M")))
    wait = facts.get("wait_after_checkout_min")
    if wait and wait > rr["wait_after_checkout_min"]:
        out.append(_note("wait_after_checkout", f"После выезда из отеля {fmt_hours(wait / 60)} ч до отправления",
                         minutes=wait))
    if "accessible" in (brief.constraints.value or []):
        out.append(_note("accessibility_unknown", "Доступность для коляски в демо-данных не проверена"))
    for path in c.relaxed:
        out.append(_note("relaxed_pref", f"Вариантов {RELAXED_RU.get(path, path)} не нашли — показываем ближайшие", field=path))
    return out[: rr["max_notes"] + 2]


def _not_for(c: Combo, facts, brief, districts, rr):
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
    cancellation, stars, beach, constraints (party/pet policies), rating. Weights: rules.ranking.fit_weights.
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
    party = brief.party.value
    if brief.constraints.value or wants_pet(brief) or (party and party.children):
        b["constraints"] = 0.0 if any(i["severity"] == "block" for i in check_party(variant, brief)) else 1.0
    if f.get("hotel_rating") is not None:
        b["rating"] = round(min(1.0, f["hotel_rating"] / 10), 3)
    b = {k: round(max(0.0, min(1.0, v)), 3) for k, v in b.items()}
    w = rr["fit_weights"]
    total = sum(w.get(k, 0) for k in b)
    score = sum(w.get(k, 0) * v for k, v in b.items()) / total if total else 0.0
    return Fit(score=round(max(0.0, min(1.0, score)), 3), breakdown=b)


def recommended_hint(variants):
    """The code's pick: highest fit (ties: cheapest, fastest, comfort order). None for no variants."""
    if not variants:
        return None
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
    Returns [] when there are no hotel offers (honest "no data"; the engine says so).
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
    dest_name = dest.get("name") or (brief.destination.value.name if brief.destination.value else None)

    hotels_all = [_dump(h) for h in _offers(results.get("hotels"))]
    hotels_all = [h for h in hotels_all if _party_ok(h, brief)]
    if not hotels_all:
        return []
    strict = [h for h in hotels_all if not _pref_fails(h, brief)]
    relaxed_paths: tuple = ()
    if strict:
        hotels = strict
    else:
        hotels = hotels_all
        relaxed_paths = tuple(sorted({p for h in hotels_all for p in _pref_fails(h, brief)}))
    hotels = _prune_hotels(hotels, rr["max_hotels"], brief, districts, segs)

    want_transport = bool(scope & {"flights", "trains"})
    outs = _legs(results, "out", brief, rr, today) if want_transport else []
    backs = _legs(results, "back", brief, rr, today) if want_transport else []
    no_transport = want_transport and not (outs and backs)
    door = want_transport and not no_transport
    tr_out_all = results.get("transfers_out") or {}
    tr_back_all = results.get("transfers_back") or {}

    combos: list[Combo] = []
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
    if door:
        timed = [c for c in combos if _max_hours_ok(c, brief)]
        if timed:
            combos = timed
        elif combos:
            for c in combos:
                c.relaxed = tuple(sorted(set(c.relaxed) | {"prefs.max_travel_hours"}))
    if not combos:
        return []
    for c in combos:
        c.comfort = _comfort(c, brief, districts, segs, rr)

    picked: list[tuple[str, Combo]] = []
    notes: dict[str, list[str]] = {}
    cheapest = min(combos, key=lambda c: (c.rub, c.hours, -c.comfort))
    picked.append(("cheapest", cheapest))
    if door:
        fastest = min(combos, key=lambda c: (c.hours, c.rub, -c.comfort))
        if fastest.key != cheapest.key and cheapest.hours - fastest.hours >= rr["fastest_min_gain_h"]:
            picked.append(("fastest", fastest))
        else:
            notes.setdefault("cheapest", []).append("fastest")
    taken = {c.key for _, c in picked}
    budget = brief.budget_rub.value.max if brief.budget_rub.value else None
    cap = budget if budget else cheapest.rub * rr["comfort_price_cap_ratio"]
    pool = [c for c in combos if c.key not in taken and c.rub <= max(cap, cheapest.rub)]
    best_other = max(c.comfort for _, c in picked)
    comfort = max(pool, key=lambda c: (c.comfort, -c.rub)) if pool else None
    if comfort is not None and comfort.comfort >= best_other + rr["comfort_min_gain"]:
        picked.append(("comfort", comfort))
    else:
        winner = max(picked, key=lambda pc: pc[1].comfort)[0]
        notes.setdefault(winner, []).append("comfort")
    if not door:
        taken = {c.key for _, c in picked}
        rest = [c for c in combos if c.key not in taken and c.rub <= max(cap, cheapest.rub)]
        def loc(c):
            d = district_fit(brief, district_of(c.hotel, districts), segs)
            beach = c.hotel.get("distance_beach_m")
            near = 0.0 if beach is None or not (set(brief.traits.value or []) & SEA_TRAITS) else max(0.0, 1 - beach / rr["beach_zero_m"])
            return (d or 0) + near
        if rest:
            best_loc = max(rest, key=lambda c: (loc(c), -c.rub))
            if loc(best_loc) > max(loc(c) for _, c in picked) + 0.05:
                picked.append(("custom-1", best_loc))
    picked = picked[: rules.get("max_variants", 3)]

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
        profile = district_of(c.hotel, districts)
        dp = DistrictProfile(labels=district_labels(profile, rr),
                             scores={k: round(float(v), 3) for k, v in ((profile or {}).get("scores") or {}).items()})
        hotel = VariantHotel(hotel_id=c.hotel["hotel_id"], offer_id=c.hotel["offer_id"], name=c.hotel["name"],
                             stars=c.hotel.get("stars"), rating=c.hotel.get("rating"), district=dp)
        risks = _risks(c, f, brief, rules, rr, steps)
        if no_transport:
            from .state import Note
            risks.insert(0, Note(code="no_transport", text="Билеты не нашлись — показываем только проживание"))
        v = Variant(id=vid, title=TITLES.get(vid, vid), destination_id=dest.get("id") or (brief.destination.value.id if brief.destination.value else None),
                    steps=steps, totals=Totals(rub=c.rub, door_to_door_hours=c.hours), hotel=hotel, facts=f,
                    risks=risks, not_for=_not_for(c, f, brief, districts, rr))
        variants.append(v)
    for v in variants:
        others = [{"id": o.id, "facts": o.facts} for o in variants if o.id != v.id]
        v.sacrifices = _sacrifices({"id": v.id, "facts": v.facts}, others, rr, brief)
        v.fit = fit(brief, v, rules, segs)
    return variants


def _coincide_text(vid, also):
    names = {"fastest": "самым быстрым", "comfort": "самым комфортным"}
    base = {"cheapest": "Самый дешёвый вариант", "fastest": "Самый быстрый вариант", "comfort": "Комфортный вариант",
            "custom-1": "Этот вариант"}[vid]
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
