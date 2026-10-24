"""Card inputs (AGENT_PLAN.md section 4 "Card inputs", binding) and the code-written Russian labels.

Every builder returns ONE JSON-ready dict: the user message of the card. Common keys:
`today`, `stage`, `brief` (values only), `assumed` (brief paths whose source is not "user"),
`assumptions` (the chips shown with this answer: [{field, label}]).

Short labels "1", "2", "3" for references are recorded in `session.shown` by `record_shown`.
"""
from datetime import date

from .state import BRIEF_FIELDS, OBJECT_FIELDS, Choice, Chip, ChipOption, Question, ShownItem

# ---------------------------------------------------------------- Russian formatting

MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря")
MONTHS_PREP = ("январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре",
               "октябре", "ноябре", "декабре")
MEALS_RU = {"any": "питание любое", "none": "без питания", "breakfast": "с завтраком", "half_board": "полупансион",
            "full_board": "полный пансион", "all_inclusive": "всё включено"}
DISTRICT_RU = {"any": "район любой", "lively": "движ", "quiet": "тихий район", "family": "семейный район",
               "walkable": "пешком до всего"}
TRANSPORT_RU = {"any": "самолёт или поезд", "plane": "самолётом", "train": "поездом"}
SCOPE_FULL = ["flights", "stay", "transfers"]
SCOPE_RU = {"flights": "перелёт", "trains": "поезд", "stay": "отель", "transfers": "трансфер"}
VARIANT_TITLES = {"cheapest": "Дешевле", "fastest": "Быстрее", "comfort": "Комфортнее"}
CHIP_ORDER = ("origin.city", "dates.start", "dates.month", "dates.nights", "party.segment", "party.adults",
              "party.children_ages", "prefs.district", "prefs.meals", "scope", "budget_rub.max")
MAX_CHIPS = 5


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_int(n):
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_rub(n):
    return f"{fmt_int(n)} ₽"


def fmt_hours(h):
    return f"{h:.1f}".replace(".", ",") + " ч"


def fmt_date_range(start, end):
    s, e = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    if s.month == e.month:
        return f"{s.day}–{e.day} {MONTHS_GEN[e.month - 1]}"
    return f"{s.day} {MONTHS_GEN[s.month - 1]} – {e.day} {MONTHS_GEN[e.month - 1]}"


def party_label(party):
    if not party:
        return "состав не указан"
    adults, children = int(party.get("adults") or 0), int(party.get("children") or 0)
    parts = []
    if adults:
        parts.append(f"{adults} {plural(adults, 'взрослый', 'взрослых', 'взрослых')}")
    if children == 1:
        parts.append("ребёнок")
    elif children:
        parts.append(f"{children} {plural(children, 'ребёнок', 'ребёнка', 'детей')}")
    label = " и ".join(parts) if parts else "состав не указан"
    pets = party.get("pets") or []
    if pets:
        label += " с питомцем"
    return label


# ---------------------------------------------------------------- provenance -> assumed paths, chips

def _model_defaults(top):
    cls = OBJECT_FIELDS[top]
    try:
        return cls().model_dump(mode="json")
    except Exception:  # origin/destination need a name
        return {}


def assumed_paths(brief):
    """Brief paths holding a value whose source is not "user" (sub-field provenance respected)."""
    out = []
    values = brief.values()
    for top in BRIEF_FIELDS:
        value = values[top]
        if value is None or value == []:
            continue
        field = getattr(brief, top)
        if top == "origin":
            if field.source != "user":
                out.append("origin.city")
        elif top == "destination":
            if field.source != "user":
                out.append("destination.name")
        elif top in OBJECT_FIELDS:
            defaults = _model_defaults(top)
            for sub, val in value.items():
                if val is None or val == [] or val == defaults.get(sub):
                    continue
                if brief.source_of(f"{top}.{sub}") != "user":
                    out.append(f"{top}.{sub}")
        elif field.source != "user":
            out.append(top)
    return out


def _choice_label(skill, field, value):
    q = (skill.questions or {}).get(field) or {}
    for c in q.get("choices") or []:
        if c.get("value") == value:
            return c["label"]
    return None


def _question_options(skill, field):
    q = (skill.questions or {}).get(field) or {}
    return [ChipOption(label=c["label"], value=c["value"]) for c in q.get("choices") or []]


