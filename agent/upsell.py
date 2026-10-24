"""Upsell candidates (AGENT_PLAN.md decision 8, foundation decision 8).

    offers = offers_for_variant(variant, results)              # what the mock can really sell here
    cands = upsell_candidates(brief, variant, offers, skill.upsell, rules=skill.rules)

Only kinds the mock sells: transfer, early_checkin, room_upgrade (incl. high floor), meal_upgrade,
flexible_fare. A rule fires when all its `when` conditions hold AND a matching offer exists; never
more than rules.max_upsell (default 2) per call, in the order of upsell.json. Rules of any other kind
(insurance, free notes), with unknown conditions, or whose reason template cannot be filled, are skipped.

`offers` (per kind, each a list of dicts, best first):
    {"transfer":      [{"offer_id", "price_rub", "kind", "duration_min"}],
     "early_checkin": [{"offer_id": None, "price_rub"}],          # requested after purchase (order action)
     "room_upgrade":  [{"offer_id", "price_rub" (extra), "room_name"}],
     "meal_upgrade":  [{"offer_id", "price_rub" (extra), "meals"}],
     "flexible_fare": [{"offer_id", "price_rub" (extra), "direction"}]}

Conditions (all must hold; this is the vocabulary upsell.json may use):
    arrival_after: "HH:MM"           arrival at the destination (first transport) at/after this clock time
                                     (times after midnight up to 05:00 count as late)
    arrival_before_checkin_min: N    minutes between arrival at the hotel and check-in >= N
    children: bool                   the brief has children (or not)
    variant_has_transfer: bool       the variant already has a transfer step (or not)
    traits_any: [trait]              brief traits intersect
    mood_any: [mood]                 brief mood intersects
    segment_any: [segment]           party.segment is one of
    prefs: {pref: value}             brief prefs equal these values
    non_refundable: bool             the variant has a non-refundable ticket (or not)
    meals_below: meals               the hotel's meals rank below this (none < breakfast < half_board < ...)
    nights_min: N                    the stay has at least N nights
    stars_below: N                   the hotel has fewer than N stars

Reason placeholders filled by code: {arrival_time} {checkin_time} {price_rub} {nights} {hotel}
{meals} / {meal_label} (Russian meals of the upgrade offer) {room} / {room_label} (room name of the
upgrade offer) {wait_hours} {transfer_min} {kind}. A rule whose label/reason promises a high floor
("этаж") only uses a room offer whose name or room id says so (этаж / floor / high); none -> dropped.
"""
from __future__ import annotations

from .checks import fmt_hours, fmt_int, ts
from .ranking import MEALS_RANK, MEALS_RU, TRANSFER_RU, _dump, _offers

SOLD_KINDS = ("transfer", "early_checkin", "room_upgrade", "meal_upgrade", "flexible_fare")
CONDITIONS = ("arrival_after", "arrival_before_checkin_min", "children", "variant_has_transfer", "traits_any",
              "mood_any", "segment_any", "prefs", "non_refundable", "meals_below", "nights_min", "stars_below")


class _Missing(dict):
    def __missing__(self, key):
        raise KeyError(key)


def _rules_list(upsell_rules):
    if isinstance(upsell_rules, dict):
        return upsell_rules.get("rules", [])
    return list(upsell_rules or [])


def _first_arrival(variant):
    t = next((s for s in variant.steps if s.type in ("flight", "train")), None)
    return ts(t.end) if t else None


def _late(clock, threshold):
    """At/after `threshold` the same evening, or after midnight up to 05:00."""
    return clock >= threshold or clock < "05:00"


