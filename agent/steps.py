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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from . import payloads as P
from .hooks import as_issue, blocking, issue
from .mcp_client import ToolError
from .opendata import LONG_TRAIN_LEAD_DAYS, train_km, train_reachable, wish_traits
from .opendata import long_train_pair as long_train_pair_rule
from .profiles import apply_profile, profile_value
from .state import (Brief, CachedResult, PatchError, Question, UpsellCandidate, Variant, apply_patch, assume,
                    changed_paths)
from .steps_spec import STEP_NAMES

RESULTS_CAP = 300         # cached tool results kept per session (80 evicted the shown variants' offers)
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
    destination_train_recs: list = field(default_factory=list)   # budget-led inspiration: train-reachable places
    destination_rank: dict = field(default_factory=dict)          # find_destinations order (id -> index)
    other_places: Optional[list] = None                    # places that fit the budget, offered at selection
    ask_other_place: bool = False                          # «или в другом городе» next to another rebuild wish
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
    extra_actions: list = field(default_factory=list)      # actions a no_data answer offers (other destinations)
    ask_other_dates: bool = False                          # «другие числа / даты» without exact values
    ask_cheaper: bool = False                              # «подешевле / попроще» without exact values
    dropped_destination: Optional[dict] = None             # a destination dropped by a change of trip kind
    other_country: bool = False                            # «другую страну»: the dropped one's country too
    best_time_asked: bool = False                          # «когда лучше поехать?» (rules.json best_time cues)
    best_month_assumed: Optional[int] = None               # dates assumed in the nearest best month this turn
    transport_gap: Optional[dict] = None                   # no transport to the place: why, in code (recover_transport)
    unsold: list = field(default_factory=list)             # services asked for that are not sold (rules.json unsold)
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
    """Cache a record; above RESULTS_CAP the oldest tool results go (engine:* records — the shown places, the
    near point — are state, never evicted)."""
    session.results[record.key] = record
    if len(session.results) > RESULTS_CAP:
        pool = [r for r in session.results.values() if not r.key.startswith("engine:")]
        oldest = sorted(pool, key=lambda r: r.fetched_at)[: len(session.results) - RESULTS_CAP]
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
    """Catalog first; one lazy city build per unknown name per session (data_build.lazy).
    Wishes and common nouns («горы», «море», «юг») never resolve and never trigger a lazy build."""
    if wish_traits(name) is not None:
        return None
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


PARTY_KEYS = ("segment", "adults", "children", "children_ages")


def segment_for(adults, children):
    return "family" if children else "solo" if adults == 1 else "couple" if adults == 2 else "friends"


def user_party(brief):
    """True when the user said anything about who travels."""
    return brief.party.value is not None and any(brief.source_of(f"party.{k}") == "user" for k in PARTY_KEYS)


def _cue_in(text, cues):
    return bool(text) and any(str(c).casefold() in text for c in cues or [])


def segment_cue(ctx):
    """A segment the user's message implies without naming the party (segments.json `cues`: a business
    trip -> solo), or None."""
    text = (ctx.text or "").casefold()
    for name, seg in (ctx.skill.segments or {}).items():
        if _cue_in(text, (seg or {}).get("cues")):
            return name
    return None


def party_of_ages(ages):
    """Ages on the trip start -> {segment, adults, children, children_ages} (adults are 18+)."""
    kids = sorted(a for a in ages if a < 18)
    adults = len(ages) - len(kids)
    return {"segment": segment_for(adults, len(kids)), "adults": adults, "children": len(kids),
            "children_ages": kids or None}


def profile_party(ctx, brief):
    """The party of the profile's saved travellers, ages on the trip start (dates.start, else today), or None."""
    saved = list(getattr(ctx.profile, "travellers", None) or [])
    if not saved:
        return None
    day = _date((brief.values().get("dates") or {}).get("start")) or ctx.session.today()
    ages = [_age_on(t.birth_date, day) for t in saved]
    if any(a < 0 for a in ages):
        return None
    party = party_of_ages(ages)
    return party if party["adults"] >= 1 else None


HOSTEL_WORD_RE = re.compile(r"хостел|hostel")
NO_HOSTEL = "no_hostel"


def hostel_refused(rooms, text):
    """True when the message refuses hostels: a rules.json `rooms.no_hostel_cues` phrase, or «хостел» with a
    negation close before it (same clause) or after it (same sentence): «хостел в нашем возрасте как-то не
    очень», «хостел нам не подходит», «не хочу жить в хостеле»."""
    text = (text or "").casefold()
    if _cue_in(text, rooms.get("no_hostel_cues")):
        return True
    wb, wa = int(rooms.get("no_hostel_window_before", 18)), int(rooms.get("no_hostel_window_after", 45))
    before_negs, after_negs = rooms.get("no_hostel_before") or [], rooms.get("no_hostel_after") or []
    except_ = rooms.get("no_hostel_except") or []
    for m in HOSTEL_WORD_RE.finditer(text):
        before = re.split(r"[.!?;,:—]", text[max(0, m.start() - wb):m.start()])[-1]
        after = re.split(r"[.!?;]", text[m.end():m.end() + wa])[0]
        if any(x in after or x in before for x in except_):
            continue   # «хостел или отель — нет разницы», «хостел — не проблема»
        if any(n in f" {before}" for n in before_negs) or any(n in after for n in after_negs):
            return True
    return False


def room_cues(ctx, brief):
    """rules.json `rooms` on the user's message: «хостел не подходит», «без хостелов» -> constraint no_hostel
    (no hotel of kind hostel, no dorm bed anywhere; «можно и хостел» drops it); «отдельный номер», «не хостел»
    -> constraint no_dorm (dorm beds never offered); «хостел» -> constraint hostel (dorm beds may be any
    variant). The user's own words, read by code as a safety net next to understand."""
    rooms = (ctx.rules or {}).get("rooms") or {}
    text = (ctx.text or "").casefold()
    have = set(brief.values().get("constraints") or [])
    if _cue_in(text, rooms.get("allow_hostel_cues")):
        if NO_HOSTEL in have:
            brief, _ = apply_patch(brief, {"set": {"constraints.remove": [NO_HOSTEL]}, "unset": []})
        return brief
    if hostel_refused(rooms, text):
        if NO_HOSTEL in have and "hostel" not in have:
            return brief
        patch = {"set": {"constraints.add": [NO_HOSTEL]}, "unset": []}
        if "hostel" in have:
            patch["set"]["constraints.remove"] = ["hostel"]
        new, _ = apply_patch(brief, patch)
        return new
    if _cue_in(text, rooms.get("private_cues")):
        add, drop = "no_dorm", "hostel"
    elif _cue_in(text, rooms.get("dorm_cues")):
        add, drop = "hostel", "no_dorm"
    else:
        return brief
    if add in have and drop not in have:
        return brief
    patch = {"set": {"constraints.add": [add]}, "unset": []}
    if drop in have:
        patch["set"]["constraints.remove"] = [drop]
    new, _ = apply_patch(brief, patch)
    return new


def train_cues(ctx, brief):
    """rules.json `trains` on the user's message: «на Сапсане», «Сапсаном» -> constraint train:sapsan, «на
    скоростном поезде» -> train:high_speed, each with prefs.transport = train (ranking keeps only high-speed
    trains, relaxed with a plain risk when a direction has none); «можно и не Сапсан», «любой поезд» drop both.
    The user's own words, read by code like the room cues."""
    rules = (ctx.rules or {}).get("trains") or {}
    text = (ctx.text or "").casefold()
    have = set(brief.values().get("constraints") or [])
    tokens = ("train:sapsan", "train:high_speed")
    if _cue_in(text, rules.get("any_train_cues")):
        drop = [t for t in tokens if t in have]
        if not drop:
            return brief
        new, _ = apply_patch(brief, {"set": {"constraints.remove": drop}, "unset": []})
        return new
    if _cue_in(text, rules.get("sapsan_cues")):
        add = "train:sapsan"
    elif _cue_in(text, rules.get("high_speed_cues")):
        add = "train:high_speed"
    else:
        return brief
    patch = {"set": {}, "unset": []}
    if add not in have:
        patch["set"]["constraints.add"] = [add]
    other = [t for t in tokens if t != add and t in have]
    if other:
        patch["set"]["constraints.remove"] = other
    prefs = brief.values().get("prefs") or {}
    if prefs.get("transport") != "train" or brief.source_of("prefs.transport") != "user":
        patch["set"]["prefs.transport"] = "train"
    if not patch["set"]:
        return brief
    new, _ = apply_patch(brief, patch)
    return new


PLANE_NEG_RE = re.compile(r"(?:\bне|\bбез)\s+(?:\S+\s+){0,2}$", re.I)
PLANE_NEG_AFTER_RE = re.compile(r"^\S*\s+(?:не|нельзя|никак)\b", re.I)


def plane_cue(rules, text):
    """rules.json `trains.plane_cues` («летим», «самолётом», «перелёт»…) at a word start, not negated («не хочу
    лететь», «лететь не хотим», «без самолёта»), in a message with no train word."""
    t = (text or "").casefold().replace("ё", "е")
    r = (rules or {}).get("trains") or {}
    if not t or _cue_in(t, [str(c).casefold().replace("ё", "е") for c in r.get("plane_block_cues") or []]):
        return False
    for cue in r.get("plane_cues") or []:
        c = str(cue).casefold().replace("ё", "е")
        for m in re.finditer(rf"(?<![а-яa-z]){re.escape(c)}", t):
            if not PLANE_NEG_RE.search(t[:m.start()][-24:]) and not PLANE_NEG_AFTER_RE.search(t[m.start():][:40]):
                return True
    return False


def plane_cues(ctx, brief):
    """The user said they fly («летим», «самолётом»; rules.json `trains.plane_cues`, read by code like the train
    cues): prefs.transport = plane as the user's own value, unless the user already set the transport. A place
    without flights is then answered «самолётов нет» with the train as a button, never rebuilt by train silently."""
    if brief.source_of("prefs.transport") == "user" or ctx.intent not in ("new_trip", "refine", "answer") or \
            not plane_cue(ctx.rules, ctx.text):
        return brief
    new, _ = apply_patch(brief, {"set": {"prefs.transport": "plane"}, "unset": []})
    return new


def _pets_payload(brief):
    """search_trains / search_flights `pets`: the party's animals priced into the tickets (none: no key, so
    offer ids and caches stay as before)."""
    from .ranking import pet_count
    n = pet_count(brief)
    return {"pets": n} if n else {}


def normalize_brief(ctx, brief):
    """Profile values, segment defaults, party counts, nights/month/scope defaults (never over the user)."""
    skill = ctx.skill
    brief = apply_profile(brief, ctx.profile)
    # origin
    if not brief.has("origin"):
        city = recommended(ctx, "origin.city")
        if city:
            brief = assume(brief, "origin", {"city": city, "iata": []}, "default")
    # party (decision 2): the user's words > a segment cue («командировка» -> solo) > the profile's saved
    # travellers > the recommended segment's defaults
    party = brief.values().get("party")
    cue = segment_cue(ctx)
    if cue and not user_party(brief) and (party or {}).get("segment") != cue:
        brief = assume(brief, "party.segment", cue, "inferred")
    elif party is None:
        saved = profile_party(ctx, brief)
        if saved:
            for key in ("segment", "adults", "children", "children_ages"):
                brief = assume(brief, f"party.{key}", saved[key], "profile")
        else:
            seg = recommended(ctx, "party.segment") or "other"
            brief = assume(brief, "party.segment", seg, "default")
    else:
        if party.get("segment") == "other" and brief.source_of("party.segment") != "user" and (
                brief.source_of("party.adults") == "user" or brief.source_of("party.children") == "user"):
            a, c = int(party.get("adults") or 0), int(party.get("children") or 0)
            brief = assume(brief, "party.segment", segment_for(a, c), "inferred")
    party = brief.values()["party"]
    defaults = ((skill.segments or {}).get(party["segment"]) or {}).get("party") or {}
    kept = ("user", "profile") if brief.source_of("party.segment") == "profile" else ("user",)
    for key in ("adults", "children"):
        if key in defaults and brief.source_of(f"party.{key}") not in kept and party.get(key) != defaults[key]:
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
    transport = (brief.values().get("prefs") or {}).get("transport")
    if not brief.has("scope"):
        scope = ["trains", "stay", "transfers"] if transport == "train" else list(P.SCOPE_FULL)
        brief = assume(brief, "scope", scope, "default")
    elif brief.source_of("scope") != "user" and transport in ("train", "plane"):
        # the transport changed after the default scope was set («а поездом?»): the assumed scope follows it,
        # else ranking keeps looking for flights and builds stay-only variants (h10)
        scope = list(brief.values().get("scope") or [])
        swap = ("flights", "trains") if transport == "train" else ("trains", "flights")
        if swap[0] in scope and swap[1] not in scope:
            brief = assume(brief, "scope", [swap[1] if s == swap[0] else s for s in scope], "default")
    elif transport == "train" and "flights" in (brief.values().get("scope") or []) and \
            "trains" not in (brief.values().get("scope") or []):
        # «нужны билеты туда-обратно» reads as scope flights, but «билеты» are tickets, not a plane: a train trip
        # (the user's «поездом», or code's train for a place without flights) searches trains for them
        scope = list(brief.values().get("scope") or [])
        brief = assume(brief, "scope", ["trains" if s == "flights" else s for s in scope], "inferred")
    elif brief.source_of("scope") != "user" and (transport or "any") == "any":
        # back to any transport (a place quoted by plane after a train choice): the assumed scope is the full one
        scope = list(brief.values().get("scope") or [])
        if "trains" in scope and "flights" not in scope:
            brief = assume(brief, "scope", ["flights" if s == "trains" else s for s in scope], "default")
    return brief


def prepare_brief(ctx, brief, patch, allow_lazy=True):
    """Patch + destination resolution + normalisation -> (brief, changed_paths, unknown_name)."""
    new, changed = apply_patch(brief, patch)
    new = room_cues(ctx, new)
    new = train_cues(ctx, new)
    new = plane_cues(ctx, new)
    unknown = None
    dest = new.values().get("destination")
    if ctx.destination_hint:
        rec = destination_record(ctx, dest_id=ctx.destination_hint)
        if rec is not None:
            new = set_destination(new, rec)
    elif dest and not dest.get("id") and wish_traits(dest["name"]) is not None:
        # «хочу в горы»: a wish, never a place (no geocode, no lazy build) -> its traits, destinations route
        wish = wish_traits(dest["name"])
        new, _ = apply_patch(new, {"unset": ["destination.name"]})
        new.destination.source = brief.destination.source
        if wish:
            new, _ = apply_patch(new, {"set": {"traits.add": wish}})
        ctx.trace["notes"].append(f"destination «{dest['name']}» is a wish -> traits {wish}")
    elif dest and not dest.get("id"):
        rec = resolve_destination(ctx, dest["name"], allow_lazy=allow_lazy)
        if rec is not None:
            new = set_destination(new, rec)
        else:
            unknown = dest["name"]
            new, _ = apply_patch(new, {"unset": ["destination.name"]})
            new.destination.source = brief.destination.source
    new = clear_near_on_new_destination(brief, new, patch)
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
            transport_of_choice(ctx, item.id)
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
        image_landmark(ctx, image, rec, conf)
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


def image_landmark(ctx, image, rec, conf):
    """A photo of a sight («Айя-София») whose place became the destination: when the landmark resolves in that
    destination's pois, prefs.near is assumed (source inferred, a chip «отель рядом: …»), so the first answer
    already has hotels next to it. Not found -> nothing (the centre logic applies, nothing is claimed)."""
    landmark = str((image or {}).get("landmark") or "").strip()
    sets = ctx.patch.get("set") or {}
    if not landmark or "prefs.near" in sets or "prefs.center_max_m" in sets:
        return
    brief = ctx.session.brief
    if brief.source_of("prefs.near") == "user" and (brief.values().get("prefs") or {}).get("near"):
        return
    poi = od(ctx, "find_poi", rec["id"], landmark)
    if poi is None:
        ctx.trace["notes"].append(f"image: landmark «{landmark}» not in {rec['id']} pois -> centre")
        return
    ctx.session.brief = assume(brief, "prefs.near", landmark[:80], "inferred", conf)
    ctx.trace["notes"].append(f"image: landmark «{landmark}» -> prefs.near ({poi['name']}, {poi['dist_center_m']} m "
                              f"from the centre)")


def step_image_match(ctx):
    run_image_match(ctx)


# ---------------------------------------------------------------- inspiration (entry A)

def _month(brief):
    d = brief.values().get("dates") or {}
    if d.get("start"):
        return int(str(d["start"])[5:7])
    return d.get("month")


# Destination suggestions honour the brief (entry A):
# - prefs.transport == "train": only places the mock sells trains to from the origin (opendata.train_reachable:
#   Russia, stations on both ends, 30..2500 km; over 1000 km long-distance sleepers, priced by the trains of the
#   two days before check-in that arrive on it, with facts long_train / on_road_ru / leave_date / road_ru), then confirmed by search_trains there and back on the quoted
#   dates; price = cheapest train there + back + the quoted stay, travel_time_h = the fastest train there.
# - prefs.domestic: only places in the origin's country (Russia when the origin is unknown).
# - budget_rub.max: after quoting, items within budget come first (in rank order, facts.within_budget true);
#   with >= DEST_BUDGET_ENOUGH fitting items, items above DEST_BUDGET_FAR x budget and unpriced ones are
#   dropped; with none fitting, the cheapest come first and every item has facts.within_budget false
#   (present/destinations.md then says so plainly).
# - budget_rub.max set, the transport not fixed (prefs.transport any) and an origin with a station: the
#   train-reachable domestic places are quoted by train too (DEST_TRAIN_POOL_X x limit of them, the same train
#   path as above); per place the cheaper of the flight and the train quote is kept, so the budget rule puts
#   the places that fit first. Every item carries facts.transport ("plane" | "train"), facts.transport_ru
#   («самолётом» | «поездом») and facts.price_includes (what the quote priced, ready Russian).
DEST_POOL = 40            # quote_destinations takes at most 40 ids
DEST_ALL = 10_000         # rank the whole catalog before filtering (find_destinations scores every place anyway)
DEST_BUDGET_POOL_X = 3    # with a budget, quote limit x 3 candidates to find ones that fit
DEST_TRAIN_POOL_X = 2     # with trains, quote limit x 2 candidates (some have no trains on the dates)
DEST_BUDGET_FAR = 1.3
DEST_BUDGET_ENOUGH = 3


def _dest_prefs(brief):
    prefs = brief.values().get("prefs") or {}
    return prefs.get("transport") == "train", bool(prefs.get("domestic"))


def _mixed_trains_origin(ctx, brief):
    """The origin place when a budget-led inspiration also quotes trains: budget_rub.max set, the user did not
    fix the transport, the origin is known with a station (the mock's train rule needs one at both ends)."""
    b = brief.values()
    if not (b.get("budget_rub") or {}).get("max") or ((b.get("prefs") or {}).get("transport") or "any") != "any":
        return None
    origin = _origin_place(ctx, brief)
    if not origin or not origin.get("stations") or origin.get("lat") is None or origin.get("lon") is None:
        return None
    return origin


def _origin_place(ctx, brief):
    city = (brief.values().get("origin") or {}).get("city")
    return destination_record(ctx, name=city) if city else None


def _passes_dest_prefs(rec, origin, train, domestic, ctx=None, nights=7):
    if domestic and (rec.get("country_code") or "") != ((origin or {}).get("country_code") or "RU"):
        return False
    if train:
        if origin and origin.get("country_code") and origin.get("lat") is not None and origin.get("stations"):
            return train_reachable(origin, rec) and (ctx is None or not long_train_pair_rule(origin, rec)
                                                     or _long_train_may_fit(ctx, origin, rec, nights))
        return rec.get("country_code") == "RU" and bool(rec.get("stations"))   # the mock decides on pricing
    return True


def step_find_destinations(ctx):
    reset_auto_transport(ctx)
    b = ctx.brief.values()
    limit = int(ctx.rules.get("max_destinations", 6))
    month = _month(ctx.brief)
    train, domestic = _dest_prefs(ctx.brief)
    budget = (b.get("budget_rub") or {}).get("max")
    narrowed = train or domestic
    recs = od(ctx, "find_destinations", traits=b.get("traits") or [], month=month,
              origin_city=(b.get("origin") or {}).get("city"), budget=budget,
              max_travel_hours=(b.get("prefs") or {}).get("max_travel_hours"),
              segment=(b.get("party") or {}).get("segment"), limit=DEST_ALL if (narrowed or budget) else limit)
    recs = [norm_destination(r) for r in (recs or [])]
    recs = [r for r in recs if r and r.get("id")]
    if ctx.image_place:
        recs = [ctx.image_place] + [r for r in recs if r["id"] != ctx.image_place["id"]]
    if ctx.dropped_destination:   # a change of trip kind dropped it: never suggest it again in the same answer
        gone = ctx.dropped_destination
        recs = [r for r in recs if r["id"] != gone.get("id") and not (
            ctx.other_country and gone.get("country_code") and r.get("country_code") == gone["country_code"])]
    ctx.destination_rank = {r["id"]: i for i, r in enumerate(recs)}
    origin_t = None if train else _mixed_trains_origin(ctx, ctx.brief)
    if origin_t is not None:
        ctx.destination_train_recs = _train_pool(origin_t, recs, limit, b.get("traits") or [], ctx,
                                                 _trip_nights(ctx.brief))
        ctx.trace["notes"].append(f"destinations: budget, any transport -> {len(ctx.destination_train_recs)} "
                                  f"train candidates")
    if narrowed:
        origin = _origin_place(ctx, ctx.brief)
        nights = _trip_nights(ctx.brief)
        recs = [r for r in recs if _passes_dest_prefs(r, origin, train, domestic, ctx, nights)]
        ctx.trace["notes"].append(f"destinations: train={train} domestic={domestic} -> {len(recs)} candidates")
    if not recs:
        city = (b.get("origin") or {}).get("city") or ""
        if train:
            ctx.fail("no_data", f"Поездом из «{city}» под такой запрос в демо-каталоге направлений не нашлось: "
                                "поезда здесь ходят только по России и примерно до 2500 км. Можно ослабить "
                                "пожелания или рассмотреть самолёт.")
        if domestic:
            ctx.fail("no_data", "По России под такой запрос в демо-каталоге направлений не нашлось. Попробуйте "
                                "ослабить условия — например, другой месяц.")
        ctx.fail("no_data", "Под такой запрос в демо-каталоге направлений не нашлось. Попробуйте ослабить "
                            "условия — например, другой месяц или бюджет.")
    pool = limit * DEST_BUDGET_POOL_X if budget else limit * DEST_TRAIN_POOL_X if train else limit
    ctx.destination_recs = recs[:min(pool, DEST_POOL)]
    if ctx.flags.insights and month:
        out = tool_safe(ctx, "insights_popular_destinations",
                        {"origin_city": (b.get("origin") or {}).get("city") or "", "month": month,
                         "segment": (b.get("party") or {}).get("segment") or "other"})
        if out:
            ids = {r["id"] for r in ctx.destination_recs}
            ctx.insight = {"popular_ids": [i["destination_id"] for i in out.get("items") or []
                                           if i.get("destination_id") in ids]}


