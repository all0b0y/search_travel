"""Level-3 `must` predicates, checked by code on the final session / cart / order.

    state = build_final_state(session, responses, lookup=None)
    check_must({"name": "total_le", "args": {"rub": 120000}}, state) -> {"name", "ok", "detail"}

A must entry is {"name": <predicate>, "args": {...}} (args optional). Every predicate returns
(ok, detail). Missing data is a failure with an explicit "no data" detail — never a pass.

FinalState normalises what the engine exposes:
  brief      Brief.values() of the final session
  chosen     the chosen variant as a dict: {id, steps:[{type, from, to, start, end, price_rub,
             title, offer_id}], hotel:{hotel_id, name, stars, rating, labels}, facts, totals}
  cart/order last non-null cart / order seen in a response
  responses  every response of the scenario (for messages and events)
  lookup     optional callable(tool, payload) -> dict (the Ostrovok client) to fetch hotel policies
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional

MEALS_ORDER = ("none", "breakfast", "half_board", "full_board", "all_inclusive")
MEALS_WORDS = {
    "all_inclusive": r"всё включено|все включено|all inclusive|ultra",
    "full_board": r"полный пансион",
    "half_board": r"полупансион",
    "breakfast": r"завтрак",
    "none": r"без питания",
}
AGE_BUCKETS = ((0, 1), (2, 6), (7, 12), (13, 17))


@dataclass
class FinalState:
    brief: dict = field(default_factory=dict)
    stage: Optional[str] = None
    chosen: Optional[dict] = None
    variants: list = field(default_factory=list)
    cart: Optional[dict] = None
    order: Optional[dict] = None
    responses: list = field(default_factory=list)
    lookup: Optional[Callable[[str, dict], dict]] = None

    def messages(self):
        return [r.get("message") or "" for r in self.responses]


# ---------------------------------------------------------------- normalisation

def _step_from_any(step):
    """Session Step (model_dump by alias) or response timeline step -> normalised dict."""
    price = step.get("price_rub")
    if price is None and isinstance(step.get("price"), dict):
        price = step["price"].get("rub", step["price"].get("amount"))
    return {"type": step.get("type"), "from": step.get("from") or step.get("from_"), "to": step.get("to"),
            "start": step.get("start"), "end": step.get("end"), "price_rub": price, "title": step.get("title") or "",
            "offer_id": step.get("offer_id")}


def normalize_variant(v):
    if v is None:
        return None
    if hasattr(v, "model_dump"):
        v = v.model_dump(mode="json", by_alias=True)
    hotel = v.get("hotel") or None
    if hotel:
        district = hotel.get("district") or {}
        hotel = {"hotel_id": hotel.get("hotel_id"), "offer_id": hotel.get("offer_id"), "name": hotel.get("name"),
                 "stars": hotel.get("stars"), "rating": hotel.get("rating"),
                 "labels": list(district.get("labels") or [])}
    return {"id": v.get("id"), "steps": [_step_from_any(s) for s in v.get("steps") or []], "hotel": hotel,
            "facts": dict(v.get("facts") or {}), "totals": dict(v.get("totals") or {}),
            "title": v.get("title")}


def build_final_state(session, responses, lookup=None):
    """`session`: agent.state.Session, a dict dump of it, or None."""
    sess = session
    if sess is not None and hasattr(sess, "model_dump"):
        brief = sess.brief.values()
        stage = sess.stage
        variants = [normalize_variant(v) for v in sess.variants]
        chosen_id = sess.chosen_variant_id
    elif isinstance(sess, dict):
        b = sess.get("brief") or {}
        brief = {k: (v.get("value") if isinstance(v, dict) and "value" in v else v) for k, v in b.items()}
        stage = sess.get("stage")
        variants = [normalize_variant(v) for v in sess.get("variants") or []]
        chosen_id = sess.get("chosen_variant_id")
    else:
        brief, stage, variants, chosen_id = {}, None, [], None
    cart = order = None
    card_variants = {}
    for r in responses:
        if r.get("cart"):
            cart = r["cart"]
        if r.get("order"):
            order = r["order"]
        for vc in (r.get("cards") or {}).get("variants") or []:
            card_variants[vc["id"]] = vc
        stage = stage or r.get("stage")
    if responses and not stage:
        stage = responses[-1].get("stage")
    chosen = next((v for v in variants if v and v["id"] == chosen_id), None) if chosen_id else None
    if chosen is None and chosen_id and chosen_id in card_variants:
        chosen = normalize_variant(card_variants[chosen_id])
    return FinalState(brief=brief, stage=stage, chosen=chosen, variants=variants, cart=cart, order=order,
                      responses=list(responses), lookup=lookup)


def _res(ok, detail=""):
    return bool(ok), detail


def _no_data(what):
    return False, f"no data: {what}"


def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _transport_steps(state):
    if not state.chosen:
        return []
    return [s for s in state.chosen["steps"] if s["type"] in ("flight", "train")]


def _stay_step(state):
    if state.chosen:
        stay = next((s for s in state.chosen["steps"] if s["type"] == "stay"), None)
        if stay:
            return stay
    for holder in (state.order, state.cart):
        if holder:
            item = next((i for i in holder.get("items") or [] if i.get("kind") == "hotel"), None)
            if item:
                return {"type": "stay", "start": item.get("start"), "end": item.get("end"), "title": item.get("title") or ""}
    return None


def _fact(state, *names):
    if not state.chosen:
        return None
    for n in names:
        if n in state.chosen["facts"] and state.chosen["facts"][n] is not None:
            return state.chosen["facts"][n]
    return None


# ---------------------------------------------------------------- predicates

def order_paid(state):
    if state.order and state.order.get("status") == "paid":
        return _res(True)
    return _res(False, f"order status: {(state.order or {}).get('status', 'no order')}")


def total_le(state, rub):
    total = None
    if state.order:
        total = state.order.get("total_rub")
    elif state.cart:
        total = state.cart.get("total_rub")
    elif state.chosen:
        total = state.chosen["totals"].get("rub")
    if total is None:
        return _no_data("no order, cart or chosen variant total")
    return _res(total <= rub, f"total {total} > {rub}" if total > rub else f"total {total}")


def _hotel_policy(state, key):
    hotel = (state.chosen or {}).get("hotel") or {}
    if state.lookup and hotel.get("hotel_id"):
        try:
            details = state.lookup("get_hotel", {"hotel_id": hotel["hotel_id"]})
            return (details.get("policies") or {}).get(key)
        except Exception:  # lookup failure = no data
            return None
    return None


def hotel_allows_pets(state):
    value = _fact(state, "pets_allowed", "hotel_pets_allowed")
    if value is None:
        value = _hotel_policy(state, "pets_allowed")
    if value is None:
        return _no_data("pets policy of the chosen hotel")
    return _res(value is True, "hotel does not allow pets" if value is not True else "")


def direct_flights_only(state):
    value = _fact(state, "direct", "direct_only", "all_direct")
    if value is not None:
        return _res(bool(value), "facts say not direct" if not value else "")
    stops = _fact(state, "stops", "stops_total", "max_stops")
    if stops is not None:
        return _res(stops == 0, f"{stops} stops")
    flights = [s for s in _transport_steps(state) if s["type"] == "flight"]
    if not flights:
        return _no_data("no flight steps in the chosen variant")
    for a, b in zip(flights, flights[1:]):
        ta, tb = _parse_dt(a["end"]), _parse_dt(b["start"])
        if a["to"] == b["from"] and ta and tb and (tb - ta).total_seconds() < 24 * 3600:
            return _res(False, f"connection at {a['to']}")
    return _res(True)


def free_cancellation(state):
    value = _fact(state, "free_cancellation", "hotel_free_cancellation")
    if value is None:
        until = _fact(state, "free_cancellation_until", "cancellation_free_until")
        if until is not None:
            value = bool(until)
    if value is None:
        stay = _stay_step(state)
        if stay and re.search(r"бесплатн\w* отмен", stay.get("title") or "", re.IGNORECASE):
            value = True
    if value is None:
        return _no_data("cancellation terms of the chosen hotel")
    return _res(bool(value), "hotel is not free-cancellable" if not value else "")


def district_label_in(state, labels):
    hotel = (state.chosen or {}).get("hotel")
    if not hotel:
        return _no_data("no hotel in the chosen variant")
    got = set(hotel.get("labels") or [])
    return _res(bool(got & set(labels)), f"district labels {sorted(got)} not in {labels}" if not got & set(labels) else "")


def _meals_of(state):
    m = _fact(state, "meals", "hotel_meals")
    if m in MEALS_ORDER:
        return m
    stay = _stay_step(state)
    title = (stay or {}).get("title") or ""
    for code, pat in MEALS_WORDS.items():
        if re.search(pat, title, re.IGNORECASE):
            return code
    return None


def meals_at_least(state, level):
    got = _meals_of(state)
    if got is None:
        return _no_data("meals of the chosen hotel")
    ok = MEALS_ORDER.index(got) >= MEALS_ORDER.index(level)
    return _res(ok, f"meals {got} < {level}" if not ok else "")


def _bucket(age):
    return next((i for i, (lo, hi) in enumerate(AGE_BUCKETS) if lo <= age <= hi), None)


def party_matches(state, adults, children_ages=None):
    party = state.brief.get("party")
    if not party:
        return _no_data("party in the brief")
    if party.get("adults") != adults:
        return _res(False, f"adults {party.get('adults')} != {adults}")
    want = list(children_ages or [])
    if (party.get("children") or 0) != len(want):
        return _res(False, f"children {party.get('children')} != {len(want)}")
    got = sorted(party.get("children_ages") or [])
    if want and len(got) != len(want):
        return _res(False, f"children ages {got} vs {sorted(want)}")
    for g, w in zip(got, sorted(want)):
        if _bucket(g) != _bucket(w):
            return _res(False, f"child age {g} not in the bucket of {w}")
    return _res(True)


def destination_in(state, names):
    dest = state.brief.get("destination") or {}
    cand = {str(dest.get("id") or "").casefold(), str(dest.get("name") or "").casefold()} - {""}
    if not cand:
        return _no_data("destination in the brief")
    want = {n.casefold() for n in names}
    ok = bool(cand & want) or any(w in c for c in cand for w in want)
    return _res(ok, f"destination {dest.get('name')!r} not in {names}" if not ok else "")


def nights_between(state, min, max):
    stay = _stay_step(state)
    nights = None
    if stay:
        a, b = _parse_dt(stay.get("start")), _parse_dt(stay.get("end"))
        if a and b:
            nights = (b.date() - a.date()).days
    if nights is None:
        dates = state.brief.get("dates") or {}
        if dates.get("start") and dates.get("end"):
            nights = (date.fromisoformat(dates["end"]) - date.fromisoformat(dates["start"])).days
        elif dates.get("nights"):
            nights = dates["nights"]
    if nights is None:
        return _no_data("stay dates")
    return _res(min <= nights <= max, f"{nights} nights not in {min}..{max}" if not min <= nights <= max else f"{nights} nights")


def _outbound(state):
    steps = _transport_steps(state)
    if not steps:
        return None
    stay = _stay_step(state)
    stay_start = _parse_dt(stay["start"]) if stay else None
    if stay_start:
        before = [s for s in steps if (_parse_dt(s["end"]) or stay_start) <= stay_start]
        if before:
            return before
    return steps[:1]


def arrival_before(state, hour):
    out = _outbound(state)
    if not out:
        return _no_data("no outbound transport")
    arr = _parse_dt(out[-1]["end"])
    if not arr:
        return _no_data("arrival time")
    local = arr.hour + arr.minute / 60
    return _res(local <= hour, f"arrives {arr.strftime('%H:%M')} after {hour}:00" if local > hour else "")


def departure_after(state, hour):
    out = _outbound(state)
    if not out:
        return _no_data("no outbound transport")
    dep = _parse_dt(out[0]["start"])
    if not dep:
        return _no_data("departure time")
    local = dep.hour + dep.minute / 60
    return _res(local >= hour, f"departs {dep.strftime('%H:%M')} before {hour}:00" if local < hour else "")


def no_night_arrival(state, start="23:00", end="06:00"):
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    for s in _transport_steps(state):
        t = _parse_dt(s["end"])
        if not t:
            continue
        m = t.hour * 60 + t.minute
        if m >= sh * 60 + sm or m < eh * 60 + em:
            return _res(False, f"arrival at {t.strftime('%H:%M')} ({s['to']})")
    if not _transport_steps(state):
        return _no_data("no transport steps")
    return _res(True)


def stars_at_least(state, n):
    hotel = (state.chosen or {}).get("hotel")
    if not hotel or hotel.get("stars") is None:
        return _no_data("hotel stars")
    return _res(hotel["stars"] >= n, f"{hotel['stars']}★ < {n}★" if hotel["stars"] < n else "")


def origin_is(state, city):
    origin = state.brief.get("origin") or {}
    got = str(origin.get("city") or "")
    return _res(got.casefold() == city.casefold(), f"origin {got!r} != {city!r}" if got.casefold() != city.casefold() else "")


def transport_is(state, kind):
    """kind: train | flight. Every transport step of the chosen variant is of this kind."""
    steps = _transport_steps(state)
    if not steps:
        return _no_data("no transport steps")
    bad = [s["type"] for s in steps if s["type"] != kind]
    return _res(not bad, f"transport {sorted(set(bad))} != {kind}" if bad else "")


def dates_within(state, start, end):
    stay = _stay_step(state)
    if not stay:
        return _no_data("stay dates")
    a, b = _parse_dt(stay.get("start")), _parse_dt(stay.get("end"))
    if not a or not b:
        return _no_data("stay dates")
    ok = date.fromisoformat(start) <= a.date() and b.date() <= date.fromisoformat(end)
    return _res(ok, f"stay {a.date()}..{b.date()} not within {start}..{end}" if not ok else "")


def stay_starts_on(state, day):
    stay = _stay_step(state)
    a = _parse_dt(stay.get("start")) if stay else None
    if not a:
        return _no_data("stay start")
    return _res(a.date().isoformat() == day, f"stay starts {a.date()} != {day}" if a.date().isoformat() != day else "")


def cart_has_kinds(state, kinds, exactly=False):
    holder = state.order or state.cart
    if not holder:
        return _no_data("no cart or order")
    got = {i.get("kind") for i in holder.get("items") or []}
    want = set(kinds)
    ok = got == want if exactly else want <= got
    return _res(ok, f"kinds {sorted(got)} vs {sorted(want)}{' (exactly)' if exactly else ''}" if not ok else "")


def no_blocking_issues(state):
    for holder in (state.order, state.cart):
        for issue in (holder or {}).get("issues") or []:
            if issue.get("severity") == "blocking":
                return _res(False, f"blocking issue {issue.get('code')}")
    return _res(True)


def order_action_done(state, kind):
    """An after-purchase action of `kind` reached the order (an order event whose kind starts with it)."""
    for r in state.responses:
        for ev in ((r.get("order") or {}).get("events") or []):
            if str(ev.get("kind", "")).startswith(kind):
                return _res(True)
    return _res(False, f"no order event {kind}*")


def agent_said(state, pattern, before_turn=None):
    """Some agent message (optionally among the first `before_turn`) matches the regex."""
    msgs = state.messages()[:before_turn] if before_turn else state.messages()
    ok = any(re.search(pattern, m, re.IGNORECASE) for m in msgs)
    return _res(ok, f"no agent message matches {pattern!r}" if not ok else "")


def agent_never_said(state, pattern):
    for i, m in enumerate(state.messages()):
        found = re.search(pattern, m, re.IGNORECASE)
        if found:
            return _res(False, f"turn {i + 1}: {found.group(0)!r}")
    return _res(True)


def brief_traits_any(state, traits):
    got = set(state.brief.get("traits") or [])
    return _res(bool(got & set(traits)), f"traits {sorted(got)} lack any of {traits}" if not got & set(traits) else "")


def brief_has_constraint(state, token):
    got = state.brief.get("constraints") or []
    pets = (state.brief.get("party") or {}).get("pets") or []
    ok = token in got or (token.startswith("pet:") and bool(pets))
    return _res(ok, f"constraint {token} missing" if not ok else "")


def budget_le(state, rub):
    got = (state.brief.get("budget_rub") or {}).get("max")
    if got is None:
        return _no_data("budget in the brief")
    return _res(got <= rub, f"budget {got} > {rub}" if got > rub else "")


def has_transfer(state):
    if state.chosen and any(s["type"] in ("transfer", "taxi") for s in state.chosen["steps"]):
        return _res(True)
    holder = state.order or state.cart
    if holder and any(i.get("kind") == "transfer" for i in holder.get("items") or []):
        return _res(True)
    return _res(False, "no transfer in variant, cart or order")


PREDICATES = {
    "order_paid": order_paid,
    "total_le": total_le,
    "hotel_allows_pets": hotel_allows_pets,
    "direct_flights_only": direct_flights_only,
    "free_cancellation": free_cancellation,
    "district_label_in": district_label_in,
    "meals_at_least": meals_at_least,
    "party_matches": party_matches,
    "destination_in": destination_in,
    "nights_between": nights_between,
    "arrival_before": arrival_before,
    "departure_after": departure_after,
    "no_night_arrival": no_night_arrival,
    "stars_at_least": stars_at_least,
    "origin_is": origin_is,
    "transport_is": transport_is,
    "dates_within": dates_within,
    "stay_starts_on": stay_starts_on,
    "cart_has_kinds": cart_has_kinds,
    "no_blocking_issues": no_blocking_issues,
    "order_action_done": order_action_done,
    "agent_said": agent_said,
    "agent_never_said": agent_never_said,
    "brief_traits_any": brief_traits_any,
    "brief_has_constraint": brief_has_constraint,
    "budget_le": budget_le,
    "has_transfer": has_transfer,
}


def check_must(spec, state):
    name = spec["name"] if isinstance(spec, dict) else spec
    args = (spec.get("args") if isinstance(spec, dict) else None) or {}
    fn = PREDICATES.get(name)
    if fn is None:
        return {"name": name, "args": args, "ok": False, "detail": f"unknown predicate {name}"}
    try:
        ok, detail = fn(state, **args)
    except (KeyError, TypeError, ValueError) as exc:
        ok, detail = False, f"predicate error: {type(exc).__name__}: {exc}"
    return {"name": name, "args": args, "ok": bool(ok), "detail": detail}


def check_all(must, state):
    return [check_must(m, state) for m in must]
