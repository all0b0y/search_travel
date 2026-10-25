"""Guide mode (AGENT_PLAN.md decisions 23-26, «Режим «гид»»), behind the flag `guide`.

On a free-text turn the pipeline's route runs as before (the default plan), but instead of its `present` card a
model (card `guide`, prompt skills/trip-agent/guide.md; reasoning = rules.json guide.thinking, "off") sees a compact context and the default plan's
results, may call the engine's steps as tools (at most `guide.max_tool_rounds` rounds), and writes the reply. Code
keeps the numbers, the cards, the actions and the checks:

    ok = run_guide(engine, ctx, failure=None, code_message=None)   # False -> the pipeline path for this turn

Tools (each on a copy of the session — the real session changes only when the reply is accepted):
- rebuild {set, unset}: the trip rebuilt with brief overrides (allowed patch paths) -> variants;
- find_savings {from_id?}: levers priced by code from the current trip — the best start day within ±3 days, one
  night fewer (two if ≥ 5 nights), a cheaper hotel (stars −1, any district, no free cancellation, any meals),
  fewer nights + a cheaper hotel, cheaper transport (connecting / train), up to 3 similar cheaper places;
- check_road {to?, from?, date?, back_date?}: flights (direct / connecting) and trains for a pair and dates;
- find_destinations {set, unset}: places for wishes / budget / month / transport overrides;
- place_info {question, destination?}: sights, distances, climate and best time, hotel terms;
- compare {ids}: two or three variants side by side.
Variants a tool builds get ids custom-11, custom-12, …; the ones the reply shows become the shown set.

Any guide failure (schema, timeout, a tool error, the turn budget, a reply the guards reject twice) returns False
with the reason in ctx.trace["guide"]["fallback"]; the engine then presents the default plan as today.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import payloads as P
from . import steps as S
from .hooks import as_issue, blocking, issue
from .llm import LLMError, LLMRequest
from .mcp_client import ToolError
from .state import (ALLOWED_PATCH_PATHS, ALLOWED_UNSET_PATHS, OBJECT_FIELDS, Choice, PatchError, Question, apply_patch,
                    assume)

CARD = "guide"
TOOLS = ("rebuild", "find_savings", "check_road", "find_destinations", "place_info", "compare")
DEFAULTS = {"turn_budget_s": 15, "fallback_reserve_s": 3, "speculative_present": True, "speculative_reserve_s": 0.5,
            "speculative_wait_min_s": 3, "max_tool_rounds": 2, "max_calls_per_round": 3,
            "max_context_chars": 24000, "history_exchanges": 4, "agent_text_chars": 300, "message_chars": [40, 1100],
            "max_questions": 1, "savings_shift_days": 3, "savings_places": 3, "auto_tools": {"cheaper": "find_savings"},
            "cards": ["present.variants", "present.update", "present.destinations", "present.compare", "present.info",
                      "present.fallback"]}
FIRST_TOOL_ID = 11
REBUILD_STEPS = ("apply_patch", "search", "build_variants", "check", "upsell")
DEST_STEPS = ("apply_patch", "find_destinations", "quote_destinations")
LEVER_TITLES = {"dates": "Другие даты", "nights_1": "На ночь короче", "nights_2": "На 2 ночи короче",
                "hotel": "Отель попроще", "nights_hotel": "Короче и проще", "connecting": "С пересадкой",
                "train": "Поездом"}
# variant facts the guide sees (truthy values only, except the ones whose false matters)
VARIANT_FACT_KEYS = ("budget_fit", "budget_rub", "budget_dropped_prefs", "dates_shifted_days", "nights_changed_from",
                     "transport_gap_ru", "out_road_ru", "back_road_ru", "out_on_road_ru", "back_on_road_ru",
                     "out_long_train", "back_long_train", "out_leave_ru", "back_home_ru", "out_days_before_checkin",
                     "out_pets_rule", "back_pets_rule", "price_pets_rub", "near_name", "distance_near_m", "dorm_bed",
                     "out_night", "back_night", "out_high_speed", "back_high_speed", "kitchen", "lift", "step_free",
                     "infant_cot", "no_transport", "price_stay_rub", "lever", "saving_rub")
KEEP_FALSE = ("free_cancellation",)
GUARD_DROP = {"message_long", "message_longish", "message_short", "message_empty", "extra_question",
              "too_many_questions"}
NOT_FOUND_CHEAPER_RE = re.compile(r"дешевле\s+(?:\S+\s+){0,2}(?:не\s+нашл|нет\b|не\s+найт|не\s+бывает)|"
                                  r"не\s+нашл\w*\s+(?:\S+\s+){0,2}дешевле|ничего\s+дешевле", re.I)


class GuideFailed(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ArgError(ValueError):
    pass


def settings(rules):
    return {**DEFAULTS, **((rules or {}).get("guide") or {})}


def eligible(engine, ctx, route):
    """A free-text turn (no button, no Jev-routed reply) whose route ends in a present card the guide may replace."""
    if not getattr(engine.flags, "guide", False) or ctx.action is not None or not ctx.text:
        return False
    if route is None or not route.present:
        return False
    try:
        engine.skill.card(CARD)
    except KeyError:
        return False
    return route.present in settings(engine.skill.rules)["cards"]


# ---------------------------------------------------------------- compact views

def _title(v):
    return v.title or P.VARIANT_TITLES.get(v.id, v.id)


def _dates_of(v):
    f = v.facts
    if f.get("check_in_date") and f.get("check_out_date"):
        return f["check_in_date"], f["check_out_date"]
    legs = [s for s in v.steps if s.type in ("flight", "train", "stay")]
    if not legs:
        return None, None
    return legs[0].start[:10], legs[-1].end[:10]


def _road(v, way):
    f = v.facts
    kind = f.get(f"{way}_kind")
    if not kind:
        return None
    out = {"kind": kind, "carrier": f.get(f"{way}_carrier"), "stops": f.get(f"{way}_stops"),
           "date": f.get(f"{way}_date"), "dep": f.get(f"{way}_dep_time"), "arr": f.get(f"{way}_arr_time"),
           "on_board_hours": f.get(f"{way}_duration_hours"), "door_hours": f.get(f"{way}_door_hours"),
           "price_rub": f.get(f"{way}_price_rub")}
    if f.get(f"{way}_arr_date") and f.get(f"{way}_arr_date") != f.get(f"{way}_date"):
        out["arr_date"] = f[f"{way}_arr_date"]
    return {k: v for k, v in out.items() if v is not None}


def compact_variant(v, base_rub=None):
    f = v.facts
    start, end = _dates_of(v)
    out = {"id": v.id, "title": _title(v), "total_rub": int(v.totals.rub)}
    if base_rub is not None and int(v.totals.rub) != int(base_rub):
        diff = int(v.totals.rub) - int(base_rub)
        out["vs_recommended"] = f"{'дешевле' if diff < 0 else 'дороже'} на {abs(diff)}"
    if start:
        out["dates"] = f"{start}..{end}"
    if f.get("nights"):
        out["nights"] = f["nights"]
    for way in ("out", "back"):
        r = _road(v, way)
        if r:
            out[way] = r
    if v.hotel is not None:
        out["hotel"] = {k: val for k, val in {
            "name": v.hotel.name, "stars": v.hotel.stars, "rating": v.hotel.rating, "meals": f.get("meals_ru"),
            "free_cancellation": f.get("free_cancellation"), "center_m": f.get("distance_center_m"),
            "district": [P.DISTRICT_RU.get(x, x) for x in v.hotel.district.labels][:2] or None}.items()
            if val is not None}
    extra = {k: f[k] for k in VARIANT_FACT_KEYS if f.get(k) not in (None, False, [], "", 0)}
    extra.update({k: f[k] for k in KEEP_FALSE if k in f and k not in out.get("hotel", {})})
    if extra:
        out["facts"] = extra
    if v.risks:
        out["risks"] = [n.text for n in v.risks][:3]
    if v.sacrifices:
        out["gives_up"] = [n.text for n in v.sacrifices][:3]
    return out


def compact_destination(i):
    f = i.get("facts") or {}
    out = {"id": i["id"], "name": i["name"], "country": i.get("country"), "price_from_rub": i.get("price_from_rub"),
           "nights": i.get("nights"), "travel_time_h": i.get("travel_time_h"), "weather": i.get("weather"),
           "match": i.get("match") or None}
    for k in ("transport_ru", "price_includes", "within_budget", "road_ru", "description"):
        if f.get(k) not in (None, ""):
            out[k] = f[k] if k != "description" else str(f[k])[:160]
    return {k: v for k, v in out.items() if v is not None}


def prune(obj, depth=0, max_str=220, max_list=6):
    """A generic compact copy: no null / false / empty values, long strings cut, long lists cut, depth ≤ 4."""
    if isinstance(obj, dict):
        if depth > 4:
            return None
        out = {}
        for k, v in obj.items():
            if k in ("image_url", "photos", "links", "osm_id", "source", "fetched_at", "offer_id", "hotel_id"):
                continue
            pv = prune(v, depth + 1, max_str, max_list)
            if pv not in (None, False, [], {}, ""):
                out[k] = pv
        return out
    if isinstance(obj, (list, tuple)):
        return [x for x in (prune(v, depth + 1, max_str, max_list) for v in list(obj)[:max_list])
                if x not in (None, [], {}, "")]
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str].rsplit(" ", 1)[0] + "…"
    if isinstance(obj, float):
        return round(obj, 1)
    return obj


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def screen_snapshot(session):
    """What the person sees before this turn: the shown variants / places with key facts, cart / order."""
    s = session
    out = {"stage": s.stage}
    vs = [(i, s.variant(i.id)) for i in s.shown.variants]
    if vs:
        out["variants"] = [{"label": i.label, **compact_variant(v)} if v is not None else
                           {"label": i.label, "id": i.id, "title": i.title} for i, v in vs]
        for x in out["variants"]:
            x.pop("gives_up", None)
    if s.shown.destinations:
        prices = {d["id"]: d for d in S.remembered_destinations(s)}
        out["destinations"] = [{"label": i.label, "id": i.id, "name": i.title,
                                **({"price_from_rub": prices[i.id].get("price_from_rub")} if i.id in prices else {})}
                               for i in s.shown.destinations][:6]
    if s.cart_id:
        out["cart"] = {"variant_id": s.chosen_variant_id}
    if s.order_id:
        out["order_id"] = s.order_id
    return out


def history_view(session, exchanges, agent_chars):
    items = P.history_items(session)[-2 * exchanges:]
    out = []
    for h in items:
        text = h["text"]
        if h["role"] == "agent" and len(text) > agent_chars:
            text = text[:agent_chars].rsplit(" ", 1)[0] + "…"
        out.append({"role": h["role"], "text": text})
    return out


def default_plan(engine, ctx, failure=None, code_message=None):
    """The pipeline's results this turn, compacted (what present would have been given)."""
    from .engine import card_hint
    s = ctx.session
    d = {"route": ctx.trace.get("route"), "card": ctx.present, "status": "ok"}
    if failure is not None:
        d.update(status="failed", failure=failure.message)
    if code_message:
        d.update(status="code_answer", code_answer=code_message)
    if ctx.variants:
        hint = card_hint(s.brief, ctx.variants)
        base = next((v.totals.rub for v in ctx.variants if v.id == hint), None)
        d["variants"] = [compact_variant(v, base) for v in ctx.variants]
        d["recommended_id"] = hint
    if ctx.changes:
        d["changes"] = prune(ctx.changes[:8])
    if ctx.questions:
        d["questions"] = [{"field": q.field, "text": q.text, "choices": [{"label": c.label, "value": c.value}
                                                                         for c in q.choices]} for q in ctx.questions]
    if ctx.destination_items:
        d["destinations"] = [compact_destination(i) for i in ctx.destination_items]
    if ctx.info_facts:
        d["info_facts"] = prune(ctx.info_facts)
    if ctx.comparison:
        d["comparison"] = prune(ctx.comparison)
    if ctx.transport_gap:
        d["transport_gap"] = ctx.transport_gap.get("line")
    if ctx.other_places:
        d["other_places"] = prune(ctx.other_places)
    if ctx.notes:
        d["notes"] = list(ctx.notes)
    if ctx.unsold:
        d["not_sold"] = S.not_sold_fact(ctx.unsold)["line"]
    try:
        q = S.search_question(ctx)
    except Exception:   # noqa: BLE001 - a missing fact never blocks the guide
        q = None
    if q:
        d["question_facts"] = prune(q)
    wishes = [w for w, on in (("cheaper", ctx.ask_cheaper), ("other_dates", ctx.ask_other_dates),
                              ("other_place", ctx.ask_other_place), ("best_time", ctx.best_time_asked)) if on]
    if wishes:
        d["wishes"] = wishes
    buttons = [a.get("label") for a in ctx.extra_actions or [] if a.get("label")]
    if buttons:
        d["buttons"] = buttons[:4]
    return d


