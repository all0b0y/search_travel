"""Validation of door-to-door variants (AGENT_PLAN.md decision 18, sections 3 and 5).

Ported from skills/trip-planner/scripts/trip.py (`finalize`): chain continuity, time order, buffers
before flights/trains and after flights, the taxi/transfer time factor, connection risk slack, late
check-in, budget and dates; extended with party fit (room capacity, children, pets), totals and
stay nights. Pure functions: nothing here mutates its input or touches the network.

    issues = check_variant(variant, brief, rules)        # rules = skills/trip-agent/rules.json
    issues += check_variant_set(variants)                # cheapest really cheapest, fastest really fastest

Issue = {"code", "severity": "block"|"warn", "message" (Russian, may be shown), "data"}.

Severity policy: broken chains, overlaps, missing buffers, a lost night, wrong totals and party
misfits block. Budget and dates block only when the user gave them (an assumed budget/date only
warns). Risky connections and late check-in warn (the agent has no plan B generator; the risk is
stated instead).

Also here: the CBR rates helpers (`parse_cbr_xml`, `to_rub`, `fill_rub`) used only when an offer is
not priced in RUB, and small Russian formatting helpers shared with ranking/upsell/render.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Iterable

TRANSPORT = ("flight", "train")
GROUND = ("taxi", "transfer")

# Used only when rules.json has no such key (the skill owns the values; these mirror trip-planner).
DEFAULT_BUFFERS = {"before_flight_international": 180, "before_flight_domestic": 120, "before_train": 30,
                   "after_flight": 45, "after_flight_with_baggage": 75}
DEFAULT_TAXI_FACTOR = 1.2
DEFAULT_RISK_SLACK_MIN = 60
DEFAULT_LATE_CHECKIN = ("22:00", "05:00")
OVERNIGHT_FROM_HOUR = 18   # an outbound night train/flight may leave the evening before the start date


# ---------------------------------------------------------------- small helpers

def issue(code, severity, message, **data):
    if severity not in ("block", "warn"):
        raise ValueError(f"bad severity {severity}")
    return {"code": code, "severity": severity, "message": message, "data": data}


def ts(text) -> datetime:
    return text if isinstance(text, datetime) else datetime.fromisoformat(str(text))


def minutes(a, b) -> float:
    return (ts(b) - ts(a)).total_seconds() / 60


def hhmm(value) -> str:
    return ts(value).strftime("%H:%M")


def in_clock_window(clock: str, window) -> bool:
    """`clock` (HH:MM) inside [from, to); windows may wrap midnight (["22:00", "05:00"])."""
    start, end = window
    if start <= end:
        return start <= clock < end
    return clock >= start or clock < end


def norm(place) -> str:
    return " ".join(str(place or "").replace("ё", "е").replace("Ё", "Е").split()).casefold()


def fmt_int(value) -> str:
    """84000 -> '84 000' (plain spaces; guards accept any space)."""
    return f"{int(round(value)):,}".replace(",", " ")


def fmt_rub(value) -> str:
    return f"{fmt_int(value)} ₽"


def fmt_hours(value) -> str:
    """6.5 -> '6,5'; 7.0 -> '7'."""
    value = round(float(value), 1)
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def plural(n, one, few, many) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def nights_text(n) -> str:
    return f"{n} {plural(n, 'ночь', 'ночи', 'ночей')}"


def _model(value, cls):
    return value if isinstance(value, cls) else cls.model_validate(value)


def _steps(variant) -> list[dict]:
    if isinstance(variant, dict):
        return [dict(s) for s in variant.get("steps", [])]
    return variant.steps_for_checks()


def _buffers(rules):
    return {**DEFAULT_BUFFERS, **(rules.get("buffers_min") or {})}


# ---------------------------------------------------------------- rates (CBR)

def parse_cbr_xml(payload) -> dict:
    """Parse https://www.cbr.ru/scripts/XML_daily.asp -> {"source", "date", "rub_per_unit": {code: rub}}.

    Values use a decimal comma and a Nominal (e.g. 10 for some currencies); the result is per one unit.
    Raises ValueError on a malformed document.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8") if "encoding=" not in payload[:100] else payload.encode("cp1251")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"bad CBR XML: {exc}") from None
    if root.tag != "ValCurs":
        raise ValueError("bad CBR XML: no ValCurs root")
    day = root.get("Date")
    iso = datetime.strptime(day, "%d.%m.%Y").date().isoformat() if day else None
    rates = {}
    for val in root.findall("Valute"):
        code = (val.findtext("CharCode") or "").strip()
        raw = (val.findtext("Value") or "").strip().replace(",", ".")
        nominal = (val.findtext("Nominal") or "1").strip()
        if not code or not raw:
            continue
        rates[code] = round(float(raw) / int(nominal), 6)
    if not rates:
        raise ValueError("bad CBR XML: no rates")
    return {"source": "CBR", "date": iso, "rub_per_unit": rates}