def chip_for(skill, brief, path):
    """A chip for an assumed brief path, or None when the path is not shown as a chip."""
    values = brief.values()
    top, _, sub = path.partition(".")
    value = (values.get(top) or {}).get(sub) if top in OBJECT_FIELDS else values.get(top)
    if top == "origin":
        value = (values.get("origin") or {}).get("city")
    if value is None:
        return None
    label = _choice_label(skill, path, value)
    options = _question_options(skill, path)
    if path == "origin.city":
        label = f"из {value}" if label is None else label
    elif path == "dates.start":
        end = (values.get("dates") or {}).get("end")
        if not end:
            return None
        label, options = fmt_date_range(value, end), []
    elif path == "dates.month":
        if (values.get("dates") or {}).get("start"):
            return None
        label, options = f"в {MONTHS_PREP[int(value) - 1]}", []
    elif path == "dates.nights":
        label = label or f"{value} {plural(value, 'ночь', 'ночи', 'ночей')}"
    elif path == "party.segment":
        seg = (skill.segments or {}).get(value) or {}
        label = label or seg.get("label") or value
    elif path == "party.adults":
        label = f"{value} {plural(value, 'взрослый', 'взрослых', 'взрослых')}"
        options = [ChipOption(label=f"{n} {plural(n, 'взрослый', 'взрослых', 'взрослых')}", value=n) for n in (1, 2, 3, 4)]
    elif path == "party.children_ages":
        label = label or ("дети: " + ", ".join(str(a) for a in value))
    elif path == "prefs.district":
        label = DISTRICT_RU.get(value, value)
        options = [ChipOption(label=DISTRICT_RU[k], value=k) for k in ("quiet", "walkable", "lively", "family")]
    elif path == "prefs.meals":
        label = MEALS_RU.get(value, value)
        options = [ChipOption(label=MEALS_RU[k], value=k) for k in ("any", "breakfast", "half_board", "all_inclusive")]
    elif path == "scope":
        label = " + ".join(SCOPE_RU.get(s, s) for s in value) or "всё"
        options = [ChipOption(label="только отель", value=["stay"]),
                   ChipOption(label="отель + дорога + трансфер", value=list(SCOPE_FULL))]
    elif path == "budget_rub.max":
        label = label or f"до {fmt_rub(value)}"
    else:
        return None
    return Chip(field=path, label=str(label), value=value, options=options)


def build_chips(skill, brief, exclude_fields=(), extra_paths=()):
    """Chips for the assumptions of this answer (sources are never shown)."""
    paths = set(assumed_paths(brief)) | set(extra_paths)
    chips = []
    for path in CHIP_ORDER:
        if path in paths and path not in exclude_fields:
            chip = chip_for(skill, brief, path)
            if chip is not None:
                chips.append(chip)
    return chips[:MAX_CHIPS]


def scope_chip(skill, brief):
    """Entry D: a stay-only (or partial) scope gets a one-tap chip to add the rest of the trip."""
    scope = brief.values().get("scope") or []
    if scope and set(scope) != set(SCOPE_FULL) and not ({"trains", "stay", "transfers"} <= set(scope)):
        return chip_for(skill, brief, "scope")
    return None


# ---------------------------------------------------------------- questions (questions.json)

def _nearest_choice(choices, value):
    if value is None:
        return None
    for c in choices:
        if c.get("value") == value:
            return c
    nums = [c for c in choices if isinstance(c.get("value"), (int, float)) and not isinstance(c.get("value"), bool)]
    if nums and isinstance(value, (int, float)):
        return min(nums, key=lambda c: abs(c["value"] - value))
    return None


def build_question(skill, field, profile_value=None, pattern_value=None, reason=None):
    """Question for an askable field with the recommended choice per questions.json `recommend`."""
    spec = (skill.questions or {}).get(field)
    if not spec:
        return None
    rec = spec.get("recommend") or {}
    recommended = None
    for src in rec.get("from") or []:
        candidate = profile_value if src == "profile" else pattern_value if src == "pattern" else None
        recommended = _nearest_choice(spec["choices"], candidate)
        if recommended is not None:
            break
    if recommended is None and rec.get("default") is not None:
        recommended = _nearest_choice(spec["choices"], rec["default"])
    choices = [Choice(label=c["label"], value=c["value"], recommended=(recommended is c)) for c in spec["choices"]]
    return Question(id=spec["id"], field=field, text=spec["text"], choices=choices, reason=reason)


def recommended_value(skill, field, profile_value=None, pattern_value=None):
    q = build_question(skill, field, profile_value, pattern_value)
    if q is None:
        return None
    rec = next((c for c in q.choices if c.recommended), None)
    return rec.value if rec else None


def question_brief(q):
    return {"id": q.id, "text": q.text}


# ---------------------------------------------------------------- shown labels

def record_shown(session, kind, entries):
    """entries: [(id, title)] in display order -> labels "1".."n" in session.shown.<kind>s."""
    items = [ShownItem(label=str(i + 1), kind=kind, id=str(eid), title=str(title or eid))
             for i, (eid, title) in enumerate(entries)]
    setattr(session.shown, f"{kind}s", items)
    return {i.id: i.label for i in items}


# ---------------------------------------------------------------- card inputs

def common(session, chips=()):
    return {"today": session.today().isoformat(), "stage": session.stage, "brief": session.brief.values(),
            "assumed": assumed_paths(session.brief),
            "assumptions": [{"field": c.field, "label": c.label} for c in chips]}