# ---------------------------------------------------------------- state of one guide turn

@dataclass
class ToolSet:
    """What one tool call built: variants (with the brief they were built from), places, a comparison."""
    call: int
    tool: str
    variants: list = field(default_factory=list)
    brief: object = None
    results: dict = None
    districts: dict = None
    upsell: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    briefs: dict = field(default_factory=dict)        # per variant id (savings levers differ)
    orig_ids: dict = field(default_factory=dict)      # custom id -> the build's own id (rebuild)
    destinations: list = field(default_factory=list)
    comparison: dict = None
    places: list = field(default_factory=list)
    result: dict = None
    result_variants: list = field(default_factory=list)   # compare: the variants compared
    lever_ms: dict = field(default_factory=dict)           # find_savings: time per lever (trace only)


@dataclass
class GuideState:
    default_variants: list
    default_brief: object
    screen_variants: list = field(default_factory=list)   # the session's variants (an info turn builds none)
    sets: list = field(default_factory=list)
    next_id: int = FIRST_TOOL_ID
    calls: int = 0

    def new_id(self):
        vid = f"custom-{self.next_id}"
        self.next_id += 1
        return vid

    def variant(self, vid):
        """-> (variant, its set: a ToolSet, or None for the default plan's / the screen's) or (None, None)."""
        for v in self.default_variants or self.screen_variants:
            if v.id == vid:
                return v, None
        for t in reversed(self.sets):
            for v in t.variants:
                if v.id == vid:
                    return v, t
        return None, None

    def base_variants(self):
        return self.default_variants or self.screen_variants


def _sub_ctx(ctx, brief=None):
    """A copy of the turn for a tool: own session copy (brief, variants, shown, results dict) — searches are cached
    in it and merged back as cache; nothing else reaches the real session unless the reply is accepted."""
    from .steps import Ctx
    s = ctx.session
    s2 = s.model_copy(update={"brief": brief if brief is not None else s.brief, "results": dict(s.results),
                              "variants": [v.model_copy(deep=True) for v in s.variants],
                              "shown": s.shown.model_copy(deep=True), "trace": [],
                              "pending_questions": list(s.pending_questions)})
    c = Ctx(engine=ctx.engine, session=s2, request=ctx.request, text=ctx.text)
    c.profile = ctx.profile
    c.understand = ctx.understand
    c.intent = "refine"
    return c


def _patch_of(args, allowed_set=ALLOWED_PATCH_PATHS):
    if not isinstance(args, dict):
        raise ArgError("args must be an object")
    sets = args.get("set") or {}
    unsets = args.get("unset") or []
    if not isinstance(sets, dict) or not isinstance(unsets, list):
        raise ArgError("set must be an object and unset a list")
    bad = [k for k in sets if k not in allowed_set] + [k for k in unsets if k not in ALLOWED_UNSET_PATHS]
    if bad:
        raise ArgError(f"unknown paths {bad}; allowed: brief patch paths")
    return {"set": dict(sets), "unset": list(unsets)}


def _base_dates(state):
    vs = state.base_variants()
    for v in vs:
        a, b = _dates_of(v)
        if a and b:
            return date.fromisoformat(a), date.fromisoformat(b)
    return None


def _known_destination(sub, patch):
    """A destination named in tool args must be in the catalog already: a tool never starts the network lazy-city
    build (up to a minute) inside the 15 s turn. -> None when fine, else the unknown name."""
    name = (patch.get("set") or {}).get("destination.name")
    if not name or S.wish_traits(str(name)) is not None:   # «море» is a wish: apply_patch turns it into traits
        return None
    return None if S.resolve_destination(sub, str(name), allow_lazy=False) is not None else str(name)


def _reconcile(patch, brief, state):
    sets, unsets = patch["set"], patch["unset"]
    k = sets.pop("dates.shift_days", None)
    if isinstance(k, int):
        span = _base_dates(state)
        if span is None:
            raise ArgError("dates.shift_days needs a trip with dates")
        sets["dates.start"] = (span[0] + timedelta(days=k)).isoformat()
        sets["dates.end"] = (span[1] + timedelta(days=k)).isoformat()
    if "dates.end" not in sets and "dates.end" not in unsets and ("dates.nights" in sets or "dates.start" in sets):
        if (brief.values().get("dates") or {}).get("end"):
            unsets.append("dates.end")
    return patch


def as_proposals(before, after, guide_patch, turn_patch):
    """The guide's own changes stay proposals: apply_patch marks every touched field source="user", so a field the
    guide's patch touched gets source "inferred" again when its value differs from before the tool (or was not the
    person's before) — unless this turn's understood patch said exactly that value. Later turns, chips and the
    budget fit then treat it as the agent's offer, not the person's words («только прямые» said this turn and
    rebuilt with connecting flights by the guide: direct_only false is inferred)."""
    t_set, t_unset = (turn_patch or {}).get("set") or {}, set((turn_patch or {}).get("unset") or [])

    def said(path, value):
        return (path in t_set and t_set[path] == value) or (path in t_unset and value is None)

    paths = list((guide_patch or {}).get("set") or {}) + list((guide_patch or {}).get("unset") or [])
    bv, av = before.values(), after.values()
    out = after
    for top in dict.fromkeys(p.partition(".")[0] for p in paths):
        if top in OBJECT_FIELDS and top not in ("origin", "destination"):
            for sub, value in (av.get(top) or {}).items():
                path = f"{top}.{sub}"
                if out.source_of(path) != "user" or said(path, value):
                    continue
                if (bv.get(top) or {}).get(sub) != value or before.source_of(path) != "user":
                    out = assume(out, path, value, "inferred")
        elif av.get(top) not in (None, []) and out.source_of(top) == "user" and (
                bv.get(top) != av.get(top) or before.source_of(top) != "user"):
            name = {"destination": "destination.name", "origin": "origin.city"}.get(top, top)
            if said(name, (av[top] or {}).get(name.partition(".")[2]) if top in ("origin", "destination")
                    else av[top]):
                continue
            out = assume(out, top, av[top], "inferred")
    return out


def _relabel(variants, state, titles=None):
    out, ids = [], {}
    for v in variants:
        nid = state.new_id()
        ids[nid] = v.id
        cands = [c.model_copy(update={"variant_id": nid}) for c in v.upsell_candidates]
        out.append(v.model_copy(update={"id": nid, "title": (titles or {}).get(v.id) or _title(v),
                                        "upsell_candidates": cands}))
    return out, ids


def _hint(brief, variants):
    from .engine import card_hint
    return card_hint(brief, variants) if variants else None


def _variants_result(brief, variants, extra=None):
    hint = _hint(brief, variants)
    base = next((v.totals.rub for v in variants if v.id == hint), None)
    out = {"ok": True, "variants": [compact_variant(v, base) for v in variants], "recommended_id": hint}
    out.update(extra or {})
    return out


# ---------------------------------------------------------------- tools

def tool_rebuild(engine, ctx, args, state, call):
    patch = _patch_of(args)
    sub = _sub_ctx(ctx)
    unknown = _known_destination(sub, patch)
    if unknown:
        return {"ok": False, "why": f"«{unknown}» нет в каталоге"}, None, sub
    sub.patch = _reconcile(patch, sub.session.brief, state)
    before = sub.session.brief
    S.STEP_REGISTRY["apply_patch"](sub)
    sub.session.brief = as_proposals(before, sub.session.brief, sub.patch, ctx.patch)
    if not sub.session.brief.has("destination"):
        why = (f"«{sub.unknown_destination}» нет в каталоге" if sub.unknown_destination else
               "нет направления — для выбора места есть find_destinations")
        return {"ok": False, "why": why}, None, sub
    try:
        for name in REBUILD_STEPS[1:]:
            S.STEP_REGISTRY[name](sub)
    except S.StepError as exc:
        if exc.code != "no_data":
            raise
        return {"ok": False, "why": exc.message}, None, sub
    variants, ids = _relabel(sub.variants, state)
    brief = sub.session.brief
    new_of = {o: n for n, o in ids.items()}
    upsell = []
    for u in sub.upsell:
        vid = getattr(u, "variant_id", None)
        if vid in new_of and hasattr(u, "model_copy"):
            upsell.append(u.model_copy(update={"variant_id": new_of[vid]}))
    tset = ToolSet(call=call, tool="rebuild", variants=variants, brief=brief, results=sub.results,
                   districts=sub.districts, changes=list(sub.changes), orig_ids=ids, upsell=upsell,
                   briefs={v.id: brief for v in variants})
    extra = {"changes": prune(sub.changes[:8])} if sub.changes else {}
    if sub.notes:
        extra["notes"] = list(sub.notes)
    return _variants_result(brief, variants, extra), tset, sub


