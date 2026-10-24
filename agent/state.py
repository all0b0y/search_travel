"""Session state of the track agent (AGENT_PLAN.md, Technical contract section 3).

Code owns the state; the model only proposes a brief patch (`understand`) that
`apply_patch` applies with provenance. Every enum used by the skill schemas is
exported here as a tuple so tests can check the JSON schemas against it.
"""
import copy
import secrets
from datetime import date, datetime, timedelta
from typing import Any, Generic, Literal, Optional, TypeVar, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic import Field as PField
from pydantic import ValidationError, field_validator

# ---------------------------------------------------------------- enums
# Literal types are the source; tuples (for schemas and tests) are derived from them.

Source = Literal["user", "profile", "pattern", "default", "inferred"]
Stage = Literal["inspiration", "selection", "cart", "checkout", "ordered", "aftercare"]
Intent = Literal["new_trip", "refine", "answer", "choose", "compare", "info", "cart", "checkout",
                 "post_purchase", "smalltalk", "out_of_scope"]
ActionType = Literal["answer_question", "set_chip", "choose_destination", "choose_variant", "swap_hotel",
                     "compare", "add_to_cart", "checkout", "pay", "order_action"]
Segment = Literal["solo", "couple", "family", "friends", "other"]
Meals = Literal["any", "none", "breakfast", "half_board", "full_board", "all_inclusive"]
District = Literal["any", "lively", "quiet", "family", "walkable"]
DistrictLabel = Literal["lively", "quiet", "family", "walkable"]  # OSM district profile scores
Transport = Literal["any", "plane", "train"]
Scope = Literal["flights", "trains", "stay", "transfers"]
Mood = Literal["relax", "party", "culture", "active", "romantic", "kids_fun"]
DestinationTrait = Literal["sea", "beach", "mountains", "lake", "river", "island", "forest", "desert",
                           "warm", "hot", "cold", "snow", "tropical", "city", "resort", "nature",
                           "village", "history", "culture", "food", "nightlife", "ski", "spa",
                           "shopping", "architecture"]
RefKind = Literal["destination", "variant", "hotel", "question"]
ShownKind = Literal["destination", "variant", "hotel"]
PostPurchaseKind = Literal["early_checkin", "change_dates", "add_transfer"]
StepType = Literal["taxi", "flight", "train", "transfer", "stay"]
StepStatus = Literal["planned", "booked", "done", "changed"]
# image traits of the `understand` output (section 4)
ImageLandscape = Literal["beach", "coast", "mountains", "forest", "desert", "lake", "river", "island",
                         "plains", "old_town", "skyline", "countryside", "snow"]
ImageWater = Literal["sea", "lake", "river", "none"]
ImageClimate = Literal["tropical", "warm", "temperate", "cold", "snow"]
ImageSetting = Literal["city", "resort", "nature", "village"]
ImageVibe = Literal["relax", "party", "culture", "active", "romantic", "kids_fun", "luxury", "budget",
                    "quiet", "lively", "exotic", "cozy"]

SOURCES = get_args(Source)
STAGES = get_args(Stage)
INTENTS = get_args(Intent)
ACTION_TYPES = get_args(ActionType)
SEGMENTS = get_args(Segment)
MEALS = get_args(Meals)
DISTRICTS = get_args(District)
DISTRICT_LABELS = get_args(DistrictLabel)
TRANSPORTS = get_args(Transport)
SCOPES = get_args(Scope)
MOODS = get_args(Mood)
DESTINATION_TRAITS = get_args(DestinationTrait)
REF_KINDS = get_args(RefKind)
SHOWN_KINDS = get_args(ShownKind)
POST_PURCHASE_KINDS = get_args(PostPurchaseKind)
STEP_TYPES = get_args(StepType)
STEP_STATUSES = get_args(StepStatus)
IMAGE_LANDSCAPES = get_args(ImageLandscape)
IMAGE_WATER = get_args(ImageWater)
IMAGE_CLIMATE = get_args(ImageClimate)
IMAGE_SETTINGS = get_args(ImageSetting)
IMAGE_VIBES = get_args(ImageVibe)