LONG_MAX_HOURS_PER_NIGHT = 6   # rules.json trains.long_max_hours_per_night


def _trip_nights(brief):
    d = brief.values().get("dates") or {}
    if d.get("nights"):
        return int(d["nights"])
    if d.get("start") and d.get("end"):
        return max(1, (_date(str(d["end"])[:10]) - _date(str(d["start"])[:10])).days)
    return 7   # quote_destinations' default


def long_train_fits_trip(ctx, hours, nights):
    """A day or more on board is offered as a destination only when the trip is long enough for it: one way at most
    rules.json trains.long_max_hours_per_night x nights (6 h: none for a 3-night trip, up to 42 h for 7 nights)."""
    per_night = float(((ctx.rules or {}).get("trains") or {}).get("long_max_hours_per_night", LONG_MAX_HOURS_PER_NIGHT))
    return hours is not None and hours <= per_night * max(1, int(nights or 1))


def _long_train_may_fit(ctx, origin, rec, nights):
    """Before quoting: the fastest a long-distance train could be (1.3 x great circle at 70 km/h) fits the trip."""
    km = train_km(origin, rec)
    return km is not None and long_train_fits_trip(ctx, km * 1.3 / 70, nights)


def _train_pool(origin, recs, limit, traits, ctx=None, nights=7):
    """Train candidates of a budget-led inspiration, in rank order: the places up to 1000 km as before
    (DEST_TRAIN_POOL_X x limit), then up to `limit` long-distance places (a day or more on board) that match the
    wish (any trait, when there are wishes) — they are kept only where they fit the budget."""
    reach = [r for r in recs if r["id"] != origin.get("id") and train_reachable(origin, r)]
    short = [r for r in reach if not long_train_pair_rule(origin, r)][:min(limit * DEST_TRAIN_POOL_X, DEST_POOL)]
    wish = set(traits or [])
    far = [r for r in reach if long_train_pair_rule(origin, r)
           and (not wish or set(r.get("match") or []) & wish or set(r.get("tags") or []) & wish)
           and (ctx is None or _long_train_may_fit(ctx, origin, r, nights))][:limit]
    return short + far


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


def quote_payload(ctx, brief, ids, dates_override=None):
    b = brief.values()
    dates = b.get("dates") or {}
    payload = {"destination_ids": list(ids), "origin_city": (b.get("origin") or {}).get("city") or "",
               "party": _party_payload(brief), "today": ctx.session.today().isoformat()}
    if dates_override:
        payload["dates"] = {"check_in": str(dates_override[0])[:10], "check_out": str(dates_override[1])[:10]}
    elif dates.get("start") and dates.get("end"):
        payload["dates"] = {"check_in": dates["start"], "check_out": dates["end"]}
    else:
        payload["month"] = _month(brief) or (ctx.session.today() + timedelta(days=30)).month
        if dates.get("nights"):
            payload["nights"] = dates["nights"]
    return payload


def _train_round_trip(ctx, brief, rec, check_in, check_out):
    """Cheapest train there + back for the party on the quoted dates -> (price_rub, hours there, extra facts) or
    (None, None, {}). A long-distance pair (over 1000 km) prices the trains of the two days before check-in that
    arrive on the check-in day (the trip the variants build) and returns its ready facts: `long_train`,
    `on_road_ru` («1 сутки 3 ч»), `leave_date` / `leave_days_before` and `road_ru` of the priced train."""
    b = brief.values()
    party = _party_payload(brief)
    origin = (b.get("origin") or {}).get("city") or ""
    base = {"passengers": min(9, party["adults"] + len(party["children_ages"])), "today": ctx.session.today().isoformat()}
    ci = _date(str(check_in)[:10])
    long_pair = bool(long_train_pair_rule(_origin_place(ctx, brief), rec))
    if long_pair:
        days = [ci - timedelta(days=k) for k in range(LONG_TRAIN_LEAD_DAYS, 0, -1) if ci - timedelta(days=k) >= ctx.session.today()]
        out_offers = _arriving_on([tool_safe(ctx, "search_trains", {**base, "origin_city": origin, "dest_city": rec["name"],
                                                                     "date": d.isoformat()}, cache=False)
                                   for d in days], ci)
    else:
        there = tool_safe(ctx, "search_trains", {**base, "origin_city": origin, "dest_city": rec["name"],
                                                 "date": str(check_in)[:10]}, cache=False)
        out_offers = (there or {}).get("offers") or []
    back = tool_safe(ctx, "search_trains", {**base, "origin_city": rec["name"], "dest_city": origin,
                                            "date": str(check_out)[:10]}, cache=False)
    back_offers = (back or {}).get("offers") or []
    if not out_offers or not back_offers:
        return None, None, {}
    price = min(o["price_rub"] for o in out_offers) + min(o["price_rub"] for o in back_offers)
    if not long_pair:
        return int(price), round(min(o["duration_min"] for o in out_offers) / 60, 1), {}
    from .ranking import day_month_ru, road_time_ru
    o = min(out_offers, key=lambda x: (x["price_rub"], x["duration_min"]))   # the priced train
    dep = _dt(o["dep"])
    n = (ci - dep.date()).days
    road = road_time_ru(o["duration_min"])
    extra = {"long_train": True, "on_road_ru": road, "leave_date": dep.date().isoformat(), "leave_days_before": n,
             "road_ru": f"поездом, в пути {road}; выезд {day_month_ru(dep)} — "
                        + ("за день до заезда" if n == 1 else f"за {n} {P.plural(n, 'день', 'дня', 'дней')} до заезда")}
    return int(price), round(o["duration_min"] / 60, 1), extra


TRANSPORT_RU = {"train": "поездом", "plane": "самолётом"}
MID_STARS_NOTE = "3–4★"   # quote_destinations notes this when no 3-4 star hotel had rooms


def price_includes_ru(brief, transport, nights, mid_stars=True):
    """What a destination's «от … ₽» quote priced, as a ready Russian line (facts.price_includes): the cheapest
    round trip for the party (basic fare: cabin bag only / the cheapest train seats) and the nights in the
    cheapest 3-4 star hotel; transfers are not in it."""
    party = _party_payload(brief)
    n = party["adults"] + len(party["children_ages"])
    who = f"на {PEOPLE_RU[n]}" if n in PEOPLE_RU else f"на {n} человек"
    road = (f"поезд туда и обратно {who} по самым дешёвым билетам" if transport == "train"
            else f"перелёт туда и обратно {who} по самому дешёвому тарифу (только ручная кладь)")
    stay = f"{nights} {P.plural(nights, 'ночь', 'ночи', 'ночей')} " + (
        "в самом недорогом отеле на 3–4 звезды" if mid_stars else "в самом недорогом из свободных отелей")
    return f"{road} и {stay}; трансферы в цену не входят"


def destination_prices(ctx, brief, recs, train=None, dates_override=None):
    """Per destination id: {"price", "facts", "nights", "travel_h", "reachable"} for the brief's transport
    (flight + stay from quote_destinations; with prefs.transport == "train" — or `train` True — train + stay).
    `dates_override` (check_in, check_out) quotes those dates instead of the brief's."""
    b = brief.values()
    dates = b.get("dates") or {}
    if train is None:
        train, _ = _dest_prefs(brief)
    if not recs:
        return {}
    payload = quote_payload(ctx, brief, [r["id"] for r in recs], dates_override)
    if train:
        payload["stay_only"] = True   # the fares come from search_trains: no flights searched for these
    out = tool(ctx, "quote_destinations", payload)
    quotes = {q["destination_id"]: q for q in out.get("quotes") or []}
    prices = {}
    for rec in recs:
        q = quotes.get(rec["id"]) or {}
        entry = {"price": None, "facts": {}, "nights": int(q.get("nights") or dates.get("nights") or 7),
                 "travel_h": None, "reachable": True}
        mid = not any(MID_STARS_NOTE in str(n) for n in q.get("notes") or [])
        if train:
            stay = q.get("stay_price_rub")
            fare, hours, extra = (_train_round_trip(ctx, brief, rec, q["check_in"], q["check_out"])
                                  if q.get("check_in") and q.get("check_out") else (None, None, {}))
            entry["reachable"], entry["travel_h"] = fare is not None, hours
            if fare is not None and stay is not None:
                entry["price"] = fare + int(stay)
            entry["facts"] = {"transport": "train", "transport_ru": TRANSPORT_RU["train"]}
            entry["facts"].update({k: v for k, v in (("train_price_rub", fare), ("stay_price_rub", stay),
                                                     ("check_in", q.get("check_in")), ("check_out", q.get("check_out")))
                                   if v is not None and fare is not None})
            entry["facts"].update(extra)
            if entry["price"] is not None:
                entry["facts"]["price_includes"] = price_includes_ru(brief, "train", entry["nights"], mid)
        else:
            ok = q.get("available")
            entry["price"] = q.get("total_rub") if ok else None
            entry["facts"] = {"transport": "plane", "transport_ru": TRANSPORT_RU["plane"]}
            entry["facts"].update({k: q.get(k) for k in ("flight_price_rub", "stay_price_rub", "check_in", "check_out")
                                   if ok and q.get(k) is not None})
            if entry["price"] is not None:
                entry["facts"]["price_includes"] = price_includes_ru(brief, "plane", entry["nights"], mid)
        if q.get("currency_notes"):
            entry["facts"]["currency_notes"] = q["currency_notes"]
        prices[rec["id"]] = entry
    return prices


def order_by_budget(items, budget, limit):
    """Budget rule for destination items (see the comment above DEST_POOL); sets facts.within_budget."""
    if not budget:
        return items[:limit]
    for i in items:
        if i["price_from_rub"] is not None:
            i["facts"]["within_budget"] = i["price_from_rub"] <= budget
    fits = [i for i in items if i["price_from_rub"] is not None and i["price_from_rub"] <= budget]
    over = sorted((i for i in items if i["price_from_rub"] is not None and i["price_from_rub"] > budget),
                  key=lambda i: i["price_from_rub"])
    unknown = [i for i in items if i["price_from_rub"] is None]
    if len(fits) >= DEST_BUDGET_ENOUGH:
        out = fits + [i for i in over if i["price_from_rub"] <= budget * DEST_BUDGET_FAR]
    else:
        out = fits + over + unknown
    return out[:limit]


def step_quote_destinations(ctx):
    brief = ctx.brief
    b = brief.values()
    origin = (b.get("origin") or {}).get("city") or ""
    limit = int(ctx.rules.get("max_destinations", 6))
    train, _ = _dest_prefs(brief)
    prices = destination_prices(ctx, brief, ctx.destination_recs)
    by_train = destination_prices(ctx, brief, ctx.destination_train_recs, train=True)
    known = {r["id"] for r in ctx.destination_recs}
    recs = list(ctx.destination_recs) + [r for r in ctx.destination_train_recs if r["id"] not in known]
    if ctx.destination_train_recs:   # one ranking for both pools: the budget rule then keeps the rank order
        recs.sort(key=lambda r: ctx.destination_rank.get(r["id"], len(ctx.destination_rank)))
    month = _month(brief) or (ctx.session.today() + timedelta(days=30)).month
    plabel = P.party_label(b.get("party"))
    traits = set(b.get("traits") or [])
    items = []
    budget = (b.get("budget_rub") or {}).get("max")
    for rec in recs:
        match = rec.get("match") or [t for t in rec.get("tags") or [] if t in traits]
        e = _cheaper_quote(prices.get(rec["id"]), by_train.get(rec["id"]), budget)
        if e is not None and e["facts"].get("long_train") and not long_train_fits_trip(ctx, e["travel_h"], e["nights"]):
            if train:
                continue   # a day or more on board for a short trip: not a train destination for it
            e = prices.get(rec["id"])
        elif e is not None and e["facts"].get("long_train") and not train and (
                not budget or e["price"] is None or e["price"] > budget or (traits and not match)):
            # a day or more on board is offered unasked only where it fits the budget and the wish
            e = prices.get(rec["id"])
        if e is None or (e["facts"].get("transport") == "train" and not e["reachable"]):
            continue   # no train there or back on these dates: not a train destination
        facts = dict(e["facts"])
        if rec.get("kind"):
            facts["kind"] = rec["kind"]
        if rec.get("description"):
            facts["description"] = rec["description"]
        if facts.get("transport") == "train":
            travel = e["travel_h"]
        elif rec.get("travel_time_h") is not None:
            travel = _travel_hours(rec.get("travel_time_h"))
        else:
            travel = _travel_hours(od(ctx, "travel_time", origin, rec["id"]))
        items.append({
            "id": rec["id"], "label": "", "name": rec["name"], "country": rec.get("country") or "",
            "price_from_rub": e["price"], "nights": e["nights"], "party_label": plabel,
            "weather": _weather(rec.get("weather")) if rec.get("weather") else (
                _weather(od(ctx, "climate", rec["id"], month)) if month else None),
            "travel_time_h": travel, "match": list(match), "facts": facts, "image_url": rec.get("image_url")})
    if not items:
        ctx.fail("no_data", f"Поездом из «{origin}» на эти даты билетов не нашлось ни в одно из подходящих "
                            "направлений. Попробуйте другие даты.")
    items = order_by_budget(items, budget, limit)
    if ctx.insight and ctx.insight.get("popular_ids") is not None:
        shown = {i["id"] for i in items}
        ctx.insight = {**ctx.insight, "popular_ids": [i for i in ctx.insight["popular_ids"] if i in shown]}
    labels = P.record_shown(ctx.session, "destination", [(i["id"], i["name"]) for i in items])
    for i in items:
        i["label"] = labels[i["id"]]
    ctx.destination_items = items
    remember_destinations(ctx.session, items, replace=True)


def _cheaper_quote(flight, train, budget=None):
    """Per place, the quote to show: the train one when it is priced and not dearer than the flight one. A
    long-distance train (a day or more on board) only when it fits the budget: no new noise where it does not."""
    t_ok = bool(train) and train["reachable"] and train["price"] is not None
    if t_ok and train["facts"].get("long_train") and flight and flight["price"] is not None \
            and not (budget and train["price"] <= budget):
        t_ok = False   # flights exist and the sleeper does not fit a budget: the flight quote, as before
    if t_ok and (not flight or flight["price"] is None or train["price"] <= flight["price"]):
        return train
    return flight


DEST_ITEMS_KEY = "engine:destination_items"
DEST_ITEM_KEYS = ("name", "price_from_rub", "nights", "transport", "transport_ru", "price_includes", "within_budget")


def remember_destinations(session, items, replace=False):
    """The shown destination items by id (name, price, transport, what the price includes) in session.results,
    so a later choose_destination knows the transport and an inspiration-stage question about the prices is
    answered from them."""
    rec = session.results.get(DEST_ITEMS_KEY)
    known = {} if replace or rec is None or not isinstance(rec.data, dict) else dict(rec.data.get("items") or {})
    order = [] if replace or rec is None or not isinstance(rec.data, dict) else list(rec.data.get("order") or [])
    for i in items:
        f = i.get("facts") or {}
        known[i["id"]] = {"name": i.get("name"), "price_from_rub": i.get("price_from_rub"), "nights": i.get("nights"),
                          **{k: f.get(k) for k in DEST_ITEM_KEYS[3:]}}
        if i["id"] not in order:
            order.append(i["id"])
    store_result(session, CachedResult(key=DEST_ITEMS_KEY, kind="destination_items", params={},
                                       fetched_at=session.clock(), data={"items": known, "order": order}))


TRANSPORT_CHOICE_KEY = "engine:transport_from_choice"


def transport_of_choice(ctx, dest_id):
    """A shown place quoted by train (budget-led inspiration) builds train variants: prefs.transport train, so
    the variants keep the quoted price; a place quoted by plane after such a choice goes back to any transport
    (a train the user asked for in words is never undone: then no place is quoted by plane)."""
    s = ctx.session
    item = next((i for i in remembered_destinations(s) if i["id"] == dest_id), None)
    if item is None or item.get("transport") not in ("train", "plane"):
        return
    have = (s.brief.values().get("prefs") or {}).get("transport") or "any"
    sets = ctx.patch.setdefault("set", {})
    if item["transport"] == "train" and have != "train":
        sets["prefs.transport"] = "train"
        store_result(s, CachedResult(key=TRANSPORT_CHOICE_KEY, kind="transport_choice", params={},
                                     fetched_at=s.clock(), data={"destination_id": dest_id}))
        ctx.trace["notes"].append(f"choose {dest_id}: quoted by train -> prefs.transport train")
    elif item["transport"] == "plane" and have == "train" and s.results.get(TRANSPORT_CHOICE_KEY) is not None:
        sets["prefs.transport"] = "any"
        s.results.pop(TRANSPORT_CHOICE_KEY, None)
        ctx.trace["notes"].append(f"choose {dest_id}: quoted by plane -> prefs.transport any")


def remembered_destinations(session):
    """[{id, name, price_from_rub, nights, transport, transport_ru, price_includes, within_budget}] in the order shown."""
    rec = session.results.get(DEST_ITEMS_KEY)
    if rec is None or not isinstance(rec.data, dict):
        return []
    items = rec.data.get("items") or {}
    return [{"id": i, **items[i]} for i in rec.data.get("order") or [] if i in items]


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
        best = best_month_for(ctx, brief, dest)
        if best is not None and best != month:
            month = best
            brief = assume(brief, "dates.month", month, "inferred")
            ctx.best_month_assumed = month
            ctx.trace["notes"].append(f"best time: dates assumed in month {month}")
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


# ---------------------------------------------------------------- location and best time

def center_search_m(ctx, brief, dest):
    """Max distance to the centre for the extra hotel search, or None when the centre does not count for this
    trip (ranking.center_applies): the user's prefs.center_max_m, else rules.ranking.center_search_m."""
    from .ranking import center_applies, ranking_rules, user_center_max
    rr = ranking_rules(ctx.rules)
    if not center_applies(brief, dest, rr):
        return None
    mx = user_center_max(brief)
    return int(mx if mx is not None else rr["center_search_m"])


NEAR_KEY = "engine:near_point"


def _loc_rules(rules):
    return (rules or {}).get("location") or {}


def clear_near_on_new_destination(before, new, patch):
    """prefs.near belongs to its destination: another (or no) destination drops it unless set in the same patch."""
    near = (new.values().get("prefs") or {}).get("near")
    if not near or "prefs.near" in ((patch or {}).get("set") or {}):
        return new
    old_id = (before.values().get("destination") or {}).get("id")
    new_id = (new.values().get("destination") or {}).get("id")
    if old_id and old_id != new_id:
        return assume(new, "prefs.near", None, "default")
    return new


def resolve_place(ctx, dest_id, text):
    """A place name of the user -> the poi point of `dest_id` (opendata pois) or None."""
    if not dest_id or not str(text or "").strip():
        return None
    poi = od(ctx, "find_poi", dest_id, text)
    if not poi:
        return None
    return {"query": str(text).strip(), "name": poi["name"], "kind": poi.get("kind"), "lat": poi["lat"],
            "lon": poi["lon"], "dist_center_m": poi.get("dist_center_m"), "osm_id": poi.get("osm_id"),
            "wikidata": poi.get("wikidata"), "sitelinks": poi.get("sitelinks"), "destination_id": dest_id}


def near_point(ctx, brief=None):
    """The resolved point of the brief's prefs.near in its destination (kept in session.results under NEAR_KEY,
    refreshed every turn, re-resolved when the destination or the words change) -> point dict or None."""
    brief = brief or ctx.session.brief
    v = brief.values()
    q = (v.get("prefs") or {}).get("near")
    dest_id = (v.get("destination") or {}).get("id")
    if not q or not dest_id:
        return None
    s = ctx.session
    with ctx.lock:
        rec = s.results.get(NEAR_KEY)
        data = rec.data if rec is not None and isinstance(rec.data, dict) else {}
    if data.get("destination_id") == dest_id and data.get("query") == q:
        point = data.get("point")
    else:
        point = resolve_place(ctx, dest_id, q)
    with ctx.lock:
        s.results.pop(NEAR_KEY, None)
        store_result(s, CachedResult(key=NEAR_KEY, kind="near_point", params={"destination_id": dest_id, "query": q},
                                     fetched_at=s.clock(), data={"destination_id": dest_id, "query": q,
                                                                 "point": point}))
    return point