def _cheapest(vs):
    return min(vs, key=lambda v: v.totals.rub) if vs else None


def _day(iso):
    d = date.fromisoformat(str(iso)[:10])
    return f"{d.day} {P.MONTHS_GEN[d.month - 1]}"


def _gives_up_hotel(base, v):
    """What the cheaper hotel gives up against the base, in plain Russian (code facts only)."""
    bf, f = base.facts, v.facts
    out = []
    if base.hotel is not None and v.hotel is not None:
        if (v.hotel.stars or 0) < (base.hotel.stars or 0):
            n = v.hotel.stars
            out.append(f"отель на {n} {P.plural(n, 'звезду', 'звезды', 'звёзд')} вместо {base.hotel.stars}" if n
                       else f"отель без звёзд вместо {base.hotel.stars}")
        if v.hotel.rating is not None and base.hotel.rating is not None and v.hotel.rating < base.hotel.rating:
            out.append(f"рейтинг {v.hotel.rating} вместо {base.hotel.rating}")
    if bf.get("meals") not in (None, "none", "any") and f.get("meals") != bf.get("meals"):
        out.append(f"{f.get('meals_ru') or 'без питания'} вместо «{bf.get('meals_ru')}»")
    if bf.get("free_cancellation") and not f.get("free_cancellation"):
        out.append("без бесплатной отмены")
    bc, c = bf.get("distance_center_m"), f.get("distance_center_m")
    if isinstance(bc, (int, float)) and isinstance(c, (int, float)) and c - bc >= 1000:
        out.append(f"до центра {_km(c)} км вместо {_km(bc)}")
    return out


def _km(metres):
    return f"{round(metres / 1000, 1):.1f}".replace(".", ",")


def _road_hours(v):
    t = v.totals
    return t.out_hours, t.back_hours


def _road_words(v):
    """«туда 3,1 ч, обратно 2,9 ч»; a long train in code's words («1 сутки 4 ч»), never decimal hours."""
    f, (oh, bh) = v.facts, _road_hours(v)
    out = f.get("out_on_road_ru") if f.get("out_long_train") else (P.fmt_hours(oh) if oh else None)
    back = f.get("back_on_road_ru") if f.get("back_long_train") else (P.fmt_hours(bh) if bh else None)
    if out and back:
        return f"туда {out}, обратно {back}"
    return out or back or ""


def _lever_variant(v, state, lever, base_total, change_ru, gives_up):
    nid = state.new_id()
    facts = dict(v.facts, lever=lever, saving_rub=int(base_total - v.totals.rub))
    return v.model_copy(update={"id": nid, "title": LEVER_TITLES[lever], "facts": facts}), {
        "lever": lever, "variant_id": nid, "total_rub": int(v.totals.rub), "saving_rub": int(base_total - v.totals.rub),
        "change_ru": change_ru, "gives_up_ru": "; ".join(gives_up) if gives_up else "ничего, кроме сказанного"}


def _price_parts(v):
    parts = {"tickets_rub": 0, "stay_rub": 0, "transfers_rub": 0}
    for st in v.steps:
        rub = st.price.rub if st.price.rub is not None else st.price.amount
        key = {"flight": "tickets_rub", "train": "tickets_rub", "stay": "stay_rub"}.get(st.type, "transfers_rub")
        parts[key] += int(rub or 0)
    return {k: val for k, val in parts.items() if val}


MAIN_TRAITS = ("sea", "mountains", "nature", "city")


def main_trait(rec):
    """A place's main trait: its catalog kind when that is one (city / nature), else the first of sea, mountains,
    nature, city among its tags (a resort by the sea -> sea, a ski village -> mountains)."""
    kind = (rec or {}).get("kind")
    if kind in MAIN_TRAITS:
        return kind
    tags = [t for t in S._json_list((rec or {}).get("tags")) if isinstance(t, str)]
    return next((t for t in MAIN_TRAITS if t in tags), kind)


def same_class(cand, rec, origin, brief):
    """A similar place for find_savings must be of the trip's class: abroad vs at home (the origin's country) and
    the same main trait — unless the person asked otherwise (prefs.domestic set by them: their wish decides home /
    abroad; traits they named: the candidate shares one of them)."""
    home = (origin or {}).get("country_code") or "RU"
    if brief.source_of("prefs.domestic") != "user" or (brief.values().get("prefs") or {}).get("domestic") is None:
        if ((cand.get("country_code") or "") != home) != ((rec.get("country_code") or "") != home):
            return False
    traits = brief.values().get("traits") or []
    if traits and brief.source_of("traits") == "user":
        tags = set(S._json_list(cand.get("tags")))
        return any(t in tags for t in traits)
    return main_trait(cand) == main_trait(rec)