def _cond(name, want, brief, variant):
    f = variant.facts
    party = brief.party.value
    if name == "arrival_after":
        arr = _first_arrival(variant)
        return arr is not None and _late(arr.strftime("%H:%M"), want)
    if name == "arrival_before_checkin_min":
        wait = f.get("wait_before_checkin_min")
        return isinstance(wait, (int, float)) and wait >= want
    if name == "children":
        return bool(party and party.children) == bool(want)
    if name == "variant_has_transfer":
        return any(s.type == "transfer" for s in variant.steps) == bool(want)
    if name == "traits_any":
        return bool(set(brief.traits.value or []) & set(want))
    if name == "mood_any":
        return bool(set(brief.mood.value or []) & set(want))
    if name == "segment_any":
        return bool(party) and party.segment in want
    if name == "prefs":
        prefs = brief.prefs.value
        return prefs is not None and all(getattr(prefs, k, None) == v for k, v in want.items())
    if name == "non_refundable":
        has = any(f.get(f"{d}_refundable") is False for d in ("out", "back"))
        return has == bool(want)
    if name == "meals_below":
        return MEALS_RANK.get(f.get("meals"), 0) < MEALS_RANK.get(want, 0)
    if name == "nights_min":
        return (f.get("nights") or 0) >= want
    if name == "stars_below":
        return (f.get("hotel_stars") or 0) < want
    raise KeyError(name)


def _fields(variant, offer, kind):
    f = variant.facts
    arr = _first_arrival(variant)
    wait = f.get("wait_before_checkin_min")
    data = {
        "arrival_time": f.get("arrival_at_hotel_time") if kind == "early_checkin" and f.get("arrival_at_hotel_time")
        else (arr.strftime("%H:%M") if arr else None),
        "checkin_time": f.get("check_in_time"),
        "price_rub": fmt_int(offer["price_rub"]) if offer.get("price_rub") is not None else None,
        "nights": f.get("nights"),
        "hotel": f.get("hotel_name"),
        "meals": MEALS_RU.get(offer.get("meals")) if offer.get("meals") else None,
        "room": offer.get("room_name"),
        "room_label": offer.get("room_name"),
        "meal_label": MEALS_RU.get(offer.get("meals")) if offer.get("meals") else None,
        "wait_hours": fmt_hours(wait / 60) if isinstance(wait, (int, float)) else None,
        "transfer_min": offer.get("duration_min"),
        "kind": TRANSFER_RU.get(offer.get("kind")) if offer.get("kind") else None,
    }
    return _Missing({k: v for k, v in data.items() if v is not None})


def _promises_floor(rule):
    text = f"{rule.get('label', '')} {rule.get('reason', '')} {rule.get('id', '')}".casefold()
    return "этаж" in text or "floor" in text


def _is_floor(offer):
    text = f"{offer.get('room_name') or ''} {offer.get('room_id') or ''}".casefold()
    return any(w in text for w in ("этаж", "floor", "high"))


def upsell_candidates(brief, variant, offers: dict, upsell_rules, rules=None):
    """Rule hits with a real offer and a reason tied to the brief/variant (≤ rules.max_upsell)."""
    from .state import UpsellCandidate
    cap = (rules or {}).get("max_upsell")
    if cap is None:
        cap = upsell_rules.get("max_upsell", 2) if isinstance(upsell_rules, dict) else 2
    out = []
    used_kinds = set()
    for rule in _rules_list(upsell_rules):
        if len(out) >= cap:
            break
        kind = rule.get("kind")
        if kind not in SOLD_KINDS or rule.get("placeholder") or kind in used_kinds:
            continue
        when = rule.get("when") or {}
        try:
            if not all(_cond(k, v, brief, variant) for k, v in when.items()):
                continue
        except KeyError:
            continue  # unknown condition: never fire on a guess
        options = [o for o in (offers or {}).get(kind, []) if o and o.get("price_rub") is not None]
        if kind == "room_upgrade" and _promises_floor(rule):
            options = [o for o in options if _is_floor(o)]
        if not options:
            continue
        offer = options[0]
        try:
            reason = rule["reason"].format_map(_fields(variant, offer, kind))
        except (KeyError, ValueError, IndexError):
            continue
        out.append(UpsellCandidate(id=f"u-{variant.id}-{rule['id']}", rule_id=rule["id"], kind=kind,
                                   label=rule.get("label") or kind, reason=reason,
                                   offer_id=offer.get("offer_id"), price_rub=int(offer["price_rub"]),
                                   variant_id=variant.id))
        used_kinds.add(kind)
    return out


