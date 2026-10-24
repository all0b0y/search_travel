"""Engine steps (AGENT_PLAN.md sections 4-5): one function per `agent.steps_spec.STEP_NAMES` entry.

    STEP_REGISTRY[name](ctx) -> None        # steps read and write the turn context `Ctx`

Code calls every tool; the model never does. Sibling modules (ENGINE-B: ranking, checks, upsell,
guards, render) are reached through `Domain`, which imports them lazily, so tests inject fakes.

`results` handed to `build_variants` (the layout of agent/ranking.py):

    {"flights_out": [FlightOffer], "flights_back": [FlightOffer], "trains_out": [TrainOffer],
     "trains_back": [TrainOffer], "hotels": [HotelOffer],
     "transfers_out": {hotel_id: [TransferOffer + "point"]}, "transfers_back": {hotel_id: [...]},
     "destination": <destination record>,
     # engine extras: "origin", "check_in", "check_out", "party", "scope"}

`districts` = {osm_id: OpenData.district_profile(osm_id)} for every hotel offer.
"""
import hashlib
import importlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from . import payloads as P
from .hooks import as_issue, blocking, issue
from .mcp_client import ToolError
from .profiles import apply_profile, profile_value
from .state import (Brief, CachedResult, PatchError, Question, UpsellCandidate, Variant, apply_patch, assume,
                    changed_paths)
from .steps_spec import STEP_NAMES

RESULTS_CAP = 80          # cached tool results kept per session
TRANSFER_HOTELS = 40      # every hotel offer gets transfer quotes: ranking may pick any of them
ITEM_KINDS = {"flight": "flight", "train": "train", "stay": "hotel", "transfer": "transfer"}
UPSELL_ITEM_KIND = {"transfer": "transfer", "early_checkin": "hotel", "room_upgrade": "hotel",
                    "meal_upgrade": "hotel", "flexible_fare": "flight"}
MEALS_RANK = {"none": 0, "breakfast": 1, "half_board": 2, "full_board": 3, "all_inclusive": 4}


class StepError(Exception):
    """A user-facing failure: answer honestly with `message` and `code` (response error)."""

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# ---------------------------------------------------------------- domain (ENGINE-B) indirection

DOMAIN_SOURCES = {"build_variants": "agent.ranking", "fit": "agent.ranking", "sensitivity": "agent.ranking",
                  "check_variant": "agent.checks", "upsell_candidates": "agent.upsell",
                  "offers_for_variant": "agent.upsell", "check_card_output": "agent.guards",
                  "render_variant": "agent.render"}


class Domain:
    """ENGINE-B functions by name; overrides (fakes) first, else imported lazily from their module."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def get(self, name, required=True):
        if name in self._overrides:
            return self._overrides[name]
        module = DOMAIN_SOURCES.get(name)
        if module is None:
            raise AttributeError(name)
        try:
            return getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError):
            if required:
                raise
            return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get(name)



# ---------------------------------------------------------------- turn context

def new_trace():
    return {"stage_path": [], "route": None, "steps": [], "calls": [], "tools": [], "hooks": [], "jev": [],
            "timings_ms": {}, "budget": {}, "notes": [], "errors": []}


@dataclass
class Ctx:
    engine: Any
    session: Any
    request: dict
    text: Optional[str] = None
    image: Optional[str] = None
    action: Optional[dict] = None
    intent: Optional[str] = None
    understand: Optional[dict] = None
    patch: dict = field(default_factory=lambda: {"set": {}, "unset": []})
    refs: list = field(default_factory=list)
    route: Any = None
    profile: Any = None
    brief_before: Any = None
    patch_applied: bool = False
    changes: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    destination_hint: Optional[str] = None
    shift_days: Optional[int] = None
    old_totals: dict = field(default_factory=dict)
    unknown_destination: Optional[str] = None
    variant_ids: list = field(default_factory=list)
    hotel_refs: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    swap: Optional[dict] = None
    image_info: Optional[dict] = None
    image_place: Optional[dict] = None
    image_done: bool = False
    image_question: Optional[Question] = None
    destination_recs: list = field(default_factory=list)
    destination_items: list = field(default_factory=list)
    insight: Optional[dict] = None
    results: Optional[dict] = None
    districts: dict = field(default_factory=dict)
    rebuilt: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    chips: list = field(default_factory=list)
    upsell: list = field(default_factory=list)
    comparison: Optional[dict] = None
    info_question: Optional[str] = None
    info_facts: list = field(default_factory=list)
    cart: Optional[dict] = None
    order: Optional[dict] = None
    order_issues: list = field(default_factory=list)
    aftercare_result: Optional[dict] = None
    post_purchase: Optional[dict] = None
    checkout_payload: Optional[dict] = None
    files: dict = field(default_factory=lambda: {"ics": None, "page": None})
    notes: list = field(default_factory=list)          # code-written Russian sentences before the message
    message: Optional[str] = None                      # deterministic message (no present card)
    present: Optional[str] = None
    card_input: Optional[dict] = None
    card_output: Optional[dict] = None
    degraded: bool = False
    error: Optional[dict] = None
    fallback_reason: str = "unknown"
    stop: bool = False
    reroute: Any = None
    trace: dict = field(default_factory=new_trace)
    flash_calls: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    # -- shortcuts
    @property
    def skill(self):
        return self.engine.skill

    @property
    def flags(self):
        return self.engine.flags

    @property
    def rules(self):
        return self.engine.skill.rules

    @property
    def domain(self):
        return self.engine.domain

    @property
    def brief(self):
        return self.session.brief

    def note(self, text):
        if text and text not in self.notes:
            self.notes.append(text)

    def fail(self, code, message, retryable=False):
        raise StepError(code, message, retryable)


# ---------------------------------------------------------------- helpers

def as_dict(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    return obj


def _json_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    return list(value or [])


def params_key(tool, payload):
    raw = json.dumps({"tool": tool, "payload": payload}, sort_keys=True, ensure_ascii=False, default=str)
    return f"{tool}:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def store_result(session, record):
    session.results[record.key] = record
    if len(session.results) > RESULTS_CAP:
        oldest = sorted(session.results.values(), key=lambda r: r.fetched_at)[: len(session.results) - RESULTS_CAP]
        for r in oldest:
            session.results.pop(r.key, None)


def tool(ctx, name, payload, cache=True):
    """Call an Ostrovok tool; cached tools are keyed by the params hash in session.results."""
    key = params_key(name, payload)
    started = time.perf_counter()
    if cache:
        with ctx.lock:
            hit = ctx.session.results.get(key)
        if hit is not None:
            ctx.trace["tools"].append({"tool": name, "cached": True, "ok": True, "ms": 0.0})
            return hit.data
    record = {"tool": name, "cached": False}
    try:
        out = ctx.engine.ostrovok.call(name, payload)
        record["ok"] = True
    except ToolError as exc:
        record.update(ok=False, error=exc.message, code=exc.code)
        raise
    finally:
        record["ms"] = round((time.perf_counter() - started) * 1000, 2)
        with ctx.lock:
            ctx.trace["tools"].append(record)
    if cache:
        with ctx.lock:
            store_result(ctx.session, CachedResult(key=key, kind=name, params=payload,
                                                   fetched_at=ctx.session.clock(), data=out))
    return out


def tool_safe(ctx, name, payload, cache=True):
    try:
        return tool(ctx, name, payload, cache)
    except ToolError:
        return None


def od(ctx, method, *args, **kwargs):
    """Call an OpenData method; a missing method or a failure is recorded and returns None."""
    data = ctx.engine.opendata
    fn = getattr(data, method, None) if data is not None else None
    if fn is None:
        ctx.trace["notes"].append(f"opendata.{method} unavailable")
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # open data is best effort: absent data is stated honestly
        ctx.trace["errors"].append(f"opendata.{method}: {type(exc).__name__}: {exc}")
        return None


def norm_destination(rec):
    """OpenData destination record -> {id, name, country, airports, stations, lat, lon, kind, tags, description}."""
    rec = as_dict(rec)
    if not rec:
        return None
    out = dict(rec)
    out["id"] = rec.get("id") or rec.get("destination_id")
    out["name"] = rec.get("name") or rec.get("name_ru") or rec.get("name_en") or out["id"]
    out["country"] = rec.get("country") or rec.get("country_ru")
    out["airports"] = [a for a in _json_list(rec.get("airports")) if a]
    out["stations"] = [s for s in _json_list(rec.get("stations")) if s]
    out["tags"] = _json_list(rec.get("tags") or rec.get("traits"))
    out["description"] = rec.get("description_ru") or rec.get("description")
    return out


def destination_record(ctx, dest_id=None, name=None):
    """Destination by catalog id (OpenData.destination / get_destination) or by name (geocode)."""
    cache = ctx.__dict__.setdefault("_dest_cache", {})
    key = ("id", dest_id) if dest_id else ("name", (name or "").casefold())
    if key in cache:
        return cache[key]
    rec = None
    if dest_id:
        for method in ("destination", "get_destination"):
            if getattr(ctx.engine.opendata, method, None) is not None:
                rec = od(ctx, method, dest_id)
                break
        else:
            rec = od(ctx, "geocode", dest_id)
    elif name:
        rec = as_dict(od(ctx, "geocode", name))
        if rec and rec.get("id") and rec.get("in_catalog", True):
            full = None
            for method in ("destination", "get_destination"):
                if getattr(ctx.engine.opendata, method, None) is not None:
                    full = od(ctx, method, rec["id"])
                    break
            rec = full or rec
        elif rec and rec.get("in_catalog") is False:
            rec = None
    rec = norm_destination(rec)
    if rec is not None and not rec.get("id"):
        rec = None   # geocoded but not in the catalog: unknown for the demo
    cache[key] = rec
    return rec


def resolve_destination(ctx, name, allow_lazy=True):
    """Catalog first; one lazy city build per unknown name per session (data_build.lazy)."""
    rec = destination_record(ctx, name=name)
    if rec is not None or not allow_lazy:
        return rec
    builder = ctx.engine.lazy_city
    if builder is None:
        return None
    key = "lazy_city:" + name.casefold()
    if key in ctx.session.results:
        return None
    built_id = None
    try:
        built_id = builder(name)
    except Exception as exc:
        ctx.trace["errors"].append(f"lazy_city: {type(exc).__name__}: {exc}")
    store_result(ctx.session, CachedResult(key=key, kind="lazy_city", params={"name": name},
                                           fetched_at=ctx.session.clock(), data=built_id))
    if built_id:
        ctx.__dict__.get("_dest_cache", {}).clear()
        return destination_record(ctx, dest_id=built_id)
    return None


def set_destination(brief, rec, source=None, confidence=None):
    """Write the resolved destination record into the brief (id/name/country) keeping provenance."""
    value = {"id": rec["id"], "name": rec["name"], "country": rec.get("country")}
    if source is not None:
        return assume(brief, "destination", value, source, confidence)
    data = brief.model_dump(mode="json")
    data["destination"]["value"] = value
    return Brief.model_validate(data)


def _iso(d):
    return d.isoformat() if isinstance(d, date) else str(d)


def _date(value):
    return date.fromisoformat(str(value)[:10]) if value else None


def _dt(value):
    return datetime.fromisoformat(value) if value else None


def _hhmm_after(dt):
    """Next full hour at or after dt -> 'HH:MM'."""
    if dt.minute or dt.second:
        dt = dt.replace(minute=0, second=0) + timedelta(hours=1)
    return f"{dt.hour:02d}:00"


# ---------------------------------------------------------------- brief normalisation (decisions 2, 7)

def pattern_values(ctx):
    """Insights patterns for questions' recommended choice (flag insights, stage 5)."""
    if not ctx.flags.insights:
        return {}
    party = ctx.brief.values().get("party") or {}
    segment = party.get("segment") or "other"
    out = tool_safe(ctx, "insights_segment_preferences", {"segment": segment})
    if not out or not out.get("available"):
        return {}
    values = {}
    if out.get("median_nights"):
        values["dates.nights"] = int(round(out["median_nights"]))
    if out.get("budget_rub_percentiles"):
        values["budget_rub.max"] = out["budget_rub_percentiles"].get("p50")
    return values