def tool_find_savings(engine, ctx, args, state, call):
    """Levers priced by code from the current trip (the cheapest of the set the person sees, or `from_id`)."""
    cfg = settings(engine.skill.rules)
    args = args if isinstance(args, dict) else {}
    pool = None
    if args.get("from_id"):
        base_v, tset = state.variant(str(args["from_id"]))
        if base_v is None:
            raise ArgError(f"unknown variant id {args['from_id']}")
        pool = tset.variants if tset else state.base_variants()
        brief = (tset.briefs.get(base_v.id) or tset.brief) if tset else state.default_brief
    else:
        pool = state.base_variants()
        brief = state.default_brief
    pool = [v for v in pool if not v.facts.get("no_transport")] or pool
    if not pool:
        return {"ok": False, "why": "пока нет собранной поездки — сначала нужна поездка с датами"}, None, None
    base = _cheapest(pool) if not args.get("from_id") else state.variant(str(args["from_id"]))[0]
    start, end = _dates_of(base)
    if not (start and end) or not brief.has("destination"):
        return {"ok": False, "why": "у поездки нет дат или направления"}, None, None
    ci, co = date.fromisoformat(start), date.fromisoformat(end)
    nights = (co - ci).days
    total = int(base.totals.rub)
    probe = _sub_ctx(ctx, brief)
    dest = S.current_destination(probe)
    scope = (brief.values().get("scope") or list(P.SCOPE_FULL))
    prefs = brief.values().get("prefs") or {}
    road_kinds = {st.type for st in base.steps if st.type in ("flight", "train")}
    origin = S._origin_place(probe, brief)
    rec = S.destination_record(probe, dest_id=dest["id"]) or dest

    def build_on(sub, b, d_in, d_out, hotel_cap=None):
        r, d, _ = S._search_dates(sub, b, dest, d_in, d_out, hotel_cap=hotel_cap)
        return _cheapest(S._built(sub, b, r, d, pin=True))

    def lever_dates(sub):
        flex = int(cfg["savings_shift_days"])
        shifts = S._flex_shifts(sub, brief, dest, ci, co, flex, {"scope": scope})
        tried = [d for d, est in shifts if est is not None][:2] or [d for d, _ in shifts if abs(d) == 1][:2]
        best = None
        for d in tried:
            v = build_on(sub, brief, ci + timedelta(days=d), co + timedelta(days=d))
            if v is not None and (best is None or v.totals.rub < best[1].totals.rub):
                best = (d, v)
        if best is None:
            return None
        d, v = best
        v.facts["dates_shifted_days"] = d
        b2 = S._moved_brief(brief, ci + timedelta(days=d), co + timedelta(days=d), ci, co)
        return ("dates", v, b2, f"заезд {_day(ci + timedelta(days=d))} вместо {_day(ci)}, "
                                f"выезд {_day(co + timedelta(days=d))}", [f"даты сдвигаются на {abs(d)} дн."])

    def lever_nights(k):
        def run(sub):
            if nights - k < 1:
                return None
            b2 = assume(brief, "dates.nights", nights - k, "inferred")
            b2 = assume(b2, "dates.end", (co - timedelta(days=k)).isoformat(), "inferred")
            v = build_on(sub, b2, ci, co - timedelta(days=k))
            if v is None:
                return None
            v.facts["nights_changed_from"] = nights
            word = P.plural(nights - k, "ночь", "ночи", "ночей")
            return (f"nights_{k}", v, b2, f"{nights - k} {word} вместо {nights}, обратно {_day(co - timedelta(days=k))}",
                    [f"на {k} {P.plural(k, 'ночь', 'ночи', 'ночей')} меньше"])
        return run

    def cheap_brief(b):
        b2, _ = S._comfort_free_brief(b)
        p = b2.values().get("prefs") or {}
        if p.get("stars_min"):
            b2 = assume(b2, "prefs.stars_min", max(1, int(p["stars_min"]) - 1) if int(p["stars_min"]) > 2 else None,
                        "inferred")
        for key, default in (("district", "any"), ("free_cancellation", None), ("meals", "any")):
            if p.get(key) not in (default, None):
                b2 = assume(b2, f"prefs.{key}", default, "inferred")
        return b2

    stay_rub = int(base.facts.get("price_stay_rub") or 0)

    def lever_hotel(sub):
        if "stay" not in scope or not stay_rub:
            return None
        b2 = cheap_brief(brief)
        v = build_on(sub, b2, ci, co, hotel_cap=stay_rub - 1)
        if v is None:
            return None
        hotel = v.hotel.name if v.hotel else "другой отель"
        return ("hotel", v, b2, f"отель «{hotel}» вместо «{base.hotel.name if base.hotel else '—'}»",
                _gives_up_hotel(base, v))

    def lever_nights_hotel(sub):
        if "stay" not in scope or nights < 3 or not stay_rub:
            return None
        b2 = cheap_brief(assume(assume(brief, "dates.nights", nights - 1, "inferred"), "dates.end",
                                (co - timedelta(days=1)).isoformat(), "inferred"))
        v = build_on(sub, b2, ci, co - timedelta(days=1), hotel_cap=stay_rub - 1)
        if v is None:
            return None
        v.facts["nights_changed_from"] = nights
        return ("nights_hotel", v, b2, f"{nights - 1} {P.plural(nights - 1, 'ночь', 'ночи', 'ночей')} вместо "
                                       f"{nights} и отель «{v.hotel.name if v.hotel else '—'}»",
                ["на ночь меньше"] + _gives_up_hotel(base, v))

    def lever_connecting(sub):
        if "flight" not in road_kinds or not prefs.get("direct_only"):
            return None
        b2 = assume(brief, "prefs.direct_only", False, "inferred")
        v = build_on(sub, b2, ci, co)
        if v is None:
            return None
        road = _road_words(v)
        return ("connecting", v, b2, "рейсы с пересадкой", [f"с пересадкой: {road}" if road else "с пересадкой"])

    def lever_train(sub):
        if "flight" not in road_kinds or prefs.get("transport") == "plane" or not S.train_reachable(origin, rec):
            return None
        b2 = S.normalize_brief(sub, assume(brief, "prefs.transport", "train", "inferred"))
        v = build_on(sub, b2, ci, co)
        if v is None:
            return None
        road = _road_words(v)
        leave = v.facts.get("out_leave_ru")
        return ("train", v, b2, "поездом вместо самолёта" + (f", {leave}" if leave else ""),
                [f"дорога дольше: {road}" if road else "поезд"])

    def places(sub):
        """Similar places (the destination's tags as wishes; its country first) priced for the same dates and
        party, cheaper than the trip: a small pool (6 by plane, 3 by train) keeps it fast."""
        b = brief.values()
        traits = [t for t in S._json_list(rec.get("tags")) if isinstance(t, str)]
        found = S.od(sub, "find_destinations", traits=traits, month=ci.month, origin_city=(b.get("origin") or {}).get(
            "city"), budget=total - 1, max_travel_hours=prefs.get("max_travel_hours"),
            segment=(b.get("party") or {}).get("segment"), limit=60) or []
        train_only, domestic = S._dest_prefs(brief)
        recs = [r for r in (S.norm_destination(x) for x in found) if r and r.get("id") and r["id"] != dest["id"]]
        recs = [r for r in recs if S._passes_dest_prefs(r, origin, train_only, domestic, sub, nights)]
        recs = [r for r in recs if same_class(r, rec, origin, brief)]
        recs.sort(key=lambda r: r.get("country_code") != rec.get("country_code"))
        dates = (ci.isoformat(), co.isoformat())
        plane = [] if train_only else [r for r in recs if S._json_list(r.get("airports"))][:6]
        trains = [r for r in recs if origin and S.train_reachable(origin, r)][:3]
        p_prices = S.destination_prices(sub, brief, plane, train=False, dates_override=dates)
        t_prices = S.destination_prices(sub, brief, trains, train=True, dates_override=dates)
        out = []
        for r in {x["id"]: x for x in plane + trains}.values():
            e = S._cheaper_quote(p_prices.get(r["id"]), t_prices.get(r["id"]), total - 1)
            if e is None or e["price"] is None or e["price"] >= total:
                continue
            f = e["facts"]
            out.append({"id": r["id"], "name": r["name"], "price_from_rub": int(e["price"]), "nights": e["nights"],
                        "transport_ru": f.get("transport_ru"), "transport": f.get("transport"),
                        "price_includes": f.get("price_includes"), "within_budget": True,
                        "saving_rub": total - int(e["price"]), "same_country": r.get("country_code") == rec.get(
                            "country_code")})
        out.sort(key=lambda p: (not p["same_country"], p["price_from_rub"]))
        return out[:int(cfg["savings_places"])]

    levers = [lever_dates, lever_nights(1), lever_hotel, lever_connecting, lever_train]
    if nights >= 5:
        levers.insert(2, lever_nights(2))
    if nights >= 3:
        levers.append(lever_nights_hotel)
    def timed(fn, sub):
        t0 = time.perf_counter()
        try:
            return fn(sub), round((time.perf_counter() - t0) * 1000, 1)
        except S.StepError:   # a lever whose trip does not build (no flights that day...) gives nothing
            return None, round((time.perf_counter() - t0) * 1000, 1)

    jobs = [(fn, _sub_ctx(ctx, brief)) for fn in levers] + [(places, _sub_ctx(ctx, brief))]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = [(fn, sub, ex.submit(timed, fn, sub)) for fn, sub in jobs]
        done = [(fn, sub, *fut.result()) for fn, sub, fut in futures]
    found, not_cheaper, place_list, subs, seen = [], [], [], [], set()
    lever_ms = {}
    for fn, sub, out, ms in done:
        subs.append(sub)
        lever_ms[getattr(fn, "__name__", "lever")] = ms
        if fn is places:
            place_list = out or []
            continue
        if out is None:
            continue
        lever, v, b2, change_ru, gives_up = out
        lever_ms[lever] = lever_ms.pop(getattr(fn, "__name__", "lever"), ms)
        if v.totals.rub >= total:
            not_cheaper.append(LEVER_TITLES[lever])
            continue
        found.append((lever, v, b2, change_ru, gives_up))
    found.sort(key=lambda x: x[1].totals.rub)
    unique = []
    for row in found:   # two levers that built the very same trip: the simpler one (listed first) stays
        sig = tuple(st.offer_id for st in row[1].steps)
        if sig not in seen:
            seen.add(sig)
            unique.append(row)
    found = unique
    tset = ToolSet(call=call, tool="find_savings", places=place_list)
    levers_out = []
    for lever, v, b2, change_ru, gives_up in found:
        nv, row = _lever_variant(v, state, lever, total, change_ru, gives_up)
        tset.variants.append(nv)
        tset.briefs[nv.id] = b2
        levers_out.append({**row, "variant": compact_variant(nv)})
    tset.brief = tset.briefs[tset.variants[0].id] if tset.variants else brief
    result = {"ok": True, "current": {"id": base.id, "title": _title(base), "total_rub": total, "nights": nights,
                                      "dates": f"{start}..{end}", "price_parts": _price_parts(base)},
              "levers": levers_out, "cheapest_lever_id": levers_out[0]["variant_id"] if levers_out else None,
              "places": [prune({k: p.get(k) for k in ("id", "name", "price_from_rub", "saving_rub", "nights",
                                                     "transport_ru", "price_includes")}) for p in place_list]}
    if not_cheaper:
        result["not_cheaper"] = not_cheaper
    tset.lever_ms = lever_ms
    if not levers_out and not place_list:
        result["summary_ru"] = "Дешевле этой поездки по датами, ночам, отелю, дороге и похожим местам код не нашёл"
    return result, tset, subs


def _airports_line(n, word):
    return f"{n} {P.plural(n, word[0], word[1], word[2])}"


def tool_check_road(engine, ctx, args, state, call):
    args = args if isinstance(args, dict) else {}
    sub = _sub_ctx(ctx)
    brief = sub.session.brief
    sets = {}
    if args.get("from"):
        sets["origin.city"] = str(args["from"])
    if args.get("to"):
        sets["destination.name"] = str(args["to"])
    if sets:
        try:
            brief, _, unknown = S.prepare_brief(sub, brief, {"set": sets, "unset": []}, allow_lazy=False)
        except PatchError as exc:
            raise ArgError(str(exc)) from exc
        if unknown:
            return {"ok": False, "why": f"«{unknown}» нет в каталоге"}, None, sub
        sub.session.brief = brief
    v = brief.values()
    dest = (v.get("destination") or {})
    if not dest.get("id"):
        return {"ok": False, "why": "не задано, куда ехать"}, None, sub
    rec = S.destination_record(sub, dest_id=dest["id"]) or {}
    span = _base_dates(state)
    day = args.get("date") or (span[0].isoformat() if span else (v.get("dates") or {}).get("start"))
    back = args.get("back_date") or (span[1].isoformat() if span and not args.get("date") else None) or (
        (v.get("dates") or {}).get("end") if not args.get("date") else None)
    if not day:
        raise ArgError("date is needed (YYYY-MM-DD)")
    try:
        d_out = date.fromisoformat(str(day)[:10])
        d_back = date.fromisoformat(str(back)[:10]) if back else None
    except ValueError as exc:
        raise ArgError(f"bad date: {exc}") from exc
    origin_city = (v.get("origin") or {}).get("city") or "город вылета"
    route = f"{origin_city} — {rec.get('name') or dest.get('name')}"
    o_iata, d_iata = S.resolve_origin_iata(sub), [a for a in S._json_list(rec.get("airports")) if a]
    adults, children, infants = S._flight_party(brief, d_out)
    out = {"ok": True, "route": route, "dest_airports": d_iata, "ways": []}
    lines = []

    def flights(frm, to, day_):
        if not (frm and to):
            return None
        payload = {"origin_iata": frm, "dest_iata": to, "date": day_.isoformat(), "adults": adults,
                   "children": children, "infants": infants, "flex_days": 0, "today": sub.session.today().isoformat()}
        offers = (S.tool(sub, "search_flights", payload) or {}).get("offers") or []
        offers = [o for o in offers if S._dt(o["dep"]).date() == day_]
        direct = [o for o in offers if not o.get("stops")]
        conn = [o for o in offers if o.get("stops")]
        info = {"direct": len(direct), "connecting": len(conn)}
        if direct:
            info["direct_from_rub"] = min(o["price_rub"] for o in direct)
            info["direct_fastest_hours"] = round(min(o["duration_min"] for o in direct) / 60, 1)
        if conn:
            info["connecting_from_rub"] = min(o["price_rub"] for o in conn)
        if not direct:
            flex = (S.tool(sub, "search_flights", {**payload, "flex_days": 3}) or {}).get("offers") or []
            days = sorted({S._dt(o["dep"]).date().isoformat() for o in flex if not o.get("stops")})
            if days:
                info["direct_days_nearby"] = days[:4]
        return info

    origin_rec = S._origin_place(sub, brief)
    can_train = bool(origin_rec) and S.train_reachable(origin_rec, rec)
    pax = adults + children + infants
    for way, day_, frm, to, tcity in (("out", d_out, o_iata, d_iata, (origin_city, rec.get("name"))),
                                       ("back", d_back, d_iata, o_iata, (rec.get("name"), origin_city))):
        if day_ is None:
            continue
        f = flights(frm, to, day_)
        row = {"way": way, "date": day_.isoformat(), "flights": f}
        if can_train:
            tr = (S.tool(sub, "search_trains", {"origin_city": tcity[0], "dest_city": tcity[1], "date": day_.isoformat(),
                                                "passengers": pax, "today": sub.session.today().isoformat()})
                  or {}).get("offers") or []
            if tr:
                row["trains"] = {"count": len(tr), "from_rub": min(o["price_rub"] for o in tr),
                                 "fastest_hours": round(min(o["duration_min"] for o in tr) / 60, 1)}
            else:
                row["trains"] = {"count": 0}
        out["ways"].append(row)
        part = f"{'туда' if way == 'out' else 'обратно'} {_day(day_)}: "
        if f is None:
            part += "аэропорта с рейсами в данных нет"
        elif f["direct"]:
            part += f"прямых {f['direct']} (от {P.fmt_rub(f['direct_from_rub'])})"
        else:
            part += "прямых нет" + (f" (прямые есть {', '.join(_day(x) for x in f['direct_days_nearby'])})"
                                    if f.get("direct_days_nearby") else "")
        if f is not None and f["connecting"]:
            part += f", с пересадкой {f['connecting']} (от {P.fmt_rub(f['connecting_from_rub'])})"
        if row.get("trains", {}).get("count"):
            part += f", поездов {row['trains']['count']} (от {P.fmt_rub(row['trains']['from_rub'])})"
        elif can_train:
            part += ", поездов нет"
        lines.append(part)
    if not can_train:
        out["trains_ru"] = "поездов по этому маршруту в демо нет"
    out["line_ru"] = f"{route}: " + "; ".join(lines) + ". Цены — за всех."
    return out, None, sub