def history_items(session):
    """≤ 6 user + 3 agent messages, oldest first, interleaved from the most recent end."""
    users, agents = list(session.history.user), list(session.history.agent)
    out, agent_turn = [], True   # the newest history entry is the agent's reply to the last user message
    while users or agents:
        if (agent_turn and agents) or not users:
            out.append({"role": "agent", "text": agents.pop()})
        else:
            out.append({"role": "user", "text": users.pop()})
        agent_turn = not agent_turn
    return list(reversed(out))


def understand_input(session, message, has_image, chips=()):
    payload = common(session, chips)
    payload["shown"] = [{"label": i.label, "kind": i.kind, "id": i.id, "title": i.title}
                        for i in session.shown.destinations + session.shown.variants + session.shown.hotels]
    payload["pending_questions"] = [{"id": q.id, "field": q.field, "text": q.text,
                                     "choices": [c.label for c in q.choices]} for q in session.pending_questions]
    payload["history"] = history_items(session)
    payload["message"] = message or ""
    payload["has_image"] = bool(has_image)
    return payload


def destinations_input(session, items, questions, image=None, insight=None, chips=()):
    payload = common(session, chips)
    payload["items"] = items
    payload["questions"] = [question_brief(q) for q in questions]
    payload["image"] = image
    payload["insight"] = insight
    return payload


def _note(n):
    return n.model_dump(mode="json")


def step_item(step):
    price = step.price
    rub = price.rub if price.rub is not None else (price.amount if price.currency == "RUB" else None)
    return {"type": step.type, "from": step.from_, "to": step.to, "start": step.start, "end": step.end,
            "carrier": step.carrier, "price_rub": int(round(rub)) if rub is not None else None}


def variant_item(variant, label, district_counts=None):
    hotel = None
    if variant.hotel is not None:
        hotel = {"name": variant.hotel.name, "stars": variant.hotel.stars, "rating": variant.hotel.rating,
                 "district": {"labels": list(variant.hotel.district.labels), "counts": district_counts or {}}}
    return {"id": variant.id, "label": label, "title": variant.title or VARIANT_TITLES.get(variant.id, variant.id),
            "totals": variant.totals.model_dump(mode="json"), "steps": [step_item(s) for s in variant.steps],
            "hotel": hotel, "facts": dict(variant.facts), "sacrifices": [_note(n) for n in variant.sacrifices],
            "risks": [_note(n) for n in variant.risks], "not_for": [_note(n) for n in variant.not_for],
            "fit": variant.fit.model_dump(mode="json") if variant.fit else None}


def upsell_item(c):
    return {"candidate_id": c.id, "variant_id": getattr(c, "variant_id", None), "kind": c.kind, "label": c.label,
            "price_rub": c.price_rub, "reason": c.reason}


def variants_input(session, variant_items, recommended_hint, questions, upsell, chips=(), changes=None, rebuilt=None):
    payload = common(session, chips)
    payload["items"] = variant_items
    payload["recommended_hint"] = recommended_hint
    payload["questions"] = [question_brief(q) for q in questions]
    payload["upsell_candidates"] = [upsell_item(c) for c in upsell]
    if changes is not None:
        payload["changes"] = changes
        payload["rebuilt"] = list(rebuilt or [])
    return payload


def compare_input(session, variant_items, dimensions, chips=()):
    payload = common(session, chips)
    payload["items"] = variant_items
    payload["dimensions"] = list(dimensions)
    return payload


def info_input(session, question, facts, chips=()):
    payload = common(session, chips)
    payload["question"] = question or ""
    payload["facts"] = facts
    return payload


def aftercare_input(session, order, result, chips=()):
    payload = common(session, chips)
    payload["order"] = order
    payload["result"] = result
    return payload


def fallback_input(session, message, reason, chips=()):
    payload = common(session, chips)
    payload["message"] = message or ""
    payload["reason"] = reason
    return payload


def compare_dimensions(brief):
    """What the user asked for (source user/profile) first, then the rest in a fixed order."""
    v = brief.values()
    own = {k: (val if brief.source_of(f"prefs.{k}") in ("user", "profile") else None)
           for k, val in (v.get("prefs") or {}).items()}
    prefs = own
    first = []
    if (v.get("budget_rub") or {}).get("max"):
        first.append("price")
    if prefs.get("max_travel_hours") or prefs.get("direct_only"):
        first.append("door_to_door")
    if prefs.get("meals") not in (None, "any"):
        first.append("meals")
    if prefs.get("free_cancellation"):
        first.append("cancellation")
    if prefs.get("district") not in (None, "any"):
        first.append("district")
    if prefs.get("stars_min"):
        first.append("hotel")
    rest = [d for d in ("price", "door_to_door", "hotel", "meals", "cancellation", "district") if d not in first]
    return first + rest