def to_rub(amount, currency, rates) -> int | None:
    """Amount in RUB (rounded) or None when there is no rate. `rates` = {code: rub} or parse_cbr_xml()."""
    if currency == "RUB":
        return round(amount)
    table = rates.get("rub_per_unit", rates) if isinstance(rates, dict) else {}
    if currency in table:
        return round(amount * table[currency])
    return None


def fill_rub(variant, rates):
    """Port of trip.py fill_prices: fill `price.rub` of every step (and plan B). Returns (variant, issues).

    Totals are recomputed from the filled steps. The input is not mutated.
    """
    from .state import Variant
    v = _model(variant, Variant).model_copy(deep=True)
    found = []
    def fill(price, sid):
        rub = to_rub(price.amount, price.currency, rates)
        if rub is None:
            found.append(issue("price_no_rate", "block", f"Нет курса для {price.currency}",
                               variant=v.id, step=sid, currency=price.currency))
        else:
            price.rub = rub

    for step in v.steps:
        fill(step.price, step.id)
        backup = (step.plan_b or {}).get("step")
        if isinstance(backup, dict) and isinstance(backup.get("price"), dict):
            b = type(step).model_validate(backup)
            fill(b.price, b.id)
            step.plan_b = {**step.plan_b, "step": b.to_check_dict()}
    v.totals.rub = int(sum((s.price.rub or 0) for s in v.steps))
    return v, found


# ---------------------------------------------------------------- chain

def origin_points(brief) -> set[str]:
    origin = brief.origin.value
    if origin is None:
        return set()
    return {norm(origin.city), *(norm(c) for c in origin.iata)}


def _at_origin(place, points, city) -> bool:
    p = norm(place)
    return p in points or bool(city and p.startswith(city + " "))  # "москва ленинградская" (a station)


def check_chain(steps, brief, vid=None) -> list[dict]:
    """Every step starts where the previous one ended; door-to-door variants start and end at the origin.

    The origin check runs only when the brief knows the origin's IATA codes or some step names the
    origin city (otherwise the airports are unknown here and the check would be a guess). A gap between a
    flight/train arrival and the stay (no transfer found) only warns: the traveller gets there alone.
    """
    found = []
    if not steps:
        return [issue("chain_empty", "block", "В варианте нет шагов", variant=vid)]
    transport = [s for s in steps if s["type"] in TRANSPORT]
    origin = brief.origin.value
    if transport and origin is not None:
        points, city = origin_points(brief), norm(origin.city)
        named = any(_at_origin(s[end], {city}, city) for s in steps for end in ("from", "to"))
        if origin.iata or named:
            if not _at_origin(steps[0]["from"], points, city):
                found.append(issue("chain_origin", "block",
                                   f"Маршрут начинается в {steps[0]['from']}, а вылет из {origin.city}",
                                   variant=vid, step=steps[0]["id"], place=steps[0]["from"]))
            if not _at_origin(steps[-1]["to"], points, city):
                found.append(issue("chain_origin", "block",
                                   f"Маршрут заканчивается в {steps[-1]['to']}, а не в {origin.city}",
                                   variant=vid, step=steps[-1]["id"], place=steps[-1]["to"]))
    for a, b in zip(steps, steps[1:]):
        if norm(a["to"]) == norm(b["from"]):
            continue
        gap_ok = (a["type"] in TRANSPORT and b["type"] == "stay") or (a["type"] == "stay" and b["type"] in TRANSPORT)
        if gap_ok:
            found.append(issue("no_transfer", "warn", f"Между {a['to']} и {b['from']} нет трансфера — добираться самим",
                               variant=vid, step=b["id"], **{"from": a["to"], "to": b["from"]}))
        else:
            found.append(issue("chain_break", "block", f"{a['id']} заканчивается в {a['to']}, а следующий шаг начинается в {b['from']}",
                               variant=vid, step=b["id"]))
    return found


# ---------------------------------------------------------------- timing (ported)

def required_gap(a, b, buffers) -> int:
    need = 0
    if a["type"] == "flight":
        need += buffers["after_flight_with_baggage" if a.get("checked_baggage") else "after_flight"]
    if b["type"] == "flight":
        need += buffers["before_flight_international" if b.get("international") else "before_flight_domestic"]
    if b["type"] == "train":
        need += buffers["before_train"]
    return need