def tool_find_destinations(engine, ctx, args, state, call):
    patch = _patch_of(args)
    sub = _sub_ctx(ctx)
    unknown = _known_destination(sub, patch)
    if unknown:
        return {"ok": False, "why": f"«{unknown}» нет в каталоге"}, None, sub
    if "destination.name" not in patch["set"] and "destination.name" not in patch["unset"]:
        patch["unset"].append("destination.name")
    sub.patch = patch
    before = sub.session.brief
    try:
        for name in DEST_STEPS:
            S.STEP_REGISTRY[name](sub)
            if name == "apply_patch":
                sub.session.brief = as_proposals(before, sub.session.brief, patch, ctx.patch)
    except S.StepError as exc:
        if exc.code != "no_data":
            raise
        return {"ok": False, "why": exc.message}, None, sub
    items = [{k: v for k, v in i.items() if k != "image_url"} for i in sub.destination_items]
    if not items:
        return {"ok": False, "why": "под эти условия мест не нашлось"}, None, sub
    tset = ToolSet(call=call, tool="find_destinations", destinations=sub.destination_items,
                   brief=sub.session.brief)
    return {"ok": True, "destinations": [compact_destination(i) for i in items]}, tset, sub


def tool_place_info(engine, ctx, args, state, call):
    args = args if isinstance(args, dict) else {}
    question = str(args.get("question") or ctx.text or "").strip()
    if not question:
        raise ArgError("question is needed")
    sub = _sub_ctx(ctx)
    if args.get("destination"):
        brief, _, unknown = S.prepare_brief(sub, sub.session.brief,
                                            {"set": {"destination.name": str(args["destination"])}, "unset": []},
                                            allow_lazy=False)
        if unknown:
            return {"ok": False, "why": f"«{unknown}» нет в каталоге"}, None, sub
        sub.session.brief = brief
    sub.understand = {"info_question": question}
    sub.text = question
    S.step_info_lookup(sub)
    facts = list(sub.info_facts)
    dest = (sub.session.brief.values().get("destination") or {})
    if dest.get("id"):
        rec = S.destination_record(sub, dest_id=dest["id"]) or {}
        facts.insert(0, {"kind": "place", "name": rec.get("name"), "country": rec.get("country"),
                         "kind_of_place": rec.get("kind"), "description": rec.get("description"),
                         "airports": S._json_list(rec.get("airports")), "stations": S._json_list(rec.get("stations"))})
        if not any(isinstance(f, dict) and f.get("kind") == "best_time" for f in facts):
            try:
                bt = S.best_time_fact(sub)
            except Exception:   # noqa: BLE001 - best time is optional here
                bt = None
            if bt:
                facts.append(bt)
    pruned = prune(facts, max_list=5)
    while len(_dumps(pruned)) > 3500 and len(pruned) > 1:
        pruned = pruned[:-1]
    return {"ok": True, "question": question, "facts": pruned}, None, sub


def tool_compare(engine, ctx, args, state, call):
    args = args if isinstance(args, dict) else {}
    ids = [str(i) for i in args.get("ids") or []][:3]
    found = [state.variant(i)[0] for i in ids]
    variants = [v for v in found if v is not None]
    if len(variants) < 2:
        raise ArgError(f"compare needs 2-3 known variant ids, got {ids}")
    sub = _sub_ctx(ctx)
    sub.session.variants = variants
    rows = S.comparison_rows(sub, variants)
    comp = {"variant_ids": [v.id for v in variants], "rows": rows, "recommended_id": S.recommended_hint(variants)}
    tset = ToolSet(call=call, tool="compare", comparison=comp, variants=[], result_variants=variants)
    return {"ok": True, "ids": comp["variant_ids"], "rows": prune(rows, max_list=10),
            "recommended_id": comp["recommended_id"]}, tset, sub


TOOL_FUNCS = {"rebuild": tool_rebuild, "find_savings": tool_find_savings, "check_road": tool_check_road,
              "find_destinations": tool_find_destinations, "place_info": tool_place_info, "compare": tool_compare}


def _merge_cache(ctx, subs):
    """Searches a tool made are cache for the rest of the session (never state: engine:* records stay out)."""
    main = ctx.session.results
    for sub in subs or []:
        if sub is None:
            continue
        for key, rec in sub.session.results.items():
            if key not in main and not key.startswith("engine:"):
                S.store_result(ctx.session, rec)
        ctx.trace["tools"].extend(dict(t, via="guide") for t in sub.trace["tools"])


def run_tools(engine, ctx, calls, state, deadline, g):
    """Run one round of calls in parallel; -> compact results (a bad-args call returns its error to the model)."""
    jobs = []
    for c in calls:
        state.calls += 1
        jobs.append((state.calls, c.get("tool"), c.get("args") or {}))

    def one(job):
        n, name, args = job
        t0 = time.perf_counter()
        try:
            res, tset, subs = TOOL_FUNCS[name](engine, ctx, args, state, n)
        except ArgError as exc:
            res, tset, subs = {"ok": False, "error": f"bad args: {exc}"}, None, None
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return n, name, args, res, tset, subs, ms

    left = deadline - time.perf_counter()
    if left <= 0:
        raise GuideFailed("budget: no time for tools")
    ex = ThreadPoolExecutor(max_workers=max(1, len(jobs)))
    try:
        futures = [ex.submit(one, j) for j in jobs]
        done = []
        for fut in futures:
            try:
                done.append(fut.result(timeout=max(0.01, deadline - time.perf_counter())))
            except FutureTimeout as exc:
                raise GuideFailed("budget: tools too slow") from exc
            except (ToolError, S.StepError, PatchError) as exc:
                raise GuideFailed(f"tool_error: {type(exc).__name__}: {exc}") from exc
            except Exception as exc:   # noqa: BLE001 - any tool failure falls back to the pipeline
                raise GuideFailed(f"tool_error: {type(exc).__name__}: {exc}") from exc
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    out = []
    for n, name, args, res, tset, subs, ms in done:
        if tset is not None:
            tset.result = res
            state.sets.append(tset)
        _merge_cache(ctx, subs if isinstance(subs, list) else [subs])
        g["tools"].append({"call": n, "tool": name, "args": args, "ms": ms, "ok": bool(res.get("ok")),
                           **({"levers_ms": tset.lever_ms} if tset is not None and tset.lever_ms else {})})
        ctx.trace["timings_ms"][f"guide_tool{n}_{name}"] = ms
        out.append({"call": n, "tool": name, "args": args, "result": res})
    return out


# ---------------------------------------------------------------- context

def build_context(engine, ctx, default, screen, tool_results=None, round_no=1, last=False):
    cfg = settings(engine.skill.rules)
    s = ctx.session
    reading = {"intent": ctx.intent, "patch": ctx.patch, "refs": ctx.refs or None,
               "info_question": (ctx.understand or {}).get("info_question")}
    payload = {
        "today": s.today().isoformat(), "stage": s.stage, "brief": s.brief.values(),
        "assumed": P.assumed_paths(s.brief), "assumptions": [{"field": c.field, "label": c.label} for c in ctx.chips],
        "home_city": getattr(ctx.profile, "home_city", None),
        "screen": screen,
        "history": history_view(s, int(cfg["history_exchanges"]), int(cfg["agent_text_chars"])),
        "message": ctx.text or "",
        "reading": {k: v for k, v in reading.items() if v is not None},
        "default_plan": default,
        "round": round_no if not last else f"{round_no} (last: reply now, calls are not run)",
    }
    if tool_results:
        payload["tool_results"] = tool_results
    return fit_context(payload, int(cfg["max_context_chars"]))


def fit_context(payload, cap):
    """Cut the context to `cap` chars, least important first: agent history, risks lists, the screen's details,
    default facts, tool variants' details."""
    if len(_dumps(payload)) <= cap:
        return payload
    p = json.loads(_dumps(payload))
    for h in p.get("history") or []:
        if h["role"] == "agent":
            h["text"] = h["text"][:120]
    steps = [
        lambda: [x.pop(k, None) for x in (p.get("screen") or {}).get("variants") or [] for k in ("risks", "facts",
                                                                                              "out", "back")],
        lambda: [x.pop("gives_up", None) for x in (p.get("default_plan") or {}).get("variants") or []],
        lambda: (p.get("default_plan") or {}).pop("info_facts", None),
        lambda: [x.pop(k, None) for t in p.get("tool_results") or [] for x in (t["result"].get("variants") or [])
                 for k in ("risks", "gives_up")],
        lambda: [lv.get("variant", {}).pop(k, None) for t in p.get("tool_results") or []
                 for lv in t["result"].get("levers") or [] for k in ("out", "back", "risks", "facts")],
        lambda: p.update(history=(p.get("history") or [])[-4:]),
    ]
    for step in steps:
        if len(_dumps(p)) <= cap:
            break
        step()
    return p


# ---------------------------------------------------------------- model calls