def near_cue_phrase(ctx, dest_id, text, cue_key="near_cues"):
    """(poi, phrase) for the first «рядом с X» / «в районе X» in the text whose X names a poi of dest_id."""
    if not dest_id or not text:
        return None, None
    found = od(ctx, "find_poi_in_text", dest_id, text, _loc_rules(ctx.rules).get(cue_key) or [])
    return tuple(found) if found else (None, None)


def near_drop_cue(rules, text):
    return _cue_in((text or "").casefold().replace("ё", "е"),
                   [str(c).replace("ё", "е") for c in _loc_rules(rules).get("near_drop_cues") or []])


def near_search_cue(rules, text):
    """«есть что-то рядом с X?», «подберите у X»: a question that asks for other hotels, not a distance."""
    low = (text or "").casefold().replace("ё", "е")
    return any(str(c).replace("ё", "е") in low for c in _loc_rules(rules).get("near_search_cues") or [])


def _hotel_point(ctx, v):
    """(lat, lon) of a variant's hotel: kept on the variant, else from its offer, else from get_hotel."""
    if v.hotel is None:
        return None
    if v.hotel.lat is not None and v.hotel.lon is not None:
        return v.hotel.lat, v.hotel.lon
    offer = find_offer(ctx, v.hotel.offer_id) if v.hotel.offer_id else None
    if not offer or offer.get("lat") is None:
        offer = tool_safe(ctx, "get_hotel", {"hotel_id": v.hotel.hotel_id})
    if offer and offer.get("lat") is not None and offer.get("lon") is not None:
        return float(offer["lat"]), float(offer["lon"])
    return None


def near_distance_fact(ctx, variants, texts):
    """«Далеко ли отель от Айя-Софии?»: the place a question names (a poi of the destination after «от / до /
    рядом с …»), else the resolved prefs.near when the question is about location -> {"kind": "near_distance",
    "place", "found": true, "place_distance_center_m", "items": [{id, title, hotel, distance_near_m,
    distance_center_m}]} with metres computed by code per variant; a capitalised name that is no poi ->
    {"kind": "near_distance", "place", "found": false, "items": []}; None when no place is asked about."""
    from .ranking import near_distance_m
    dest_id = (ctx.session.brief.values().get("destination") or {}).get("id")
    texts = [t for t in texts if t]
    if not dest_id or not texts:
        return None
    point, phrase = None, None
    for t in texts:
        poi, ph = near_cue_phrase(ctx, dest_id, t, "question_cues")
        if poi is not None:
            point = resolve_place(ctx, dest_id, ph)
            break
        if ph and phrase is None and ph[:1].isupper():
            phrase = ph
    if point is None and phrase is None and any(LOCATION_Q_RE.search(t) for t in texts):
        point = near_point(ctx)
    if point is None:
        if phrase is None or od(ctx, "find_poi", dest_id, phrase) is not None:
            return None
        from .opendata import name_score
        from .places import is_generic, norm
        dest_name = (ctx.session.brief.values().get("destination") or {}).get("name") or ""
        if all(is_generic(w) for w in norm(phrase).split()) or name_score(phrase, dest_name) >= 0.85:
            return None          # «от центра», «от Стамбула»: not a place inside the destination
        return {"kind": "near_distance", "place": phrase, "found": False, "items": []}
    rows = []
    for v in variants or []:
        if v.hotel is None:
            continue
        f = v.facts or {}
        if f.get("near_name") == point["name"] and f.get("distance_near_m") is not None:
            d = int(f["distance_near_m"])
        else:
            pt = _hotel_point(ctx, v)
            d = near_distance_m({"lat": pt[0], "lon": pt[1]}, point) if pt else None
        if d is None:
            continue
        rows.append({"id": v.id, "title": v.title, "hotel": v.hotel.name, "distance_near_m": d,
                     "distance_center_m": f.get("distance_center_m")})
    return {"kind": "near_distance", "place": point["name"], "found": True, "place_kind": point.get("kind"),
            "place_distance_center_m": point.get("dist_center_m"), "items": rows}


def location_cue(rules, text):
    """rules.json `location` on the user's words -> metres for prefs.center_max_m («в центре» 1500, «в самом
    центре» 800) or None (no cue, or «не в центре»)."""
    loc = (rules or {}).get("location") or {}
    low = (text or "").casefold().replace("ё", "е")
    norm = [str(c).casefold().replace("ё", "е") for c in loc.get("not_center_cues") or []]
    if any(c in low for c in norm):
        return None
    if _cue_in(low, [str(c).replace("ё", "е") for c in loc.get("very_center_cues") or []]):
        return int(loc.get("center_very_m", 800))
    if _cue_in(low, [str(c).replace("ё", "е") for c in loc.get("center_cues") or []]):
        return int(loc.get("center_m", 1500))
    return None


def walkable_cue(rules, text):
    return _cue_in((text or "").casefold(), ((rules or {}).get("location") or {}).get("walkable_cues"))


def best_time_cue(rules, text):
    """«когда лучше поехать», «в какой сезон», «когда там хорошо» (rules.json best_time.cues)."""
    return _cue_in((text or "").casefold().replace("ё", "е"),
                   [str(c).replace("ё", "е") for c in ((rules or {}).get("best_time") or {}).get("cues") or []])


def _user_dates(brief):
    return any(brief.source_of(p) in ("user", "profile") for p in ("dates.start", "dates.month"))


def nearest_best_month(today, months, min_lead_days=7):
    """The first best month from today on whose trip can still start in it (the current month only while at
    least min_lead_days of it are left) -> month or None."""
    months = {int(m) for m in months or []}
    for k in range(13):
        m = (today.month - 1 + k) % 12 + 1
        if m not in months:
            continue
        if k == 0:
            nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
            if (nxt - today).days <= min_lead_days:
                continue
        return m
    return None


def best_month_for(ctx, brief, dest):
    """When the user asked when to go and gave no dates: the nearest best month (source inferred), else None."""
    if not ctx.best_time_asked or _user_dates(brief) or not (dest or {}).get("id"):
        return None
    bt = (ctx.rules or {}).get("best_time") or {}
    months = od(ctx, "best_months", dest["id"], brief.values().get("traits") or None, bt) or []
    return nearest_best_month(ctx.session.today(), [m["month"] for m in months], int(bt.get("min_lead_days", 7)))


MONTHS_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
              "ноябрь", "декабрь")


def _ru_list(words):
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " и " + words[-1]


def _deg_range(values):
    vals = sorted({int(round(v)) for v in values if v is not None})
    if not vals:
        return None
    return f"{vals[0]}°" if vals[0] == vals[-1] else f"{vals[0]}–{vals[-1]}°"


def best_time_fact(ctx, brief=None):
    """The best months for the brief's destination as ready Russian facts (opendata.best_months + the rules.json
    best_time rule) -> an info-style record {"kind": "best_time", "id", "data"} or None without climate data.
    data: best_months, best_months_ru, months [{month, month_ru, air_max_c, sea_c?}], summary_ru (months + day
    temperature, + sea temperature for a place by the sea), trip_month(_ru), trip_month_is_best, dates_assumed."""
    brief = brief or ctx.session.brief
    dest = brief.values().get("destination") or {}
    if not dest.get("id"):
        return None
    bt = (ctx.rules or {}).get("best_time") or {}
    months = od(ctx, "best_months", dest["id"], brief.values().get("traits") or None, bt) or []
    if not months:
        return None
    rec = as_dict(od(ctx, "destination", dest["id"])) or {}
    sea = bool(set(rec.get("tags") or []) & {"sea", "beach", "island"}) and any(m.get("sea_c") is not None for m in months)
    names = [MONTHS_NOM[m["month"] - 1] for m in months]
    rows = []
    for m in months:
        row = {"month": m["month"], "month_ru": MONTHS_NOM[m["month"] - 1], "air_max_c": m.get("air_max_c")}
        if sea and m.get("sea_c") is not None:
            row["sea_c"] = m["sea_c"]
        rows.append(row)
    summary = f"Лучшее время для поездки в {dest.get('name') or rec.get('name_ru') or dest['id']} — {_ru_list(names)}"
    air = _deg_range(m.get("air_max_c") for m in months)
    if air:
        summary += f": днём около {air}"
        water = _deg_range(m.get("sea_c") for m in months) if sea else None
        if water:
            summary += f", море {water}"
    data = {"place": dest.get("name"), "best_months": [m["month"] for m in months], "best_months_ru": _ru_list(names),
            "months": rows, "summary_ru": summary}
    trip_month = _month(brief)
    if trip_month:
        data["trip_month"] = int(trip_month)
        data["trip_month_ru"] = MONTHS_NOM[int(trip_month) - 1]
        data["trip_month_is_best"] = int(trip_month) in data["best_months"]
        data["dates_assumed"] = not _user_dates(brief)
    return {"kind": "best_time", "id": dest["id"], "data": data}


def center_distance_fact(variants):
    """Distance to the centre of every shown variant's hotel (ready numbers for «далеко от центра?»)."""
    rows = []
    for v in variants or []:
        f = v.facts or {}
        if v.hotel is None or f.get("distance_center_m") is None:
            continue
        d = int(f["distance_center_m"])
        rows.append({"id": v.id, "title": v.title, "hotel": v.hotel.name, "distance_center_m": d,
                     "distance_center_km": round(d / 1000, 1)})
    return {"kind": "center_distance", "items": rows} if rows else None


def unsold_cues(rules, text):
    """Services the user asked for that the agent does not sell (rules.json `unsold`: car rental, visas,
    excursions, insurance, cruises) -> their Russian names in the accusative, in rules order."""
    out = []
    for row in (rules or {}).get("unsold") or []:
        if text and re.search(row["re"], text, re.I) and row["ru"] not in out:
            out.append(row["ru"])
    return out


def unsold_only(rules, text):
    """«…и больше ничего не надо», «только машину»: the message asks for nothing the agent sells."""
    return bool(text and any(re.search(c, text, re.I) for c in (rules or {}).get("unsold_only_cues") or []))


def not_sold_fact(items):
    """{kind: not_sold, items_ru, line}: the ready sentence about what the agent does not book."""
    what = _ru_list(items)
    return {"kind": "not_sold", "items_ru": what,
            "line": f"{what[:1].upper() + what[1:]} я не оформляю — подбираю дорогу, отели и трансферы"}


def search_question(ctx):
    """A question inside a search / refine message (understand info_question, or a best-time cue): never dropped.
    -> {"text", "facts"} for present.variants / present.update, or None. facts: best_time (when asked), the
    distance to the centre of each variant's hotel, the distance to a named place (near_distance), the climate
    of the trip month."""
    if ctx.action is not None:
        return None
    text = ((ctx.understand or {}).get("info_question") or "").strip()
    asked = ctx.best_time_asked or best_time_cue(ctx.rules, text)
    road = transport_search_fact(ctx) if ctx.transport_gap or ctx.session.results.get(TRANSPORT_GAP_KEY) else None
    q_text = f"{text} {ctx.text or ''}"
    road_asked = road is not None and bool(TRANSPORT_ASK_RE.search(q_text) or TRAIN_ASK_RE.search(q_text)) and \
        bool(text or "?" in (ctx.text or "") or re.match(r"\s*а\s", ctx.text or "", re.I))
    if not text and (asked or ctx.unsold or road_asked):
        text = (ctx.text or "").strip()
    if not text:
        return None
    facts = []
    if road_asked:   # «а с пересадкой?», «а поездом?» about a place without a road: what code searched
        facts.append(road)
    if ctx.unsold:   # car rental, a visa, excursions…: not sold here — one sentence, the trip goes on
        facts.append(not_sold_fact(ctx.unsold))
    if asked:
        bt = best_time_fact(ctx)
        if bt is not None:
            facts.append(bt)
    cd = center_distance_fact(ctx.variants)
    if cd is not None:
        facts.append(cd)
    nd = near_distance_fact(ctx, ctx.variants, [text, ctx.text])
    if nd is not None:
        facts.append(nd)
    dest = ctx.session.brief.values().get("destination") or {}
    month = _month(ctx.session.brief)
    if dest.get("id") and month:
        clim = od(ctx, "climate", dest["id"], month)
        if clim is not None:
            facts.append({"kind": "climate", "id": dest["id"], "month": month,
                          "data": {k: v for k, v in as_dict(clim).items() if k not in INFO_DROP}})
    return {"text": text, "facts": facts}


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
    if NO_HOSTEL in (v.get("constraints") or []):
        f["kinds"] = list(NON_HOSTEL_KINDS)   # the mock drops hostels before its top-`limit` cut
    return f


def amenity_filters(brief):
    """search_hotels filters for the synthetic amenities the user asked for: constraint kitchen -> kitchen,
    accessible -> step_free (used by the extra search only: ranking relaxes them when nothing has them)."""
    cons = brief.values().get("constraints") or []
    out = {}
    if "kitchen" in cons:
        out["kitchen"] = True
    if "accessible" in cons:
        out["step_free"] = True
    return out


# catalog hotels.kind values other than hostel (constraint no_hostel searches only these)
NON_HOSTEL_KINDS = ("hotel", "apartment", "guest_house", "motel", "resort", "chalet")


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


OVERNIGHT_FROM_HOUR = 18   # same rule as checks.check_dates: an evening departure the day before is fine


def _overnight_before(value, check_in):
    """Trains of the day before check-in that leave in the evening and arrive on the check-in day."""
    out = []
    for o in (value or {}).get("offers", []):
        dep, arr = _dt(o.get("dep")), _dt(o.get("arr"))
        if dep and arr and dep.hour >= OVERNIGHT_FROM_HOUR and arr.date() == check_in:
            out.append(o)
    return out


def _arriving_on(values, check_in):
    """Long-distance trains (a pair over 1000 km) of the days before check-in that arrive on the check-in day,
    whatever hour they leave (a berth is a bed: the nights on board are no nights without one)."""
    out = []
    for value in values:
        for o in (value or {}).get("offers", []):
            arr = _dt(o.get("arr"))
            if arr and arr.date() == check_in:
                out.append(o)
    return out


def long_train_pair(ctx, brief, dest):
    """True when origin -> dest is a long-distance train pair (opendata.long_train_pair: Russia, stations at both
    ends, 1000..2500 km): the mock sells only sleepers there, up to ~two days on the road."""
    origin = _origin_place(ctx, brief)
    rec = destination_record(ctx, dest_id=dest.get("id")) if dest.get("id") else dest
    return bool(long_train_pair_rule(origin, rec or dest))


def _flex_days(brief):
    return max(0, min(3, int((brief.values().get("dates") or {}).get("flexible_days") or 0)))


def _ranking_rule(ctx, key, default):
    return ((ctx.rules or {}).get("ranking") or {}).get(key, default)


def run_search(ctx, brief, dest, check_in, check_out, track=True):
    """Every search the scope needs, in parallel, cached by params. -> (results, districts, called_tools).

    All offers of one result share ONE date set: outbound on check-in (or the overnight leg arriving on
    it), hotel check-in..check-out, return on check-out. `dates.flexible_days` never mixes dates: the
    user's own dates come first; within ±flexible_days the whole trip moves (check-in = the arrival day,
    check-out = check-in + nights, the hotel/return/transfer searches re-run for the moved dates) only
    when the user's dates build no variant, or another start day is cheaper overall by
    rules.ranking.flex_shift_min_saving_pct. A moved trip carries results["date_shift"]
    ({days, flex_days, requested_check_in, reason: "no_options"|"cheaper"}); ranking turns it into
    facts.dates_shifted_days and a `dates_shifted` risk note.
    """
    results, districts, called = _search_dates(ctx, brief, dest, check_in, check_out)
    flex = _flex_days(brief)
    if not flex:
        return results, districts, called
    base = [v for v in build(ctx, brief, results, districts, pin=False) if not v.facts.get("no_transport")]
    base_min = min((v.totals.rub for v in base), default=None)
    tickets = _transport_min(results)
    pct = float(_ranking_rule(ctx, "flex_shift_min_saving_pct", 10)) / 100
    nights = (check_out - check_in).days
    for days, est in _flex_shifts(ctx, brief, dest, check_in, check_out, flex, results):
        if base_min is not None and (est is None or tickets is None or est > tickets - pct * base_min):
            continue   # the user's dates work and this day's tickets cannot make the trip clearly cheaper
        ci = check_in + timedelta(days=days)
        r2, d2, c2 = _search_dates(ctx, brief, dest, ci, ci + timedelta(days=nights))
        called = sorted(set(called) | set(c2))
        moved = [v for v in build(ctx, brief, r2, d2, pin=False) if not v.facts.get("no_transport")]
        if not moved:
            continue
        cheapest = min(v.totals.rub for v in moved)
        if base_min is None or cheapest <= base_min * (1 - pct):
            r2["date_shift"] = {"days": days, "flex_days": flex, "requested_check_in": check_in.isoformat(),
                                "reason": "no_options" if base_min is None else "cheaper"}
            return r2, d2, called
    return results, districts, called


def _transport_min(results):
    """Cheapest outbound + return tickets of a result (None without both)."""
    def low(*keys):
        prices = [o.get("price_rub") for k in keys for o in results.get(k) or [] if o.get("price_rub") is not None]
        return min(prices) if prices else None
    out, back = low("flights_out", "trains_out"), low("flights_back", "trains_back")
    return None if out is None or back is None else out + back


def _flex_shifts(ctx, brief, dest, check_in, check_out, flex, results):
    """Candidate shifts within ±flex days -> [(days, cheapest tickets for that date set | None)], nearest
    first. The flight window comes from one search_flights(flex_days=flex) each way (cached); a day with
    no flight pair keeps est None (trains are searched per day only when that day is tried)."""
    scope = results.get("scope") or []
    est = {}
    transport = (brief.values().get("prefs") or {}).get("transport") or "any"
    jobs = (_flight_jobs(ctx, brief, dest, check_in, check_out, flex)
            if "flights" in scope and transport != "train" else {})
    if jobs:
        found = _parallel(ctx, jobs, tolerant=True)
        outs = (found.get("flights.out") or {}).get("offers", [])
        backs = (found.get("flights.back") or {}).get("offers", [])
        nights = (check_out - check_in).days
        for days in range(-flex, flex + 1):
            ci = check_in + timedelta(days=days)
            co = ci + timedelta(days=nights)
            o = [f["price_rub"] for f in outs if _dt(f["arr"]).date() == ci]
            b = [f["price_rub"] for f in backs if _dt(f["dep"]).date() == co]
            if o and b:
                est[days] = min(o) + min(b)
    shifts = [d for d in range(-flex, flex + 1) if d and check_in + timedelta(days=d) >= ctx.session.today()]
    priced = sorted((d for d in shifts if d in est), key=lambda d: (est[d], abs(d), d))
    rest = sorted((d for d in shifts if d not in est), key=lambda d: (abs(d), d))
    return [(d, est[d]) for d in priced] + [(d, None) for d in rest]


def _flight_jobs(ctx, brief, dest, check_in, check_out, flex=0):
    o_iata = resolve_origin_iata(ctx)
    d_iata = dest.get("airports") or []
    if not (o_iata and d_iata):
        return {}
    adults, children, infants = _flight_party(brief, check_in)
    base = {"adults": adults, "children": children, "infants": infants, "flex_days": flex,
            "today": ctx.session.today().isoformat(), **_pets_payload(brief)}
    return {"flights.out": ("search_flights", {**base, "origin_iata": o_iata, "dest_iata": d_iata,
                                               "date": check_in.isoformat()}),
            "flights.back": ("search_flights", {**base, "origin_iata": d_iata, "dest_iata": o_iata,
                                                "date": check_out.isoformat()})}