def _effective_gap(a, b):
    """Minutes between a and b. A stay may start before the arrival (regular check-in time earlier than
    the arrival) and may end after the departure (leaving before check-out): these are not overlaps."""
    return minutes(a["end"], b["start"])


def connection_risks(steps, rules) -> dict[str, dict]:
    """trip.py check_timing slack: a flight/train reached from another flight/train (through taxis and
    transfers) is missable. -> {step_id: {"slack_min", "risky"}} for every such step."""
    buffers = _buffers(rules)
    slack = [_effective_gap(a, b) - required_gap(a, b, buffers) for a, b in zip(steps, steps[1:])]
    limit = rules.get("risk_slack_min", DEFAULT_RISK_SLACK_MIN)
    out = {}
    for i, step in enumerate(steps):
        if step["type"] not in TRANSPORT:
            continue
        spare, j = 0.0, i - 1
        while j >= 0 and steps[j]["type"] in GROUND:
            spare += slack[j]
            j -= 1
        if j < 0 or steps[j]["type"] not in TRANSPORT:
            continue
        spare += slack[j]
        out[step["id"]] = {"slack_min": round(spare), "risky": spare < limit}
    return out


def check_timing(steps, rules, vid=None) -> list[dict]:
    found = []
    buffers = _buffers(rules)
    factor = rules.get("taxi_time_factor", DEFAULT_TAXI_FACTOR)
    for step in steps:
        length = minutes(step["start"], step["end"])
        if length <= 0:
            found.append(issue("time_order", "block", f"Шаг {step['id']} заканчивается раньше, чем начинается",
                               variant=vid, step=step["id"]))
        estimate = step.get("duration_estimate_min")
        if step["type"] in GROUND and estimate and length < estimate * factor:
            found.append(issue("taxi_margin", "block",
                               f"На дорогу {length:.0f} мин, а с запасом нужно {estimate * factor:.0f}",
                               variant=vid, step=step["id"], planned_min=round(length),
                               needed_min=round(estimate * factor)))
    for a, b in zip(steps, steps[1:]):
        if a["type"] == "stay" or b["type"] == "stay":
            continue  # check-in/check-out are bounds, not appointments: see check_stay
        gap, need = _effective_gap(a, b), required_gap(a, b, buffers)
        if gap < 0:
            found.append(issue("time_overlap", "block", f"Шаг {b['id']} начинается раньше, чем закончился {a['id']}",
                               variant=vid, step=b["id"], overlap_min=round(-gap)))
        elif gap < need:
            found.append(issue("buffer", "block", f"После {a['id']} нужно {need} мин запаса, а есть {gap:.0f}",
                               variant=vid, step=b["id"], need_min=need, have_min=round(gap)))
    for sid, risk in connection_risks(steps, rules).items():
        step = next(s for s in steps if s["id"] == sid)
        if risk["risky"] and not step.get("plan_b"):
            found.append(issue("risky_connection", "warn", f"Короткая стыковка: запас {risk['slack_min']} мин",
                               variant=vid, step=sid, slack_min=risk["slack_min"]))
    return found


def _stay_index(steps):
    return next((i for i, s in enumerate(steps) if s["type"] == "stay"), None)


def arrival_at_stay(steps):
    """When the traveller reaches the stay: end of the step before it (None for stay-only)."""
    i = _stay_index(steps)
    if not i:
        return None
    return ts(steps[i - 1]["end"])


def check_stay(steps, rules, vid=None) -> list[dict]:
    """Arrival vs check-in: a lost night blocks; arriving in the late check-in window warns."""
    found = []
    i = _stay_index(steps)
    if i is None:
        return found
    stay = steps[i]
    arrival = arrival_at_stay(steps)
    if arrival is not None:
        start = ts(stay["start"])
        if minutes(start, arrival) > 0:
            check_in_day = datetime.combine(start.date(), datetime.min.time(), tzinfo=start.tzinfo)
            if arrival >= check_in_day + timedelta(days=1, hours=_late_end_hour(rules)):
                found.append(issue("night_lost", "block",
                                   f"Прибытие {arrival:%d.%m} в {arrival:%H:%M}, а заселение с {start:%d.%m} — первая ночь пропадает",
                                   variant=vid, step=stay["id"]))
        if in_clock_window(arrival.strftime("%H:%M"), rules.get("late_checkin", DEFAULT_LATE_CHECKIN)):
            found.append(issue("late_checkin", "warn", f"Заселение поздно, в {arrival:%H:%M}: отель стоит предупредить",
                               variant=vid, step=stay["id"], arrival=arrival.strftime("%H:%M")))
    after = steps[i + 1] if i + 1 < len(steps) else None
    if after is not None and minutes(stay["start"], after["start"]) <= 0:
        found.append(issue("time_order", "block", "Отъезд раньше заселения", variant=vid, step=after["id"]))
    return found