# Allowed `understand` patch paths (section 4). Single source of truth: the enum in
# skills/trip-agent/schema/understand.schema.json must equal this tuple (tested).
ALLOWED_PATCH_PATHS = (
    "destination.name",
    "dates.start", "dates.end", "dates.month", "dates.nights", "dates.shift_days", "dates.flexible_days",
    "party.segment", "party.adults", "party.children", "party.children_ages", "party.pets",
    "budget_rub.max",
    "prefs.meals", "prefs.free_cancellation", "prefs.stars_min", "prefs.direct_only",
    "prefs.max_travel_hours", "prefs.district", "prefs.beach_max_m", "prefs.transport",
    "constraints.add", "constraints.remove",
    "traits.add", "mood.add",
    "origin.city",
    "scope",
)
# Operations (shift, remove) and non-nullable counters cannot be unset.
ALLOWED_UNSET_PATHS = tuple(p for p in ALLOWED_PATCH_PATHS if p not in (
    "dates.shift_days", "constraints.remove", "party.segment", "party.adults", "party.children"))

TRACE_CAP = 30          # per-turn trace records kept in a session
HISTORY_USER_CAP = 6    # last user messages verbatim
HISTORY_AGENT_CAP = 3   # last agent messages (text only)

T = TypeVar("T")


class Model(BaseModel):
    """Base: strict keys, `from` fields by alias, lossless JSON round-trip."""
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True,
                              serialize_by_alias=True)


class Field(Model, Generic[T]):
    """A brief field with provenance. `source` is internal: the UI never shows it.

    `source`/`confidence` describe the value as a whole. For object fields (dates, party, prefs...)
    `sub_sources`/`sub_confidence` record provenance per sub-field ("segment" -> "pattern"), so a
    user-set `party.adults` does not turn an assumed `party.segment` into a user fact.
    Read provenance through `Brief.source_of(path)`.
    """
    value: Optional[T] = None
    source: Source = "default"
    confidence: Optional[float] = PField(default=None, ge=0.0, le=1.0)
    sub_sources: dict[str, Source] = PField(default_factory=dict)
    sub_confidence: dict[str, float] = PField(default_factory=dict)


# ---------------------------------------------------------------- brief

class Origin(Model):
    city: str
    iata: list[str] = PField(default_factory=list)


class Destination(Model):
    id: Optional[str] = None
    name: str
    country: Optional[str] = None


class DateWindow(Model):
    from_: date = PField(alias="from")
    to: date


class Dates(Model):
    start: Optional[date] = None
    end: Optional[date] = None
    month: Optional[int] = PField(default=None, ge=1, le=12)
    window: Optional[DateWindow] = None
    nights: Optional[int] = PField(default=None, ge=1, le=60)
    flexible_days: int = PField(default=0, ge=0, le=14)


class Party(Model):
    segment: Segment = "other"
    adults: int = PField(default=1, ge=0, le=20)
    children: int = PField(default=0, ge=0, le=20)
    children_ages: Optional[list[int]] = None
    pets: list[str] = PField(default_factory=list)

    @field_validator("children_ages")
    @classmethod
    def _ages(cls, ages):
        if ages is not None and any(a < 0 or a > 17 for a in ages):
            raise ValueError("children ages must be 0..17")
        return ages


class Budget(Model):
    max: Optional[int] = PField(default=None, ge=0)


class Prefs(Model):
    meals: Meals = "any"
    free_cancellation: Optional[bool] = None
    stars_min: Optional[int] = PField(default=None, ge=1, le=5)
    direct_only: Optional[bool] = None
    max_travel_hours: Optional[float] = PField(default=None, gt=0)
    district: District = "any"
    beach_max_m: Optional[int] = PField(default=None, ge=0)
    transport: Transport = "any"