def _search_dates(ctx, brief, dest, check_in, check_out, hotel_id=None, hotel_cap=None):
    """The searches of one date set (flights on the exact days, never a flex window). `hotel_id`: only that
    hotel, every fitting room (search_hotels all_rooms) — the checkout re-price of a chosen variant.
    `hotel_cap`: only stays up to that total (search_hotels max_price_total, a wider list) — the budget fit."""
    v = brief.values()
    scope = v.get("scope") or list(P.SCOPE_FULL)
    transport = (v.get("prefs") or {}).get("transport") or "any"
    today = ctx.session.today().isoformat()
    party = _party_payload(brief)
    pax = party["adults"] + len(party["children_ages"])
    origin = v.get("origin") or {}
    before = len(ctx.trace["tools"])
    jobs = {}
    if "flights" in scope and transport != "train":
        jobs.update(_flight_jobs(ctx, brief, dest, check_in, check_out))
    long_pair = False
    if "trains" in scope or transport == "train":
        base = {"passengers": pax, "today": today, **_pets_payload(brief)}
        jobs["trains.out"] = ("search_trains", {**base, "origin_city": origin.get("city") or "",
                                                "dest_city": dest["name"], "date": check_in.isoformat()})
        jobs["trains.back"] = ("search_trains", {**base, "origin_city": dest["name"],
                                                 "dest_city": origin.get("city") or "", "date": check_out.isoformat()})
        long_pair = long_train_pair(ctx, brief, dest)
        if "stay" in scope:
            # an overnight train the evening before check-in arrives in the morning of the first day
            # (a same-day night train loses the first night): ranking may pick either. A long-distance pair
            # (over 1000 km, up to ~two days on board) also searches two days before: the trains that arrive on
            # the check-in day are the trip (the return leaves on check-out and may arrive a day or two later).
            for k in range(1, (LONG_TRAIN_LEAD_DAYS if long_pair else 1) + 1):
                jobs["trains.out.prev" + (str(k) if k > 1 else "")] = (
                    "search_trains", {**base, "origin_city": origin.get("city") or "", "dest_city": dest["name"],
                                      "date": (check_in - timedelta(days=k)).isoformat()})
    if "stay" in scope:
        filters = _hotel_filters(brief)
        if hotel_cap is not None:
            filters["max_price_total"] = int(hotel_cap)
        jobs["hotels"] = ("search_hotels", {"destination_id": dest["id"], "check_in": check_in.isoformat(),
                                            "check_out": check_out.isoformat(), "adults": party["adults"],
                                            "children_ages": party["children_ages"], "filters": filters,
                                            "limit": 40 if "crib" in (v.get("constraints") or []) or hotel_id
                                            or hotel_cap is not None else 20,
                                            "today": today, **({"hotel_id": hotel_id, "all_rooms": True}
                                                               if hotel_id else {})})
    near = near_point(ctx, brief) if "stay" in scope else None
    near_m = int(_ranking_rule(ctx, "near_max_m", 1500))
    if near is not None and "hotels" in jobs:
        # a named place (prefs.near): every offer carries its distance, and an extra search filtered by the
        # radius around it (applied before the mock's top-`limit` cut) brings the hotels next to it
        name, payload = jobs["hotels"]
        point = {"lat": near["lat"], "lon": near["lon"]}
        jobs["hotels"] = (name, {**payload, "filters": {**payload["filters"], "near": point}})
        if not hotel_id:
            jobs["hotels.near"] = (name, {**payload, "limit": 20, "filters": {**payload["filters"], "near": point,
                                                                              "max_distance_near_m": near_m}})
    elif "hotels" in jobs and not hotel_id:
        center_m = center_search_m(ctx, brief, dest)
        if center_m is not None:
            # the mock returns its top `limit` hotels by rating / stars / price: central hotels reach the ranking
            # only through a search filtered by the distance to the centre (applied before that cut)
            name, payload = jobs["hotels"]
            jobs["hotels.center"] = (name, {**payload, "limit": 20,
                                            "filters": {**payload["filters"], "max_distance_center_m": center_m}})
    amenity = amenity_filters(brief)
    if amenity and "hotels" in jobs and not hotel_id:
        # kitchen / step-free (constraints kitchen, accessible): the mock's top `limit` rarely holds them, so an
        # extra search filtered by them (applied before that cut) brings them to the ranking, which keeps them
        # and relaxes the wish only when none is left
        name, payload = jobs["hotels"]
        jobs["hotels.amenity"] = (name, {**payload, "limit": 20, "filters": {**payload["filters"], **amenity}})
    out = _parallel(ctx, jobs)
    if "hotels.near" in jobs and not (out.get("hotels.near") or {}).get("offers"):
        # no hotel within the radius: one wider search finds the nearest ones (ranking relaxes and says so)
        name, payload = jobs["hotels.near"]
        wide = int(_ranking_rule(ctx, "near_wide_m", 6000))
        out["hotels.near"] = tool_safe(ctx, name, {**payload, "filters": {**payload["filters"],
                                                                          "max_distance_near_m": wide}})
    for extra_key in ("hotels.near", "hotels.center", "hotels.amenity"):
        if out.get(extra_key) is None:
            continue
        seen = {o["offer_id"] for o in (out.get("hotels") or {}).get("offers") or []}
        extra = [o for o in out[extra_key].get("offers") or [] if o["offer_id"] not in seen]
        base = out.get("hotels") or {"offers": [], "total_found": 0}
        out["hotels"] = {**base, "offers": extra + list(base.get("offers") or [])}
    if "flights.out" in jobs and "stay" in scope:
        # every flight of the check-in day lands on a later day (eastern origins: OVB 22:25 -> MCT 11:25 next
        # day): the first night is lost and nothing pairs with the stay (h01 Muscat no_data). Overnight flights
        # of the evening before that land on the check-in day are the trip the quote priced, like overnight trains.
        outs = (out.get("flights.out") or {}).get("offers", [])
        if outs and not any(_dt(o.get("arr")) and _dt(o["arr"]).date() == check_in for o in outs):
            name, payload = jobs["flights.out"]
            prev = tool_safe(ctx, name, {**payload, "date": (check_in - timedelta(days=1)).isoformat()})
            extra = _overnight_before(prev, check_in)
            if extra:
                out["flights.out"] = {**out["flights.out"], "offers": extra + outs}
                ctx.trace["notes"].append(f"flights: {len(extra)} overnight outbound(s) of the evening before "
                                          f"{check_in.isoformat()} added (none of that day lands on it)")
    if out.get("hotels"):
        out["hotels"] = {**out["hotels"], "offers": _ungendered_rooms(ctx, jobs["hotels"][1], out["hotels"].get("offers") or [])}
    results = {"origin": origin, "destination": dest, "check_in": check_in.isoformat(),
               "check_out": check_out.isoformat(), "party": party, "scope": list(scope),
               "flights_out": (out.get("flights.out") or {}).get("offers", []),
               "flights_back": (out.get("flights.back") or {}).get("offers", []),
               "trains_out": (_arriving_on([out.get("trains.out.prev" + (str(k) if k > 1 else ""))
                                            for k in range(LONG_TRAIN_LEAD_DAYS, 0, -1)], check_in) if long_pair
                              else _overnight_before(out.get("trains.out.prev"), check_in))
                             + (out.get("trains.out") or {}).get("offers", []),
               "trains_back": (out.get("trains.back") or {}).get("offers", []),
               "hotels": (out.get("hotels") or {}).get("offers", []), "transfers_out": {}, "transfers_back": {}}
    if long_pair and transport != "train" and results["flights_out"] and results["flights_back"]:
        # no new noise where flights exist: a day-long sleeper shows up only when the user wants a train
        # (prefs.transport train, also when code assumed it for a place without flights), when the pair has no
        # flight on these dates, or in a budget-led inspiration where it fits the budget
        if results["trains_out"] or results["trains_back"]:
            ctx.trace["notes"].append("trains: long-distance sleepers left out (flights exist, no train wish)")
        results["trains_out"], results["trains_back"] = [], []
    if near is not None and "stay" in scope:
        results["near"] = {"name": near["name"], "query": near.get("query"), "lat": near["lat"], "lon": near["lon"],
                           "kind": near.get("kind"), "dist_center_m": near.get("dist_center_m"), "radius_m": near_m}
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


def _ungendered_rooms(ctx, payload, offers):
    """The mock offers each hotel's cheapest room; when that is a men-/women-only room (we never know
    the traveller's gender) the hotel's cheapest other room is asked for instead (search_hotels with
    hotel_id + all_rooms); a hotel without one is dropped."""
    from .ranking import is_gendered_room
    bad = [o for o in offers if is_gendered_room(o.get("room"))]
    if not bad:
        return offers
    jobs = {o["hotel_id"]: ("search_hotels", {**payload, "hotel_id": o["hotel_id"], "all_rooms": True, "limit": 40})
            for o in bad}
    found = _parallel(ctx, jobs, tolerant=True)
    swap = {}
    for hid, value in found.items():
        rooms = [o for o in (value or {}).get("offers", []) if not is_gendered_room(o.get("room"))]
        if rooms:
            swap[hid] = min(rooms, key=lambda o: (o.get("price_total_rub") or 0, o.get("offer_id") or ""))
    return [swap[o["hotel_id"]] if is_gendered_room(o.get("room")) and o["hotel_id"] in swap else o
            for o in offers if not is_gendered_room(o.get("room")) or o["hotel_id"] in swap]


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
    reset_auto_transport(ctx)
    dest = current_destination(ctx)
    check_in, check_out = resolve_dates(ctx, dest)
    results, districts, called = run_search(ctx, ctx.session.brief, dest, check_in, check_out)
    ctx.results, ctx.districts = results, districts
    ctx.rebuilt = [{"search_flights": "flights", "search_trains": "trains", "search_hotels": "stay",
                    "search_transfers": "transfers"}.get(t, t) for t in called if t.startswith("search_")]
    for i in ctx.engine.hooks.run("after_search", ctx, kinds=("validate",)):
        ctx.issues.append(i)
    # no hotels on these dates is no dead end here: build_variants tries the neighbouring dates first and
    # then says in plain words what is missing (no_build_message)


# ---------------------------------------------------------------- variants

def rules_view(ctx):
    """skill rules for ranking/checks, plus segments.json (district weights) under `segments`."""
    from .night_arrival import traveller_context
    view = dict(ctx.rules)
    view.setdefault("segments", ctx.skill.segments)
    view.update(traveller_context(ctx))
    return view


def _as_variant(v):
    return v if isinstance(v, Variant) else Variant.model_validate(v)


def build(ctx, brief, results, districts, exclude=(), pin=True):
    if exclude:
        results = _exclude_offers(results, set(exclude))
    built = ctx.domain.build_variants(brief, results, districts, rules_view(ctx), ctx.session.today())
    variants = [_as_variant(v) for v in built or []][: int(ctx.rules.get("max_variants", 3))]
    return _pin_transfers(ctx, variants, results, brief, districts) if pin else variants


def _pin_transfers(ctx, variants, results, brief, districts, attempt=0):
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
    party = _party_payload(brief)
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
            where[key] = (v, s, o, reverse)
    found = _parallel(ctx, jobs, tolerant=True)
    rebuilds = {}
    for key in jobs:
        value = found.get(key)
        v, s, old, reverse = where[key]
        offers = (value or {}).get("offers") or []
        pick = next((o for o in offers if o.get("kind") == old.get("kind")), None) or (offers[0] if offers else None)
        changed = pick is None or any(pick.get(field) != old.get(field)
                                      for field in ("kind", "duration_min", "price_rub"))
        if changed and v.id not in rebuilds:
            pinned = dict(results)
            legs = [step for step in v.steps if step.type in TRANSPORT_STEPS]
            for direction, leg in zip(("out", "back"), legs):
                for kind in ("flights", "trains"):
                    pinned[f"{kind}_{direction}"] = [o for o in results.get(f"{kind}_{direction}") or []
                                                     if o["offer_id"] == leg.offer_id]
            if v.hotel is not None:
                pinned["hotels"] = [o for o in results.get("hotels") or [] if o["offer_id"] == v.hotel.offer_id]
            for direction in ("out", "back"):
                pinned[f"transfers_{direction}"] = {}
            for source, _, offer, back in where.values():
                if source is v:
                    pinned["transfers_back" if back else "transfers_out"][offer["hotel_id"]] = [offer]
            rebuilds[v.id] = pinned
        if v.id in rebuilds:
            rebuilds[v.id]["transfers_back" if reverse else "transfers_out"][old["hotel_id"]] = (
                [{**old, **pick}] if pick is not None else [])
        if pick is not None:
            s.offer_id = pick["offer_id"]
            s.link = f"ostrovok-mock:transfer/{pick['offer_id']}"
    if not rebuilds:
        return variants
    out = []
    for variant in variants:
        if variant.id not in rebuilds:
            out.append(variant)
            continue
        # A replacement changes arrival/leave times too. Reuse the builder instead of patching its facts.
        # Re-quote the rebuilt pickup once more; unstable quotes are left out rather than shown as valid.
        if attempt >= 2:
            ctx.trace["notes"].append(f"transfer quote did not settle for {variant.id}")
            continue
        pinned = rebuilds[variant.id]
        built = build(ctx, brief, pinned, districts, pin=False)
        if not built:
            continue
        fresh = built[0].model_copy(update={"id": variant.id, "title": variant.title})
        for field in ("also_fastest", "also_comfort", "coincide_note"):
            fresh.facts.pop(field, None)  # a one-combination rebuild cannot establish these claims
        out.extend(_pin_transfers(ctx, [fresh], pinned, brief, districts, attempt + 1))
    # A timed replacement can make another shown trip cheaper/faster. Keep the actual winner,
    # rather than retaining a card with a superlative that no longer matches its journey.
    for label, metric, title in (("cheapest", "rub", "Дешевле"),
                                 ("fastest", "door_to_door_hours", "Быстрее")):
        named = next((v for v in out if v.id == label), None)
        winner = min(out, key=lambda v: getattr(v.totals, metric)) if out else None
        if named is not None and getattr(named.totals, metric) > getattr(winner.totals, metric):
            out.remove(named)
            if winner.id == "cheapest":
                winner.facts["also_fastest"] = True
            else:
                was_fastest = winner.id == "fastest"
                winner.id, winner.title = label, title
                if was_fastest:
                    winner.facts["also_fastest"] = True
    from .ranking import refresh_variant_comparison
    return refresh_variant_comparison(out, brief, rules_view(ctx))


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
    first = ctx.results
    ctx.variants = build(ctx, ctx.brief, ctx.results, ctx.districts)
    if not ctx.variants:
        ctx.variants = rescue_dates(ctx)
    if not ctx.variants:   # nothing at all: a missing road gets the same honest recovery as stay-only variants
        ctx.results = first
        recover_transport(ctx, built_nothing=True)
    elif any(v.facts.get("no_transport") for v in ctx.variants):
        recover_transport(ctx)
    if ctx.variants:
        ctx.variants = fit_budget(ctx, ctx.variants)
        offer_other_places(ctx)
    ctx.session.variants = ctx.variants
    if not ctx.variants:
        gap_actions = list(ctx.extra_actions or []) if ctx.transport_gap else []
        ctx.extra_actions = gap_actions + [a for a in other_destination_actions(ctx) if a not in gap_actions]
        ctx.fail("no_data", transport_gap_message(ctx) if ctx.transport_gap else no_build_message(ctx, first))


# ---------------------------------------------------------------- a build that gives nothing / nothing in budget
# (held-out level-3 run): a chosen destination that builds nothing on its dates tries the flexible window
# (±RESCUE_FLEX_DAYS, then ±1 night) before giving up, and then says in plain words what is missing with the
# other shown destinations one tap away; a budget no variant fits (or «другие числа» / «подешевле» without
# values) tries other start days in the same month, then cheaper hotels, then both, and reports the change
# in facts (budget_fit, budget_rub, budget_dropped_prefs) and a risk note the update card can say.

RESCUE_FLEX_DAYS = 3
BUDGET_SHIFT_DAYS = 3
BUDGET_COMBINED_TRIES = 3
# comfort prefs a budget fit may drop when the user never set them (segment defaults, profile guesses)
COMFORT_DEFAULTS = (("district", "any"), ("meals", "any"), ("free_cancellation", None), ("stars_min", None))
COMFORT_RU = {"district": "район", "meals": "питание", "free_cancellation": "бесплатная отмена",
              "stars_min": "звёздность"}


def _built(ctx, brief, results, districts, pin=False):
    """Door-to-door variants of a result (stay-only `no_transport` ones never count as built). pin: transfers
    re-quoted at their legs' times (the price the user will see)."""
    want_transport = bool(set(results.get("scope") or []) & {"flights", "trains"})
    out = build(ctx, brief, results, districts, pin=pin)
    return [v for v in out if not (want_transport and v.facts.get("no_transport"))] if want_transport else out


def _low(variants):
    return min((v.totals.rub for v in variants), default=None)


def _moved_brief(brief, check_in, check_out, base_in, base_out):
    """The brief for a trip moved from base dates: the user's own dates stay and the flexible days widen to
    cover the move (checks then accept it); inferred dates simply move. A changed number of nights is inferred."""
    if (check_in, check_out) == (base_in, base_out):
        return brief
    nights, base_nights = (check_out - check_in).days, (base_out - base_in).days
    if brief.source_of("dates.start") == "user" or brief.source_of("dates.end") == "user":
        days = max(abs((check_in - base_in).days), abs((check_out - base_out).days))
        if days > _flex_days(brief):
            brief = assume(brief, "dates.flexible_days", min(3, days), "inferred")
    else:
        brief = assume(brief, "dates.start", check_in.isoformat(), "inferred")
        brief = assume(brief, "dates.end", check_out.isoformat(), "inferred")
    if nights != base_nights:
        brief = assume(brief, "dates.nights", nights, "inferred")
    return brief


def _date_changes(ctx, base_in, base_out, check_in, check_out):
    rows = []
    if check_in != base_in:
        rows.append({"field": "dates.start", "before": base_in.isoformat(), "after": check_in.isoformat()})
    if check_out != base_out:
        rows.append({"field": "dates.end", "before": base_out.isoformat(), "after": check_out.isoformat()})
    if (check_out - check_in).days != (base_out - base_in).days:
        rows.append({"field": "dates.nights", "before": (base_out - base_in).days,
                     "after": (check_out - check_in).days})
    known = {r["field"] for r in ctx.changes}
    ctx.changes += [r for r in rows if r["field"] not in known]


def _adopt(ctx, brief, results, districts):
    """Make a found result the turn's result: the brief, results and the pinned variants."""
    ctx.session.brief = brief
    ctx.results, ctx.districts = results, districts
    for kind in ("flights", "stay", "transfers") if "flights" in (results.get("scope") or []) else ("stay",):
        if kind not in ctx.rebuilt:
            ctx.rebuilt.append(kind)
    return build(ctx, brief, results, districts)


def rescue_dates(ctx):
    """Nothing builds on the dates: the same trip ±RESCUE_FLEX_DAYS days (whole trip moved), then one night
    shorter / longer (also within the window). -> pinned variants or []."""
    res = ctx.results or {}
    if not res.get("check_in") or not res.get("check_out"):
        return []
    dest = res.get("destination") or current_destination(ctx)
    base_in, base_out = _date(res["check_in"]), _date(res["check_out"])
    nights = (base_out - base_in).days
    brief = ctx.session.brief
    flexed = brief if _flex_days(brief) >= RESCUE_FLEX_DAYS else assume(
        brief, "dates.flexible_days", RESCUE_FLEX_DAYS, "inferred")
    tries = [] if flexed is brief else [nights]
    tries += [n for n in (nights - 1, nights + 1) if n >= 1]
    for n in tries:
        b2 = flexed if n == nights else assume(flexed, "dates.nights", n, "inferred")
        r2, d2, _ = run_search(ctx, b2, dest, base_in, base_in + timedelta(days=n))
        if not _built(ctx, b2, r2, d2):
            continue
        ci, co = _date(r2["check_in"]), _date(r2["check_out"])
        shift = r2.get("date_shift")
        if shift:
            shift.update(auto=True, flex_days=RESCUE_FLEX_DAYS)
        moved = _moved_brief(brief, ci, co, base_in, base_out)
        variants = _adopt(ctx, moved, r2, d2)
        _date_changes(ctx, base_in, base_out, ci, co)
        if n != nights:
            for v in variants:
                v.facts["nights_changed_from"] = nights
                v.risks.insert(0, _risk_note("nights_changed", f"На {n} {P.plural(n, 'ночь', 'ночи', 'ночей')} вместо "
                                                               f"{nights}: на {nights} не стыкуется ни на одни даты рядом",
                                             nights=n, requested=nights))
        ctx.trace["notes"].append(f"rescue: nothing on {base_in}..{base_out}, built on {ci}..{co}")
        return variants
    ctx.trace["notes"].append(f"rescue: nothing within ±{RESCUE_FLEX_DAYS} days and ±1 night of {base_in}")
    return []


def _risk_note(code, text, **data):
    from .state import Note
    return Note(code=code, text=text, data=data)


def _comfort_free_brief(brief):
    """The brief without the comfort prefs the user never set -> (brief, dropped pref paths)."""
    prefs = brief.values().get("prefs") or {}
    dropped = []
    for key, default in COMFORT_DEFAULTS:
        if brief.source_of(f"prefs.{key}") == "user" or prefs.get(key) in (default, None):
            continue
        brief = assume(brief, f"prefs.{key}", default, "inferred")
        dropped.append(f"prefs.{key}")
    return brief, dropped