def _late_end_hour(rules):
    end = (rules.get("late_checkin") or DEFAULT_LATE_CHECKIN)[1]
    return int(end[:2]) + int(end[3:5]) / 60


# ---------------------------------------------------------------- dates, budget, party, totals

def _dates_window(dates, which):
    """Allowed (from, to) for the outbound ('start') or return ('end') date, or None."""
    flex = timedelta(days=dates.flexible_days or 0)
    day = dates.start if which == "start" else dates.end
    if which == "end" and day is None and dates.start and dates.nights:
        day = dates.start + timedelta(days=dates.nights)
    if day is not None:
        return day - flex, day + flex
    if dates.window is not None:
        return dates.window.from_, dates.window.to
    return None


def check_dates(steps, brief, vid=None, rules=None) -> list[dict]:
    """Outbound departure within dates.start ± flexible_days (or the window / month), return around
    dates.end (or start + nights), stay nights == dates.nights. An overnight outbound leaving after
    18:00 the evening before the start date and arriving on it is fine."""
    found = []
    dates = brief.dates.value
    if dates is None or not steps:
        return found
    i = _stay_index(steps)
    out_steps = steps[:i] if i is not None else steps
    back_steps = steps[i + 1:] if i is not None else []
    first_out = next((s for s in out_steps if s["type"] in TRANSPORT), None) or (steps[i] if i is not None else None)
    first_back = next((s for s in back_steps if s["type"] in TRANSPORT), None)
    for key, path, step in (("start", "dates.start", first_out), ("end", "dates.end", first_back)):
        if step is None:
            continue
        day = ts(step["start"]).date() if key == "start" or step["type"] != "stay" else ts(step["end"]).date()
        window = _dates_window(dates, key)
        sev = "block" if brief.source_of(path) == "user" or brief.source_of("dates.window") == "user" else "warn"
        overnight = (key == "start" and step["type"] in TRANSPORT and window is not None
                     and day == window[0] - timedelta(days=1) and ts(step["start"]).hour >= OVERNIGHT_FROM_HOUR
                     and window[0] <= ts(step["end"]).date() <= window[1])
        if window and not window[0] <= day <= window[1] and not overnight:
            label = "Вылет" if key == "start" else "Обратно"
            found.append(issue("dates", sev, f"{label} {day:%d.%m} — вне дат поездки",
                               variant=vid, step=step["id"], day=day.isoformat(),
                               **{"from": window[0].isoformat(), "to": window[1].isoformat()}))
        elif key == "start" and window is None and dates.month and day.month != dates.month:
            found.append(issue("dates", "block" if brief.source_of("dates.month") == "user" else "warn",
                               f"Вылет {day:%d.%m} — не в выбранном месяце", variant=vid, step=step["id"],
                               day=day.isoformat(), month=dates.month))
    if i is not None and dates.nights:
        stay = steps[i]
        start = ts(stay["start"])
        first_night = start.date() - timedelta(days=1) if start.hour + start.minute / 60 < _late_end_hour(rules or {}) else start.date()
        nights = (ts(stay["end"]).date() - first_night).days   # booked nights: 03:23 on the 8th uses the night of the 7th
        if nights != dates.nights:
            found.append(issue("nights", "block" if brief.source_of("dates.nights") == "user" else "warn",
                               f"В отеле {nights_text(nights)}, а нужно {nights_text(dates.nights)}",
                               variant=vid, step=stay["id"], nights=nights, wanted=dates.nights))
    return found


def check_budget(variant, brief) -> list[dict]:
    budget = brief.budget_rub.value
    if budget is None or budget.max is None:
        return []
    rub = variant.totals.rub
    if rub <= budget.max:
        return []
    sev = "block" if brief.source_of("budget_rub.max") == "user" else "warn"
    return [issue("budget", sev, f"{fmt_rub(rub)} — больше бюджета {fmt_rub(budget.max)}",
                  variant=variant.id, total_rub=rub, budget_rub=budget.max, over_rub=rub - budget.max)]


def wants_pet(brief) -> bool:
    party = brief.party.value
    return bool((party and party.pets) or any(str(c).startswith("pet:") for c in (brief.constraints.value or [])))