class Brief(Model):
    origin: Field[Origin] = PField(default_factory=Field[Origin])
    destination: Field[Destination] = PField(default_factory=Field[Destination])
    traits: Field[list[DestinationTrait]] = PField(default_factory=Field[list[DestinationTrait]])
    dates: Field[Dates] = PField(default_factory=Field[Dates])
    party: Field[Party] = PField(default_factory=Field[Party])
    budget_rub: Field[Budget] = PField(default_factory=Field[Budget])
    scope: Field[list[Scope]] = PField(default_factory=Field[list[Scope]])
    prefs: Field[Prefs] = PField(default_factory=Field[Prefs])
    constraints: Field[list[str]] = PField(default_factory=Field[list[str]])
    mood: Field[list[Mood]] = PField(default_factory=Field[list[Mood]])

    def values(self):
        """Values only (no provenance): the `brief` part of the understand input."""
        return {name: getattr(self, name).model_dump(mode="json")["value"] for name in BRIEF_FIELDS}

    def sources(self):
        return {name: getattr(self, name).source for name in BRIEF_FIELDS}

    def source_of(self, path):
        """Provenance of a top-level field (`party`) or a sub-field (`party.segment`)."""
        top, _, sub = path.partition(".")
        if top not in BRIEF_FIELDS:
            raise KeyError(f"unknown brief path: {path}")
        field = getattr(self, top)
        if sub and sub in field.sub_sources:
            return field.sub_sources[sub]
        return field.source

    def confidence_of(self, path):
        top, _, sub = path.partition(".")
        field = getattr(self, top)
        if sub and sub in field.sub_confidence:
            return field.sub_confidence[sub]
        return field.confidence

    def has(self, path):
        """True when a top-level field (`dates`) or a sub-field (`dates.start`) holds a value.

        Empty lists and None count as missing.
        """
        top, _, sub = path.partition(".")
        if top not in BRIEF_FIELDS:
            raise KeyError(f"unknown brief path: {path}")
        value = self.values()[top]
        if sub:
            if not isinstance(value, dict):
                return False
            if sub not in value:
                raise KeyError(f"unknown brief path: {path}")
            value = value[sub]
        return value is not None and value != []


BRIEF_FIELDS = tuple(Brief.model_fields)
OBJECT_FIELDS = {"origin": Origin, "destination": Destination, "dates": Dates, "party": Party,
                 "budget_rub": Budget, "prefs": Prefs}
LIST_FIELDS = ("traits", "scope", "constraints", "mood")
# every path usable in route conditions `has` / `missing`
BRIEF_PATHS = BRIEF_FIELDS + tuple(f"{top}.{info.alias or sub}" for top, cls in OBJECT_FIELDS.items()
                                   for sub, info in cls.model_fields.items())


# ---------------------------------------------------------------- patches

class PatchError(ValueError):
    """The patch uses an unknown/forbidden path or a value of the wrong shape."""


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _merge_unique(old, new):
    out = list(old or [])
    for item in new:
        if item not in out:
            out.append(item)
    return out


_EMPTY_OBJECTS = {"dates": Dates, "party": Party, "budget_rub": Budget, "prefs": Prefs}


def _empty_object(top) -> dict:
    """Default sub-object for a field whose value is still None (origin/destination have none)."""
    cls = _EMPTY_OBJECTS.get(top)
    if cls is None:
        raise PatchError(f"{top} has no default value; set its name first")
    return cls().model_dump(mode="json")


def _shift(day, days):
    if day is None:
        return None
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _apply_set(data, path, value):
    top, _, sub = path.partition(".")
    field = data[top]
    if path in ("constraints.add", "traits.add", "mood.add"):
        field["value"] = _merge_unique(field["value"], _as_list(value))
        return
    if path == "constraints.remove":
        drop = set(_as_list(value))
        field["value"] = [c for c in (field["value"] or []) if c not in drop]
        return
    if path == "scope":
        field["value"] = _merge_unique([], _as_list(value))
        return
    if path == "origin.city":
        old = field["value"]
        if not (old and str(old.get("city", "")).casefold() == str(value).casefold()):
            field["value"] = {"city": value, "iata": []}  # airports are resolved by code
        return
    if path == "destination.name":
        old = field["value"]
        if not (old and str(old.get("name", "")).casefold() == str(value).casefold()):
            field["value"] = {"id": None, "name": value, "country": None}  # id resolved by code
        return
    obj = field["value"] if field["value"] is not None else _empty_object(top)
    if path == "dates.shift_days":
        if not isinstance(value, int) or isinstance(value, bool):
            raise PatchError("dates.shift_days must be an integer")
        obj["start"], obj["end"] = _shift(obj.get("start"), value), _shift(obj.get("end"), value)
        if obj.get("window"):
            obj["window"] = {"from": _shift(obj["window"]["from"], value),
                             "to": _shift(obj["window"]["to"], value)}
        if obj.get("start"):
            obj["month"] = int(obj["start"][5:7])
    else:
        obj[sub] = value
    field["value"] = obj