def _call(engine, ctx, payload, deadline, g, round_label, rerun_issues=None):
    from .engine import rerun_system
    card = engine.skill.card(CARD)
    system = rerun_system(engine.skill.system_prompt(CARD), rerun_issues)
    user = _dumps(payload)
    req = LLMRequest(card=CARD, system=system, user=user, schema=card.schema, thinking=card.thinking,
                     max_tokens=card.max_tokens, temperature=card.temperature)
    left = deadline - time.perf_counter()
    if left <= 0.2:
        raise GuideFailed("budget: no time for a guide call")
    record = {"card": CARD, "round": round_label, "rerun": rerun_issues is not None, "context_chars": len(user),
              "system_chars": len(system)}
    t0 = time.perf_counter()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(engine.backend.complete, req)
        try:
            res = fut.result(timeout=left)
        except FutureTimeout as exc:
            record.update(ok=False, error="timeout", latency_ms=round((time.perf_counter() - t0) * 1000))
            ctx.trace["calls"].append(record)
            _round_failed(g, round_label, t0, user, "timeout")
            raise GuideFailed(f"budget: guide call over {round(left, 1)} s") from exc
        except LLMError as exc:
            record.update(ok=False, error=str(exc)[:300], retries=exc.retries, tokens=exc.usage,
                          latency_ms=exc.latency_ms or round((time.perf_counter() - t0) * 1000))
            ctx.trace["calls"].append(record)
            _round_failed(g, round_label, t0, user, "llm", exc.usage)
            raise GuideFailed(f"llm: {str(exc)[:200]}") from exc
        except Exception as exc:   # noqa: BLE001 - a transport failure of the backend: the pipeline answers
            record.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}",
                          latency_ms=round((time.perf_counter() - t0) * 1000))
            ctx.trace["calls"].append(record)
            _round_failed(g, round_label, t0, user, "transport")
            raise GuideFailed(f"llm: {type(exc).__name__}: {str(exc)[:200]}") from exc
    finally:
        ex.shutdown(wait=False)
    record.update(ok=True, model=res.model, provider=res.provider, tokens=res.usage, latency_ms=res.latency_ms,
                  cost=(res.usage or {}).get("cost_usd"), retries=res.retries)
    ctx.trace["calls"].append(record)
    budget = ctx.trace["budget"]
    budget["guide_calls"] = budget.get("guide_calls", 0) + 1
    g["rounds"].append({"round": round_label, "ms": round((time.perf_counter() - t0) * 1000, 1),
                        "latency_ms": res.latency_ms, "usage": round_usage(res.usage),
                        "context_chars": len(user), "calls": [c.get("tool") for c in res.data.get("calls") or []],
                        "reply": res.data.get("reply") is not None})
    return res.data


def round_usage(usage):
    """Tokens of one guide round for the trace: input, cached, output, reasoning."""
    u = usage or {}
    return {k: int(u.get(f"{k}_tokens") or 0) for k in ("input", "cached", "output", "reasoning")}


def _round_failed(g, round_label, t0, user, error, usage=None):
    g["rounds"].append({"round": round_label, "ms": round((time.perf_counter() - t0) * 1000, 1), "error": error,
                        "context_chars": len(user), **({"usage": round_usage(usage)} if usage else {})})


# ---------------------------------------------------------------- the shown set and the guards

@dataclass
class Plan:
    show: str
    card: str
    rec: object = None
    variants: list = field(default_factory=list)
    tset: object = None
    destinations: list = field(default_factory=list)
    comparison: dict = None
    items: list = field(default_factory=list)


def resolve_show(ctx, reply, state, default_card):
    """-> (Plan, issues). The set the reply shows: the one holding recommended_id (default first)."""
    show, rec = reply.get("show"), reply.get("recommended_id")
    s = ctx.session
    issues = []
    if show == "variants":
        card = default_card if default_card in ("present.variants", "present.update") else "present.variants"
        if rec:
            v, tset = state.variant(str(rec))
            if v is None:
                return Plan(show, card), [issue("unknown_recommended", f"recommended_id {rec} is not a variant of "
                                                                       f"the default plan or a tool result")]
        else:
            tset = None if state.base_variants() else next((t for t in reversed(state.sets) if t.variants), None)
        variants = tset.variants if tset is not None else state.base_variants()
        if tset is not None and tset.tool == "find_savings" and rec:
            first = next(v for v in variants if v.id == rec)
            variants = [first] + [v for v in variants if v.id != rec][:2]
        if not variants:
            return Plan(show, card), [issue("nothing_to_show", "show variants, but no variants were built")]
        rec = rec or _hint(tset.brief if tset else s.brief, variants)
        return Plan(show, card, rec=rec, variants=list(variants), tset=tset), issues
    if show == "destinations":
        tset = next((t for t in reversed(state.sets) if t.destinations), None)
        items = tset.destinations if tset else list(ctx.destination_items)
        if not items:
            return Plan(show, "present.destinations"), [issue("nothing_to_show", "show destinations, but none found")]
        if rec and rec not in {i["id"] for i in items}:
            return Plan(show, "present.destinations"), [issue("unknown_recommended", f"{rec} is not a shown place")]
        return Plan(show, "present.destinations", rec=rec, destinations=items, tset=tset), issues
    if show == "comparison":
        tset = next((t for t in reversed(state.sets) if t.comparison), None)
        comp = tset.comparison if tset else ctx.comparison
        if not comp:
            return Plan(show, "present.compare"), [issue("nothing_to_show", "show comparison, but nothing compared")]
        variants = tset.result_variants if tset else list(ctx.variants)
        if rec and rec not in comp["variant_ids"]:
            return Plan(show, "present.compare"), [issue("unknown_recommended", f"{rec} is not compared")]
        return Plan(show, "present.compare", rec=rec, variants=variants, comparison=comp, tset=tset), issues
    return Plan("none", "present.info", rec=None), issues


def guard_input(engine, ctx, plan, state, default, screen, tool_results, question):
    """The facts pool of the reply: the shown items as `items`, everything else (default plan, tools, screen) as
    facts — numbers may come from any of them, ids only from the shown items."""
    from .engine import budget_max
    s = ctx.session
    budget = budget_max(plan.tset.brief if plan.tset and plan.tset.brief is not None else s.brief)
    items = []
    if plan.variants:
        base = next((v.totals.rub for v in plan.variants if v.id == plan.rec), None)
        items = [P.variant_item(v, str(n + 1), {}, base_rub=base, budget_max=budget)
                 for n, v in enumerate(plan.variants)]
    elif plan.destinations:
        items = [{k: v for k, v in i.items() if k != "image_url"} for i in plan.destinations]
    pool = P.common(s, ctx.chips)
    changes = list(ctx.changes)
    for t in state.sets:
        changes += list(t.changes or [])
    facts = [{"kind": "default_plan", **default}, {"kind": "screen", **(screen or {})}]
    facts += [{"kind": "tool", "tool": t["tool"], **t["result"]} for t in tool_results or []]
    facts += [f for f in ctx.info_facts or [] if isinstance(f, dict)]
    if plan.tset is not None and plan.tset.tool == "find_savings":
        for v in plan.variants:
            if v.facts.get("dates_shifted_days"):
                changes.append({"field": "dates.start", "before": None, "after": _dates_of(v)[0]})
    pool.update(items=items, recommended_hint=plan.rec, questions=[{"id": "q_guide", "text": question["text"]}]
                if question else [], upsell_candidates=[], changes=changes, facts=facts,
                other_places=[p for p in (ctx.other_places or []) if isinstance(p, dict)])
    return pool


def check_reply(engine, ctx, reply, plan, pool, tool_results):
    from .guards import check_card_output, question_count
    cfg = settings(engine.skill.rules)
    msg = reply.get("message") or ""
    q = reply.get("question")
    text = msg + (f"\n{q['text']} " + " ".join(c.get("label", "") for c in q.get("choices") or []) if q else "")
    out = {"message": text, "items": [], "recommended_id": plan.rec, "upsell": []}
    issues = [as_issue(i) for i in check_card_output(plan.card, pool, out, engine.skill.rules) or []
              if i.get("code") not in GUARD_DROP]
    lo, hi = cfg["message_chars"]
    if not msg.strip():
        issues.append(issue("message_empty", "Пустое сообщение"))
    elif len(msg) > int(hi * 1.2):
        issues.append(issue("message_long", f"Сообщение {len(msg)} знаков — сократите до {hi}"))
    asked = question_count(msg) + (1 if q and q["text"].strip() not in msg else 0)
    if asked > int(cfg["max_questions"]):
        issues.append(issue("too_many_questions", f"Вопросов {asked} — оставьте не больше "
                                                  f"{cfg['max_questions']}, тот, от ответа на который зависит выдача"))
    savings = [t["result"] for t in tool_results or [] if t["tool"] == "find_savings" and t["result"].get("ok")]
    cheaper = [lv for r in savings for lv in r.get("levers") or []] + [p for r in savings for p in r.get("places") or []]
    m = NOT_FOUND_CHEAPER_RE.search(msg)
    if m and cheaper:
        issues.append(issue("savings_claim", f"«{m.group(0)}» — find_savings нашёл дешевле: "
                                             + ", ".join(str(x.get("total_rub") or x.get("price_from_rub"))
                                                         for x in cheaper[:3])))
    issues += unannounced_change(ctx, plan, msg)
    return issues


MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")


def span_ru(start, end):
    """ISO dates -> «8–12 декабря», «30 декабря – 3 января»."""
    d1, d2 = date.fromisoformat(start), date.fromisoformat(end)
    if (d1.year, d1.month) == (d2.year, d2.month):
        return f"{d1.day}–{d2.day} {MONTHS_GEN[d1.month - 1]}"
    return f"{d1.day} {MONTHS_GEN[d1.month - 1]} – {d2.day} {MONTHS_GEN[d2.month - 1]}"


def names_dates(msg, start, end):
    """Does the message name these trip dates: «8–12 декабря», «с 8 по 12», «заезд 8 декабря» (a price «8 400 ₽»
    or «8 ч» is not a date)."""
    d1, d2 = date.fromisoformat(start), date.fromisoformat(end)
    m1 = MONTHS_GEN[d1.month - 1]
    pats = (rf"\b{d1.day}\s*[–—-]\s*{d2.day}\b", rf"\b{d1.day}(?:-?го)?\s+{m1}",
            rf"\bс\s+{d1.day}(?:\s+{m1})?\s+по\s+{d2.day}\b")
    return any(re.search(p, msg, re.IGNORECASE) for p in pats)


def _nights_word(n):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "ночь"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "ночи"
    return "ночей"