def check_party(variant, brief) -> list[dict]:
    """Room capacity, children policy and pets, from the hotel facts the variant carries
    (`room_capacity_adults`, `room_capacity_children`, `children_allowed`, `min_child_age`, `pets_allowed`).
    Missing facts are not guessed: that sub-check is skipped."""
    found = []
    party = brief.party.value
    f = variant.facts
    vid = variant.id
    if variant.hotel is None and not any(s.type == "stay" for s in variant.steps):
        return found
    if party is not None:
        cap_a, cap_c = f.get("room_capacity_adults"), f.get("room_capacity_children")
        if isinstance(cap_a, int):
            seats = cap_a + (cap_c if isinstance(cap_c, int) else 0)
            if party.adults > cap_a or party.adults + party.children > seats:
                found.append(issue("room_capacity", "block",
                                   f"Номер на {cap_a} взр. — а едут {party.adults} взр. и {party.children} дет.",
                                   variant=vid, adults=party.adults, children=party.children,
                                   capacity_adults=cap_a, capacity_children=cap_c))
        if party.children:
            if f.get("children_allowed") is False:
                found.append(issue("children_policy", "block", "Отель не принимает детей", variant=vid))
            min_age = f.get("min_child_age")
            if isinstance(min_age, int) and party.children_ages and min(party.children_ages) < min_age:
                found.append(issue("children_policy", "block", f"Отель принимает детей с {min_age} лет",
                                   variant=vid, min_child_age=min_age, youngest=min(party.children_ages)))
    if wants_pet(brief) and f.get("pets_allowed") is False:
        found.append(issue("pets_policy", "block", "В отель нельзя с животными", variant=vid))
    return found


def check_totals(variant) -> list[dict]:
    rubs = [s.price.rub if s.price.rub is not None else (s.price.amount if s.price.currency == "RUB" else None)
            for s in variant.steps]
    if any(r is None for r in rubs):
        return [issue("price_no_rate", "block", "Не у всех шагов есть цена в рублях", variant=variant.id)]
    total = round(sum(rubs))
    if total != variant.totals.rub:
        return [issue("totals", "block", f"Итог {fmt_rub(variant.totals.rub)} не равен сумме шагов {fmt_rub(total)}",
                      variant=variant.id, total_rub=variant.totals.rub, steps_rub=total)]
    return []


# ---------------------------------------------------------------- entry points

def check_variant(variant, brief, rules) -> list[dict]:
    """All checks for one variant. `variant`: state.Variant (or its dict), `brief`: state.Brief,
    `rules`: rules.json dict. Returns Issues (empty = fine)."""
    from .state import Brief, Variant
    v = _model(variant, Variant)
    b = _model(brief, Brief)
    steps = _steps(v)  # the variant's order defines the chain
    found = []
    found += check_chain(steps, b, v.id)
    if steps:
        found += check_timing(steps, rules, v.id)
        found += check_stay(steps, rules, v.id)
        found += check_dates(steps, b, v.id, rules)
    found += check_totals(v)
    found += check_budget(v, b)
    found += check_party(v, b)
    return found


def check_variant_set(variants) -> list[dict]:
    """Port of trip.py check_variants: unique ids; `cheapest` lowest by rub, `fastest` lowest by hours."""
    from .state import Variant
    vs = [_model(v, Variant) for v in variants]
    found = []
    ids = [v.id for v in vs]
    for vid in sorted(set(ids)):
        if ids.count(vid) > 1:
            found.append(issue("variants_dup", "block", f"Вариант {vid} дважды", variant=vid))
    for vid, key in (("cheapest", "rub"), ("fastest", "door_to_door_hours")):
        best = next((v for v in vs if v.id == vid), None)
        if best and any(getattr(v.totals, key) < getattr(best.totals, key) for v in vs):
            found.append(issue("variants_order", "block", f"«{vid}» не лучший по {key}", variant=vid, key=key))
    return found


def blocking(issues: Iterable[dict]) -> list[dict]:
    return [i for i in issues if i["severity"] == "block"]


def as_dates(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


__all__ = ["check_variant", "check_variant_set", "check_chain", "check_timing", "check_stay", "check_dates",
           "check_budget", "check_party", "check_totals", "connection_risks", "required_gap", "parse_cbr_xml",
           "to_rub", "fill_rub", "issue", "blocking", "fmt_rub", "fmt_hours", "fmt_int", "plural",
           "nights_text", "in_clock_window", "arrival_at_stay"]