def offers_for_variant(variant, results: dict) -> dict:
    """Derive the sellable add-ons for a variant from the same `results` dict build_variants used.

    transfer: arrival-point -> hotel transfers when the variant has none (cheapest first);
    early_checkin: the hotel offer's early check-in when available (no offer id: an order action);
    room_upgrade / meal_upgrade: other offers of the same hotel with a dearer room / better meals,
    priced as the difference, from results["hotels"] plus results["hotel_upgrades"][hotel_id]
    (the engine's search_hotels with hotel_id + all_rooms, and with filters.meals set higher); flexible_fare: a refundable fare of the same flight number and date.
    """
    out = {k: [] for k in SOLD_KINDS}
    hotel_id = variant.hotel.hotel_id if variant.hotel else None
    hotel_offer_id = variant.hotel.offer_id if variant.hotel else None
    hotels = [_dump(h) for h in _offers((results or {}).get("hotels"))]
    extra = ((results or {}).get("hotel_upgrades") or {}).get(hotel_id) if hotel_id else None
    known = {h["offer_id"] for h in hotels}
    hotels += [h for h in (_dump(x) for x in _offers(extra)) if h["offer_id"] not in known]
    current = next((h for h in hotels if h["offer_id"] == hotel_offer_id), None)
    has_transfer = any(s.type == "transfer" for s in variant.steps)
    first = next((s for s in variant.steps if s.type in ("flight", "train")), None)
    if hotel_id and first and not has_transfer:
        opts = [_dump(t) for t in _offers(((results or {}).get("transfers_out") or {}).get(hotel_id))]
        opts = [t for t in opts if t.get("point") in (None, first.to)]
        out["transfer"] = sorted(({"offer_id": t["offer_id"], "price_rub": t["price_rub"], "kind": t.get("kind"),
                                   "duration_min": t.get("duration_min")} for t in opts),
                                 key=lambda t: t["price_rub"])
    if current is not None:
        early = current.get("early_checkin") or {}
        if early.get("available") and early.get("price_rub") is not None:
            out["early_checkin"] = [{"offer_id": None, "price_rub": early["price_rub"]}]
        same = [h for h in hotels if h["hotel_id"] == current["hotel_id"] and h["offer_id"] != current["offer_id"]
                and h["price_total_rub"] > current["price_total_rub"]]
        rank = MEALS_RANK.get(current.get("meals"), 0)
        rooms = [h for h in same if (h.get("room") or {}).get("room_id") != (current.get("room") or {}).get("room_id")
                 and MEALS_RANK.get(h.get("meals"), 0) == rank]
        out["room_upgrade"] = [{"offer_id": h["offer_id"], "price_rub": h["price_total_rub"] - current["price_total_rub"],
                                "room_name": (h.get("room") or {}).get("name"),
                                "room_id": (h.get("room") or {}).get("room_id")}
                               for h in sorted(rooms, key=lambda h: h["price_total_rub"])]
        meals = [h for h in same if MEALS_RANK.get(h.get("meals"), 0) > rank
                 and (h.get("room") or {}).get("room_id") == (current.get("room") or {}).get("room_id")]
        out["meal_upgrade"] = [{"offer_id": h["offer_id"], "price_rub": h["price_total_rub"] - current["price_total_rub"],
                                "meals": h.get("meals")} for h in sorted(meals, key=lambda h: h["price_total_rub"])]
    for direction in ("out", "back"):
        step = next((s for s in (variant.steps if direction == "out" else reversed(variant.steps)) if s.type == "flight"), None)
        if step is None:
            continue
        flights = [_dump(o) for o in _offers((results or {}).get(f"flights_{direction}"))]
        mine = next((o for o in flights if o["offer_id"] == step.offer_id), None)
        if mine is None or (mine.get("fare") or {}).get("refundable"):
            continue
        flex = [o for o in flights if o["flight_no"] == mine["flight_no"] and o["dep"] == mine["dep"]
                and (o.get("fare") or {}).get("refundable") and o["price_rub"] >= mine["price_rub"]]
        out["flexible_fare"] += [{"offer_id": o["offer_id"], "price_rub": o["price_rub"] - mine["price_rub"],
                                  "direction": direction} for o in sorted(flex, key=lambda o: o["price_rub"])]
    return out