def unannounced_change(ctx, plan, msg):
    """The shown trip's dates differ from the trip the person saw before this turn, they did not ask to change the
    dates, and the message does not name the new ones (the pipeline's budget fit moves dates by itself on
    «подешевле») -> [unannounced_change] carrying the sentence code adds in front (data.say)."""
    if plan.show != "variants" or not plan.variants:
        return []
    patch = getattr(ctx, "patch", None) or {}
    if any(str(p).startswith("dates.") for p in list(patch.get("set") or {}) + list(patch.get("unset") or [])):
        return []
    seen = [x["dates"] for x in (ctx.__dict__.get("guide_screen") or {}).get("variants") or []
            if isinstance(x, dict) and x.get("dates")]
    if not seen:
        return []
    v = next((x for x in plan.variants if x.id == plan.rec), plan.variants[0])
    start, end = _dates_of(v)
    if not start or not end or f"{start}..{end}" in seen:
        return []
    old_start, old_end = seen[0].split("..")
    nights = (date.fromisoformat(end) - date.fromisoformat(start)).days
    old_nights = (date.fromisoformat(old_end) - date.fromisoformat(old_start)).days
    if names_dates(msg, start, end):
        return []
    if start == old_start and nights != old_nights and re.search(rf"\b{nights}\s+ноч", msg):
        return []
    say = f"Даты теперь {span_ru(start, end)}"
    if nights != old_nights:
        say += f" — {nights} {_nights_word(nights)} вместо {old_nights}"
    say += "."
    return [issue("unannounced_change", f"Даты поездки поменялись ({seen[0]} → {start}..{end}), а сообщение их не "
                                        f"называет — скажите прямо: «{say}»", say=say)]


def offline_issues(card_input, output, rules):
    """The guards a level-2 run applies to one guide output (evals/run.py; the engine checks against the real shown
    set): numbers / Latin / claims against the whole input, recommended_id among the input ids, ≤ 1 question, the
    message length, no «дешевле не нашлось» after find_savings found cheaper. A tool round (reply null) passes."""
    from .guards import check_card_output, question_count
    reply = (output or {}).get("reply")
    if not isinstance(reply, dict):
        return []
    cfg = settings(rules)
    ids = set()
    dp, screen = card_input.get("default_plan") or {}, card_input.get("screen") or {}
    for key in ("variants", "destinations"):
        ids |= {x.get("id") for x in (dp.get(key) or []) + (screen.get(key) or []) if isinstance(x, dict)}
    for t in card_input.get("tool_results") or []:
        r = t.get("result") or {}
        ids |= {x.get("id") for x in (r.get("variants") or []) + (r.get("destinations") or []) if isinstance(x, dict)}
        ids |= {x.get("variant_id") for x in r.get("levers") or [] if isinstance(x, dict)}
    pool = dict(card_input, items=[{"id": i} for i in sorted(i for i in ids if i)])
    msg = reply.get("message") or ""
    q = reply.get("question")
    text = msg + (f"\n{q['text']} " + " ".join(c.get("label", "") for c in q.get("choices") or []) if q else "")
    out = {"message": text, "items": [], "recommended_id": reply.get("recommended_id"), "upsell": []}
    issues = [as_issue(i) for i in check_card_output("present.info", pool, out, rules) or []
              if i.get("code") not in GUARD_DROP]
    lo, hi = cfg["message_chars"]
    if len(msg) > int(hi * 1.2):
        issues.append(issue("message_long", f"Сообщение {len(msg)} знаков — сократите до {hi}"))
    asked = question_count(msg) + (1 if q and q["text"].strip() not in msg else 0)
    if asked > int(cfg["max_questions"]):
        issues.append(issue("too_many_questions", f"Вопросов {asked} — оставьте не больше {cfg['max_questions']}"))
    tools = [{"tool": t.get("tool"), "result": t.get("result") or {}} for t in card_input.get("tool_results") or []]
    savings = [t["result"] for t in tools if t["tool"] == "find_savings" and t["result"].get("ok")]
    cheaper = [x for r in savings for x in (r.get("levers") or []) + (r.get("places") or [])]
    m = NOT_FOUND_CHEAPER_RE.search(msg)
    if m and cheaper:
        issues.append(issue("savings_claim", f"«{m.group(0)}» — find_savings нашёл дешевле"))
    return issues


# ---------------------------------------------------------------- commit

def _question(ctx, q):
    """The reply's question as buttons when it maps to a brief field with valid values; else None."""
    if not q or not q.get("field") or q["field"] not in ALLOWED_PATCH_PATHS:
        return None
    choices = []
    for n, c in enumerate(q.get("choices") or []):
        if "value" not in c or c.get("value") is None and q["field"] not in ALLOWED_UNSET_PATHS:
            return None
        if c.get("value") is not None:
            try:
                apply_patch(ctx.session.brief, {"set": {q["field"]: c["value"]}, "unset": []})
            except (PatchError, ValueError, TypeError):
                return None
        choices.append(Choice(label=c["label"], value=c.get("value"), recommended=n == 0))
    if not choices:
        return None
    try:
        return Question(id="q_guide", field=q["field"], text=q["text"], choices=choices, reason="guide")
    except ValueError:
        return None


def _mentioned_places(message, places):
    low = (message or "").casefold()
    return [p for p in places if isinstance(p, dict) and p.get("name") and p["name"].casefold()[:5] in low]


def commit(engine, ctx, reply, plan, state, tool_results):
    s = ctx.session
    if plan.show == "variants":
        t = plan.tset
        if t is not None:
            rec_brief = t.briefs.get(plan.rec) or t.brief
            if rec_brief is not None:
                s.brief = rec_brief
            ctx.results, ctx.districts = t.results or ctx.results, t.districts or ctx.districts
            ids = {v.id for v in plan.variants}
            ctx.upsell = [u for u in t.upsell if getattr(u, "variant_id", None) in ids]
            ctx.changes = list(ctx.changes) + list(t.changes or [])
        ctx.variants = list(plan.variants)
        s.variants = list(plan.variants)
        ctx.present = plan.card
    elif plan.show == "destinations":
        t = plan.tset
        if t is not None:
            if t.brief is not None:
                s.brief = t.brief
            ctx.destination_items = list(plan.destinations)
            S.remember_destinations(s, plan.destinations, replace=True)
            P.record_shown(s, "destination", [(i["id"], i["name"]) for i in plan.destinations])
        ctx.present = "present.destinations"
    elif plan.show == "comparison":
        ctx.comparison = dict(plan.comparison, recommended_id=plan.rec or plan.comparison.get("recommended_id"))
        ctx.variants = list(plan.variants)
        ctx.present = "present.compare"
    else:
        if ctx.variants and ctx.present in ("present.variants", "present.update"):
            # the default plan rebuilt the variants: the buttons must point at what the session now holds
            P.record_shown(s, "variant", [(v.id, _title(v)) for v in ctx.variants])
        ctx.present = "present.info"
    if plan.show == "variants":
        P.record_shown(s, "variant", [(v.id, _title(v)) for v in plan.variants])
    message = reply["message"].strip()
    q = _question(ctx, reply.get("question"))
    if reply.get("question") and q is None and reply["question"]["text"].strip() not in message:
        message = f"{message} {reply['question']['text'].strip()}"
    ctx.questions = [q] if q is not None else []
    ctx.chips = engine._chips(ctx, ctx.present) if ctx.present in ("present.variants", "present.update",
                                                                   "present.destinations") else []
    ctx.card_output = {"message": message, "items": [], "recommended_id": plan.rec, "upsell": []}
    ctx.notes = []
    ctx.message = None
    places = [p for t in state.sets for p in t.places] + list(ctx.other_places or [])
    named = _mentioned_places(message, places)
    if named:
        S.remember_destinations(s, [{"id": p["id"], "name": p["name"], "price_from_rub": p.get("price_from_rub"),
                                     "nights": p.get("nights"), "facts": p} for p in named])
        from .state import ShownItem
        have = {i.id for i in s.shown.destinations}
        for p in named:
            if p["id"] not in have:
                s.shown.destinations.append(ShownItem(label=str(len(s.shown.destinations) + 1), kind="destination",
                                                      id=p["id"], title=p["name"]))
        acts = [{"type": "choose_destination", "label": f"Смотреть {p['name']}", "payload": {"destination_id": p["id"]}}
                for p in named[:3]]
        ctx.extra_actions = [a for a in ctx.extra_actions if a not in acts] + acts


# ---------------------------------------------------------------- code repairs of formal issues

REPAIRABLE = {"too_many_questions", "unannounced_change"}


def repair_questions(reply, max_questions=1):
    """«Вопросов 2»: keep the final question (the reply's `question` when it is not in the message, else the last
    question sentence) and drop the other question sentences -> a repaired copy, or None when fewer than 2 sentences
    of the message would be left (the model fix-up then rewrites it)."""
    from .guards import _sentences, question_count
    msg = reply.get("message") or ""
    q = reply.get("question")
    separate = bool(q and (q.get("text") or "").strip() and q["text"].strip() not in msg)
    sents = _sentences(msg)
    asked = [x for x in sents if question_count(x)]
    keep = set() if separate else ({asked[-1]} if asked else set())
    drop = [x for x in asked if x not in keep]
    if not drop:
        return None
    left = [x for x in sents if x not in drop]
    if len(left) < 2 or question_count(" ".join(left)) + (1 if separate else 0) > max_questions:
        return None
    out = msg
    for x in drop:
        out = out.replace(x, "", 1)
    out = re.sub(r"[ \t]{2,}", " ", re.sub(r"\s+([.,!?…])", r"\1", out)).strip()
    return dict(reply, message=out)


# ---------------------------------------------------------------- dead ends: the likely fixes run by code

HOTEL_RELAX = (("meals", "any"), ("stars_min", -1), ("district", "any"), ("free_cancellation", None))


def hotel_relaxations(brief):
    """The hotel filters a dead end may relax, one at a time: [(path, value)] (value None = unset). Filters the
    person never set (segment defaults, profile guesses) come back as `base` — relaxed in every rebuild."""
    prefs = brief.values().get("prefs") or {}
    own, base = [], {}
    for key, to in HOTEL_RELAX:
        cur = prefs.get(key)
        if cur in (None, "any", False):
            continue
        if key == "stars_min":
            to = int(cur) - 1 if int(cur) > 2 else None
        path = f"prefs.{key}"
        if brief.source_of(path) == "user":
            own.append((path, to))
        else:
            base[path] = to
    return own, base


def _direct(o):
    return not o.get("stops")