def _apply_unset(data, path):
    top, _, sub = path.partition(".")
    field = data[top]
    if top in LIST_FIELDS or path in ("destination.name", "origin.city"):
        field["value"] = None
        return
    if field["value"] is None:
        return
    default = _EMPTY_OBJECTS[top]().model_dump(mode="json") if top in _EMPTY_OBJECTS else {}
    field["value"][sub] = default.get(sub)


def _normalize(data, sets):
    """Small consistency rules applied after a patch (documented in apply_patch)."""
    dates = data["dates"]["value"]
    if dates:
        if "dates.start" in sets and dates.get("start") and "dates.month" not in sets:
            dates["month"] = int(str(dates["start"])[5:7])
        if "dates.month" in sets and "dates.start" not in sets and dates.get("start"):
            if int(str(dates["start"])[5:7]) != dates.get("month"):
                dates["start"] = None
                if "dates.end" not in sets:
                    dates["end"] = None
    party = data["party"]["value"]
    if party:
        if "party.children_ages" in sets and "party.children" not in sets and party.get("children_ages") is not None:
            party["children"] = len(party["children_ages"])
        if "party.children" in sets and party.get("children") == 0:
            party["children_ages"] = None


def _leaf_values(brief):
    out = {}
    for top, value in brief.values().items():
        if top in OBJECT_FIELDS:
            cls = OBJECT_FIELDS[top]
            if value is None and top in _EMPTY_OBJECTS:  # unknown == defaults for change detection
                value = _EMPTY_OBJECTS[top]().model_dump(mode="json")
            for name, info in cls.model_fields.items():
                key = info.alias or name
                out[f"{top}.{key}"] = None if value is None else value.get(key)
        else:
            out[top] = value
    return out


def changed_paths(before, after):
    """Sorted brief paths whose value differs (lists as a whole, objects per sub-field)."""
    a, b = _leaf_values(before), _leaf_values(after)
    return sorted(p for p in a if a[p] != b[p])


def _note_sub(patched_subs, path):
    """Record which sub-fields a patch path touches (for per-sub-field provenance)."""
    top, _, sub = path.partition(".")
    if top not in OBJECT_FIELDS or not sub:
        return
    subs = patched_subs.setdefault(top, set())
    if path == "dates.shift_days":
        subs.update({"start", "end", "window", "month"})
    elif path == "dates.start":
        subs.update({"start", "month"})          # month is derived from the user's start date
    elif path in ("party.children_ages", "party.children"):
        subs.update({"children", "children_ages"})
    else:
        subs.add(sub)


_WHOLE_VALUE_PATHS = {"origin.city": "origin", "destination.name": "destination"}


def _mark_sub_sources(data, before, top, subs, sets):
    """Keep per-sub-field provenance when a patch touches part of an object field.

    The whole-value replacements (`origin.city`, `destination.name`) make the value the user's.
    Otherwise the sub-fields the patch did not touch keep their old provenance: "default" when the
    object was created by this patch, or the field's previous source when it existed.
    """
    if top not in OBJECT_FIELDS:
        return
    field = data[top]
    if any(sets.get(p) is not None and t == top for p, t in _WHOLE_VALUE_PATHS.items()):
        field["sub_sources"], field["sub_confidence"] = {}, {}
        return
    old = getattr(before, top)
    inherited = "default" if old.value is None else old.source
    sub_sources = dict(field.get("sub_sources") or {})
    cls = OBJECT_FIELDS[top]
    for name, info in cls.model_fields.items():
        key = info.alias or name
        sub_sources.setdefault(key, inherited)
    sub_confidence = dict(field.get("sub_confidence") or {})
    for key in subs:
        sub_sources[key] = "user"
        sub_confidence.pop(key, None)
    if old.value is not None and old.confidence is not None:
        for key, src in sub_sources.items():
            if key not in subs and src == old.source:
                sub_confidence.setdefault(key, old.confidence)
    field["sub_sources"], field["sub_confidence"] = sub_sources, sub_confidence