def recommended(ctx, path):
    patterns = ctx.__dict__.setdefault("_patterns", None)
    if patterns is None:
        patterns = ctx.__dict__["_patterns"] = pattern_values(ctx)
    return P.recommended_value(ctx.skill, path, profile_value(ctx.profile, path), patterns.get(path))


def normalize_brief(ctx, brief):
    """Profile values, segment defaults, party counts, nights/month/scope defaults (never over the user)."""
    skill = ctx.skill
    brief = apply_profile(brief, ctx.profile)
    # origin
    if not brief.has("origin"):
        city = recommended(ctx, "origin.city")
        if city:
            brief = assume(brief, "origin", {"city": city, "iata": []}, "default")
    # party (decision 2)
    party = brief.values().get("party")
    if party is None:
        seg = recommended(ctx, "party.segment") or "other"
        brief = assume(brief, "party.segment", seg, "default")
    else:
        if party.get("segment") == "other" and brief.source_of("party.segment") != "user" and (
                brief.source_of("party.adults") == "user" or brief.source_of("party.children") == "user"):
            a, c = int(party.get("adults") or 0), int(party.get("children") or 0)
            seg = "family" if c else "solo" if a == 1 else "couple" if a == 2 else "friends"
            brief = assume(brief, "party.segment", seg, "inferred")
    party = brief.values()["party"]
    defaults = ((skill.segments or {}).get(party["segment"]) or {}).get("party") or {}
    for key in ("adults", "children"):
        if key in defaults and brief.source_of(f"party.{key}") != "user" and party.get(key) != defaults[key]:
            brief = assume(brief, f"party.{key}", defaults[key], "default")
    party = brief.values()["party"]
    children = int(party.get("children") or 0)
    if children and party.get("children_ages") is None:
        q = (skill.questions or {}).get("party.children_ages") or {}
        choices = q.get("choices") or []
        if choices:   # one representative age per child (decision 7); asked by sensitivity
            rep = choices[(len(choices) - 1) // 2]["value"]
            rep = rep if isinstance(rep, list) else [rep]
            brief = assume(brief, "party.children_ages", (rep * children)[:children], "default")
    elif not children and party.get("children_ages") and brief.source_of("party.children_ages") != "user":
        brief = assume(brief, "party.children_ages", None, "default")
    # segment default prefs (chips)
    seg_prefs = ((skill.segments or {}).get(brief.values()["party"]["segment"]) or {}).get("prefs") or {}
    for key, val in seg_prefs.items():
        if brief.source_of(f"prefs.{key}") in ("user", "profile") and brief.prefs.value is not None:
            continue
        if (brief.values().get("prefs") or {}).get(key) != val or brief.prefs.value is None:
            brief = assume(brief, f"prefs.{key}", val, "default")
    # dates
    dates = brief.values().get("dates") or {}
    start, end, nights = _date(dates.get("start")), _date(dates.get("end")), dates.get("nights")
    if start and end and end > start:
        span = (end - start).days
        if nights != span:
            if brief.source_of("dates.nights") == "user" and brief.source_of("dates.end") != "user":
                brief = assume(brief, "dates.end", _iso(start + timedelta(days=nights)), "inferred")
            elif brief.source_of("dates.nights") != "user":
                brief = assume(brief, "dates.nights", span, "inferred")
    elif start and not end:
        if not nights:
            nights = recommended(ctx, "dates.nights") or 7
            brief = assume(brief, "dates.nights", nights, "default")
        brief = assume(brief, "dates.end", _iso(start + timedelta(days=int(nights))), "inferred")
    else:
        if not nights:
            rec = recommended(ctx, "dates.nights")
            if rec:
                brief = assume(brief, "dates.nights", rec, "default")
        if not start and not dates.get("month"):
            brief = assume(brief, "dates.month", (ctx.session.today() + timedelta(days=30)).month, "default")
    # scope
    if not brief.has("scope"):
        transport = (brief.values().get("prefs") or {}).get("transport")
        scope = ["trains", "stay", "transfers"] if transport == "train" else list(P.SCOPE_FULL)
        brief = assume(brief, "scope", scope, "default")
    return brief


def prepare_brief(ctx, brief, patch, allow_lazy=True):
    """Patch + destination resolution + normalisation -> (brief, changed_paths, unknown_name)."""
    new, changed = apply_patch(brief, patch)
    unknown = None
    dest = new.values().get("destination")
    if ctx.destination_hint:
        rec = destination_record(ctx, dest_id=ctx.destination_hint)
        if rec is not None:
            new = set_destination(new, rec)
    elif dest and not dest.get("id"):
        rec = resolve_destination(ctx, dest["name"], allow_lazy=allow_lazy)
        if rec is not None:
            new = set_destination(new, rec)
        else:
            unknown = dest["name"]
            new, _ = apply_patch(new, {"unset": ["destination.name"]})
            new.destination.source = brief.destination.source
    new = normalize_brief(ctx, new)
    return new, changed_paths(brief, new), unknown


# ---------------------------------------------------------------- steps: brief

def step_apply_patch(ctx):
    if ctx.patch_applied:
        return
    before = ctx.session.brief
    ctx.brief_before = before
    new, changed, unknown = prepare_brief(ctx, before, ctx.patch)
    if unknown:
        ctx.unknown_destination = unknown
        ctx.note(f"«{unknown}» в демо-каталоге пока нет — показываю похожие направления.")
    old_leaf, new_leaf = P_leaves(before), P_leaves(new)
    ctx.changes = ([{"field": "dates.shift_days", "before": 0, "after": ctx.shift_days}] if ctx.shift_days else [])
    ctx.changes += [{"field": p, "before": old_leaf.get(p), "after": new_leaf.get(p)} for p in changed]
    ctx.changed = changed
    ctx.session.brief = new
    ctx.patch_applied = True


def P_leaves(brief):
    from .state import _leaf_values  # same leaf view as changed_paths
    return _leaf_values(brief)


def step_resolve_refs(ctx):
    for ref in ctx.refs:
        kind, label = ref.get("kind"), ref.get("ref")
        if kind not in ("destination", "variant", "hotel"):
            continue
        item = ctx.session.find_shown(kind, label)
        if item is None:
            ctx.trace["notes"].append(f"unresolved ref {kind}:{label}")
            continue
        if kind == "destination":
            ctx.destination_hint = item.id
            ctx.patch.setdefault("set", {})["destination.name"] = item.title
            ctx.patch_applied = False
        elif kind == "variant" and item.id not in ctx.variant_ids:
            ctx.variant_ids.append(item.id)
        elif kind == "hotel" and item.id not in ctx.hotel_refs:
            ctx.hotel_refs.append(item.id)


# ---------------------------------------------------------------- image (decision 3)

LANDSCAPE_TRAITS = {"beach": ["beach", "sea"], "coast": ["sea"], "mountains": ["mountains"], "forest": ["forest"],
                    "desert": ["desert"], "lake": ["lake"], "river": ["river"], "island": ["island"],
                    "plains": ["nature"], "old_town": ["history", "architecture"], "skyline": ["city"],
                    "countryside": ["village", "nature"], "snow": ["snow"]}
WATER_TRAITS = {"sea": ["sea"], "lake": ["lake"], "river": ["river"]}
CLIMATE_TRAITS = {"tropical": ["tropical", "warm"], "warm": ["warm"], "cold": ["cold"], "snow": ["snow"]}
SETTING_TRAITS = {"city": ["city"], "resort": ["resort"], "nature": ["nature"], "village": ["village"]}
VIBE_MOODS = ("relax", "party", "culture", "active", "romantic", "kids_fun")


def image_traits(image):
    t = (image or {}).get("traits") or {}
    traits = []
    for land in t.get("landscape") or []:
        traits += LANDSCAPE_TRAITS.get(land, [])
    traits += WATER_TRAITS.get(t.get("water"), [])
    traits += CLIMATE_TRAITS.get(t.get("climate"), [])
    traits += SETTING_TRAITS.get(t.get("setting"), [])
    moods = [v for v in t.get("vibe") or [] if v in VIBE_MOODS]
    return list(dict.fromkeys(traits)), list(dict.fromkeys(moods))


def place_question(ctx, rec):
    """Confirmation question from SkillPackage.place_confirm (questions.json), "{place}" filled by code."""
    spec = getattr(ctx.skill, "place_confirm", None) or {}
    name = rec["name"]

    def fill(value):
        return value.replace("{place}", name) if isinstance(value, str) else value
    choices = spec.get("choices") or [{"label": "да, туда", "value": "{place}", "recommended": True},
                                      {"label": "нет, просто похоже", "value": None}]
    return Question(id=spec.get("id") or "q_place", field=spec.get("field") or "destination.name",
                    text=fill(spec.get("text") or "На фото {place}?"),
                    choices=[P.Choice(label=fill(c["label"]), value=fill(c.get("value")),
                                      recommended=bool(c.get("recommended"))) for c in choices],
                    reason="image_confirm")


def run_image_match(ctx):
    """Pre-route photo decision (foundation decision 3 + image.want):

    want=here, conf >= place_min_conf, place resolves -> destination assumed (inferred) -> route B;
    want=here, confirm_min_conf <= conf < place_min_conf -> destinations with the place first + question;
    want=similar (or low confidence) -> traits only; a recognised catalog place may be listed first.
    """
    if ctx.image_done or not ctx.image_info:
        return
    ctx.image_done = True
    image = ctx.image_info
    rules = ctx.rules.get("image") or {}
    place_min = float(rules.get("place_min_conf", 0.8))
    confirm_min = float(rules.get("confirm_min_conf", 0.5))
    want = image.get("want") or "here"
    place = image.get("place") or None
    conf = float(place.get("confidence") or 0.0) if place else 0.0
    rec = resolve_destination(ctx, place["name"]) if place and conf >= confirm_min else None
    brief = ctx.session.brief
    user_dest = "destination.name" in (ctx.patch.get("set") or {})
    if want == "here" and rec is not None and conf >= place_min and not user_dest:
        ctx.session.brief = set_destination(brief, rec, source="inferred", confidence=conf)
        ctx.trace["notes"].append(f"image: here, place {rec['id']} at {conf:.2f} -> destination")
        return
    traits, moods = image_traits(image)
    if traits and brief.source_of("traits") != "user":
        brief = assume(brief, "traits", list(dict.fromkeys((brief.values().get("traits") or []) + traits)),
                       "inferred", conf or None)
    if moods and brief.source_of("mood") != "user":
        brief = assume(brief, "mood", list(dict.fromkeys((brief.values().get("mood") or []) + moods)), "inferred")
    ctx.session.brief = brief
    if rec is not None and not user_dest:
        ctx.image_place = rec            # listed first among the destinations
        if want == "here":
            ctx.image_question = place_question(ctx, rec)
        ctx.trace["notes"].append(f"image: {want}, place {rec['id']} at {conf:.2f} -> destinations"
                                  + (" + confirm" if want == "here" else ""))
    else:
        ctx.trace["notes"].append(f"image: {want}, traits only ({conf:.2f})")


def step_image_match(ctx):
    run_image_match(ctx)


# ---------------------------------------------------------------- inspiration (entry A)

def _month(brief):
    d = brief.values().get("dates") or {}
    if d.get("start"):
        return int(str(d["start"])[5:7])
    return d.get("month")


def step_find_destinations(ctx):
    b = ctx.brief.values()
    limit = int(ctx.rules.get("max_destinations", 6))
    month = _month(ctx.brief)
    recs = od(ctx, "find_destinations", traits=b.get("traits") or [], month=month,
              origin_city=(b.get("origin") or {}).get("city"), budget=(b.get("budget_rub") or {}).get("max"),
              max_travel_hours=(b.get("prefs") or {}).get("max_travel_hours"),
              segment=(b.get("party") or {}).get("segment"), limit=limit)
    recs = [norm_destination(r) for r in (recs or [])]
    recs = [r for r in recs if r and r.get("id")]
    if ctx.image_place:
        recs = [ctx.image_place] + [r for r in recs if r["id"] != ctx.image_place["id"]]
    if not recs:
        ctx.fail("no_data", "Под такой запрос в демо-каталоге направлений не нашлось. Попробуйте ослабить "
                            "условия — например, другой месяц или бюджет.")
    ctx.destination_recs = recs[:limit]
    if ctx.flags.insights and month:
        out = tool_safe(ctx, "insights_popular_destinations",
                        {"origin_city": (b.get("origin") or {}).get("city") or "", "month": month,
                         "segment": (b.get("party") or {}).get("segment") or "other"})
        if out:
            ids = {r["id"] for r in ctx.destination_recs}
            ctx.insight = {"popular_ids": [i["destination_id"] for i in out.get("items") or []
                                           if i.get("destination_id") in ids]}


def _party_payload(brief):
    party = brief.values().get("party") or {}
    return {"adults": max(1, int(party.get("adults") or 1)), "children_ages": list(party.get("children_ages") or [])}


def _travel_hours(value):
    value = as_dict(value)
    if isinstance(value, dict):
        for k in ("hours", "travel_time_h", "door_to_door_h", "duration_h"):
            if isinstance(value.get(k), (int, float)):
                return round(float(value[k]), 1)
        return None
    return round(float(value), 1) if isinstance(value, (int, float)) else None


def _weather(value):
    value = as_dict(value)
    if not isinstance(value, dict) or not isinstance(value.get("air_max_c"), (int, float)):
        return None
    rain = value.get("rain_days")
    return {"air_max_c": value["air_max_c"], "sea_c": value.get("sea_c"),
            "rain_days": rain if isinstance(rain, (int, float)) and rain >= 0 else 0}


def quote_payload(ctx, brief, ids):
    b = brief.values()
    dates = b.get("dates") or {}
    payload = {"destination_ids": list(ids), "origin_city": (b.get("origin") or {}).get("city") or "",
               "party": _party_payload(brief), "today": ctx.session.today().isoformat()}
    if dates.get("start") and dates.get("end"):
        payload["dates"] = {"check_in": dates["start"], "check_out": dates["end"]}
    else:
        payload["month"] = _month(brief) or (ctx.session.today() + timedelta(days=30)).month
        if dates.get("nights"):
            payload["nights"] = dates["nights"]
    return payload


def step_quote_destinations(ctx):
    brief = ctx.brief
    b = brief.values()
    dates = b.get("dates") or {}
    origin = (b.get("origin") or {}).get("city") or ""
    payload = quote_payload(ctx, brief, [r["id"] for r in ctx.destination_recs])
    out = tool(ctx, "quote_destinations", payload)
    quotes = {q["destination_id"]: q for q in out.get("quotes") or []}
    month = payload.get("month") or _month(brief)
    plabel = P.party_label(b.get("party"))
    traits = set(b.get("traits") or [])
    items = []
    for rec in ctx.destination_recs:
        q = quotes.get(rec["id"]) or {}
        ok = q.get("available")
        nights = q.get("nights") or dates.get("nights") or 7
        facts = {k: q.get(k) for k in ("flight_price_rub", "stay_price_rub", "check_in", "check_out")
                 if ok and q.get(k) is not None}
        if q.get("currency_notes"):
            facts["currency_notes"] = q["currency_notes"]
        if rec.get("kind"):
            facts["kind"] = rec["kind"]
        if rec.get("description"):
            facts["description"] = rec["description"]
        match = rec.get("match") or [t for t in rec.get("tags") or [] if t in traits]
        items.append({
            "id": rec["id"], "label": "", "name": rec["name"], "country": rec.get("country") or "",
            "price_from_rub": q.get("total_rub") if ok else None, "nights": int(nights), "party_label": plabel,
            "weather": _weather(rec.get("weather")) if rec.get("weather") else (
                _weather(od(ctx, "climate", rec["id"], month)) if month else None),
            "travel_time_h": _travel_hours(rec.get("travel_time_h")) if rec.get("travel_time_h") is not None
            else _travel_hours(od(ctx, "travel_time", origin, rec["id"])),
            "match": list(match), "facts": facts, "image_url": rec.get("image_url")})
    labels = P.record_shown(ctx.session, "destination", [(i["id"], i["name"]) for i in items])
    for i in items:
        i["label"] = labels[i["id"]]
    ctx.destination_items = items


# ---------------------------------------------------------------- search (entries B-E)

def resolve_origin_iata(ctx):
    origin = ctx.brief.origin.value
    if origin is None:
        return []
    if origin.iata:
        return list(origin.iata)
    iata = [a for a in (od(ctx, "origin_airports", origin.city) or []) if a]
    rec = {} if iata else (as_dict(od(ctx, "geocode", origin.city)) or {})
    iata = iata or [a for a in _json_list(rec.get("airports")) if a]
    if not iata and rec.get("lat") is not None and rec.get("lon") is not None:
        near = od(ctx, "airports_near", rec["lat"], rec["lon"]) or []
        iata = [as_dict(a).get("iata") for a in near if as_dict(a).get("iata")][:3]
    if iata:
        origin.iata = iata
    return iata


def resolve_dates(ctx, dest):
    """Concrete check-in/out: the brief's dates, else the mock's cheapest window in the month (inferred)."""
    brief = ctx.session.brief
    d = brief.values().get("dates") or {}
    start, end = _date(d.get("start")), _date(d.get("end"))
    nights = int(d.get("nights") or 7)
    if start and not end:
        end = start + timedelta(days=nights)
    if not (start and end):
        month = _month(brief) or (ctx.session.today() + timedelta(days=30)).month
        out = tool_safe(ctx, "quote_destinations", {
            "destination_ids": [dest["id"]], "origin_city": (brief.values().get("origin") or {}).get("city") or "",
            "month": month, "nights": nights, "party": _party_payload(brief),
            "today": ctx.session.today().isoformat()})
        q = next((q for q in (out or {}).get("quotes") or [] if q.get("available") and q.get("check_in")), None)
        if q:
            start, end = _date(q["check_in"]), _date(q["check_out"])
        else:
            today = ctx.session.today()
            year = today.year if month > today.month or (month == today.month and today.day < 20) else today.year + 1
            start = max(date(year, month, 1), today + timedelta(days=7))
            end = start + timedelta(days=nights)
        if brief.source_of("dates.start") != "user":
            brief = assume(brief, "dates.start", start.isoformat(), "inferred")
        if brief.source_of("dates.end") != "user":
            brief = assume(brief, "dates.end", end.isoformat(), "inferred")
        ctx.session.brief = brief
    return start, end


def _flight_party(brief, start):
    party = brief.values().get("party") or {}
    ages = list(party.get("children_ages") or [])
    infants = sum(1 for a in ages if a < 2)
    children = sum(1 for a in ages if 2 <= a < 12)
    adults = max(1, int(party.get("adults") or 1)) + sum(1 for a in ages if a >= 12)
    return adults, children, infants


def _hotel_filters(brief):
    v = brief.values()
    prefs = v.get("prefs") or {}
    party = v.get("party") or {}
    f = {}
    if prefs.get("meals") not in (None, "any"):
        f["meals"] = prefs["meals"]
    if prefs.get("free_cancellation"):
        f["free_cancellation"] = True
    if prefs.get("stars_min"):
        f["stars_min"] = prefs["stars_min"]
    if party.get("pets") or any(str(c).startswith("pet:") for c in v.get("constraints") or []):
        f["pets"] = True
    return f


def _cheapest(offers, price="price_rub"):
    return min(offers, key=lambda o: o.get(price) or 0) if offers else None


def _transfer_hotels(hotels):
    """Every distinct hotel of the search: a variant built on a hotel without transfer quotes would
    wrongly say «трансфер не нашли» (the mock answers these in-process in ~2 ms each)."""
    picks, seen = [], set()
    for h in hotels:
        if h["hotel_id"] not in seen:
            seen.add(h["hotel_id"])
            picks.append(h)
    return picks[:TRANSFER_HOTELS]


def run_search(ctx, brief, dest, check_in, check_out, track=True):
    """Every search the scope needs, in parallel, cached by params. -> (results, districts, called_tools)."""
    v = brief.values()
    scope = v.get("scope") or list(P.SCOPE_FULL)
    transport = (v.get("prefs") or {}).get("transport") or "any"
    today = ctx.session.today().isoformat()
    party = _party_payload(brief)
    adults, children, infants = _flight_party(brief, check_in)
    pax = party["adults"] + len(party["children_ages"])
    origin = v.get("origin") or {}
    flex = min(3, int((v.get("dates") or {}).get("flexible_days") or 0))
    before = len(ctx.trace["tools"])
    jobs = {}
    if "flights" in scope and transport != "train":
        o_iata = resolve_origin_iata(ctx)
        d_iata = dest.get("airports") or []
        if o_iata and d_iata:
            base = {"adults": adults, "children": children, "infants": infants, "flex_days": flex, "today": today}
            jobs["flights.out"] = ("search_flights", {**base, "origin_iata": o_iata, "dest_iata": d_iata,
                                                       "date": check_in.isoformat()})
            jobs["flights.back"] = ("search_flights", {**base, "origin_iata": d_iata, "dest_iata": o_iata,
                                                        "date": check_out.isoformat()})
    if "trains" in scope or transport == "train":
        base = {"passengers": pax, "today": today}
        jobs["trains.out"] = ("search_trains", {**base, "origin_city": origin.get("city") or "",
                                                "dest_city": dest["name"], "date": check_in.isoformat()})
        jobs["trains.back"] = ("search_trains", {**base, "origin_city": dest["name"],
                                                 "dest_city": origin.get("city") or "", "date": check_out.isoformat()})
    if "stay" in scope:
        jobs["hotels"] = ("search_hotels", {"destination_id": dest["id"], "check_in": check_in.isoformat(),
                                            "check_out": check_out.isoformat(), "adults": party["adults"],
                                            "children_ages": party["children_ages"], "filters": _hotel_filters(brief),
                                            "limit": 20, "today": today})
    out = _parallel(ctx, jobs)
    results = {"origin": origin, "destination": dest, "check_in": check_in.isoformat(),
               "check_out": check_out.isoformat(), "party": party, "scope": list(scope),
               "flights_out": (out.get("flights.out") or {}).get("offers", []),
               "flights_back": (out.get("flights.back") or {}).get("offers", []),
               "trains_out": (out.get("trains.out") or {}).get("offers", []),
               "trains_back": (out.get("trains.back") or {}).get("offers", []),
               "hotels": (out.get("hotels") or {}).get("offers", []), "transfers_out": {}, "transfers_back": {}}
    if "transfers" in scope and results["hotels"]:
        # One transfer quote per hotel and per arrival/departure point that any offered leg uses
        # (a destination can have several airports, e.g. Antalya AYT and Alanya GZP): ranking pairs each
        # leg with the transfer from its own point, so quoting only the cheapest leg's point leaves the
        # other legs with «трансфер не нашли» and a wrong ride time.
        arrive, depart = {}, {}
        for f in sorted(results["flights_out"], key=lambda o: o.get("arr") or ""):
            arrive.setdefault(f["dest_iata"], ({"iata": f["dest_iata"]}, f["arr"]))
        for t in sorted(results["trains_out"], key=lambda o: o.get("arr") or ""):
            arrive.setdefault(t["to_station"], ({"station": t["to_station"]}, t["arr"]))
        for f in sorted(results["flights_back"], key=lambda o: o.get("dep") or ""):
            depart.setdefault(f["origin_iata"], ({"iata": f["origin_iata"]},
                                                 (_dt(f["dep"]) - timedelta(hours=3)).isoformat(timespec="minutes")))
        for t in sorted(results["trains_back"], key=lambda o: o.get("dep") or ""):
            depart.setdefault(t["from_station"], ({"station": t["from_station"]},
                                                  (_dt(t["dep"]) - timedelta(minutes=90)).isoformat(timespec="minutes")))
        tjobs, meta = {}, {}
        for h in _transfer_hotels(results["hotels"]):
            for point, (frm, at) in arrive.items():
                key = f"{h['hotel_id']}|out|{point}"
                tjobs[key] = ("search_transfers", {"from": frm, "to": {"hotel_id": h["hotel_id"]}, "at": at,
                                                   "pax": pax, "today": today})
                meta[key] = (h["hotel_id"], "out", point, frm)
            for point, (frm, at) in depart.items():
                key = f"{h['hotel_id']}|back|{point}"
                tjobs[key] = ("search_transfers", {"from": frm, "to": {"hotel_id": h["hotel_id"]}, "reverse": True,
                                                   "at": at, "pax": pax, "today": today})
                meta[key] = (h["hotel_id"], "back", point, frm)
        tout = _parallel(ctx, tjobs, tolerant=True)   # a missing transfer is a risk, not a failed search
        for key, value in tout.items():
            hotel_id, direction, point, frm = meta[key]
            offers = [{**o, "point": point, "point_ref": frm, "hotel_id": hotel_id}
                      for o in (value or {}).get("offers", [])]
            results[f"transfers_{direction}"].setdefault(hotel_id, []).extend(offers)
    districts = {}
    for h in results["hotels"]:
        if h.get("osm_id") and h["osm_id"] not in districts:
            prof = od(ctx, "district_profile", h["osm_id"])
            if prof is not None:
                districts[h["osm_id"]] = prof
    called = sorted({t["tool"] for t in ctx.trace["tools"][before:] if not t.get("cached")})
    return results, districts, called


def _parallel(ctx, jobs, tolerant=False):
    """Run tool jobs in parallel. tolerant: a failed job is left out (and noted) instead of failing all."""
    if not jobs:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        futures = {k: pool.submit(tool, ctx, name, payload) for k, (name, payload) in jobs.items()}
        out, errors = {}, []
        for key, fut in futures.items():
            try:
                out[key] = fut.result()
            except ToolError as exc:
                errors.append(exc)
    if errors and not tolerant:
        raise errors[0]
    if errors:
        ctx.trace["notes"].append(f"{len(errors)} of {len(jobs)} tool jobs failed and were skipped: {errors[0]}")
    return out


def merge_prefetched(ctx):
    pf = ctx.engine.prefetcher
    if pf is None:
        return 0
    done = pf.drain(ctx.session.id)
    for rec in done.values():
        if isinstance(rec, CachedResult):
            store_result(ctx.session, rec)
    if done:
        ctx.trace["notes"].append(f"prefetch: merged {len(done)} result(s)")
    return len(done)


def current_destination(ctx):
    dest = ctx.brief.values().get("destination") or {}
    if not dest.get("id"):
        ctx.fail("no_data", "Не понял, куда именно едем — назовите город или выберите из предложенных.")
    rec = destination_record(ctx, dest_id=dest["id"])
    if rec is None:
        ctx.fail("no_data", f"Про «{dest.get('name')}» в демо-каталоге данных нет — выдумывать не буду.")
    return rec


def step_search(ctx):
    merge_prefetched(ctx)
    dest = current_destination(ctx)
    check_in, check_out = resolve_dates(ctx, dest)
    results, districts, called = run_search(ctx, ctx.session.brief, dest, check_in, check_out)
    ctx.results, ctx.districts = results, districts
    ctx.rebuilt = [{"search_flights": "flights", "search_trains": "trains", "search_hotels": "hotels",
                    "search_transfers": "transfers"}.get(t, t) for t in called if t.startswith("search_")]
    for i in ctx.engine.hooks.run("after_search", ctx, kinds=("validate",)):
        ctx.issues.append(i)
    scope = results["scope"]
    if "stay" in scope and not results["hotels"]:
        ctx.fail("no_data", f"На {P.fmt_date_range(check_in, check_out)} в «{dest['name']}» свободных отелей под "
                            "ваши условия не нашлось. Попробуйте сдвинуть даты или ослабить фильтры.")


# ---------------------------------------------------------------- variants

def rules_view(ctx):
    """skill rules for ranking/checks, plus segments.json (district weights) under `segments`."""
    view = dict(ctx.rules)
    view.setdefault("segments", ctx.skill.segments)
    return view


def _as_variant(v):
    return v if isinstance(v, Variant) else Variant.model_validate(v)


def build(ctx, brief, results, districts, exclude=(), pin=True):
    if exclude:
        results = _exclude_offers(results, set(exclude))
    built = ctx.domain.build_variants(brief, results, districts, rules_view(ctx), ctx.session.today())
    variants = [_as_variant(v) for v in built or []][: int(ctx.rules.get("max_variants", 3))]
    return _pin_transfers(ctx, variants, results) if pin else variants


def _pin_transfers(ctx, variants, results):
    """Re-quote every transfer step at the exact time of its own leg.

    Transfers are searched once per hotel and arrival point (at the earliest arrival there), so the
    offer behind a step may carry another pickup time; booking it would put a car at the airport before
    the plane lands. Here each step gets the offer of the same kind at the step's own start time.
    """
    index = {}
    for key in ("transfers_out", "transfers_back"):
        for offers in (results.get(key) or {}).values():
            for o in offers:
                index[o.get("offer_id")] = (o, key == "transfers_back")
    party = _party_payload(ctx.brief)
    pax = party["adults"] + len(party["children_ages"])
    jobs, where = {}, {}
    for v in variants:
        for s in v.steps:
            if s.type != "transfer" or s.offer_id not in index:
                continue
            o, reverse = index[s.offer_id]
            if not o.get("point_ref") or not o.get("hotel_id"):
                continue
            payload = {"from": o["point_ref"], "to": {"hotel_id": o["hotel_id"]}, "at": s.start,
                       "pax": pax, "today": ctx.session.today().isoformat()}
            if reverse:
                payload["reverse"] = True
            key = f"{v.id}|{s.id}"
            jobs[key] = ("search_transfers", payload)
            where[key] = (v, s, o.get("kind"))
    found = _parallel(ctx, jobs, tolerant=True)
    for key, value in found.items():
        v, s, kind = where[key]
        offers = (value or {}).get("offers") or []
        pick = next((o for o in offers if o.get("kind") == kind), None) or (offers[0] if offers else None)
        if pick is None:
            continue
        new_rub, old_rub = pick.get("price_rub"), s.price.rub if s.price.rub is not None else s.price.amount
        s.offer_id = pick["offer_id"]
        s.link = f"ostrovok-mock:transfer/{pick['offer_id']}"
        delta = int(new_rub) - int(old_rub) if new_rub is not None and old_rub is not None else 0
        if delta:
            s.price = s.price.model_copy(update={"amount": int(pick["price_rub"]), "rub": int(pick["price_rub"])})
            v.totals = v.totals.model_copy(update={"rub": v.totals.rub + delta})
    return variants


def _exclude_offers(results, bad):
    out = dict(results)
    for key in ("flights_out", "flights_back", "trains_out", "trains_back", "hotels"):
        out[key] = [o for o in results.get(key) or [] if o.get("offer_id") not in bad]
    for key in ("transfers_out", "transfers_back"):
        out[key] = {h: [o for o in v if o.get("offer_id") not in bad] for h, v in (results.get(key) or {}).items()}
    return out


def step_build_variants(ctx):
    if ctx.results is None:
        step_search(ctx)
    if ctx.swap:
        return _swap_hotel(ctx)
    ctx.variants = build(ctx, ctx.brief, ctx.results, ctx.districts)
    ctx.session.variants = ctx.variants
    if not ctx.variants:
        ctx.fail("no_data", "Собрать поездку из найденного не получилось: не хватает билетов или отелей на эти даты. "
                            "Попробуйте другие даты.")


def _swap_hotel(ctx):
    vid, offer_id = ctx.swap["variant_id"], ctx.swap["hotel_offer_id"]
    offer = next((h for h in ctx.results["hotels"] if h.get("offer_id") == offer_id), None) or find_offer(ctx, offer_id)
    if offer is None or ctx.session.variant(vid) is None:
        ctx.fail("bad_request", "Этот отель или вариант уже не найти — обновите подборку.")
    results = dict(ctx.results)
    results["hotels"] = [offer]
    built = build(ctx, ctx.brief, results, ctx.districts)
    if not built:
        ctx.fail("no_data", "С этим отелем вариант не собирается — оставил прежний.")
    new = next((v for v in built if v.id == vid), built[0]).model_copy(update={"id": vid})
    ctx.session.variants = [new if v.id == vid else v for v in ctx.session.variants]
    ctx.variants = list(ctx.session.variants)


def check_variants_hook(ctx):
    """Default validate hook `after_variants`: agent/checks.py on every variant."""
    out = []
    check = ctx.domain.check_variant
    for v in ctx.variants:
        for i in check(v, ctx.brief, ctx.rules) or []:
            i = as_issue(i)
            i["data"].setdefault("variant_id", v.id)
            out.append(i)
    return out


def _offer_ids_of(issues, variants=()):
    """Offers to leave out of the rebuild: named by the issue, or the offer of the step it points at."""
    ids = set()
    by_id = {v.id: v for v in variants}
    for i in issues:
        d = i.get("data") or {}
        if d.get("offer_id"):
            ids.add(d["offer_id"])
        ids.update(d.get("offer_ids") or [])
        v = by_id.get(d.get("variant_id") or d.get("variant"))
        if v is not None and d.get("step"):
            step = next((s for s in v.steps if s.id == d["step"]), None)
            if step is not None and getattr(step, "offer_id", None):
                ids.add(step.offer_id)
    return ids


def step_check(ctx):
    """Validation hooks on variants; one rerun of build_variants without the failing offers, then honesty."""
    if not ctx.variants:
        return
    issues = ctx.engine.hooks.run("after_variants", ctx, kinds=("validate",))
    if blocking(issues):
        bad = _offer_ids_of(blocking(issues), ctx.variants)
        rebuilt = build(ctx, ctx.brief, ctx.results, ctx.districts, exclude=bad) if bad else []
        ctx.trace["notes"].append(f"check: rerun build_variants without {sorted(bad)}")
        if rebuilt:
            ctx.variants = rebuilt
            ctx.session.variants = rebuilt
        issues = ctx.engine.hooks.run("after_variants", ctx, kinds=("validate",))
        broken = {(i.get("data") or {}).get("variant_id") for i in blocking(issues)}
        kept = [v for v in ctx.variants if v.id not in broken]
        if blocking(issues) and kept:
            # a variant that still breaks a hard rule is not an offer: show the sound ones only
            ctx.trace["notes"].append(f"check: dropped {sorted(broken)} after the rerun")
            ctx.variants = kept
            ctx.session.variants = kept
            issues = [i for i in issues if (i.get("data") or {}).get("variant_id") not in broken]
        if blocking(issues):
            ctx.degraded = True
            for i in blocking(issues):
                v = ctx.session.variant((i.get("data") or {}).get("variant_id"))
                if v is not None and all(n.code != i["code"] for n in v.risks):
                    v.risks.append(P_note(i))
    ctx.issues.extend(issues)


def P_note(i):
    from .state import Note
    data = {k: v for k, v in (i.get("data") or {}).items() if isinstance(v, (int, float, str, bool)) or v is None}
    return Note(code=i["code"], text=i["message"] or i["code"], data=data)


def rerun_for(ctx):
    """`rerun` for ranking.sensitivity: variants for an alternative brief (searches cached by params)."""
    def rerun(brief2):
        brief2 = normalize_brief(ctx, brief2)
        dest = current_destination(ctx)
        d = brief2.values().get("dates") or {}
        start, end = _date(d.get("start")), _date(d.get("end"))
        if not (start and end):
            start, end = _date(ctx.results["check_in"]), _date(ctx.results["check_out"])
        results, districts, _ = run_search(ctx, brief2, dest, start, end, track=False)
        return build(ctx, brief2, results, districts, pin=False)   # sensitivity only compares, never books
    return rerun


def requote_for(ctx):
    """`rerun` for destination items: the same destinations re-quoted for an alternative brief."""
    def rerun(brief2):
        brief2 = normalize_brief(ctx, brief2)
        out = tool(ctx, "quote_destinations", quote_payload(ctx, brief2, [r["id"] for r in ctx.destination_recs]))
        quotes = {q["destination_id"]: q for q in out.get("quotes") or []}
        return [{"id": r["id"], "price_from_rub": (quotes.get(r["id"]) or {}).get("total_rub")
                 if (quotes.get(r["id"]) or {}).get("available") else None} for r in ctx.destination_recs]
    return rerun


def _as_question(q):
    return q if isinstance(q, Question) else Question.model_validate(q)


def _with_recommendation(ctx, q):
    if any(c.recommended for c in q.choices):
        return q
    rebuilt = P.build_question(ctx.skill, q.field, profile_value(ctx.profile, q.field),
                               (ctx.__dict__.get("_patterns") or {}).get(q.field), reason=q.reason)
    return rebuilt or q


def step_sensitivity(ctx):
    """Questions (<= rules.max_questions): ask only when the answer changes the result (ranking.sensitivity)."""
    limit = int(ctx.rules.get("max_questions", 2))
    questions = [ctx.image_question] if ctx.image_question else []
    if ctx.flags.sensitivity:
        if ctx.variants:
            found = ctx.domain.sensitivity(ctx.brief, ctx.variants, rerun_for(ctx), ctx.skill) or []
        elif ctx.destination_items:
            found = ctx.domain.sensitivity(ctx.brief, ctx.destination_items, requote_for(ctx), ctx.skill) or []
        else:
            found = []
        found = [_with_recommendation(ctx, _as_question(q)) for q in found]
        asked = {q.field for q in questions}
        questions += [q for q in found if q.field not in asked]
    ctx.questions = questions[:limit]


def step_ask_or_assume(ctx):
    if not ctx.flags.jev_ask or not ctx.questions:
        return
    from .jev_policy import ask_or_assume
    fixed = [q for q in ctx.questions if q.reason == "image_confirm"]
    candidates = [q for q in ctx.questions if q.reason != "image_confirm"]
    ask, assumed = ask_or_assume(ctx.engine.jev, ctx.brief, candidates, ctx.skill, ctx.trace["jev"])
    brief = ctx.brief
    current = P_leaves(brief)
    for path, value in assumed:
        if current.get(path) == value:     # same value the results were built with: only provenance changes
            brief = assume(brief, path, value, "inferred")
        else:
            ask.append(next(q for q in candidates if q.field == path))
    ctx.session.brief = brief
    ctx.questions = fixed + [q for q in candidates if q in ask]


# ---------------------------------------------------------------- upsell

def find_offer(ctx, offer_id):
    if not offer_id:
        return None
    with ctx.lock:
        records = list(ctx.session.results.values())
    for rec in records:
        data = rec.data if isinstance(rec.data, dict) else {}
        for offer in data.get("offers") or []:
            if isinstance(offer, dict) and offer.get("offer_id") == offer_id:
                return offer
    return None


def upsell_offers(ctx, variant):
    """Offers the mock actually sells for this variant, keyed by upsell kind (agent/upsell.offers_for_variant).

    The variant's hotel is re-searched with `hotel_id` + `all_rooms` (one offer per fitting room type,
    cached) so room upgrades exist to choose from; the extra offers only feed the upsell, not ranking.
    """
    results = dict(ctx.results or {})
    hotel = find_offer(ctx, variant.hotel.offer_id) if variant.hotel else None
    if hotel is not None and results.get("check_in"):
        party = results.get("party") or _party_payload(ctx.brief)
        rooms = tool_safe(ctx, "search_hotels", {
            "destination_id": results["destination"]["id"], "check_in": results["check_in"],
            "check_out": results["check_out"], "adults": party["adults"], "children_ages": party["children_ages"],
            "hotel_id": hotel["hotel_id"], "all_rooms": True, "limit": 20, "today": ctx.session.today().isoformat()})
        known = {h.get("offer_id") for h in results.get("hotels") or []}
        extra = [o for o in (rooms or {}).get("offers") or [] if o.get("offer_id") not in known]
        results["hotels"] = list(results.get("hotels") or []) + extra
    return ctx.domain.offers_for_variant(variant, results)


def _params(fn):
    import inspect
    try:
        return set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return set()


def _as_candidate(c):
    return c if isinstance(c, UpsellCandidate) else UpsellCandidate.model_validate(c)


def step_upsell(ctx):
    if not ctx.flags.upsell or not ctx.variants:
        return
    fn = ctx.domain.upsell_candidates
    seen, ordered = set(), []
    takes_rules = "rules" in _params(fn)
    for v in ctx.variants:
        kw = {"rules": ctx.rules} if takes_rules else {}
        cands = [_as_candidate(c) for c in fn(ctx.brief, v, upsell_offers(ctx, v), ctx.skill.upsell, **kw) or []]
        cands = [c if c.variant_id else c.model_copy(update={"variant_id": v.id}) for c in cands]
        v.upsell_candidates = cands
        for c in cands:
            if c.id not in seen:
                seen.add(c.id)
                ordered.append(c)
    ctx.upsell = ordered


# ---------------------------------------------------------------- compare / info

def _cancel_text(offer):
    c = (offer or {}).get("cancellation") or {}
    if c.get("free_until"):
        d = _date(c["free_until"])
        return f"бесплатно до {d.day} {P.MONTHS_GEN[d.month - 1]}", 2
    if c.get("penalty_pct") is not None and c["penalty_pct"] < 100:
        return f"штраф {c['penalty_pct']} %", 1
    return "невозвратный", 0


def comparison_rows(ctx, variants):
    hotels = {v.id: find_offer(ctx, v.hotel.offer_id) if v.hotel else None for v in variants}
    ids = [v.id for v in variants]

    def best(scores, highest=True):
        vals = {k: s for k, s in scores.items() if s is not None}
        if not vals:
            return None
        top = max(vals.values()) if highest else min(vals.values())
        winners = [k for k, s in vals.items() if s == top]
        return winners[0] if len(winners) == 1 else None

    rows = [
        {"key": "price", "label": "Цена за всё", "values": {v.id: P.fmt_rub(v.totals.rub) for v in variants},
         "best_id": best({v.id: v.totals.rub for v in variants}, highest=False)},
        {"key": "door_to_door", "label": "В дороге, туда и обратно",
         "values": {v.id: P.fmt_hours(v.totals.door_to_door_hours) for v in variants},
         "best_id": best({v.id: v.totals.door_to_door_hours for v in variants}, highest=False)}]
    if any(v.hotel for v in variants):
        rows.append({"key": "hotel", "label": "Отель", "values": {
            v.id: (f"{v.hotel.stars or '–'}★, {v.hotel.rating if v.hotel.rating is not None else '–'}"
                   if v.hotel else "без отеля") for v in variants},
            "best_id": best({v.id: v.hotel.rating if v.hotel else None for v in variants})})
    if any(hotels.values()):
        rows.append({"key": "meals", "label": "Питание",
                     "values": {i: P.MEALS_RU.get((h or {}).get("meals"), "—") for i, h in hotels.items()},
                     "best_id": best({i: MEALS_RANK.get((h or {}).get("meals")) for i, h in hotels.items()})})
        rows.append({"key": "cancellation", "label": "Отмена",
                     "values": {i: _cancel_text(h)[0] if h else "—" for i, h in hotels.items()},
                     "best_id": best({i: _cancel_text(h)[1] if h else None for i, h in hotels.items()})})
    district = (ctx.brief.values().get("prefs") or {}).get("district")
    if any(v.hotel and v.hotel.district.labels for v in variants):
        rows.append({"key": "district", "label": "Район", "values": {
            v.id: ", ".join(P.DISTRICT_RU.get(lb, lb) for lb in v.hotel.district.labels) or "—" if v.hotel else "—"
            for v in variants},
            "best_id": best({v.id: v.hotel.district.scores.get(district) if v.hotel else None for v in variants})
            if district not in (None, "any") else None})
    assert all(set(r["values"]) == set(ids) for r in rows)
    return rows


def recommended_hint(variants):
    scored = [v for v in variants if v.fit is not None]
    if scored:
        return max(scored, key=lambda v: v.fit.score).id
    return variants[0].id if variants else None


def step_compare(ctx):
    ids = ctx.variant_ids or [i.id for i in ctx.session.shown.variants] or [v.id for v in ctx.session.variants]
    variants = [v for v in (ctx.session.variant(i) for i in ids) if v is not None]
    if len(variants) < 2:
        variants = list(ctx.session.variants)
    if len(variants) < 2:
        ctx.fail("no_data", "Сравнивать пока нечего: нужно хотя бы два варианта. Давайте сначала подберу их.")
    variants = variants[:4]
    ctx.variants = variants
    ctx.comparison = {"variant_ids": [v.id for v in variants], "rows": comparison_rows(ctx, variants),
                      "recommended_id": recommended_hint(variants)}


def _info_variant(ctx):
    for vid in ctx.variant_ids + ([ctx.session.chosen_variant_id] if ctx.session.chosen_variant_id else []):
        v = ctx.session.variant(vid)
        if v is not None:
            return v
    if ctx.session.variants:
        return ctx.session.variant(recommended_hint(ctx.session.variants)) or ctx.session.variants[0]
    return None


def step_info_lookup(ctx):
    """Facts only from get_hotel / climate / district data (and the order); empty when nothing is known."""
    ctx.info_question = ((ctx.understand or {}).get("info_question") or ctx.text or "").strip()
    facts = []
    v = _info_variant(ctx)
    hotel_ids = list(ctx.hotel_refs)
    if v is not None and v.hotel is not None and v.hotel.hotel_id not in hotel_ids:
        hotel_ids.append(v.hotel.hotel_id)
    for hid in hotel_ids[:2]:
        details = tool_safe(ctx, "get_hotel", {"hotel_id": hid})
        if details:
            facts.append({"kind": "hotel", "id": hid, "data": details})
            prof = od(ctx, "district_profile", details.get("osm_id")) if details.get("osm_id") else None
            if prof is not None:
                facts.append({"kind": "district", "id": details["osm_id"], "data": as_dict(prof)})
    dest = ctx.brief.values().get("destination") or {}
    month = _month(ctx.brief)
    if dest.get("id") and month:
        clim = od(ctx, "climate", dest["id"], month)
        if clim is not None:
            facts.append({"kind": "climate", "id": dest["id"], "month": month, "data": as_dict(clim)})
    if ctx.session.order_id:
        order = tool_safe(ctx, "order_get", {"order_id": ctx.session.order_id}, cache=False)
        if order:
            facts.append({"kind": "order", "id": order["order_id"], "data": order_summary(order)})
    ctx.info_facts = facts


# ---------------------------------------------------------------- cart / checkout / pay / aftercare

def now_iso(ctx):
    return ctx.session.clock().isoformat(timespec="seconds")


def step_cart_add(ctx):
    s = ctx.session
    vid = (ctx.variant_ids or [None])[0] or s.chosen_variant_id
    variant = s.variant(vid) if vid else None
    if variant is None:
        ctx.fail("bad_request", "Не понял, какой вариант положить в корзину — нажмите «Беру» на нужном.")
    if s.cart_id:
        cart = tool(ctx, "cart_get", {"session_id": s.id}, cache=False)
        if cart.get("status") == "checked_out":
            cart = tool(ctx, "cart_create", {"session_id": s.id, "now": now_iso(ctx)}, cache=False)
    else:
        cart = tool(ctx, "cart_create", {"session_id": s.id, "now": now_iso(ctx)}, cache=False)
    wanted = {st.offer_id: ITEM_KINDS[st.type] for st in variant.steps if st.offer_id and st.type in ITEM_KINDS}
    cands = {c.offer_id: c for c in variant.upsell_candidates if c.offer_id}
    for extra in ctx.extras:
        kind = UPSELL_ITEM_KIND.get(cands[extra].kind) if extra in cands else None
        offer = find_offer(ctx, extra)
        if kind is None and offer is not None:
            kind = "hotel" if "hotel_id" in offer and "room" in offer else "flight" if "flight_no" in offer else (
                "train" if "train_no" in offer else "transfer")
        if kind is None:
            ctx.fail("bad_request", "Этот доп больше недоступен — обновите варианты.")
        wanted[extra] = kind
    for item in cart.get("items") or []:
        if item["offer_id"] not in wanted:
            cart = tool(ctx, "cart_remove", {"session_id": s.id, "offer_id": item["offer_id"], "kind": item["kind"]},
                        cache=False)
    have = {i["offer_id"] for i in cart.get("items") or []}
    for offer_id, kind in wanted.items():
        if offer_id not in have:
            cart = tool(ctx, "cart_add", {"session_id": s.id, "offer_id": offer_id, "kind": kind, "now": now_iso(ctx)},
                        cache=False)
    s.cart_id = cart["cart_id"]
    s.chosen_variant_id = variant.id
    s.stage = "cart"
    ctx.cart = cart
    title = variant.title or P.VARIANT_TITLES.get(variant.id, variant.id)
    msg = f"Положил «{title}» в корзину: {P.fmt_rub(cart.get('total_rub') or 0)} за всё."
    problems = attention(cart.get("issues"))
    msg += (" Есть что проверить: " + problems) if problems else " Дальше — данные путешественников."
    ctx.message = msg


def _age_on(birth, day):
    b = _date(birth)
    return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


def after_travellers_hook(ctx):
    """Default validate hook `after_travellers`: exact ages -> brief; re-check the chosen variant."""
    travellers = (ctx.checkout_payload or {}).get("travellers") or []
    if not travellers:
        return []
    dates = ctx.brief.values().get("dates") or {}
    trip_day = _date(dates.get("start")) or ctx.session.today()
    ages = [_age_on(t["birth_date"], trip_day) for t in travellers]
    adults = sum(1 for a in ages if a >= 18)
    kids = sorted(a for a in ages if a < 18)
    party = ctx.brief.values().get("party") or {}
    out = []
    if adults != party.get("adults") or kids != sorted(party.get("children_ages") or []):
        patch = {"set": {"party.children_ages": kids or None, "party.adults": max(adults, 0)}}
        if not kids:
            patch = {"set": {"party.adults": adults, "party.children": 0}}
        try:
            ctx.session.brief, _ = apply_patch(ctx.session.brief, patch)
        except PatchError as exc:
            out.append(issue("travellers_invalid", f"Не получилось учесть данные путешественников: {exc}", "warn"))
        if len(ages) != int(party.get("adults") or 0) + int(party.get("children") or 0):
            out.append(issue("party_changed", "Путешественников другое число, чем в подборке: цены в корзине "
                                              "считались на прежний состав.", "warn"))
    variant = ctx.session.variant(ctx.session.chosen_variant_id) if ctx.session.chosen_variant_id else None
    check = ctx.domain.get("check_variant", required=False)
    if variant is not None and check is not None:
        out += [as_issue(i) for i in check(variant, ctx.session.brief, ctx.rules) or []]
    return out


def step_checkout(ctx):
    s = ctx.session
    if not s.cart_id:
        ctx.fail("bad_request", "Корзина пока пустая — сначала выберите вариант.")
    payload = ctx.checkout_payload
    if not payload or not payload.get("travellers"):
        ctx.message = "Чтобы оформить, нужны имена и даты рождения всех путешественников и контакт для билетов."
        ctx.__dict__["need_travellers"] = True
        return
    out = tool(ctx, "checkout", {"cart_id": s.cart_id, "travellers": payload["travellers"],
                                 "contact": payload.get("contact"), "now": now_iso(ctx)}, cache=False)
    s.order_id = out["order_id"]
    s.stage = "checkout"
    hook_issues = ctx.engine.hooks.run("after_travellers", ctx, kinds=("validate",))
    ctx.order = tool(ctx, "order_get", {"order_id": out["order_id"]}, cache=False)
    ctx.order_issues = list(hook_issues)
    msg = f"Заказ собран, осталось оплатить {P.fmt_rub(ctx.order['total_rub'])}."
    problems = attention(list(ctx.order.get("issues") or []) + list(hook_issues))
    if problems:
        msg += " Обратите внимание: " + problems
    ctx.message = msg


def order_summary(order):
    return {"order_id": order["order_id"], "status": order["status"], "total_rub": order["total_rub"],
            "items": [{k: i.get(k) for k in ("item_id", "kind", "title", "price_rub", "booking_number", "status",
                                             "start", "end")} for i in order.get("items") or []]}


def render_files(ctx, order):
    """agent/render.render_variant for the bought variant -> {"ics": "/files/..", "page": "/files/.."}."""
    fn = ctx.domain.get("render_variant", required=False)
    variant = ctx.session.variant(ctx.session.chosen_variant_id) if ctx.session.chosen_variant_id else None
    if fn is None or variant is None:
        ctx.trace["notes"].append("render: unavailable (no agent/render.py or no chosen variant), files null")
        return {"ics": None, "page": None}
    try:
        out = fn(ctx.session.id, variant, ctx.session.brief, ctx.rules, out_root=str(ctx.engine.files_dir),
                 now=ctx.session.clock())
    except Exception as exc:
        ctx.trace["errors"].append(f"render: {type(exc).__name__}: {exc}")
        return {"ics": None, "page": None}
    files = {}
    for key in ("ics", "page"):
        value = (out or {}).get(key)
        if value:
            value = str(value)
            if not value.startswith("/files/"):
                base = str(ctx.engine.files_dir)
                value = "/files/" + (value[len(base):].lstrip("/") if value.startswith(base) else value.lstrip("/"))
        files[key] = value or None
    return files


def step_pay(ctx):
    s = ctx.session
    order_id = ((ctx.action or {}).get("payload") or {}).get("order_id") or s.order_id
    if not order_id or order_id != s.order_id:
        ctx.fail("bad_request", "Не нашёл заказ для оплаты — оформите корзину ещё раз.")
    tool(ctx, "pay", {"order_id": order_id, "method": "mock_card", "now": now_iso(ctx)}, cache=False)
    ctx.order = tool(ctx, "order_get", {"order_id": order_id}, cache=False)
    s.stage = "ordered"
    ctx.files = render_files(ctx, ctx.order)
    offers = []
    arr, checkin = _arrival(ctx), _hotel_checkin(ctx)
    if arr is not None and (checkin is None or arr < checkin):
        offers.append("заселиться пораньше")
    if _dates_changeable(ctx):
        offers.append("поменять даты")
    if not _order_has_transfers(ctx):
        offers.append("добавить трансфер")
    ctx.message = "Оплачено, всё подтверждено!"
    if offers:
        ctx.message += (" Если захотите " + (", ".join(offers[:-1]) + " или " + offers[-1] if len(offers) > 1
                                             else offers[0]) + " — пишите.")


def attention(issues) -> str:
    """Cart/order issues worth a sentence: warnings and blockers ("info" notes stay in the cart card)."""
    texts = []
    for i in issues or []:
        sev = (i.get("severity") or "").lower()
        if sev in ("info", "") or not i.get("message"):
            continue
        t = i["message"].strip().rstrip(".")
        t = t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t
        if t not in texts:
            texts.append(t)
    return ("; ".join(texts) + ".") if texts else ""


def _hotel_checkin(ctx, order=None):
    """Booked check-in time of the order's hotel (the order item start)."""
    for item in (order or ctx.order or {}).get("items") or []:
        if item.get("kind") == "hotel" and item.get("start"):
            return _dt(item["start"])
    return None


def _dates_changeable(ctx) -> bool:
    """Dates can move only if every flight of the order can be exchanged (a fare without exchange stays put)."""
    for item in (ctx.order or {}).get("items") or []:
        if item.get("kind") == "flight" and item.get("status") != "cancelled":
            fare = (((item.get("details") or {}).get("offer") or {}).get("fare") or {})
            if fare and fare.get("change_fee_rub") is None:
                return False
    return True


def _order_has_transfers(ctx) -> bool:
    items = (ctx.order or {}).get("items") or []
    transfers = [i for i in items if i.get("kind") == "transfer" and i.get("status") != "cancelled"]
    legs = [i for i in items if i.get("kind") in ("flight", "train") and i.get("status") != "cancelled"]
    return bool(transfers) and len(transfers) >= len(legs)


def _arrival(ctx):
    v = ctx.session.variant(ctx.session.chosen_variant_id) if ctx.session.chosen_variant_id else None
    if v is None:
        return None
    stay = next((i for i, s in enumerate(v.steps) if s.type == "stay"), None)
    before = v.steps[:stay] if stay is not None else []
    return _dt(before[-1].end) if before else None


def step_post_purchase(ctx):
    s = ctx.session
    pp = ctx.post_purchase or (ctx.understand or {}).get("post_purchase") or {}
    if not s.order_id:
        ctx.present = None
        ctx.message = "Пока нет оплаченного заказа — сначала соберём и оплатим поездку."
        return
    kind, params = pp.get("kind"), dict(pp.get("params") or {})
    order = tool(ctx, "order_get", {"order_id": s.order_id}, cache=False)
    if order.get("status") not in ("paid", "changed"):
        ctx.present = None
        ctx.message = "Заказ ещё не оплачен — действия после покупки станут доступны после оплаты."
        ctx.order = order
        return
    if kind == "early_checkin":
        time_s = params.get("time")
        arr, checkin = _arrival(ctx), _hotel_checkin(ctx, order)
        if not params.get("time") and arr is not None and checkin is not None and arr >= checkin.astimezone(arr.tzinfo):
            # they arrive after the standard check-in: nothing to buy, say so instead of sending a request
            ctx.present = None
            ctx.order = order
            ctx.message = (f"Ранний заезд не понадобится: вы будете в отеле около {arr:%H:%M}, а заселение "
                           f"с {checkin.astimezone(arr.tzinfo):%H:%M} — номер к вашему приезду уже будет готов.")
            return
        if not time_s:
            time_s = _hhmm_after(arr) if arr else None
        if not time_s:
            ctx.present = None
            ctx.message = "Во сколько хотите заселиться? Напишите время — запрошу у отеля."
            return
        result = tool(ctx, "order_request_early_checkin", {"order_id": s.order_id, "time": time_s,
                                                           "now": now_iso(ctx)}, cache=False)
        result = {**result, "requested_time": time_s}
    elif kind == "change_dates":
        new = params.get("new_dates")
        hotel = next((i for i in order.get("items") or [] if i["kind"] == "hotel"), None)
        if not new and params.get("shift_days") and hotel and hotel.get("start") and hotel.get("end"):
            k = int(params["shift_days"])
            new = {"check_in": (_date(hotel["start"]) + timedelta(days=k)).isoformat(),
                   "check_out": (_date(hotel["end"]) + timedelta(days=k)).isoformat()}
        if not new:
            ctx.present = None
            ctx.message = "На какие даты перенести? Напишите новые даты заезда и выезда."
            return
        result = tool(ctx, "order_change_dates", {"order_id": s.order_id, "new_dates": new,
                                                  "confirm": bool(params.get("confirm")), "now": now_iso(ctx)},
                      cache=False)
    elif kind == "add_transfer":
        offer_id = params.get("offer_id")
        if not offer_id:
            offer_id = _transfer_offer_for_order(ctx, order)
        if not offer_id:
            ctx.present = None
            ctx.message = "Трансфер под этот заказ сейчас не находится — попробуйте позже."
            return
        result = order_summary(tool(ctx, "order_add_transfer", {"order_id": s.order_id, "offer_id": offer_id,
                                                                "now": now_iso(ctx)}, cache=False))
    else:
        ctx.present = None
        ctx.message = "Что сделать с заказом: ранний заезд, перенос дат или трансфер?"
        return
    ctx.order = tool(ctx, "order_get", {"order_id": s.order_id}, cache=False)
    ctx.aftercare_result = {"kind": kind, **result}
    s.stage = "aftercare"


def _transfer_offer_for_order(ctx, order):
    hotel = next((i for i in order.get("items") or [] if i["kind"] == "hotel"), None)
    flight = next((i for i in order.get("items") or [] if i["kind"] == "flight"), None)
    hotel_id = ((hotel or {}).get("details") or {}).get("hotel_id")
    iata = ((flight or {}).get("details") or {}).get("dest_iata")
    if not (hotel_id and iata and flight.get("end")):
        return None
    party = _party_payload(ctx.brief)
    out = tool_safe(ctx, "search_transfers", {"from": {"iata": iata}, "to": {"hotel_id": hotel_id}, "at": flight["end"],
                                              "pax": party["adults"] + len(party["children_ages"]),
                                              "today": ctx.session.today().isoformat()})
    best = _cheapest((out or {}).get("offers") or [])
    return best["offer_id"] if best else None


# ---------------------------------------------------------------- analytics

def build_event(ctx, kind="turn"):
    b = ctx.brief.values()
    dest = b.get("destination") or {}
    party = b.get("party") or {}
    data = {"route": ctx.trace.get("route"), "intent": ctx.intent,
            "action": (ctx.action or {}).get("type"), "questions": len(ctx.questions),
            "variants": len(ctx.variants), "destinations": len(ctx.destination_items),
            "error": (ctx.error or {}).get("code")}
    return {"kind": kind, "stage": ctx.session.stage, "segment": party.get("segment"),
            "origin_city": (b.get("origin") or {}).get("city"), "destination_id": dest.get("id"),
            "month": _month(ctx.brief), "data": {k: v for k, v in data.items() if v is not None},
            "at": now_iso(ctx)}


def step_log_event(ctx):
    if not ctx.flags.insights:
        return
    tool_safe(ctx, "insights_log_event", {"event": build_event(ctx)}, cache=False)


# ---------------------------------------------------------------- registry

STEP_REGISTRY = {
    "apply_patch": step_apply_patch,
    "resolve_refs": step_resolve_refs,
    "image_match": step_image_match,
    "find_destinations": step_find_destinations,
    "quote_destinations": step_quote_destinations,
    "search_flights": step_search,      # the parts are run by `search` (scope-aware, parallel, cached)
    "search_trains": step_search,
    "search_transfers": step_search,
    "search_hotels": step_search,
    "search": step_search,
    "build_variants": step_build_variants,
    "check": step_check,
    "sensitivity": step_sensitivity,
    "ask_or_assume": step_ask_or_assume,
    "upsell": step_upsell,
    "compare": step_compare,
    "info_lookup": step_info_lookup,
    "cart_add": step_cart_add,
    "checkout": step_checkout,
    "pay": step_pay,
    "post_purchase": step_post_purchase,
    "log_event": step_log_event,
}
assert set(STEP_REGISTRY) == set(STEP_NAMES), set(STEP_NAMES) ^ set(STEP_REGISTRY)