def dead_end(engine, ctx, failure, screen):
    """-> {why: [codes], calls: [auto tool calls]} when the default plan built nothing (or only stays for lack of
    a road) or the turn asks about a failure that still stands (the screen shows nothing built); else None.
    Fixes by the failure kind, from what the engine already found (ctx.transport_gap, the search results):
    direct-only with no direct flight but connecting ones -> rebuild direct_only false + check_road (days with
    direct flights); no flight at all but a train -> rebuild by train; the hotel filters leave no hotel -> one
    rebuild per filter relaxed (meals, stars −1, district, free cancellation; the road fix rides along); ≤ 3."""
    s = ctx.session
    stay_only = bool(ctx.variants) and all(v.facts.get("no_transport") for v in ctx.variants)
    standing = (failure is None and not ctx.variants and not (screen or {}).get("variants")
                and s.brief.has("destination") and _last_turn_failed(s))
    if failure is None and not stay_only and not standing:
        return None
    if failure is not None and failure.code != "no_data":
        return None
    brief = s.brief
    if not brief.has("destination"):
        return None
    results = ctx.results
    if standing or not results:
        sub = _sub_ctx(ctx)
        sub.patch = {"set": {}, "unset": []}
        try:
            S.STEP_REGISTRY["apply_patch"](sub)
            S.STEP_REGISTRY["search"](sub)
        except (S.StepError, ToolError, PatchError):
            return None
        results = sub.results
    if not results:
        return None
    v = brief.values()
    prefs = v.get("prefs") or {}
    scope = list(results.get("scope") or v.get("scope") or [])
    fo, fb = results.get("flights_out") or [], results.get("flights_back") or []
    to, tb = results.get("trains_out") or [], results.get("trains_back") or []
    gap = ctx.transport_gap or {}
    why, road, check = [], {}, False
    if scope and set(scope) & {"flights", "trains"}:
        if gap.get("code") == "direct_only" or (prefs.get("direct_only") and fo and fb and not (
                any(_direct(o) for o in fo) and any(_direct(o) for o in fb))):
            why.append("no_direct_flights")
            road, check = {"prefs.direct_only": False}, True
        elif gap.get("code") == "plane_fixed" or ("flights" in scope and prefs.get("transport") != "train"
                                                   and not (fo and fb) and not (to and tb) and _train_ok(ctx, brief)):
            why.append("no_flights_train_exists")
            road = {"prefs.transport": "train"}
            if "flights" in scope:
                road["scope"] = [("trains" if x == "flights" else x) for x in scope]
        elif gap.get("code") == "train_fixed":
            why.append("no_trains_plane_exists")
            road = {"prefs.transport": "plane"}
            if "trains" in scope:
                road["scope"] = [("flights" if x == "trains" else x) for x in scope]
    own, base, hotel = [], {}, "stay" in scope and not results.get("hotels")
    if hotel:
        why.append("no_hotel_under_filters")
        own, base = hotel_relaxations(brief)
    if not why:
        return None
    cap = int(settings(engine.skill.rules)["max_calls_per_round"])
    calls = [{"tool": "check_road", "args": {}}] if check else []

    def rebuild(extra):
        sets = dict(road)
        sets.update({p: v for p, v in extra.items() if v is not None})
        return {"tool": "rebuild", "args": {"set": sets, "unset": [p for p, v in extra.items() if v is None]}}

    if hotel:
        for path, to in own[:max(1, cap - len(calls))]:
            calls.append(rebuild({**base, path: to}))
        if not own and base:
            calls.append(rebuild(base))
    elif road:
        calls.append(rebuild({}))
    return {"why": why, "calls": calls[:cap]}


def _train_ok(ctx, brief):
    try:
        origin = S._origin_place(ctx, brief)
        rec = S.destination_record(ctx, dest_id=(brief.values().get("destination") or {}).get("id")) or {}
        return bool(origin) and bool(S.train_reachable(origin, rec))
    except Exception:   # noqa: BLE001 - a missing record just means no train fix
        return False


def _last_turn_failed(session):
    """The last turn ended in a dead end: its no_data answer, or a guide reply on a failed default plan."""
    last = (session.trace[-1] if session.trace else {}) or {}
    g = last.get("guide") or {}
    return (last.get("error") or {}).get("code") == "no_data" or bool(g.get("dead_end") and g.get("show") != "variants")


# ---------------------------------------------------------------- the loop

def run_guide(engine, ctx, failure=None, code_message=None, speculative=False):
    """One guide turn on top of the default plan's results -> True (the reply is in ctx) or False (fallback).
    speculative: the pipeline's present is already running next to the guide, so a fallback costs no new model
    call and the guide may use the turn budget up to `speculative_reserve_s` instead of `fallback_reserve_s`."""
    cfg = settings(engine.skill.rules)
    started = ctx.__dict__.get("turn_started") or time.perf_counter()
    reserve = float(cfg["speculative_reserve_s"] if speculative else cfg["fallback_reserve_s"])
    deadline = started + float(cfg["turn_budget_s"]) - reserve
    t0 = time.perf_counter()
    g = {"rounds": [], "tools": [], "fallback": None, "used": False}
    ctx.trace["guide"] = g
    screen = ctx.__dict__.get("guide_screen") or {"stage": ctx.session.stage}
    default_card = ctx.present
    state = GuideState(default_variants=list(ctx.variants), default_brief=ctx.session.brief,
                       screen_variants=list(ctx.session.variants))
    try:
        default = default_plan(engine, ctx, failure, code_message)
        tool_results = []
        # a wish whose tool is certain («подешевле» -> find_savings) runs before round 1: one model round less
        auto = [{"tool": t, "args": {}} for w, t in (cfg.get("auto_tools") or {}).items()
                if w in (default.get("wishes") or []) and t in TOOL_FUNCS and state.base_variants()]
        auto = auto[:1]
        # a dead end: code runs the likely fixes too (the guide then answers with whole trips in one call)
        de = dead_end(engine, ctx, failure, screen)
        if de is not None:
            default["dead_end"] = {"why": de["why"], "auto_fixes": len(de["calls"])}
            g["dead_end"] = de
            auto += de["calls"][:max(0, int(cfg["max_calls_per_round"]) - len(auto))]
        if auto:
            ran = [dict(r, auto=True) for r in run_tools(engine, ctx, auto, state, deadline, g)]
            # auto rebuilds that built nothing are left out — unless none built (then the attempts are the news)
            kept = [r for r in ran if r["tool"] != "rebuild" or r["result"].get("ok")]
            tried = [r for r in ran if r["tool"] == "rebuild"]
            tool_results += kept if any(r["result"].get("ok") for r in tried) or not tried else ran
        rounds = int(cfg["max_tool_rounds"])
        payload, reply = None, None
        for round_no in range(1, rounds + 2):
            last = round_no == rounds + 1
            payload = build_context(engine, ctx, default, screen, tool_results, round_no, last)
            g.setdefault("context_chars", []).append(len(_dumps(payload)))
            out = _call(engine, ctx, payload, deadline, g, round_no)
            calls = [c for c in out.get("calls") or [] if c.get("tool") in TOOL_FUNCS]
            if calls and not last:
                tool_results += run_tools(engine, ctx, calls[:int(cfg["max_calls_per_round"])], state, deadline, g)
                continue
            reply = out.get("reply")
            if reply is None:
                raise GuideFailed("no_reply" if not last else "no_reply after the last tool round")
            break
        plan, issues = _checked(engine, ctx, reply, state, default_card, default, screen, tool_results)
        _hook(ctx, "guide", issues)
        if blocking(issues):
            _rejected(ctx, g, round_no, reply, blocking(issues))
            fixed = _repair(engine, ctx, reply, blocking(issues), cfg)
            if fixed is not None:
                plan2, issues2 = _checked(engine, ctx, fixed, state, default_card, default, screen, tool_results)
                _hook(ctx, "guide.repair", issues2)
                if not blocking(issues2):
                    g["repaired"] = sorted({i["code"] for i in blocking(issues)})
                    reply, plan, issues = fixed, plan2, issues2
        if blocking(issues):
            out2 = _call(engine, ctx, payload, deadline, g, "fixup", rerun_issues=blocking(issues))
            reply2 = out2.get("reply")
            if reply2 is None:
                raise GuideFailed("guard: the fix-up gave no reply")
            plan, issues2 = _checked(engine, ctx, reply2, state, default_card, default, screen, tool_results)
            _hook(ctx, "guide.fixup", issues2)
            if blocking(issues2):
                fixed2 = _repair(engine, ctx, reply2, blocking(issues2), cfg)
                if fixed2 is not None:
                    plan3, issues3 = _checked(engine, ctx, fixed2, state, default_card, default, screen, tool_results)
                    _hook(ctx, "guide.repair", issues3)
                    if not blocking(issues3):
                        g["repaired"] = sorted(set(g.get("repaired") or []) | {i["code"] for i in blocking(issues2)})
                        reply2, plan, issues2 = fixed2, plan3, issues3
            if blocking(issues2):
                _rejected(ctx, g, "fixup", reply2, blocking(issues2))
                raise GuideFailed("guard: " + "; ".join(f"{i['code']}" for i in blocking(issues2))[:300])
            reply = reply2
        commit(engine, ctx, reply, plan, state, tool_results)
        g["used"] = True
        g["show"] = plan.show
        g["recommended_id"] = plan.rec
        return True
    except GuideFailed as exc:
        g["fallback"] = exc.reason
        ctx.trace["notes"].append(f"guide fallback: {exc.reason}")
        return False
    finally:
        g["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        ctx.trace["timings_ms"]["guide"] = g["ms"]


def _checked(engine, ctx, reply, state, default_card, default, screen, tool_results):
    plan, issues = resolve_show(ctx, reply, state, default_card)
    pool = guard_input(engine, ctx, plan, state, default, screen, tool_results, reply.get("question"))
    issues += check_reply(engine, ctx, reply, plan, pool, tool_results)
    return plan, issues


def _repair(engine, ctx, reply, issues, cfg):
    """Formal issues code can fix without a model call (guide mode only) -> the repaired reply or None."""
    codes = {i["code"] for i in issues}
    if not codes or not codes <= REPAIRABLE:
        return None
    out = reply
    if "too_many_questions" in codes:
        out = repair_questions(out, int(cfg["max_questions"]))
        if out is None:
            return None
    says = [(i.get("data") or {}).get("say") for i in issues if i["code"] == "unannounced_change"]
    if says:
        if not all(says):
            return None
        out = dict(out, message=" ".join([str(x) for x in says] + [(out.get("message") or "").strip()]))
    return out


def _rejected(ctx, g, round_label, reply, issues):
    """A reply the guards blocked: kept in the trace for debugging only (debug sessions)."""
    if not getattr(ctx.session, "debug", False):
        return
    g.setdefault("rejected", []).append({
        "round": round_label, "message": (reply or {}).get("message"), "question": (reply or {}).get("question"),
        "show": (reply or {}).get("show"), "recommended_id": (reply or {}).get("recommended_id"),
        "issues": [{"code": i["code"], "message": i.get("message")} for i in issues]})


def _hook(ctx, name, issues):
    verdict = "block" if blocking(issues) else ("warn" if issues else "ok")
    ctx.trace["hooks"].append({"event": "after_present", "name": name, "kind": "validate", "verdict": verdict,
                               "issues": issues, "card": CARD})