def assume(brief, path, value, source, confidence=None):
    """Engine-side assumption: set `path` to `value` with a non-user provenance.

    Used for profile data, segment defaults, insights patterns and inferred values (photo place).
    Object sub-fields get `sub_sources[sub] = source`; whole fields get `source`. Never use this for
    something the user said: that goes through `apply_patch`. Returns a new Brief.
    """
    if source == "user":
        raise ValueError("user values go through apply_patch")
    top, _, sub = path.partition(".")
    if top not in BRIEF_FIELDS:
        raise KeyError(f"unknown brief path: {path}")
    data = copy.deepcopy(brief.model_dump(mode="json"))
    field = data[top]
    if top in OBJECT_FIELDS and sub:
        created = field["value"] is None
        obj = field["value"] if not created else (
            _empty_object(top) if top in _EMPTY_OBJECTS else {})
        obj[sub] = copy.deepcopy(value)
        field["value"] = obj
        if created:
            field["source"] = source
            field["confidence"] = confidence
            for name, info in OBJECT_FIELDS[top].model_fields.items():
                field["sub_sources"].setdefault(info.alias or name, "default")
        field["sub_sources"][sub] = source
        if confidence is None:
            field["sub_confidence"].pop(sub, None)
        else:
            field["sub_confidence"][sub] = confidence
    else:
        field["value"] = copy.deepcopy(value)
        field["source"] = source
        field["confidence"] = confidence
        field["sub_sources"], field["sub_confidence"] = {}, {}
    return Brief.model_validate(data)