def fit_budget(ctx, variants):
    """budget_rub.max set and every variant above it — or «другие числа» / «подешевле» without values: other
    start days in the same month (±BUDGET_SHIFT_DAYS), then cheaper hotels (the user's hard prefs stay; comfort
    prefs nobody set are dropped; stays capped by what the tickets leave of the budget), then both. The first
    that fits is taken; when nothing fits, the closest cheaper result is shown and said so."""
    s = ctx.session
    res = ctx.results or {}
    budget = (s.brief.values().get("budget_rub") or {}).get("max")
    low = _low(variants)
    if not res.get("check_in") or not res.get("check_out") or low is None:
        return variants
    over = bool(budget) and low > budget
    if not (over or ctx.ask_other_dates or ctx.ask_cheaper):
        return variants
    target = budget if over else (low - 1 if ctx.ask_cheaper else None)
    reason = "budget" if over else ("cheaper" if ctx.ask_cheaper else "other_dates")
    dest = res.get("destination") or current_destination(ctx)
    base_in, base_out = _date(res["check_in"]), _date(res["check_out"])
    requested = _date((res.get("date_shift") or {}).get("requested_check_in")) or base_in
    nights = (base_out - base_in).days
    brief = s.brief
    scope = set(res.get("scope") or [])
    fits = (lambda m: m is not None and (target is None or m <= target))
    best = None   # (low, label, brief, results, districts, dropped)

    def consider(label, b2, r2, d2, dropped=()):
        nonlocal best
        m = _low(_built(ctx, b2, r2, d2))
        if m is not None and (best is None or m < best[0]):
            best = (m, label, b2, r2, d2, list(dropped))
        return fits(m)

    shifts = [(d, est) for d, est in _flex_shifts(ctx, brief, dest, base_in, base_out, BUDGET_SHIFT_DAYS, res)
              if (base_in + timedelta(days=d)).month == base_in.month and base_in + timedelta(days=d) != requested]
    found = None
    # 1. other start days in the same month
    for d, est in shifts:
        if target is not None and est is not None and est >= target:
            continue   # the tickets alone are over
        ci = base_in + timedelta(days=d)
        r2, d2, _ = _search_dates(ctx, brief, dest, ci, ci + timedelta(days=nights))
        if consider("dates", brief, r2, d2):
            found = best
            break
    # 2. cheaper hotels on the dates shown, 3. both (only when a price target exists)
    if found is None and target is not None and "stay" in scope:
        cheap, dropped = _comfort_free_brief(brief)
        tickets = _transport_min(res) if scope & {"flights", "trains"} else 0
        if tickets is not None and target - tickets > 0:
            r2, d2, _ = _search_dates(ctx, cheap, dest, base_in, base_out, hotel_cap=target - tickets)
            if consider("hotel", cheap, r2, d2, dropped):
                found = best
        tried = 0
        for d, est in shifts:
            if found is not None or tried >= BUDGET_COMBINED_TRIES:
                break
            if est is None or est >= target:
                continue
            tried += 1
            ci = base_in + timedelta(days=d)
            r2, d2, _ = _search_dates(ctx, cheap, dest, ci, ci + timedelta(days=nights), hotel_cap=target - est)
            if consider("dates_and_hotel", cheap, r2, d2, dropped):
                found = best
    pick = found or (best if best is not None and best[0] < low and target is not None else None)
    if pick is None:
        status = "not_found" if (over or ctx.ask_cheaper) else "other_dates_not_found"
        _mark_budget(variants, status, budget, low, [], no_hostel=_no_hostel(brief))
        ctx.trace["notes"].append(f"budget fit ({reason}): nothing found (±{BUDGET_SHIFT_DAYS} days"
                                  + (", cheaper hotels)" if target is not None else ")"))
        return variants
    m, label, b2, r2, d2, dropped = pick
    ci, co = _date(r2["check_in"]), _date(r2["check_out"])
    if ci != base_in:
        r2["date_shift"] = {"days": (ci - requested).days, "flex_days": BUDGET_SHIFT_DAYS,
                            "requested_check_in": requested.isoformat(), "reason": reason, "auto": True,
                            **({"budget_rub": int(budget)} if over else {})}
    moved = _moved_brief(b2, ci, co, base_in, base_out)
    out = _adopt(ctx, moved, r2, d2)
    _date_changes(ctx, base_in, base_out, ci, co)
    for p in dropped:
        before = (brief.values().get("prefs") or {}).get(p.split(".", 1)[1])
        ctx.changes.append({"field": p, "before": before, "after": (moved.values().get("prefs") or {}).get(
            p.split(".", 1)[1])})
    status = label if found is not None else "not_found"
    _mark_budget(out, status, budget, _low(out), dropped, no_hostel=_no_hostel(moved))
    ctx.trace["notes"].append(f"budget fit ({reason}): {label} {base_in}->{ci} low {low}->{_low(out)}"
                              + ("" if found is not None else " (closest, still over)"))
    return out


def _no_hostel(brief):
    return NO_HOSTEL in (brief.values().get("constraints") or [])


def _mark_budget(variants, status, budget, low, dropped, no_hostel=False):
    """facts.budget_fit (dates|hotel|dates_and_hotel|not_found), facts.budget_rub, facts.budget_dropped_prefs and a
    `budget_fit` risk note the update card can say («сдвинул на 8–16 декабря — так укладываемся в 180 000 ₽»)."""
    if not status:
        return
    for v in variants:
        v.facts["budget_fit"] = status
        if budget:
            v.facts["budget_rub"] = int(budget)
        if dropped:   # facts are scalars: the dropped comfort prefs as a ready Russian list
            v.facts["budget_dropped_prefs"] = ", ".join(COMFORT_RU[p.split(".", 1)[1]] for p in dropped)
    stay = lambda v: next((st for st in v.steps if st.type == "stay"), None)  # noqa: E731
    for v in variants:
        st = stay(v)
        span = P.fmt_date_range(st.start[:10], st.end[:10]) if st is not None else None
        fits = bool(budget) and v.totals.rub <= budget
        tail = f" — так укладываемся в {P.fmt_rub(budget)}" if fits else ""
        if status == "not_found":
            text = (f"Пробовал другие числа (±{BUDGET_SHIFT_DAYS} дня) и отели попроще"
                    + (" без хостелов" if no_hostel else "") + f" — дешевле {P.fmt_rub(low)} ")
            text += (f"не выходит, это выше бюджета {P.fmt_rub(budget)}" if budget and low > budget
                     else "не нашлось")
        elif status == "other_dates_not_found":
            text = f"Соседние числа (±{BUDGET_SHIFT_DAYS} дня) в этом месяце не собираются — оставил эти даты"
        elif status == "dates":
            text = f"Сдвинул на {span}{tail}" if span else f"Сдвинул даты{tail}"
        elif status == "hotel":
            text = f"Взял отели попроще{tail}"
        else:
            text = f"Сдвинул на {span} и взял отели попроще{tail}"
        if dropped and status != "not_found":
            text += " (без условий: " + ", ".join(COMFORT_RU[p.split('.', 1)[1]] for p in dropped) + ")"
        v.risks = [n for n in v.risks if n.code != "dates_shifted"] if status in ("dates", "dates_and_hotel") else v.risks
        v.risks.insert(0, _risk_note("budget_fit", text, status=status, budget=int(budget) if budget else None,
                                     fits=fits))


OTHER_PLACES_MAX = 3


def offer_other_places(ctx):
    """The budget is still not reached here (after fit_budget), or the user asked for another place next to
    another wish («в другие даты или в другом городе»): the places that fit the budget become
    `choose_destination` actions and the card input key `other_places` (name, «от» price, transport) — from the
    places already shown, re-quoted for the brief's dates and party, else from a fresh destinations search."""
    budget = (ctx.session.brief.values().get("budget_rub") or {}).get("max")
    low = _low(ctx.variants)
    over = bool(budget) and low is not None and low > budget
    if not (over or ctx.ask_other_place):
        return
    t0 = time.perf_counter()
    places = other_places(ctx, budget, keep_over=ctx.ask_other_place)
    ctx.other_places = places
    actions = [{"type": "choose_destination", "label": f"Смотреть {p['name']}",
                "payload": {"destination_id": p["id"]}} for p in places]
    ctx.extra_actions = actions + [a for a in ctx.extra_actions if a not in actions]
    ctx.trace["notes"].append(f"other places ({'over budget' if over else 'asked'}): "
                              f"{[(p['id'], p['price_from_rub']) for p in places]} in "
                              f"{round((time.perf_counter() - t0) * 1000)} ms")


def other_places(ctx, budget, keep_over=False, traits=None, fresh=False, country=None, mode=None):
    """[{id, name, price_from_rub, nights, transport_ru, within_budget, price_includes, road_ru?}]: other places priced
    for this trip's dates and party (the shown ones, else a fresh search), the ones within the budget first by
    price; places above it only when the user asked for another place (the two cheapest). mode plane / train:
    priced only that way (the user fixed the transport)."""
    s = ctx.session
    brief = s.brief
    b = brief.values()
    current = (b.get("destination") or {}).get("id")
    res = ctx.results or {}
    dates = (res["check_in"], res["check_out"]) if res.get("check_in") and res.get("check_out") else None
    train_only, domestic = _dest_prefs(brief)
    train_only = train_only or mode == "train"
    shown = [] if fresh else [i for i in remembered_destinations(s) if i["id"] != current]
    if shown:
        recs = [r for r in (destination_record(ctx, dest_id=i["id"]) for i in shown) if r]
        by_train = {i["id"] for i in shown if i.get("transport") == "train"}
        plane = [] if train_only else [r for r in recs if r["id"] not in by_train]
        trains = recs if train_only else [r for r in recs if r["id"] in by_train]
    else:
        limit = int(ctx.rules.get("max_destinations", 6))
        found = od(ctx, "find_destinations", traits=traits if traits is not None else (b.get("traits") or []),
                   month=_month(brief),
                   origin_city=(b.get("origin") or {}).get("city"), budget=budget,
                   max_travel_hours=(b.get("prefs") or {}).get("max_travel_hours"),
                   segment=(b.get("party") or {}).get("segment"), limit=DEST_ALL)
        recs = [r for r in (norm_destination(x) for x in found or []) if r and r.get("id") and r["id"] != current]
        origin = _origin_place(ctx, brief)
        recs = [r for r in recs if _passes_dest_prefs(r, origin, train_only, domestic, ctx, _trip_nights(brief))]
        if country:   # a place without transport: its country's places are the closest alternatives
            recs.sort(key=lambda r: r.get("country_code") != country)
        plane = [] if train_only else recs[:limit * DEST_BUDGET_POOL_X]
        origin_t = None if train_only or mode == "plane" else _mixed_trains_origin(ctx, brief)
        if origin_t is None and fresh and not train_only and mode != "plane" and origin and origin.get("stations") \
                and origin.get("lat") is not None:
            origin_t = origin   # alternatives for a place without transport are priced by train too
        trains = recs[:limit * DEST_TRAIN_POOL_X] if train_only else (
            _train_pool(origin_t, recs, limit, traits if traits is not None else (b.get("traits") or []), ctx,
                        _trip_nights(brief))
            if origin_t is not None else [])
    p_prices = destination_prices(ctx, brief, plane, train=False, dates_override=dates)
    t_prices = destination_prices(ctx, brief, trains, train=True, dates_override=dates)
    out = []
    for rec in {r["id"]: r for r in plane + trains}.values():
        e = _cheaper_quote(p_prices.get(rec["id"]), t_prices.get(rec["id"]), budget)
        if e is not None and e["facts"].get("long_train") and (
                not long_train_fits_trip(ctx, e["travel_h"], e["nights"])
                or not train_only and rec.get("airports") and (not budget or e["price"] is None or e["price"] > budget)):
            # a day or more on board unasked: only within the budget (or without flights) and for a long enough trip
            e = p_prices.get(rec["id"])
        if e is None or e["price"] is None:
            continue
        f = e["facts"]
        out.append({"id": rec["id"], "name": rec["name"], "price_from_rub": int(e["price"]), "nights": e["nights"],
                    "transport_ru": f.get("transport_ru"), "within_budget": (e["price"] <= budget) if budget else None,
                    "price_includes": f.get("price_includes"), "transport": f.get("transport"),
                    **({"road_ru": f["road_ru"]} if f.get("road_ru") else {})})
    out.sort(key=lambda p: p["price_from_rub"])
    if country:   # alternatives to a place without transport: its country first, then by likeness (the rank)
        rank = {r["id"]: i for i, r in enumerate(dict((r["id"], r) for r in plane + trains).values())}
        cc = {r["id"]: r.get("country_code") for r in plane + trains}
        out.sort(key=lambda p: (cc.get(p["id"]) != country, rank.get(p["id"], 999)))
    fits = [p for p in out if p["within_budget"] is not False]
    picked = fits[:OTHER_PLACES_MAX] or (out[:2] if keep_over else [])
    if picked:
        remember_destinations(s, [{"id": p["id"], "name": p["name"], "price_from_rub": p["price_from_rub"],
                                   "nights": p["nights"], "facts": p} for p in picked])
        from .state import ShownItem
        have = {i.id for i in s.shown.destinations}
        for p in picked:
            if p["id"] not in have:
                s.shown.destinations.append(ShownItem(label=str(len(s.shown.destinations) + 1), kind="destination",
                                                      id=p["id"], title=p["name"]))
    return [{**{k: p[k] for k in ("id", "name", "price_from_rub", "nights", "transport_ru", "within_budget",
                                  "price_includes")}, **({"road_ru": p["road_ru"]} if p.get("road_ru") else {})}
            for p in picked]


def other_destination_actions(ctx):
    """choose_destination actions for the destinations already shown (except the one that did not build)."""
    current = (ctx.session.brief.values().get("destination") or {}).get("id")
    out = []
    for item in ctx.session.shown.destinations:
        if item.id != current and len(out) < 5:
            out.append({"type": "choose_destination", "label": f"Смотреть {item.title}",
                        "payload": {"destination_id": item.id}})
    return out


# ---------------------------------------------------------------- no transport to the place (honest recovery)
# The stay-only answer «Билеты не нашлись» was a dead end and sometimes untrue: the user's direct-only wish had
# removed every flight while connecting ones existed, or no plane flies there while a train does. Code finds the
# reason and the real way out; the card says the ready line (facts.transport_gap_ru) and the buttons do the rest.

TRANSPORT_GAP_KEY = "engine:transport_gap"     # the last gap found ({destination_id, code, line, ...})
TRANSPORT_AUTO_KEY = "engine:transport_auto"   # prefs.transport train set by code for a place without flights
GAP_CODES = ("direct_only", "train_instead", "plane_fixed", "train_fixed", "none", "filtered")


def _route_ru(ctx, brief, res):
    origin = (brief.values().get("origin") or {}).get("city") or "город вылета"
    dest = (res.get("destination") or {}).get("name") or (brief.values().get("destination") or {}).get("name") or "туда"
    return f"{origin} — {dest}"


def _mode_variants(ctx, brief, dest, res, mode):
    """Variants of the same trip with prefs.transport = mode (assumed by code) -> (brief, results, districts, built)."""
    b2 = normalize_brief(ctx, assume(brief, "prefs.transport", mode, "inferred"))
    r2, d2, _ = run_search(ctx, b2, dest, _date(res["check_in"]), _date(res["check_out"]))
    return b2, r2, d2, _built(ctx, b2, r2, d2, pin=True)


def recover_transport(ctx, built_nothing=False):
    """Every variant is stay-only because no ticket passed (risk no_transport) while the trip wants transport — or
    nothing was built at all (built_nothing: tickets-only scope, a direction without a single ticket): find why
    and the real way out —
    - the user's direct-only wish removed every flight and connecting ones exist -> say so with the connecting
      trip's price, a set_chip action prefs.direct_only false («Показать с пересадкой»);
    - no flight (or no train) at all but the other mode exists -> build by it (prefs.transport assumed, a
      `transport_switched` risk «… самолётов нет — собрал поездом»; «билеты» are tickets, so a scope `flights` the
      user did not tie to a plane follows), or when the user fixed the mode, say it with the other mode's price,
      offer it as a set_chip action and up to 3 similar places reachable by the fixed mode;
    - nothing at all -> «… в данных нет ни рейсов, ни поездов», the stays stay what they are, and up to 3 similar
      places that do have transport from the origin become `other_places` + choose_destination actions.
    Variant facts: transport_gap (code), transport_gap_ru (ready line), transport_gap_from_rub, dest_airports.
    With nothing built the gap is kept in ctx.transport_gap and the no_data answer is transport_gap_message."""
    res = ctx.results or {}
    scope = set(res.get("scope") or [])
    vs = ctx.variants
    if not scope & {"flights", "trains"} or not res.get("check_in") or not res.get("destination"):
        return
    fo, fb = res.get("flights_out") or [], res.get("flights_back") or []
    to, tb = res.get("trains_out") or [], res.get("trains_back") or []
    if built_nothing:
        if vs or ("stay" in scope and not res.get("hotels")):
            return
    elif not vs or not all(v.facts.get("no_transport") for v in vs):
        return
    t0 = time.perf_counter()
    s = ctx.session
    brief = s.brief
    prefs = brief.values().get("prefs") or {}
    dest = res.get("destination") or current_destination(ctx)
    rec = destination_record(ctx, dest_id=dest["id"]) or dest
    origin = _origin_place(ctx, brief)
    route = _route_ru(ctx, brief, res)
    fixed = prefs.get("transport") if brief.source_of("prefs.transport") == "user" else None
    flights_searched = "flights" in scope and (prefs.get("transport") or "any") != "train"
    trains_searched = "trains" in scope or prefs.get("transport") == "train"
    gap, actions = None, []
    if prefs.get("direct_only") and brief.source_of("prefs.direct_only") == "user" and fo and fb:
        conn = _built(ctx, assume(brief, "prefs.direct_only", False, "inferred"), res, ctx.districts, pin=True)
        if conn:
            low = _low(conn)
            gap = {"code": "direct_only", "from_rub": low,
                   "line": f"Прямых рейсов по маршруту {route} на эти даты нет; с пересадкой поездка — от {P.fmt_rub(low)}"}
            actions.append({"type": "set_chip", "label": "Показать с пересадкой",
                            "payload": {"field": "prefs.direct_only", "value": False}})
    if gap is None:
        has_flights = bool(fo and fb) if flights_searched else None
        has_trains = bool(to and tb) if trains_searched else None
        other = None
        if not has_flights and not has_trains:
            if fixed != "train" and has_trains is None and train_reachable(origin, rec):
                other = "train"
            elif fixed == "train" and has_flights is None and _json_list(rec.get("airports")):
                other = "plane"
        built = None
        if other is not None:
            built = _mode_variants(ctx, brief, dest, res, other)
            if not built[3]:
                built = None
        word = {"train": "поездом", "plane": "самолётом"}
        if built is not None and fixed is None:
            b2, r2, d2, _ = built
            before_rub = _low(vs)
            ctx.variants = _adopt(ctx, b2, r2, d2)
            if "trains" not in ctx.rebuilt:
                ctx.rebuilt.append("trains")
            store_result(s, CachedResult(key=TRANSPORT_AUTO_KEY, kind="transport_auto", params={},
                                         fetched_at=s.clock(), data={"destination_id": dest["id"], "mode": other}))
            ctx.changes.append({"field": "prefs.transport", "before": prefs.get("transport") or "any", "after": other})
            gap = {"code": "train_instead",
                   "line": f"Самолётов по маршруту {route} в данных нет — собрал поездом"}
            ctx.trace["notes"].append(f"transport: no flights {route}, built by train "
                                      f"({'nothing built' if built_nothing else f'stay-only was {before_rub}'})")
        elif built is not None:
            low = _low(built[3])
            no_word = "самолётов" if fixed == "plane" else "поездов"
            gap = {"code": f"{fixed}_fixed", "from_rub": low, "mode": other,
                   "line": f"{no_word.capitalize()} по маршруту {route} в данных нет; {word[other]} поездка — от {P.fmt_rub(low)}"}
            actions.append({"type": "set_chip", "label": f"Поехать {word[other]}",
                            "payload": {"field": "prefs.transport", "value": other}})
        elif not has_flights and not has_trains:
            line = (f"По маршруту {route} в данных нет ни рейсов, ни поездов" if not (fo or fb or to or tb) else
                    f"По маршруту {route} на эти даты нет билетов в обе стороны")
            gap = {"code": "none", "line": line + ("" if built_nothing else " — показываю только проживание")}
    if gap is None:   # tickets exist both ways, but none passed the user's conditions
        if built_nothing:   # they exist but stack with nothing: no_build_message says what does not fit
            return
        gap = {"code": "filtered", "line": f"По маршруту {route} на эти даты нет билетов под ваши условия — "
                                           f"показываю только проживание"}
    dest_airports = bool(_json_list(rec.get("airports")))
    for v in ctx.variants:
        v.facts.update({"transport_gap": gap["code"], "transport_gap_ru": gap["line"], "dest_airports": dest_airports})
        if gap.get("from_rub") is not None:
            v.facts["transport_gap_from_rub"] = int(gap["from_rub"])
        if v.facts.get("no_transport"):
            v.risks = [n.model_copy(update={"text": gap["line"]}) if n.code == "no_transport" else n for n in v.risks]
        else:
            v.risks.insert(0, _risk_note("transport_switched", gap["line"], mode="train"))
    if gap["code"] in ("none", "plane_fixed", "train_fixed"):
        places = reachable_places(ctx, rec, mode=fixed if gap["code"] != "none" else None)
        ctx.other_places = places
        actions += [{"type": "choose_destination", "label": f"Смотреть {p['name']}",
                     "payload": {"destination_id": p["id"]}} for p in places]
    ctx.transport_gap = dict(gap, destination_id=dest["id"], route=route, dest_airports=dest_airports,
                             trains_in_data=bool(train_reachable(origin, rec)), built=bool(ctx.variants),
                             flights_in_data=not (flights_searched and not (fo or fb)),
                             connecting_ru=connecting_line(gap["code"], route, fo, fb, flights_searched))
    store_result(s, CachedResult(key=TRANSPORT_GAP_KEY, kind="transport_gap", params={}, fetched_at=s.clock(),
                                 data=ctx.transport_gap))
    ctx.extra_actions = actions + [a for a in ctx.extra_actions if a not in actions]
    ctx.trace["notes"].append(f"transport gap {gap['code']}{' (nothing built)' if built_nothing else ''}: "
                              f"{gap['line']} ({round((time.perf_counter() - t0) * 1000)} ms)")


def connecting_line(code, route, fo, fb, flights_searched):
    """«а с пересадкой?» answered from what code searched: the mock's flight search returns connecting flights
    too, so a route without a single flight has none with a connection either (None when that is not the case)."""
    if code == "direct_only" or not flights_searched or (fo and fb):
        return None
    way = "туда" if not fo and fb else "обратно" if fo and not fb else None
    if way:
        return f"Рейсов по маршруту {route} {way} в данных нет — ни прямых, ни с пересадкой"
    return f"Рейсов по маршруту {route} в данных нет — ни прямых, ни с пересадкой"


TRANSPORT_ASK_RE = re.compile(r"пересад|стыков|транзит", re.I)
TRAIN_ASK_RE = re.compile(r"поезд|ржд|ж/?д|по\s+железн", re.I)