def apply_patch(brief, patch, allowed_paths=ALLOWED_PATCH_PATHS):
    """Apply an `understand` patch {"set": {path: value}, "unset": [path]} -> (new_brief, changed_paths).

    - Paths must be in both ALLOWED_PATCH_PATHS and `allowed_paths`; unset paths in
      ALLOWED_UNSET_PATHS; a path may not be both set and unset. Violations -> PatchError.
    - Every touched top-level field gets source="user", confidence=None (even if the
      value did not change: the user confirmed it). `changed_paths` lists only real changes.
    - `*.add` merge unique values, `constraints.remove` drops tokens, `scope` replaces.
    - `destination.name` / `origin.city` with a new name reset id/country / iata (resolved by code).
    - `dates.shift_days` moves start/end/window by N days; with no concrete dates it is a no-op.
    - `dates.start` also sets `dates.month`; a new `dates.month` alone clears start/end of another month.
    - `party.children_ages` alone sets `party.children`; `party.children = 0` clears the ages.
    - Unsetting a list op (`traits.add`...) clears the list (value None = unknown).
    The input brief is never mutated.
    """
    if not isinstance(patch, dict) or set(patch) - {"set", "unset"}:
        raise PatchError("patch must be an object with optional keys 'set' and 'unset'")
    sets = patch.get("set") or {}
    unsets = patch.get("unset") or []
    if not isinstance(sets, dict) or not isinstance(unsets, list):
        raise PatchError("'set' must be an object and 'unset' a list")
    allowed = set(allowed_paths)
    for path in list(sets) + list(unsets):
        if path not in ALLOWED_PATCH_PATHS or path not in allowed:
            raise PatchError(f"path not allowed: {path}")
    for path in unsets:
        if path not in ALLOWED_UNSET_PATHS:
            raise PatchError(f"path cannot be unset: {path}")
    both = set(sets) & set(unsets)
    if both:
        raise PatchError(f"path both set and unset: {sorted(both)[0]}")

    data = copy.deepcopy(brief.model_dump(mode="json"))
    touched = []
    patched_subs = {}
    for path in unsets:
        _apply_unset(data, path)
        touched.append(path.partition(".")[0])
        _note_sub(patched_subs, path)
    for path in sorted(sets, key=ALLOWED_PATCH_PATHS.index):  # canonical order: shift after start/end
        try:
            _apply_set(data, path, copy.deepcopy(sets[path]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise PatchError(f"bad value for {path}: {exc}") from None
        touched.append(path.partition(".")[0])
        _note_sub(patched_subs, path)
    try:
        _normalize(data, sets)
    except (TypeError, ValueError) as exc:
        raise PatchError(f"bad dates or party values: {exc}") from None
    for top in touched:
        _mark_sub_sources(data, brief, top, patched_subs.get(top, set()), sets)
        data[top]["source"] = "user"
        data[top]["confidence"] = None
    try:
        new = Brief.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"] if p != "value")
        raise PatchError(f"invalid value at {where}: {first['msg']}") from None
    return new, changed_paths(brief, new)


# ---------------------------------------------------------------- shown items, questions, chips

class ShownItem(Model):
    """A short label the user can reference ("второй", "1") -> what was shown."""
    label: str
    kind: ShownKind
    id: str
    title: str


class Shown(Model):
    destinations: list[ShownItem] = PField(default_factory=list)
    variants: list[ShownItem] = PField(default_factory=list)
    hotels: list[ShownItem] = PField(default_factory=list)


class Choice(Model):
    label: str
    value: Any = None
    recommended: bool = False


class Question(Model):
    """A question asked as buttons; answering it patches `field`."""
    id: str
    field: str
    text: str
    choices: list[Choice] = PField(default_factory=list)
    reason: Optional[str] = None  # internal: why the engine asks (sensitivity / Jev), never shown

    @field_validator("field")
    @classmethod
    def _field(cls, value):
        if value not in ALLOWED_PATCH_PATHS:
            raise ValueError(f"question field must be an allowed patch path: {value}")
        return value


class ChipOption(Model):
    label: str
    value: Any = None


class Chip(Model):
    """An assumption shown as a chip (without its source)."""
    field: str
    label: str
    value: Any = None
    options: list[ChipOption] = PField(default_factory=list)


# ---------------------------------------------------------------- variants

class Price(Model):
    """Compatible with trip-planner `$defs/price`; money in RUB here."""
    amount: int | float
    currency: str = "RUB"
    checked_at: str
    estimate: Optional[bool] = None
    note: Optional[str] = None
    rub: Optional[int | float] = None


class Step(Model):
    """A timeline step compatible with trip-planner `$defs/step` (and agent/checks.py)."""
    id: str
    type: StepType
    from_: str = PField(alias="from")
    to: str
    start: str   # ISO 8601 with offset
    end: str
    price: Price
    link: str
    status: StepStatus = "planned"
    carrier: Optional[str] = None
    international: Optional[bool] = None
    checked_baggage: Optional[bool] = None
    duration_estimate_min: Optional[int] = None
    risk: Optional[dict[str, Any]] = None
    plan_b: Optional[dict[str, Any]] = None
    offer_id: Optional[str] = None  # Ostrovok-mock offer behind this step
    title: Optional[str] = None     # Russian label for the UI timeline

    def to_check_dict(self):
        """Plain dict for checks.py (aliases, no null optionals)."""
        return self.model_dump(mode="json", exclude_none=True)


class Totals(Model):
    rub: int
    door_to_door_hours: float


class Note(Model):
    """A computed finding (sacrifice / risk / not-for) with a code-written Russian fallback text."""
    code: str
    text: str
    data: dict[str, int | float | str | bool | None] = PField(default_factory=dict)


class Fit(Model):
    score: float = PField(ge=0.0, le=1.0)
    breakdown: dict[str, float] = PField(default_factory=dict)


class UpsellCandidate(Model):
    """A rule hit from upsell.json; the present card may mention it only by `id`."""
    id: str
    rule_id: str
    kind: str
    label: str
    reason: str
    offer_id: Optional[str] = None
    price_rub: Optional[int] = None
    variant_id: Optional[str] = None   # the variant this add-on belongs to


class DistrictProfile(Model):
    labels: list[DistrictLabel] = PField(default_factory=list)
    scores: dict[str, float] = PField(default_factory=dict)


class VariantHotel(Model):
    hotel_id: str
    offer_id: Optional[str] = None
    name: str
    stars: Optional[int] = None
    rating: Optional[float] = None
    district: DistrictProfile = PField(default_factory=DistrictProfile)


class Variant(Model):
    """Built by code. The model never produces its numbers."""
    id: str = PField(pattern=r"^(cheapest|fastest|comfort|custom-[0-9]+)$")
    title: str = ""
    destination_id: Optional[str] = None
    steps: list[Step] = PField(default_factory=list)
    totals: Totals
    hotel: Optional[VariantHotel] = None
    facts: dict[str, int | float | str | bool | None] = PField(default_factory=dict)
    sacrifices: list[Note] = PField(default_factory=list)
    risks: list[Note] = PField(default_factory=list)
    not_for: list[Note] = PField(default_factory=list)
    fit: Optional[Fit] = None
    upsell_candidates: list[UpsellCandidate] = PField(default_factory=list)

    def steps_for_checks(self):
        return [s.to_check_dict() for s in self.steps]


# ---------------------------------------------------------------- session

class CachedResult(Model):
    key: str            # hash of the search params
    kind: str           # tool or step name, e.g. "search_hotels"
    params: dict[str, Any] = PField(default_factory=dict)
    fetched_at: AwareDatetime
    data: Any = None


class History(Model):
    user: list[str] = PField(default_factory=list)
    agent: list[str] = PField(default_factory=list)


def new_session_id():
    return "s_" + secrets.token_hex(8)


class Session(Model):
    id: str = PField(default_factory=new_session_id)
    created_at: AwareDatetime
    now: Optional[AwareDatetime] = None  # pinned clock (evals, tests); None = wall clock
    profile_id: Optional[str] = None
    debug: bool = False   # Response.debug is filled only for debug sessions
    turn: int = 0         # Response.turn
    brief: Brief = PField(default_factory=Brief)
    stage: Stage = "inspiration"
    shown: Shown = PField(default_factory=Shown)
    results: dict[str, CachedResult] = PField(default_factory=dict)
    variants: list[Variant] = PField(default_factory=list)
    chosen_variant_id: Optional[str] = None
    cart_id: Optional[str] = None
    order_id: Optional[str] = None
    pending_questions: list[Question] = PField(default_factory=list)
    history: History = PField(default_factory=History)
    trace: list[dict[str, Any]] = PField(default_factory=list)

    @classmethod
    def new(cls, now=None, profile_id=None, debug=False, session_id=None):
        """`now`: pinned aware datetime (also used as created_at) or None for the wall clock."""
        created = now or datetime.now().astimezone()
        fields = dict(created_at=created, now=now, profile_id=profile_id, debug=debug)
        if session_id:
            fields["id"] = session_id
        return cls(**fields)

    def clock(self):
        """Current time for this session: the pinned `now` or the aware wall clock."""
        return self.now or datetime.now().astimezone()

    def today(self):
        return self.clock().date()

    def remember_user(self, text):
        self.history.user = (self.history.user + [text])[-HISTORY_USER_CAP:]

    def remember_agent(self, text):
        self.history.agent = (self.history.agent + [text])[-HISTORY_AGENT_CAP:]

    def add_trace(self, record):
        self.trace = (self.trace + [record])[-TRACE_CAP:]

    def find_shown(self, kind, ref):
        """Resolve a reference ("2" or an id) among shown items of `kind`; None if absent."""
        items = {"destination": self.shown.destinations, "variant": self.shown.variants,
                 "hotel": self.shown.hotels}.get(kind)
        if items is None:
            raise KeyError(f"unknown shown kind: {kind}")
        ref = str(ref).strip()
        return next((i for i in items if i.label == ref), None) or next((i for i in items if i.id == ref), None)

    def shown_titles(self):
        """{kind: {label: title}} for the understand input."""
        return {kind: {i.label: i.title for i in getattr(self.shown, kind)}
                for kind in ("destinations", "variants", "hotels")}

    def variant(self, variant_id):
        return next((v for v in self.variants if v.id == variant_id), None)

    def to_json(self):
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text):
        return cls.model_validate_json(text)