def transport_gap_message(ctx):
    """The no_data answer when nothing was built for lack of a road (recover_transport with built_nothing): the
    gap line, the connecting flights code searched, the other mode's price as the button, the similar places —
    and never the same sentence as the agent's last answer."""
    gap = ctx.transport_gap or {}
    text_in = f"{ctx.text or ''} {(ctx.understand or {}).get('info_question') or ''}"
    parts = []
    conn = gap.get("connecting_ru")
    if conn and TRANSPORT_ASK_RE.search(text_in):
        parts.append(f"С пересадкой тоже нет: {conn[:1].lower() + conn[1:]}.")
    elif gap.get("code") in ("plane_fixed", "train_fixed") and conn:
        parts.append(f"{conn}.")
    else:
        parts.append(f"{gap.get('line', '').rstrip('.')}.")
    if gap.get("code") in ("plane_fixed", "train_fixed") and gap.get("from_rub") is not None:
        word = "Поездом" if gap.get("mode") == "train" else "Самолётом"
        parts.append(f"{word} та же поездка — от {P.fmt_rub(gap['from_rub'])}: нажмите «Поехать "
                     f"{word.lower()}», и я соберу её.")
    elif gap.get("code") == "direct_only":
        parts.append("Нажмите «Показать с пересадкой», и я соберу её.")
    places = ctx.other_places or []
    if places:
        parts.append("Похожие места, куда можно добраться: " + ", ".join(
            f"{p['name']}{' поездом' if p.get('transport_ru') == 'поездом' else ''} — от {P.fmt_rub(p['price_from_rub'])}"
            for p in places[:3]) + " — кнопки ниже.")
    elif gap.get("code") == "none":
        parts.append("Можно назвать другое направление или город вылета — соберу заново.")
    text = " ".join(parts)
    agent_said = list(getattr(getattr(ctx.session, "history", None), "agent", None) or [])
    if agent_said and agent_said[-1] == text:   # the same dead end twice: the next step, not the same sentence
        nxt = [a["label"] for a in ctx.extra_actions or [] if a.get("label")][:2]
        core = (conn or gap.get("line") or "").rstrip(".")
        text = (f"Проверил ещё раз — всё так же: {core[:1].lower() + core[1:]}. "
                + (f"Дальше могу так: {' или '.join('«' + x + '»' for x in nxt)} — кнопки ниже."
                   if nxt else "Назовите другое направление или другой город вылета."))
    return text


def reachable_places(ctx, rec, limit=OTHER_PLACES_MAX, mode=None):
    """Up to `limit` places like `rec` (its catalog tags as wishes) that do have transport from the origin, priced
    for this trip's dates and party (a plane quote, or a train one for train-reachable places; mode plane / train:
    only that way — the user fixed it)."""
    traits = [t for t in _json_list(rec.get("tags")) if isinstance(t, str)]
    return other_places(ctx, None, keep_over=True, traits=traits, fresh=True, country=rec.get("country_code"),
                        mode=mode)[:limit]


def reset_auto_transport(ctx):
    """prefs.transport train set by code for a place without flights belongs to that place: another destination
    (or the destinations route) goes back to any transport."""
    s = ctx.session
    rec = s.results.get(TRANSPORT_AUTO_KEY)
    if rec is None or not isinstance(rec.data, dict):
        return
    dest = (s.brief.values().get("destination") or {}).get("id")
    if dest == rec.data.get("destination_id"):
        return
    s.results.pop(TRANSPORT_AUTO_KEY, None)
    if s.brief.source_of("prefs.transport") != "user":
        s.brief = normalize_brief(ctx, assume(s.brief, "prefs.transport", "any", "inferred"))
        ctx.trace["notes"].append("transport: code's train for another place -> any")


def transport_search_fact(ctx):
    """What code searched for the road to the current place («а с пересадкой?», «а поездом?»): the last
    transport gap of this place as a ready line, never «билетов я не смотрел»."""
    rec = ctx.session.results.get(TRANSPORT_GAP_KEY)
    dest = (ctx.brief.values().get("destination") or {}).get("id")
    if rec is None or not isinstance(rec.data, dict) or rec.data.get("destination_id") != dest:
        return None
    d = rec.data
    out = {"kind": "transport_search", "searched": True, "route": d.get("route"), "code": d.get("code"),
           "line": d.get("line"), "from_rub": d.get("from_rub"), "dest_airports": d.get("dest_airports"),
           "trains_in_data": d.get("trains_in_data")}
    if d.get("connecting_ru"):   # no flight at all: none with a connection either (the mock returns those too)
        out["connecting_ru"] = d["connecting_ru"]
    if d.get("mode"):            # the other way the trip builds, one button away («Поехать поездом»)
        out["other_mode"] = d["mode"]
    return out


def road_answer_ru(fact, question):
    """«а с пересадкой?» / «а поездом?» in a state with a transport gap, answered from what code searched (a
    transport_search fact): one or two ready sentences, or None when the question is about something else."""
    if not isinstance(fact, dict) or fact.get("kind") != "transport_search":
        return None
    q = question or ""
    route = fact.get("route") or "туда"
    code, low = fact.get("code"), fact.get("from_rub")
    price = P.fmt_rub(low) if isinstance(low, (int, float)) else None
    if TRANSPORT_ASK_RE.search(q):
        if code == "direct_only" and fact.get("line"):
            return f"С пересадкой есть: {fact['line'].split('; ', 1)[-1]} — нажмите «Показать с пересадкой»."
        conn = fact.get("connecting_ru")
        if conn:
            text = f"С пересадкой тоже нет: {conn[:1].lower() + conn[1:]}."
            if code == "plane_fixed" and price:
                text += f" Поездом та же поездка — от {price}, кнопка «Поехать поездом» ниже."
            elif code == "train_instead":
                text += " Поэтому поездка собрана поездом."
            return text
    if TRAIN_ASK_RE.search(q):
        if code == "train_instead":
            return f"Да, поездом: самолётов по маршруту {route} в данных нет, поэтому поездка уже собрана поездом."
        if fact.get("other_mode") == "train" and price:
            return f"Поездом можно: та же поездка — от {price}, кнопка «Поехать поездом» ниже."
        if not fact.get("trains_in_data"):
            return f"Поездов по маршруту {route} в данных нет."
    return None


def transport_gap_actions(gap):
    """The buttons of a transport gap again (an info answer about it): the other mode / connecting flights."""
    if not isinstance(gap, dict):
        return []
    if gap.get("code") == "direct_only":
        return [{"type": "set_chip", "label": "Показать с пересадкой",
                 "payload": {"field": "prefs.direct_only", "value": False}}]
    mode = gap.get("mode") or gap.get("other_mode")
    if gap.get("code") in ("plane_fixed", "train_fixed") and mode in ("train", "plane"):
        return [{"type": "set_chip", "label": f"Поехать {'поездом' if mode == 'train' else 'самолётом'}",
                 "payload": {"field": "prefs.transport", "value": mode}}]
    return []


def response_offers(ctx, variants, chips):
    """card-input `offers`: what the buttons of this answer really do ([{kind, label}]): add_road (the scope chip
    adds the road to stay-only variants), connecting / train / plane (set_chip actions of a transport gap), place
    (choose_destination). The card promises only these (variants.md / update.md; guard promise_claim)."""
    out = []
    stay_only = bool(variants) and all(not any(st.type in ("flight", "train") for st in v.steps) for v in variants)
    if stay_only and not any(v.facts.get("no_transport") for v in variants) and any(c.field == "scope" for c in chips):
        out.append({"kind": "add_road", "label": "добавить дорогу"})
    for a in ctx.extra_actions or []:
        p = a.get("payload") or {}
        if a.get("type") == "set_chip" and p.get("field") == "prefs.direct_only":
            out.append({"kind": "connecting", "label": a["label"]})
        elif a.get("type") == "set_chip" and p.get("field") == "prefs.transport":
            out.append({"kind": "train" if p.get("value") == "train" else "plane", "label": a["label"]})
        elif a.get("type") == "choose_destination":
            out.append({"kind": "place", "label": a["label"]})
    return out


def no_build_message(ctx, results):
    """What exactly is missing, in plain words (never «нет данных»), plus the way out."""
    s = ctx.session
    v = s.brief.values()
    res = results or ctx.results or {}
    origin = (v.get("origin") or {}).get("city") or "город вылета"
    dest = (res.get("destination") or {}).get("name") or (v.get("destination") or {}).get("name") or "туда"
    ci, co = res.get("check_in"), res.get("check_out")
    when = P.fmt_date_range(ci, co) if ci and co else "эти даты"
    scope = set(res.get("scope") or [])
    trains_only = (v.get("prefs") or {}).get("transport") == "train" or ("trains" in scope and "flights" not in scope)
    word = "поездов" if trains_only else "рейсов"
    outs = (res.get("flights_out") or []) + (res.get("trains_out") or [])
    backs = (res.get("flights_back") or []) + (res.get("trains_back") or [])
    want_transport = bool(scope & {"flights", "trains"})
    if "stay" in scope and not res.get("hotels"):
        reason = f"на {when} в «{dest}» свободных отелей под ваши условия не нашлось"
        if NO_HOSTEL in (v.get("constraints") or []):   # never silently keep a hostel: say what is left
            reason = f"на {when} в «{dest}» свободных отелей без хостелов под ваши условия не нашлось"
    elif want_transport and not outs:
        reason = f"по маршруту {origin} — {dest} на {when} нет {word} туда"
    elif want_transport and not backs:
        reason = f"по маршруту {origin} — {dest} на {when} нет {word} обратно"
    elif "stay" in scope:
        reason = (f"по маршруту {origin} — {dest} на {when} нет {word}, которые стыкуются с отелем: "
                  f"прилёт не попадает в даты заезда или обратный вылет — в даты выезда")
    else:
        reason = f"билеты туда и обратно на {when} не стыкуются между собой"
    text = (f"Собрать поездку не получилось: {reason}. Соседние даты (±{RESCUE_FLEX_DAYS} дня) и на ночь "
            f"короче или длиннее тоже не собираются.")
    others = [a["label"].removeprefix("Смотреть ") for a in ctx.extra_actions]
    if others:
        text += f" Можно посмотреть другие места из подборки — например, {others[0]} — или назвать другие даты."
    else:
        text += " Можно назвать другие даты или другое направление — соберу заново."
    agent_said = list(getattr(getattr(s, "history", None), "agent", None) or [])
    last = agent_said[-1] if agent_said else None
    if last == text:   # the same dead end twice: name the next step, not the same sentence
        text = (f"Всё так же: {reason}. " + (f"Давайте посмотрим {others[0]} — кнопка ниже, или назовите "
                                             f"другой месяц." if others else "Назовите другой месяц или город."))
    return text


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
    """`rerun` for destination items: the shown destinations re-priced for an alternative brief."""
    def rerun(brief2):
        brief2 = normalize_brief(ctx, brief2)
        shown = {i["id"] for i in ctx.destination_items}
        recs = [r for r in ctx.destination_recs if not shown or r["id"] in shown]
        prices = destination_prices(ctx, brief2, recs)
        return [{"id": r["id"], "price_from_rub": prices[r["id"]]["price"]} for r in recs]
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
    destination_times = {v.id: v.facts.get("destination_hours") for v in variants}
    if any(hours is not None for hours in destination_times.values()):
        rows.append({"key": "destination_time", "label": "На месте, включая ночи",
                     "values": {key: P.fmt_hours(hours) if hours is not None else "Нет данных"
                                for key, hours in destination_times.items()},
                     "best_id": best(destination_times)})
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
    private = [v for v in variants if not (v.facts or {}).get("dorm_bed")]
    variants = private or list(variants)   # a bed in a shared room is never the pick when a room exists
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


def _info_variants(ctx):
    """The variants an info question is about: the referenced ones, else the chosen one, else the shown ones."""
    s = ctx.session
    ids = list(ctx.variant_ids) or ([s.chosen_variant_id] if s.chosen_variant_id else []) or \
        [i.id for i in s.shown.variants] or [v.id for v in s.variants]
    return [v for v in (s.variant(i) for i in dict.fromkeys(ids)) if v is not None][:3]


def _hhmm(iso):
    dt = _dt(iso) if iso else None
    return f"{dt:%H:%M}" if dt else None


def _fare_fact(offer):
    """Fare, baggage and stops of a flight offer, with the plain flags the info card reads."""
    fare, bag = offer.get("fare") or {}, offer.get("baggage") or {}
    stops = offer.get("stops")
    segs = offer.get("segments") or []
    return {"direct": None if stops is None else stops == 0, "stops": stops,
            "via": [seg.get("dest_iata") for seg in segs[:-1]] or None,
            "baggage_checked_kg": bag.get("checked_kg"), "baggage_cabin_kg": bag.get("cabin_kg"),
            "refundable": fare.get("refundable"), "fare": fare.get("name"),
            "exchange_allowed": (fare.get("change_fee_rub") is not None) if fare else None,
            "exchange_fee_rub": fare.get("change_fee_rub")}


def _leg_fact(step, offer, pets=False):
    """One transport step as the info card sees it: local times, carrier, number, direct, baggage, fare.
    `pets` (a pet in the brief or a pet question): the ticket's ready pet line `pets_rule` (+ `pet_fee_rub`
    when the party's animals are priced into it)."""
    o = offer or {}
    dep, arr = o.get("dep") or step.start, o.get("arr") or step.end
    out = {"type": step.type, "from": step.from_, "to": step.to, "carrier": o.get("carrier") or step.carrier,
           "dep_date": dep[:10], "dep_time": _hhmm(dep), "arr_date": arr[:10], "arr_time": _hhmm(arr),
           "price_rub": int(step.price.amount)}
    if step.type == "flight":
        out.update(flight_no=o.get("flight_no"), duration_min=o.get("duration_min"),
                   **(_fare_fact(o) if o else {}))
    elif step.type == "train":
        out.update(train_no=o.get("train_no"), car_class=o.get("car_class"), duration_min=o.get("duration_min"),
                   high_speed=True if o.get("train_kind") == "high_speed" else None)
        from .ranking import LONG_LEG_MIN, road_time_ru
        if (o.get("duration_min") or 0) >= LONG_LEG_MIN:   # a long-distance sleeper: the time on board, ready
            out["on_road_ru"] = road_time_ru(o["duration_min"])
    elif step.type == "transfer":
        out.update(kind=o.get("kind"), duration_min=o.get("duration_min"), meeting_point=o.get("meeting_point"))
    if pets and step.type in ("flight", "train"):
        out.update(_pet_fact(step.type, o))
    return {k: v for k, v in out.items() if v is not None}


def _stay_fact(variant, offer):
    """The stay as the info card sees it: dates AND times of check-in / check-out (local), terms, distances."""
    h, o = variant.hotel, offer or {}
    c = o.get("cancellation") or {}
    stay = next((st for st in variant.steps if st.type == "stay"), None)
    ci, co = (stay.start if stay else None) or o.get("check_in"), (stay.end if stay else None) or o.get("check_out")
    out = {"name": h.name, "stars": h.stars, "rating": h.rating, "meals": o.get("meals"),
           "room": (o.get("room") or {}).get("name"),
           "check_in_date": ci[:10] if ci else None, "check_in_time": _hhmm(ci),
           "check_out_date": co[:10] if co else None, "check_out_time": _hhmm(co),
           "cancellation": {"free_until": c.get("free_until"), "penalty_pct": c.get("penalty_pct"),
                            "free": bool(c.get("free_until"))} if c else None,
           "pets_allowed": o.get("pets_allowed"), "early_checkin": o.get("early_checkin"),
           "distance_beach_m": o.get("distance_beach_m"), "distance_center_m": o.get("distance_center_m"),
           # synthetic amenities of the mock: «есть ли кухня?», «можно ли без ступенек?»
           "kitchen": (o.get("room") or {}).get("kitchen") if o.get("room") else None,
           "step_free": (o.get("accessibility") or {}).get("step_free") if o.get("accessibility") else None,
           "lift": (o.get("accessibility") or {}).get("lift") if o.get("accessibility") else None}
    return {k: v for k, v in out.items() if v is not None}


def nights_without_stay(variant):
    """Nights between the arrival and the check-in date (and between check-out and the trip home) that no
    booked stay covers: [{"from": "2026-12-14", "to": "2026-12-15"}] = the night from the 14th to the 15th."""
    idx = next((i for i, st in enumerate(variant.steps) if st.type == "stay"), None)
    if idx is None:
        return []
    stay = variant.steps[idx]
    ci, co = _date(stay.start), _date(stay.end)
    before = [st for st in variant.steps[:idx] if st.type in ("flight", "train")]
    after = [st for st in variant.steps[idx + 1:] if st.type in ("flight", "train")]
    nights = []
    if before and _date(before[-1].end) < ci:
        d = _date(before[-1].end)
        while d < ci:
            nights.append(d)
            d += timedelta(days=1)
    if after and co < _date(after[0].start):
        d = co
        while d < _date(after[0].start):
            nights.append(d)
            d += timedelta(days=1)
    return [{"from": d.isoformat(), "to": (d + timedelta(days=1)).isoformat()} for d in nights]


def _fact_offer(ctx, step, variant=None):
    """The offer behind a variant step for the info facts. Cached search results are capped per session
    (RESULTS_CAP), so after a few rebuilds the offers of the shown variants may be gone (s07: «про
    возвратность нет данных»): then the flight / stay is searched again for exactly that step and matched by
    its deterministic offer id."""
    offer = find_offer(ctx, step.offer_id)
    if offer is not None or not step.offer_id:
        return offer
    b, today = ctx.brief.values(), ctx.session.today().isoformat()
    payload = None
    if step.type == "flight" and step.from_ and step.to and step.start:
        adults, children, infants = _flight_party(ctx.brief, _date(step.start))
        payload = ("search_flights", {"origin_iata": [step.from_], "dest_iata": [step.to], "date": step.start[:10],
                                      "adults": adults, "children": children, "infants": infants, "flex_days": 0,
                                      "today": today, **_pets_payload(ctx.brief)})
    elif step.type == "stay" and variant is not None and variant.hotel and (b.get("destination") or {}).get("id"):
        party = _party_payload(ctx.brief)
        payload = ("search_hotels", {"destination_id": b["destination"]["id"], "check_in": step.start[:10],
                                     "check_out": step.end[:10], "adults": party["adults"],
                                     "children_ages": party["children_ages"], "hotel_id": variant.hotel.hotel_id,
                                     "all_rooms": True, "limit": 40, "today": today})
    if payload is None:
        return None
    found = tool_safe(ctx, *payload)
    offer = next((o for o in (found or {}).get("offers") or [] if o.get("offer_id") == step.offer_id), None)
    meals = ((b.get("prefs") or {}).get("meals"))
    if offer is None and step.type == "stay" and meals not in (None, "any"):
        found = tool_safe(ctx, payload[0], {**payload[1], "filters": {"meals": meals}})
        offer = next((o for o in (found or {}).get("offers") or [] if o.get("offer_id") == step.offer_id), None)
    ctx.trace["notes"].append(f"info: offer {step.type} re-fetched ({'found' if offer else 'not found'})")
    return offer


def variant_fact(ctx, variant):
    """Facts about a shown/chosen variant for present.info: transport legs (local dates and times), the stay
    (check-in / check-out dates and times, terms) and the nights no booked stay covers."""
    pets = pet_context(ctx)
    legs = [_leg_fact(st, _fact_offer(ctx, st), pets) for st in variant.steps if st.type in ("flight", "train", "transfer")]
    stay_step = next((st for st in variant.steps if st.type == "stay"), None)
    stay_offer = (_fact_offer(ctx, stay_step, variant) if stay_step is not None and stay_step.offer_id
                  else find_offer(ctx, variant.hotel.offer_id)) if variant.hotel else None
    stay = _stay_fact(variant, stay_offer) if variant.hotel else None
    out = {"kind": "variant", "id": variant.id, "title": variant.title or P.VARIANT_TITLES.get(variant.id, variant.id),
           "total_rub": variant.totals.rub, "legs": legs, "stay": stay}
    if variant.facts.get("price_pets_rub"):   # the animals' fee, already inside total_rub
        out["price_pets_rub"] = variant.facts["price_pets_rub"]
    gaps = nights_without_stay(variant) if variant.hotel else []
    if gaps:
        out["nights_without_stay"] = gaps
    return out


def cancel_quote(order, now):
    """What cancelling the booked order would return by its terms — ready numbers for the cards.

    Hotel: free until `free_until`, else the penalty (penalty_pct of the price, rounded like the mock);
    flights: refundable fares come back, the rest is lost; items without stated terms go to other_rub.
    """
    q = {"hotel_refund_rub": 0, "hotel_penalty_rub": 0, "tickets_refund_rub": 0, "tickets_lost_rub": 0,
         "other_rub": 0}
    for i in order.get("items") or []:
        if i.get("status") == "cancelled":
            continue
        price = int(i.get("price_rub") or 0)
        offer = (i.get("details") or {}).get("offer") or {}
        if i.get("kind") == "hotel" and offer.get("cancellation"):
            c = offer["cancellation"]
            free = bool(c.get("free_until")) and now <= _dt(c["free_until"])
            pct = 100 if c.get("penalty_pct") is None else int(c["penalty_pct"])
            penalty = 0 if free else int(round(price * pct / 100 / 10.0)) * 10
            q["hotel_penalty_rub"] += min(penalty, price)
            q["hotel_refund_rub"] += price - min(penalty, price)
        elif i.get("kind") == "flight" and "refundable" in (offer.get("fare") or {}):
            q["tickets_refund_rub" if offer["fare"]["refundable"] else "tickets_lost_rub"] += price
        else:
            q["other_rub"] += price
    q["refund_rub"] = q["hotel_refund_rub"] + q["tickets_refund_rub"]
    q["lost_rub"] = q["hotel_penalty_rub"] + q["tickets_lost_rub"]
    return q


def order_terms_fact(order, pets=False):
    """Fare / cancellation terms of the booked items (the order summary has titles and dates only); with
    `pets` the tickets' ready pet lines (`pets_rule`, `pet_fee_rub` when the animals are in the price)."""
    terms = []
    for i in order.get("items") or []:
        offer = (i.get("details") or {}).get("offer") or {}
        pet = _pet_fact(i.get("kind"), offer) if pets and i.get("kind") in ("flight", "train") else {}
        if i.get("kind") == "flight" and offer:
            terms.append({"item_id": i["item_id"], "title": i.get("title"), **_fare_fact(offer), **pet})
        elif pet:
            terms.append({"item_id": i["item_id"], "title": i.get("title"), **pet})
        elif i.get("kind") == "hotel" and offer.get("cancellation"):
            c = offer["cancellation"]
            terms.append({"item_id": i["item_id"], "title": i.get("title"),
                          "cancellation": {"free_until": c.get("free_until"), "penalty_pct": c.get("penalty_pct")}})
    return terms


# provenance and bulk the info card must not talk about or pay tokens for (voice: never name sources)
INFO_DROP = {"source", "fetched_at", "synthetic", "synthetic_fields", "photos", "lat", "lon", "osm_id"}
# a question about where the hotel is: the distances to the centre of the variants' hotels go with the facts
LOCATION_Q_RE = re.compile(r"центр|далек|далеко|близк|рядом|пешком|достопримечат|старый город|старом город", re.I)


def step_info_lookup(ctx):
    """Facts only from the shown variants, get_hotel / climate / district data and the order; empty when nothing."""
    ctx.info_question = ((ctx.understand or {}).get("info_question") or ctx.text or "").strip()
    facts = [variant_fact(ctx, v) for v in _info_variants(ctx)]
    v = _info_variant(ctx)
    hotel_ids = list(ctx.hotel_refs)
    if v is not None and v.hotel is not None and v.hotel.hotel_id not in hotel_ids:
        hotel_ids.append(v.hotel.hotel_id)
    for hid in hotel_ids[:2]:
        details = tool_safe(ctx, "get_hotel", {"hotel_id": hid})
        if details:
            facts.append({"kind": "hotel", "id": hid, "data": {k: v for k, v in details.items() if k not in INFO_DROP}})
            prof = od(ctx, "district_profile", details.get("osm_id")) if details.get("osm_id") else None
            if prof is not None:
                facts.append({"kind": "district", "id": details["osm_id"], "data": as_dict(prof)})
    dest = ctx.brief.values().get("destination") or {}
    month = _month(ctx.brief)
    if dest.get("id") and month:
        clim = od(ctx, "climate", dest["id"], month)
        if clim is not None:
            facts.append({"kind": "climate", "id": dest["id"], "month": month,
                          "data": {k: v for k, v in as_dict(clim).items() if k not in INFO_DROP}})
    if ctx.session.order_id:
        order = tool_safe(ctx, "order_get", {"order_id": ctx.session.order_id}, cache=False)
        if order:
            fact = {"kind": "order", "id": order["order_id"],
                    "data": order_summary(order, checkout_issues(ctx.session, order["order_id"]), ctx.brief)}
            terms = order_terms_fact(order, pets=pet_context(ctx))
            if terms:
                fact["terms"] = terms
            if order.get("status") in ("paid", "changed"):
                fact["if_cancelled"] = cancel_quote(order, ctx.session.clock())
            facts.append(fact)
    elif ctx.session.cart_id and pet_context(ctx):   # «где в корзине собака?»: the lines that hold the pet fee
        cart = tool_safe(ctx, "cart_get", {"session_id": ctx.session.id}, cache=False)
        if cart and cart.get("status") == "open" and cart.get("items"):
            facts.append({"kind": "cart", "id": cart["cart_id"], "total_rub": int(cart.get("total_rub") or 0),
                          "items": [{**{k: i.get(k) for k in ("item_id", "kind", "title", "price_rub")},
                                     **(pet_fee_note(i, ctx.brief) or {})} for i in cart["items"]]})
    if best_time_cue(ctx.rules, ctx.info_question) or best_time_cue(ctx.rules, ctx.text):
        bt = best_time_fact(ctx)
        if bt is not None:
            facts.append(bt)
    nd = near_distance_fact(ctx, _info_variants(ctx), [ctx.info_question, ctx.text])
    asked_where = LOCATION_Q_RE.search(ctx.info_question or "") or (nd is not None and not nd["found"])
    cd = center_distance_fact(_info_variants(ctx)) if asked_where else None
    if cd is not None:
        facts.append(cd)
    if nd is not None:
        facts.append(nd)
    if pet_context(ctx):
        lines = ((ctx.rules or {}).get("pets") or {}).get("general_ru") or []
        if lines:
            facts.append({"kind": "pet_rules", "lines": list(lines)})
    warnings = open_warnings(ctx)
    if warnings:
        facts.append({"kind": "warnings", "items": warnings})
    dest_fact = destinations_fact(ctx)
    if dest_fact is not None:
        facts.append(dest_fact)
    road = transport_search_fact(ctx)
    if road is not None:
        facts.append(road)
        if not ctx.session.cart_id and not ctx.session.order_id:
            acts = transport_gap_actions(road)
            ctx.extra_actions = acts + [a for a in ctx.extra_actions if a not in acts]
    if dest.get("id") and re.search(r"аэропорт", f"{ctx.info_question} {ctx.text or ''}", re.I):
        rec = destination_record(ctx, dest_id=dest["id"]) or {}
        airports = [a for a in _json_list(rec.get("airports")) if a]
        facts.append({"kind": "place", "id": dest["id"], "name": rec.get("name") or dest.get("name"),
                      "airports": airports, "dest_airports": bool(airports)})   # «нет аэропорта» only when empty
    ctx.info_facts = facts


def destinations_fact(ctx):
    """At the inspiration stage (places shown, no variants yet) the shown places with their «от» price, transport
    and what that price includes (facts.price_includes, made by code from the quote) — «что входит в цену?»."""
    s = ctx.session
    if s.stage != "inspiration" or s.variants:
        return None
    items = remembered_destinations(s)
    if not items:
        return None
    budget = (s.brief.values().get("budget_rub") or {}).get("max")
    return {"kind": "destinations", "budget_rub": int(budget) if budget else None,
            "items": [{k: i.get(k) for k in ("id", "name", "price_from_rub", "nights", "transport_ru",
                                             "price_includes", "within_budget")} for i in items[:6]]}


def pet_context(ctx):
    """Pet lines go into the info facts: a pet in the brief, or a pet word in the question (rules.json
    pets.question_cues: «собак», «корги», «кошк»…)."""
    from .checks import wants_pet
    if wants_pet(ctx.brief):
        return True
    text = f" {ctx.info_question or ''} {ctx.text or ''} ".casefold()
    return _cue_in(text, ((ctx.rules or {}).get("pets") or {}).get("question_cues"))


def _pet_word(brief, count):
    """«кот» / «собака» / «животное» (the brief's animal), «2 животных» for more than one."""
    if count > 1:
        return f"{count} {P.plural(count, 'животное', 'животных', 'животных')}"
    v = brief.values() if brief is not None else {}
    words = [str(p).casefold() for p in ((v.get("party") or {}).get("pets") or [])]
    words += [str(c).casefold()[4:] for c in (v.get("constraints") or []) if str(c).startswith("pet:")]
    w = words[0] if words else ""
    if re.search(r"cat|кош|кот", w):
        return "кот"
    if re.search(r"dog|соб|пёс|пес|щен", w):
        return "собака"
    return "животное"


def pet_fee_note(item, brief=None):
    """A flight / train cart or order item whose offer has animals priced in (the mock's `pets` terms, count > 0)
    -> {"pet_fee_rub", "includes_ru": «в т.ч. кот в салоне — 2 390 ₽»}, else None. The fee is inside the item's
    price_rub (cart and order totals stay the sum of the lines); this only makes it visible on its line."""
    if (item or {}).get("kind") not in ("flight", "train"):
        return None
    terms = (((item.get("details") or {}).get("offer") or {}).get("pets")) or {}
    fee, count = int(terms.get("fee_total_rub") or 0), int(terms.get("count") or 0)
    if fee <= 0 or count <= 0:
        return None
    where = "в салоне" if item["kind"] == "flight" else "в переноске"
    return {"pet_fee_rub": fee, "includes_ru": f"в т.ч. {_pet_word(brief, count)} {where} — {P.fmt_rub(fee)}"}


def pet_fee_sentence(items, brief=None):
    """One sentence naming the cart / order lines that hold the pet fee, or "" (no animals priced in)."""
    lines = []
    for i in items or []:
        note = pet_fee_note(i, brief)
        if note:
            lines.append(f"«{i['title']}» — {note['includes_ru']}")
    if not lines:
        return ""
    return "Провоз питомца уже в цене билетов: " + "; ".join(lines[:2]) + "."


def _pet_fact(kind, offer):
    """{pets_rule, pet_fee_rub} of one ticket offer (empty without pet terms)."""
    from .ranking import pet_rule_line
    terms = (offer or {}).get("pets") or {}
    line = pet_rule_line(kind, offer) if terms else None
    if not line:
        return {}
    out = {"pets_rule": line}
    if terms.get("count"):
        out["pet_fee_rub"] = int(terms.get("fee_total_rub") or 0)
    return out


def open_warnings(ctx):
    """Open warnings / blockers of the cart (cart stage) and of the current order: the info card never
    contradicts them (s20: «ночь без отеля» in the cart vs «да, ночь входит»)."""
    s = ctx.session
    issues = []
    if s.cart_id and s.stage == "cart":
        cart = tool_safe(ctx, "cart_get", {"session_id": s.id}, cache=False)
        issues += list((cart or {}).get("issues") or [])
    if s.order_id and s.stage in ("checkout", "ordered", "aftercare"):
        order = tool_safe(ctx, "order_get", {"order_id": s.order_id}, cache=False)
        issues += list((order or {}).get("issues") or [])
    issues += [i for i in ctx.order_issues or []]
    out = []
    for i in issues:
        sev = str(i.get("severity") or "").lower()
        msg = str(i.get("message") or "").strip()
        if sev in ("warning", "warn", "blocking", "block") and msg and msg not in out:
            out.append(msg)
    return out


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
    pets = pet_fee_sentence(cart.get("items"), s.brief)
    if pets:   # the fee is inside the ticket lines: name the line, so the sum of the lines is the total
        msg += " " + pets
    problems = attention(cart.get("issues"))
    msg += (" Есть что проверить: " + problems) if problems else " Дальше — данные путешественников."
    ctx.message = msg


def _age_on(birth, day):
    b = _date(birth)
    return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


# ---------------------------------------------------------------- checkout re-price for the travellers

PEOPLE_RU = {1: "одного", 2: "двоих", 3: "троих", 4: "четверых", 5: "пятерых", 6: "шестерых", 7: "семерых"}
TRANSPORT_STEPS = ("flight", "train")


def _item_size(item):
    """Head count an item was priced for (the mock's cart item details.party), or None."""
    p = (item.get("details") or {}).get("party") or {}
    if item.get("kind") == "flight" and "adults" in p:
        return int(p.get("adults") or 0) + int(p.get("children") or 0) + int(p.get("infants") or 0)
    if item.get("kind") == "train" and "passengers" in p:
        return int(p["passengers"])
    if item.get("kind") == "hotel" and "adults" in p:
        return int(p["adults"]) + len(p.get("children_ages") or [])
    return None


def _variant_day(variant):
    starts = [d for d in (_date(st.start) for st in variant.steps if st.start) if d is not None]
    return min(starts) if starts else None


def _variant_dates(variant, items):
    """(check_in, check_out) the variant was searched on: the hotel item's dates, else the legs' days."""
    hotel = next((i for i in items if i.get("kind") == "hotel"), None)
    params = ((hotel or {}).get("details") or {}).get("params") or {}
    if params.get("ci") and params.get("co"):
        return _date(params["ci"]), _date(params["co"])
    stay = next((st for st in variant.steps if st.type == "stay"), None)
    legs = [st for st in variant.steps if st.type in TRANSPORT_STEPS]
    if stay is not None:
        return _date(stay.start), _date(stay.end)
    if len(legs) >= 2:
        return _date(legs[0].start), _date(legs[-1].start)
    return None, None


def _same_leg(offer, step):
    return (_dt(offer.get("dep")) == _dt(step.start) and _dt(offer.get("arr")) == _dt(step.end)
            and (offer.get("carrier") or None) == (step.carrier or None))


def _pin_to_variant(results, variant):
    """Keep the old variant's own legs and room kind in fresh results (when they are still on sale)."""
    out = dict(results)
    legs = [st for st in variant.steps if st.type in TRANSPORT_STEPS]
    for direction, step in (("out", legs[0] if legs else None), ("back", legs[-1] if len(legs) >= 2 else None)):
        if step is None:
            continue
        found = {k: [o for o in results.get(f"{k}_{direction}") or [] if _same_leg(o, step)]
                 for k in ("flights", "trains")}
        if found["flights"] or found["trains"]:
            out[f"flights_{direction}"], out[f"trains_{direction}"] = found["flights"][:1], found["trains"][:1]
    if variant.hotel is not None and out.get("hotels"):
        from .ranking import _is_dorm
        was_dorm = bool(variant.facts.get("dorm_bed"))
        meals, free = variant.facts.get("meals"), variant.facts.get("free_cancellation")
        rooms = [h for h in out["hotels"] if h.get("hotel_id") == variant.hotel.hotel_id
                 and _is_dorm(h.get("room") or {}) == was_dorm] or [
                h for h in out["hotels"] if h.get("hotel_id") == variant.hotel.hotel_id]
        if rooms:
            def same_terms(h):
                canc = h.get("cancellation") or {}
                return (h.get("meals") != meals, bool(canc.get("free_until")) != bool(free),
                        h.get("price_total_rub") or 0, h.get("offer_id") or "")
            out["hotels"] = [min(rooms, key=same_terms)]
    return out


def _brief_for_party(ctx, brief, target):
    """The travellers' party into the brief (the user's own data: source user); an unsaid segment follows."""
    kids = target["children_ages"] or []
    patch = {"party.adults": target["adults"], "party.children": len(kids)}
    if kids:
        patch["party.children_ages"] = kids
    new, _ = apply_patch(brief, {"set": patch, "unset": []})
    if new.source_of("party.segment") != "user":
        new = assume(new, "party.segment", target["segment"], "inferred")
    return normalize_brief(ctx, new)


def _who_ru(target):
    n = target["adults"] + target["children"]
    who = f"на {PEOPLE_RU[n]}" if n in PEOPLE_RU else f"на {n} человек"
    return who + (f" (детей: {target['children']})" if target["children"] else "")


def reprice_for_travellers(ctx, travellers):
    """The travellers are another party than the cart was priced for (e.g. one traveller for a variant
    priced for the assumed two adults): rebuild the same variant (the same hotel and legs while they are
    on sale, else the same kind) for the travellers, refresh the other variants and replace the cart.
    -> {"old_rub", "new_rub", "who"} or None (nothing to do, or the rebuild failed: the honest blocker stays)."""
    s = ctx.session
    variant = s.variant(s.chosen_variant_id) if s.chosen_variant_id else None
    if variant is None or not s.cart_id:
        return None
    day = _variant_day(variant) or _date((s.brief.values().get("dates") or {}).get("start")) or s.today()
    try:
        ages = [_age_on(t["birth_date"], day) for t in travellers]
    except (KeyError, TypeError, ValueError):
        return None
    target = party_of_ages(ages)
    if target["adults"] < 1 or any(a < 0 for a in ages):
        return None   # the mock's own blocker says why
    cart = tool_safe(ctx, "cart_get", {"session_id": s.id}, cache=False) or {}
    items = [i for i in cart.get("items") or [] if i.get("kind") != "transfer"]
    sizes = {n for n in (_item_size(i) for i in items) if n is not None}
    party = s.brief.values().get("party") or {}
    same_split = (int(party.get("adults") or 0), int(party.get("children") or 0)) == (target["adults"],
                                                                                    target["children"])
    if sizes <= {len(ages)} and same_split:
        return None
    before_brief, before_variants = s.brief, list(s.variants)
    old_rub = cart.get("total_rub") or variant.totals.rub
    try:
        dest = destination_record(ctx, dest_id=variant.destination_id or (
            (s.brief.values().get("destination") or {}).get("id")))
        check_in, check_out = _variant_dates(variant, cart.get("items") or [])
        if dest is None or check_in is None or check_out is None:
            raise StepError("no_data", "no destination or dates for the re-price")
        s.brief = _brief_for_party(ctx, s.brief, target)
        hotel_id = variant.hotel.hotel_id if variant.hotel is not None else None
        results, districts, _ = _search_dates(ctx, s.brief, dest, check_in, check_out, hotel_id=hotel_id)
        built = build(ctx, s.brief, _pin_to_variant(results, variant), districts) if (
            results.get("hotels") or hotel_id is None) else []
        fresh, fdist, _ = _search_dates(ctx, s.brief, dest, check_in, check_out)
        others = build(ctx, s.brief, fresh, fdist)
        if not built:   # the hotel is gone for this party: the same kind of variant from a fresh search
            built = [v for v in others if v.id == variant.id]
            ctx.trace["notes"].append("reprice: the variant's hotel has no room for the travellers, same kind rebuilt")
        if not built:
            raise StepError("no_data", "the variant does not build for the travellers")
        new = next((v for v in built if v.id == variant.id), built[0]).model_copy(
            update={"id": variant.id, "title": variant.title})
        s.variants = [new] + [v for v in others if v.id != variant.id]
        ctx.variant_ids, ctx.extras = [variant.id], []
        step_cart_add(ctx)
    except (StepError, ToolError, PatchError) as exc:
        ctx.trace["notes"].append(f"reprice for travellers failed, the blocker stays: {exc}")
        s.brief, s.variants = before_brief, before_variants
        return None
    ctx.variants = list(s.variants)
    ctx.trace["notes"].append(f"reprice: party {target} -> {variant.id} {old_rub} -> {ctx.cart.get('total_rub')}")
    return {"old_rub": old_rub, "new_rub": ctx.cart.get("total_rub"), "who": _who_ru(target)}


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
    if s.stage not in ("cart", "checkout"):
        ctx.fail("bad_request", "Выберите вариант из новой подборки - затем оформим поездку.")
    payload = ctx.checkout_payload
    if not payload or not payload.get("travellers"):
        ctx.message = "Чтобы оформить, нужны имена и даты рождения всех путешественников и контакт для билетов."
        ctx.__dict__["need_travellers"] = True
        return
    # travellers of another party than the cart was priced for: the same variant re-priced in this turn
    repriced = reprice_for_travellers(ctx, payload["travellers"])
    out = tool(ctx, "checkout", {"cart_id": s.cart_id, "travellers": payload["travellers"],
                                 "contact": payload.get("contact"), "now": now_iso(ctx)}, cache=False)
    s.order_id = out["order_id"]
    s.stage = "checkout"
    hook_issues = ctx.engine.hooks.run("after_travellers", ctx, kinds=("validate",))
    ctx.order = tool(ctx, "order_get", {"order_id": out["order_id"]}, cache=False)
    ctx.order_issues = list(hook_issues)
    save_checkout_issues(ctx, out["order_id"], hook_issues)
    ctx.message = checkout_text(ctx.order, hook_issues, brief=s.brief)
    if repriced:
        ctx.message = (f"Пересчитал {repriced['who']}: {P.fmt_rub(ctx.order.get('total_rub') or 0)} вместо "
                       f"{P.fmt_rub(repriced['old_rub'])}. " + ctx.message)


# ---------------------------------------------------------------- order at checkout (pay or a way forward)

CHECKOUT_ISSUES_KIND = "checkout_issues"
BLOCK_SEVERITIES = ("block", "blocking")


def pay_label(total_rub) -> str:
    """The pay button's label (respond.build_actions) — also named in texts and info facts."""
    return f"Оплатить {P.fmt_rub(total_rub or 0)}"


def _checkout_issues_key(order_id):
    return f"{CHECKOUT_ISSUES_KIND}:{order_id}"


def save_checkout_issues(ctx, order_id, issues):
    """Keep the after_travellers issues of an order for the later turns of the checkout stage (an info
    question must not lose a blocker, nor invent one)."""
    data = {"order_id": order_id, "issues": [as_issue(i) for i in issues or []]}
    with ctx.lock:
        store_result(ctx.session, CachedResult(key=_checkout_issues_key(order_id), kind=CHECKOUT_ISSUES_KIND,
                                               params={"order_id": order_id}, fetched_at=ctx.session.clock(),
                                               data=json.loads(json.dumps(data, ensure_ascii=False, default=str))))


def checkout_issues(session, order_id) -> list:
    rec = session.results.get(_checkout_issues_key(order_id)) if order_id else None
    data = rec.data if rec is not None and isinstance(rec.data, dict) else {}
    return list(data.get("issues") or [])


def order_blockers(order, extra_issues=()) -> list:
    return [i for i in list((order or {}).get("issues") or []) + list(extra_issues or [])
            if (i.get("severity") or "").lower() in BLOCK_SEVERITIES]


def pending_order(ctx):
    """The session's order while it waits for payment in the checkout stage, for turns that did not load it
    (an info question, small talk, an error): every checkout response carries the order and its pay button.
    -> (order, extra_issues) or (None, [])."""
    s = ctx.session
    if s.stage != "checkout" or not s.order_id:
        return None, []
    order = tool_safe(ctx, "order_get", {"order_id": s.order_id}, cache=False)
    if not order:
        return None, []
    return order, checkout_issues(s, order["order_id"])


def checkout_text(order, extra_issues=(), brief=None) -> str:
    total = order.get("total_rub") or 0
    issues = list(order.get("issues") or []) + list(extra_issues or [])
    if order_blockers(order, extra_issues):
        problems = attention(order_blockers(order, extra_issues))
        return (f"Заказ собран на {P.fmt_rub(total)}, но оплатить его пока нельзя: {problems} "
                "Выберите другой вариант кнопкой ниже или напишите, что поменять, — пересоберу.")
    msg = f"Заказ собран, осталось оплатить {P.fmt_rub(total)} — кнопка «{pay_label(total)}»."
    pets = pet_fee_sentence(order.get("items"), brief)
    if pets:
        msg += " " + pets
    problems = attention(issues)
    if problems:
        msg += " Обратите внимание: " + problems
    return msg


def payment_fact(order, extra_issues=()) -> dict | None:
    """What the user can do about paying (for card facts): the pay button's label, or why there is none."""
    if (order or {}).get("status") != "pending_payment":
        return None
    blockers = order_blockers(order, extra_issues)
    if blockers:
        return {"pay_button": None, "can_pay": False,
                "blocked_by": [str(i.get("message") or i.get("code")) for i in blockers][:2],
                "next_step": "выбрать другой вариант кнопкой «Беру …» или написать, что поменять"}
    label = pay_label(order.get("total_rub"))
    return {"pay_button": label, "can_pay": True,
            "next_step": f"нажать кнопку «{label}» — оплата картой (демо) сразу подтверждает заказ"}


def order_summary(order, extra_issues=(), brief=None):
    out = {"order_id": order["order_id"], "status": order["status"], "total_rub": order["total_rub"],
           "items": [{**{k: i.get(k) for k in ("item_id", "kind", "title", "price_rub", "booking_number", "status",
                                               "start", "end")}, **(pet_fee_note(i, brief) or {})}
                     for i in order.get("items") or []]}
    pay = payment_fact(order, extra_issues)
    if pay is not None:
        out["payment"] = pay
    return out


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
    if s.stage not in ("checkout", "ordered", "aftercare"):
        ctx.fail("bad_request", "Подборка изменилась - выберите вариант и оформите его перед оплатой.")
    blockers = [i for i in checkout_issues(s, order_id) if (i.get("severity") or "").lower() in BLOCK_SEVERITIES]
    if blockers:
        ctx.fail("bad_request", f"Оплатить этот заказ пока нельзя: {attention(blockers)} "
                                "Выберите другой вариант или напишите, что поменять, — пересоберу.")
    try:
        tool(ctx, "pay", {"order_id": order_id, "method": "mock_card", "now": now_iso(ctx)}, cache=False)
    except ToolError as exc:
        kind = next((k for k in ("blocking_issues", "order_cancelled", "order_not_found")
                     if k == exc.code or str(exc.message).startswith(k)), None)
        if kind is None:
            raise
        order = tool_safe(ctx, "order_get", {"order_id": order_id}, cache=False) or {}
        problems = attention(order_blockers(order)) if kind == "blocking_issues" else ""
        ctx.fail("bad_request", ("Оплатить этот заказ пока нельзя: " + problems + " Выберите другой вариант или "
                                 "напишите, что поменять, — пересоберу.") if problems else
                 "Этот заказ уже не оплатить — выберите вариант заново, соберу новый заказ.")
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


ATTENTION_MAX = 2
ATTENTION_RANK = {"block": 0, "blocking": 0, "warn": 1, "warning": 1}


def attention(issues, limit=ATTENTION_MAX) -> str:
    """Cart/order issues worth a sentence: at most `limit`, blockers first, one per code ("info" notes stay
    in the cart card). Several missing transfers are one note."""
    picked = []
    for i in issues or []:
        sev = (i.get("severity") or "").lower()
        if sev not in ATTENTION_RANK or not i.get("message"):
            continue
        picked.append((ATTENTION_RANK[sev], i))
    picked.sort(key=lambda p: p[0])   # stable: the producer's order within a severity
    by_code = {}
    for _, i in picked:
        by_code.setdefault(i.get("code") or i["message"], []).append(i)
    texts = []
    for code, group in by_code.items():
        if code == "no_transfer" and len(group) > 1:
            t = "Трансферов до отеля и обратно нет — добираться самим"
        else:
            t = group[0]["message"]
        t = t.strip().rstrip(".")
        t = t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t
        if t not in texts:
            texts.append(t)
        if len(texts) >= limit:
            break
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
        result = {**result, "requested_time": time_s, **_stay_arrival_facts(order, arr)}
    elif kind == "change_dates":
        new = _change_dates_target(ctx, params, order)
        problem = _new_dates_problem(ctx, new)
        if problem:
            ctx.present = None
            ctx.order = order
            ctx.message = problem
            return
        ctx.order = order
        if not _dates_changeable(ctx):
            _unmovable(ctx, order, new)       # a fare without exchange: the same trip anew, or the numbers
            return
        confirm = bool(params.get("confirm"))
        result = tool(ctx, "order_change_dates", {"order_id": s.order_id, "new_dates": new, "confirm": confirm,
                                                  "now": now_iso(ctx)}, cache=False)
        if not result.get("available") and any(i.get("code") == "flight_change_not_allowed"
                                                for i in result.get("issues") or []):
            _unmovable(ctx, order, new)
            return
        result = {**result, "new_dates": new, "moved": _moved_view(result.get("moved"))}
        result.pop("order", None)           # the card has the order summary; the raw order is noise (s16)
        store_result(s, CachedResult(key=CHANGE_QUOTE_KEY, kind="order_change_dates", params={},
                                     fetched_at=s.clock(),
                                     data={"new_dates": new} if result.get("available") and not result.get("applied")
                                     else {}))
    elif kind == "add_transfer":
        offer_id = params.get("offer_id")
        if offer_id:
            result = order_summary(tool(ctx, "order_add_transfer", {"order_id": s.order_id, "offer_id": offer_id,
                                                                    "now": now_iso(ctx)}, cache=False))
        else:
            result = _add_transfers(ctx, order)   # every leg without a fitting transfer, timed to it
            if result is None:
                return
    else:
        ctx.present = None
        ctx.message = "Что сделать с заказом: ранний заезд, перенос дат или трансфер?"
        return
    ctx.order = tool(ctx, "order_get", {"order_id": s.order_id}, cache=False)
    ctx.aftercare_result = {"kind": kind, **result}
    s.stage = "aftercare"


CHANGE_QUOTE_KEY = "aftercare:change_dates"


def _moved_view(moved):
    """The mock's `moved` rows for the aftercare card: ready local date + time of every moved item (a transfer
    re-timed to its new flight is said in the same confirmation — s16)."""
    out = []
    for m in moved or []:
        new = _dt(m.get("new_start")) if m.get("new_start") else None
        row = {"kind": m.get("kind"), "title": m.get("title"), "way": P.transfer_way(m.get("title")),
               "new_start": m.get("new_start"),
               "new_date": new.date().isoformat() if new else None, "new_time": f"{new:%H:%M}" if new else None,
               "old_time": _hhmm(m.get("old_start")), "new_price_rub": m.get("new_price_rub")}
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def _stay_arrival_facts(order, at_hotel):
    """Ready facts for the early check-in card (s15: the model cited the landing time, the regular check-in and
    the hotel's name, none of which the input had in plain form): hotel name, regular check-in time, the
    arrival flight / train landing time and the time they reach the hotel."""
    items = [i for i in order.get("items") or [] if i.get("status") != "cancelled"]
    hotel = next((i for i in items if i.get("kind") == "hotel"), None)
    out = {}
    if hotel is not None:
        name = (((hotel.get("details") or {}).get("offer") or {}).get("name")
                or str(hotel.get("title") or "").split(":")[0].strip() or None)
        if name:
            out["hotel"] = name
        if hotel.get("start"):
            out["check_in_date"] = hotel["start"][:10]
            out["check_in_from"] = _hhmm(hotel["start"])
        ci = _date(hotel["start"]) if hotel.get("start") else None
        legs = [i for i in items if i.get("kind") in ("flight", "train") and i.get("end")
                and ci is not None and _date(i["start"]) <= ci]
        if legs:
            out["lands_at"] = _hhmm(max(legs, key=lambda i: _dt(i["end"]))["end"])
    if at_hotel is not None:
        out["at_hotel_at"] = f"{at_hotel:%H:%M}"
    return out


def _stay_pair(start, end):
    s, e = _iso_date_or_none(start), _iso_date_or_none(end)
    return (s, e) if s else None


def _iso_date_or_none(value):
    try:
        return _date(value)
    except (TypeError, ValueError):
        return None


def _change_dates_target(ctx, params, order):
    """New stay dates {check_in, check_out} for change_dates, or None when the user gave none.

    Read in order: params.new_dates {check_in, check_out} (or {start, end}); params check_in/check_out or
    start/end; the understand patch dates.start/dates.end; params.shift_days against the booked stay; a
    bare confirmation reuses the dates quoted last. A start alone keeps the booked number of nights.
    """
    hotel = next((i for i in order.get("items") or [] if i["kind"] == "hotel"), None)
    booked = (_date(hotel["start"]), _date(hotel["end"])) if hotel and hotel.get("start") and hotel.get("end") else None
    nd = params.get("new_dates") if isinstance(params.get("new_dates"), dict) else {}
    sets = (ctx.patch or {}).get("set") or {}
    pair = None
    for src in (nd, params):
        pair = pair or _stay_pair(src.get("check_in") or src.get("start"), src.get("check_out") or src.get("end"))
    pair = pair or _stay_pair(sets.get("dates.start"), sets.get("dates.end"))
    if pair is None and params.get("shift_days") and booked:
        k = int(params["shift_days"])
        pair = (booked[0] + timedelta(days=k), booked[1] + timedelta(days=k))
    if pair is None and params.get("confirm"):
        rec = ctx.session.results.get(CHANGE_QUOTE_KEY)
        quoted = (rec.data or {}).get("new_dates") if rec is not None else None
        if quoted:
            pair = (_date(quoted["check_in"]), _date(quoted["check_out"]))
    if pair is None:
        return None
    start, end = pair
    if end is None:
        nights = (booked[1] - booked[0]).days if booked else int(sets.get("dates.nights") or 0) or None
        if not nights:
            return {"check_in": start.isoformat(), "check_out": None}
        end = start + timedelta(days=nights)
    return {"check_in": start.isoformat(), "check_out": end.isoformat()}


def _new_dates_problem(ctx, new):
    """A Russian question when the new dates are missing or impossible (asked instead of calling the mock)."""
    if not new:
        return "На какие даты перенести? Напишите даты заезда и выезда — например, «21–24 ноября»."
    if not new.get("check_out"):
        return "До какого числа остаётесь? Напишите дату выезда."
    ci, co = _date(new["check_in"]), _date(new["check_out"])
    if ci < ctx.session.today():
        return "Эта дата заезда уже прошла — напишите будущие даты."
    if co <= ci:
        return "Дата выезда должна быть позже заезда — напишите даты ещё раз."
    return None


REBOOK_KEY = "aftercare:rebook"


def _stay_dates(variant):
    stay = next((st for st in variant.steps if st.type == "stay"), None)
    return (stay.start[:10], stay.end[:10]) if stay else None


def _unmovable(ctx, order, new):
    """The booked trip cannot move (tickets without exchange). The first request rebuilds the same trip on the
    new dates; the same request again (the variants for those dates are already there) is answered with the
    numbers instead of an identical rebuild (s16): what cancelling returns and what the new trip costs."""
    s = ctx.session
    rec = s.results.get(REBOOK_KEY)
    done = rec.data if rec is not None and isinstance(rec.data, dict) else {}
    target = (new["check_in"], new["check_out"])
    ready = [v for v in s.variants if _stay_dates(v) == target]
    if done.get("order_id") == s.order_id and done.get("new_dates") == new and ready:
        _no_move_answer(ctx, order, new, ready)
        return
    store_result(s, CachedResult(key=REBOOK_KEY, kind="rebook", params={}, fetched_at=s.clock(),
                                 data={"order_id": s.order_id, "new_dates": new}))
    _rebook_on_dates(ctx, new)


def _no_move_answer(ctx, order, new, variants):
    s = ctx.session
    same = next((v for v in variants if v.id == s.chosen_variant_id), None)
    trip = same or s.variant(recommended_hint(variants)) or variants[0]
    quote = cancel_quote(order, s.clock())
    gap = trip.totals.rub - quote["refund_rub"]
    result = {"kind": "change_dates", "available": False, "reason": "tickets_not_exchangeable", "new_dates": new,
              "if_cancelled": quote,
              "new_trip": {"title": trip.title or P.VARIANT_TITLES.get(trip.id, trip.id),
                           "total_rub": trip.totals.rub, "check_in": new["check_in"], "check_out": new["check_out"]}}
    result["extra_rub" if gap >= 0 else "saving_rub"] = abs(gap)
    ctx.order = order
    ctx.aftercare_result = result
    ctx.present = "present.aftercare"
    ctx.trace["notes"].append("post_purchase: same unmovable change again -> cancel quote + new trip price")


def _rebook_on_dates(ctx, new):
    """The booked trip cannot move (a fare without exchange): build the same trip anew on the new dates.

    The paid order stays as it is; a new selection after an order is allowed (choosing a variant opens a
    new cart). Runs the variants route of the skill (data-driven) and answers with present.variants.
    """
    s = ctx.session
    route = ctx.skill.match_route(intent="new_trip", stage="selection", brief=s.brief)
    if route is None or not s.brief.has("destination") or route.present != "present.variants":
        ctx.present = None
        ctx.message = ("Перенести этот заказ нельзя: билеты по тарифу без обмена. Напишите, куда и на какие даты "
                       "собрать новую поездку, — подберу.")
        return
    ctx.order = None
    ctx.patch = {"set": {"dates.start": new["check_in"], "dates.end": new["check_out"]}, "unset": []}
    ctx.patch_applied = False
    ctx.trace["notes"].append(f"post_purchase: order cannot move -> new selection {new['check_in']}..{new['check_out']}"
                              f" via {route.id}")
    for name in route.steps:
        if ctx.stop:
            break
        STEP_REGISTRY[name](ctx)
        ctx.trace["steps"].append(name)
    ctx.present = route.present
    ctx.note(f"Оплаченный заказ на другие даты не перенести: билеты по тарифу без обмена. Собрал ту же поездку "
             f"на {P.fmt_date_range(new['check_in'], new['check_out'])} заново — текущий заказ остаётся в силе.")


def _transfer_point(leg, arriving):
    det = leg.get("details") or {}
    off = det.get("offer") or det
    if leg["kind"] == "flight":
        return {"iata": off.get("dest_iata" if arriving else "origin_iata")}
    return {"station": off.get("to_station" if arriving else "from_station")}


def _transfer_fits(tr, leg, reverse):
    """The mock's own check (compat_issues): pickup 30 min – 4 h after landing; at the airport at least the
    check-in buffer before departure (and not more than 3 h on top)."""
    off = (leg.get("details") or {}).get("offer") or {}
    if not reverse:
        gap = (_dt(tr["start"]) - _dt(leg["end"])).total_seconds() / 60
        return (10 if leg["kind"] == "train" else 30) <= gap <= 240
    need = 30 if leg["kind"] == "train" else (180 if off.get("international") else 120)
    gap = (_dt(leg["start"]) - _dt(tr["end"])).total_seconds() / 60
    return need <= gap <= need + 180


def _transfer_pickup(ctx, leg, reverse, duration_min):
    """Pickup time for a leg, as ranking plans it: landing + the after-flight buffer (75 min with checked
    baggage, 45 without; trains 10), or departure - the before-departure buffer - the planned ride."""
    from .checks import DEFAULT_BUFFERS, DEFAULT_TAXI_FACTOR
    buffers = {**DEFAULT_BUFFERS, **((ctx.rules or {}).get("buffers_min") or {})}
    off = (leg.get("details") or {}).get("offer") or {}
    if not reverse:
        if leg["kind"] == "train":
            mins = int(((ctx.rules or {}).get("ranking") or {}).get("after_train_min", 10))
        else:
            mins = buffers["after_flight_with_baggage" if (off.get("baggage") or {}).get("checked_kg")
                           else "after_flight"]
        return _dt(leg["end"]) + timedelta(minutes=mins)
    before = buffers["before_train"] if leg["kind"] == "train" else buffers[
        "before_flight_international" if off.get("international") else "before_flight_domestic"]
    factor = float((ctx.rules or {}).get("taxi_time_factor") or DEFAULT_TAXI_FACTOR)
    ride = int(-(-duration_min * factor // 5) * 5)
    return _dt(leg["start"]) - timedelta(minutes=before + ride)


def _transfer_offers_for_order(ctx, order):
    """What add_transfer books: for the arrival leg (airport/station -> hotel) and the departure leg (hotel ->
    airport/station) that have no transfer meeting them, an offer timed to that leg (a booked transfer of the
    same way at a wrong time is re-timed: the mock replaces it). -> (picks, fitting): picks = [{offer, leg,
    reverse, replaces}], fitting = the booked transfers that already meet their legs."""
    items = [i for i in order.get("items") or [] if i.get("status") != "cancelled"]
    hotel = next((i for i in items if i["kind"] == "hotel"), None)
    hdet = (hotel or {}).get("details") or {}
    hotel_id = (hdet.get("params") or {}).get("h") or (hdet.get("offer") or {}).get("hotel_id") or hdet.get("hotel_id")
    if not hotel_id or not hotel.get("start"):
        return [], []
    ci = _dt(hotel["start"]).date()
    transport = sorted([i for i in items if i["kind"] in ("flight", "train") and i.get("start")],
                       key=lambda i: _dt(i["start"]))
    arrivals = [t for t in transport if _dt(t["start"]).date() <= ci]
    departures = [t for t in transport if _dt(t["start"]).date() > ci]
    legs = ([(arrivals[-1], False)] if arrivals else []) + ([(departures[0], True)] if departures else [])
    transfers = [i for i in items if i["kind"] == "transfer"]
    pax = len(order.get("travellers") or []) or (lambda p: p["adults"] + len(p["children_ages"]))(
        _party_payload(ctx.brief))
    picks, fitting = [], []
    for leg, reverse in legs:
        point = _transfer_point(leg, arriving=not reverse)
        same = [t for t in transfers if bool(((t.get("details") or {}).get("offer") or {}).get("reverse")) == reverse
                and ((t.get("details") or {}).get("params") or {}).get("fr") == {k: v for k, v in point.items() if v}]
        ok = [t for t in same if t.get("start") and t.get("end") and _transfer_fits(t, leg, reverse)]
        if ok:
            fitting.append(ok[0])
            continue
        kind = ((same[0]["details"].get("offer") or {}).get("kind")) if same else None
        payload = {"from": point, "to": {"hotel_id": hotel_id}, "pax": pax, "today": ctx.session.today().isoformat()}
        if reverse:
            payload["reverse"] = True
        guess = _transfer_pickup(ctx, leg, reverse, 60)
        offers = ((tool_safe(ctx, "search_transfers", {**payload, "at": guess.isoformat(timespec="seconds")})
                   or {}).get("offers") or [])
        pick = next((o for o in offers if o.get("kind") == kind), None) or _cheapest(offers)
        if pick is None:
            continue
        at = _transfer_pickup(ctx, leg, reverse, int(pick["duration_min"]))
        if at != guess:
            offers = ((tool_safe(ctx, "search_transfers", {**payload, "at": at.isoformat(timespec="seconds")})
                       or {}).get("offers") or [])
            pick = next((o for o in offers if o.get("kind") == pick.get("kind")), None) or _cheapest(offers)
        if pick is not None:
            picks.append({"offer": pick, "leg": leg, "reverse": reverse, "at": at,
                          "replaces": same[0] if same else None})
    return picks, fitting


def _transfer_line(item):
    """«Такси до отеля — 21 ноября в 10:15» from an order transfer item."""
    when = _dt(item["start"])
    return f"«{item['title']}» — {when.day} {P.MONTHS_GEN[when.month - 1]} в {when:%H:%M}"


def _add_transfers(ctx, order):
    """add_transfer without an offer id: book / re-time the transfers the order's legs miss (one call each).
    -> the aftercare result, or None when there is nothing to do (ctx.message is then set by code)."""
    s = ctx.session
    picks, fitting = _transfer_offers_for_order(ctx, order)
    if not picks:
        ctx.present = None
        ctx.order = order
        if fitting:
            ctx.message = ("Трансферы в заказе уже под ваши рейсы: " + "; ".join(_transfer_line(t) for t in fitting)
                           + ".")
        else:
            ctx.message = ("Трансфер к этому заказу сейчас не нашёлся: на это время машин нет. До отеля можно "
                           "доехать на такси на месте.")
        return None
    out, done = None, []
    for p in picks:
        out = tool(ctx, "order_add_transfer", {"order_id": s.order_id, "offer_id": p["offer"]["offer_id"],
                                               "now": now_iso(ctx)}, cache=False)
        item = next((i for i in out.get("items") or [] if i.get("offer_id") == p["offer"]["offer_id"]), None)
        if item is not None and item.get("start"):
            done.append({"title": item["title"], "way": P.transfer_way(item["title"]), "start": item["start"], "time": f"{_dt(item['start']):%H:%M}",
                         "price_rub": int(item["price_rub"]), "replaced": p["replaces"] is not None,
                         "for": "arrival" if not p["reverse"] else "departure",
                         "leg_time": f"{_dt(p['leg']['end' if not p['reverse'] else 'start']):%H:%M}"})
    return {**order_summary(out), "transfers": done}


def _transfer_offer_for_order(ctx, order):
    picks, _ = _transfer_offers_for_order(ctx, order)
    return picks[0]["offer"]["offer_id"] if picks else None


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

