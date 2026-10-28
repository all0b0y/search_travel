"""Load and validate the skill package (AGENT_PLAN.md section 4).

    pkg = load_skill("skills/trip-agent")
    route = pkg.match_route(intent="new_trip", stage="inspiration", brief=session.brief)
    system = pkg.system_prompt("present.variants")

Every problem raises SkillLoadError("<file>: <problem>").
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Optional

import jsonschema

from .state import (ACTION_TYPES, ALLOWED_PATCH_PATHS, BRIEF_PATHS, DISTRICT_LABELS, INTENTS, SEGMENTS,
                    STAGES, Prefs)
from .steps_spec import STEP_NAMES

DEFAULT_SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "trip-agent"
DRAFT7_IDS = ("http://json-schema.org/draft-07/schema#", "http://json-schema.org/draft-07/schema")
THINKING = ("off", "low", "high")
CARD_KEYS = {"prompt", "schema", "thinking", "max_tokens", "voice", "temperature"}
ROUTE_KEYS = {"id", "when", "steps", "present"}
WHEN_KEYS = {"intent", "has", "missing", "stage", "action"}
UPSELL_KINDS = ("transfer", "early_checkin", "room_upgrade", "meal_upgrade", "flexible_fare")  # what the mock sells
UPSELL_CONDITIONS = ("arrival_after", "arrival_before_checkin_min", "children", "variant_has_transfer",
                     "traits_any", "mood_any", "prefs", "segment_any", "non_refundable", "meals_below",
                     "nights_min", "stars_below")   # implemented in agent/upsell.py
RECOMMEND_SOURCES = ("profile", "pattern")
RULES_REQUIRED = {"max_questions": int, "max_upsell": int, "max_jokes": int, "sensitivity": dict, "jev": dict,
                  "retries": dict, "buffers_min": dict}
JEV_THRESHOLDS = ("routing", "ask_or_assume", "guard_fail")


class SkillLoadError(Exception):
    pass


@dataclass(frozen=True)
class Card:
    name: str
    prompt_file: str
    prompt: str
    schema_file: str
    schema: dict
    thinking: str
    max_tokens: int
    voice: bool = False
    temperature: Optional[float] = None   # None: the backend default (OpenRouter/DeepSeek 1.0)


@dataclass(frozen=True)
class Route:
    index: int
    id: Optional[str]
    when: dict
    steps: tuple
    present: Optional[str]

    def matches(self, intent=None, action=None, stage=None, brief=None):
        """`intent` for text turns, `action` (type) for UI action turns."""
        w = self.when
        if "action" in w and (action is None or action not in w["action"]):
            return False
        if "intent" in w and (intent is None or intent not in w["intent"]):
            return False
        if "stage" in w and stage not in w["stage"]:
            return False
        for path in w.get("has", []):
            if brief is None or not brief.has(path):
                return False
        for path in w.get("missing", []):
            if brief is not None and brief.has(path):
                return False
        return True


@dataclass
class SkillPackage:
    root: Path
    cards: dict
    routes: list
    voice: str
    questions: dict
    segments: dict
    upsell: dict
    rules: dict
    lessons: str = ""
    readme: str = field(default="", repr=False)
    place_confirm: dict = field(default_factory=dict)   # questions.json "place_confirm" (photo place question)
    # cards outside turns.json routes: `guide` (guide mode, decisions 23-26) — prompt guide.md, schema
    # schema/guide.schema.json, thinking / temperature / max_tokens from rules.json "guide"
    extra_cards: dict = field(default_factory=dict)

    def card(self, name) -> Card:
        if name in self.cards:
            return self.cards[name]
        if name in self.extra_cards:
            return self.extra_cards[name]
        raise KeyError(f"unknown card: {name}")

    def system_prompt(self, name):
        """Card prompt + voice.md (when the card has voice) + the output schema reminder."""
        card = self.card(name)
        parts = [card.prompt.strip()]
        if card.voice:
            parts.append(self.voice.strip())
        parts.append("Output: reply with ONE JSON object (no prose, no code fences) valid against this JSON Schema:\n"
                     + json.dumps(card.schema, ensure_ascii=False, separators=(",", ":")))
        return "\n\n".join(parts)

    def match_route(self, intent=None, action=None, stage=None, brief=None) -> Optional[Route]:
        return next((r for r in self.routes if r.matches(intent, action, stage, brief)), None)


# ---------------------------------------------------------------- helpers

def _fail(file, problem) -> NoReturn:
    raise SkillLoadError(f"{file}: {problem}")


def _read_json(root, rel) -> Any:
    path = root / rel
    if not path.is_file():
        _fail(rel, "file not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(rel, f"invalid JSON: {exc}")


def _read_text(root, rel, owner):
    path = root / rel
    if not path.is_file():
        _fail(owner, f"file not found: {rel}")
    return path.read_text(encoding="utf-8")


def _check_schema(rel, schema):
    if not isinstance(schema, dict):
        _fail(rel, "schema must be a JSON object")
    declared = schema.get("$schema")
    if declared is not None and declared not in DRAFT7_IDS:
        _fail(rel, f"$schema must be draft-07, got {declared}")
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        _fail(rel, f"invalid draft-07 schema: {exc.message}")


def _str_list(file, where, value, allowed=None):
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        _fail(file, f"{where} must be a non-empty list of strings")
    if allowed is not None:
        bad = [v for v in value if v not in allowed]
        if bad:
            _fail(file, f"{where}: unknown value(s) {bad}")


# ---------------------------------------------------------------- loaders

def _load_cards(root, raw):
    cards = {}
    if not isinstance(raw, dict) or not raw:
        _fail("turns.json", "cards must be a non-empty object")
    for name, spec in raw.items():
        where = f"cards.{name}"
        if not isinstance(spec, dict):
            _fail("turns.json", f"{where} must be an object")
        extra = set(spec) - CARD_KEYS
        if extra:
            _fail("turns.json", f"{where}: unknown key(s) {sorted(extra)}")
        for key in ("prompt", "schema", "thinking", "max_tokens"):
            if key not in spec:
                _fail("turns.json", f"{where}: missing {key}")
        if spec["thinking"] not in THINKING:
            _fail("turns.json", f"{where}: thinking must be one of {THINKING}")
        if not isinstance(spec["max_tokens"], int) or isinstance(spec["max_tokens"], bool) or spec["max_tokens"] <= 0:
            _fail("turns.json", f"{where}: max_tokens must be a positive integer")
        if "voice" in spec and not isinstance(spec["voice"], bool):
            _fail("turns.json", f"{where}: voice must be a boolean")
        temp = spec.get("temperature")
        if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or not 0 <= temp <= 2):
            _fail("turns.json", f"{where}: temperature must be a number in 0..2")
        prompt = _read_text(root, spec["prompt"], f"turns.json {where}")
        schema = _read_json(root, spec["schema"])
        _check_schema(spec["schema"], schema)
        cards[name] = Card(name=name, prompt_file=spec["prompt"], prompt=prompt, schema_file=spec["schema"],
                           schema=schema, thinking=spec["thinking"], max_tokens=spec["max_tokens"],
                           voice=bool(spec.get("voice", False)),
                           temperature=float(temp) if temp is not None else None)
    if "understand" not in cards:
        _fail("turns.json", "cards: missing the understand card")
    return cards


def _load_routes(raw, cards, step_names):
    if not isinstance(raw, list) or not raw:
        _fail("turns.json", "routes must be a non-empty list")
    routes = []
    for i, spec in enumerate(raw):
        where = f"routes[{i}]"
        if not isinstance(spec, dict):
            _fail("turns.json", f"{where} must be an object")
        extra = set(spec) - ROUTE_KEYS
        if extra:
            _fail("turns.json", f"{where}: unknown key(s) {sorted(extra)}")
        for key in ("when", "steps", "present"):
            if key not in spec:
                _fail("turns.json", f"{where}: missing {key}")
        when = spec["when"]
        if not isinstance(when, dict):
            _fail("turns.json", f"{where}.when must be an object")
        bad_keys = set(when) - WHEN_KEYS
        if bad_keys:
            _fail("turns.json", f"{where}.when: unknown condition(s) {sorted(bad_keys)}")
        if "intent" in when and "action" in when:
            _fail("turns.json", f"{where}.when: intent and action never hold together")
        allowed = {"intent": INTENTS, "stage": STAGES, "action": ACTION_TYPES, "has": BRIEF_PATHS,
                   "missing": BRIEF_PATHS}
        for key, value in when.items():
            _str_list("turns.json", f"{where}.when.{key}", value, allowed[key])
        steps = spec["steps"]
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            _fail("turns.json", f"{where}.steps must be a list of step names")
        unknown = [s for s in steps if s not in step_names]
        if unknown:
            _fail("turns.json", f"{where}.steps: unknown step(s) {unknown}")
        present = spec["present"]
        if present is not None and present not in cards:
            _fail("turns.json", f"{where}.present: unknown card {present}")
        if present == "understand":
            _fail("turns.json", f"{where}.present: understand is not a present card")
        rid = spec.get("id")
        if rid is not None and not isinstance(rid, str):
            _fail("turns.json", f"{where}.id must be a string")
        routes.append(Route(index=i, id=rid, when=when, steps=tuple(steps), present=present))
    ids = [r.id for r in routes if r.id]
    if len(ids) != len(set(ids)):
        _fail("turns.json", "route ids must be unique")
    return routes


def _check_questions(data):
    f = "questions.json"
    qs = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(qs, dict) or not qs:
        _fail(f, "questions must be a non-empty object")
    seen = set()
    for path, q in qs.items():
        if path not in ALLOWED_PATCH_PATHS:
            _fail(f, f"{path}: not an allowed patch path")
        if not isinstance(q, dict):
            _fail(f, f"{path} must be an object")
        for key in ("id", "text", "choices"):
            if key not in q:
                _fail(f, f"{path}: missing {key}")
        if q["id"] in seen:
            _fail(f, f"{path}: duplicate id {q['id']}")
        seen.add(q["id"])
        if not isinstance(q["text"], str) or not q["text"].strip():
            _fail(f, f"{path}: text must be a non-empty string")
        choices = q["choices"]
        if not isinstance(choices, list) or not choices:
            _fail(f, f"{path}: choices must be a non-empty list")
        for c in choices:
            if not isinstance(c, dict) or not isinstance(c.get("label"), str) or "value" not in c:
                _fail(f, f"{path}: every choice needs label and value")
        rec = q.get("recommend", {})
        if not isinstance(rec, dict):
            _fail(f, f"{path}: recommend must be an object")
        for src in rec.get("from", []):
            if src not in RECOMMEND_SOURCES:
                _fail(f, f"{path}: recommend.from: unknown source {src}")
        if rec.get("default") is not None and rec["default"] not in [c["value"] for c in choices]:
            _fail(f, f"{path}: recommend.default is not one of the choice values")


def _check_segments(data):
    f = "segments.json"
    segs = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segs, dict) or not segs:
        _fail(f, "segments must be a non-empty object")
    prefs_keys = set(Prefs.model_fields)
    for name, seg in segs.items():
        if name not in SEGMENTS:
            _fail(f, f"unknown segment {name}")
        if not isinstance(seg, dict):
            _fail(f, f"{name} must be an object")
        for key in ("label", "party", "prefs", "district_weights"):
            if key not in seg:
                _fail(f, f"{name}: missing {key}")
        bad = set(seg["prefs"]) - prefs_keys
        if bad:
            _fail(f, f"{name}.prefs: unknown pref(s) {sorted(bad)}")
        try:
            Prefs(**seg["prefs"])
        except Exception as exc:
            _fail(f, f"{name}.prefs: {exc}")
        weights = seg["district_weights"]
        if set(weights) != set(DISTRICT_LABELS):
            _fail(f, f"{name}.district_weights must have exactly {DISTRICT_LABELS}")
        if abs(sum(weights.values()) - 1.0) > 1e-6:
            _fail(f, f"{name}.district_weights must sum to 1")


def _check_upsell(data):
    f = "upsell.json"
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not rules:
        _fail(f, "rules must be a non-empty list")
    seen = set()
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, dict):
            _fail(f, f"{where} must be an object")
        for key in ("id", "kind", "label", "when", "reason"):
            if key not in rule:
                _fail(f, f"{where}: missing {key}")
        if rule["id"] in seen:
            _fail(f, f"{where}: duplicate id {rule['id']}")
        seen.add(rule["id"])
        if rule["kind"] not in UPSELL_KINDS:
            _fail(f, f"{where}: unknown kind {rule['kind']}")
        if not isinstance(rule["when"], dict) or not rule["when"]:
            _fail(f, f"{where}: when must be a non-empty object")
        bad = set(rule["when"]) - set(UPSELL_CONDITIONS)
        if bad:
            _fail(f, f"{where}: unknown condition(s) {sorted(bad)}")
        if not isinstance(rule["reason"], str) or not rule["reason"].strip():
            _fail(f, f"{where}: reason must be a non-empty string")


def _check_rules(data):
    f = "rules.json"
    if not isinstance(data, dict):
        _fail(f, "must be an object")
    for key, kind in RULES_REQUIRED.items():
        if key not in data:
            _fail(f, f"missing {key}")
        if not isinstance(data[key], kind) or isinstance(data[key], bool):
            _fail(f, f"{key} must be {kind.__name__}")
    for key in JEV_THRESHOLDS:
        value = data["jev"].get(key)
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            _fail(f, f"jev.{key} must be a number in 0..1")
    if not isinstance(data["sensitivity"].get("price_delta_pct"), (int, float)):
        _fail(f, "sensitivity.price_delta_pct must be a number")


GUIDE_DEFAULTS = {"thinking": "off", "temperature": None, "max_tokens": 1800}


def _load_rules(root):
    rules = _read_json(root, "rules.json")
    _check_rules(rules)
    owners = dict.fromkeys(rules, "rules.json")
    for path in sorted((root / "rules.d").glob("*.json")):
        rel = path.relative_to(root).as_posix()
        fragment = _read_json(root, rel)
        if not isinstance(fragment, dict):
            _fail(rel, "must be an object")
        for key in fragment:
            if key in owners:
                _fail(rel, f"duplicate rule key {key!r} (already defined in {owners[key]})")
        rules.update(fragment)
        owners.update(dict.fromkeys(fragment, rel))
    return rules


def _load_guide_card(root, rules):
    """The `guide` card (agent/guide.py) when guide.md and its schema exist; settings from rules.json "guide"."""
    if not (root / "guide.md").is_file():
        return {}
    spec = {**GUIDE_DEFAULTS, **{k: v for k, v in (rules.get("guide") or {}).items() if k in GUIDE_DEFAULTS}}
    if spec["thinking"] not in THINKING:
        _fail("rules.json", f"guide.thinking must be one of {THINKING}")
    temp = spec["temperature"]
    if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or not 0 <= temp <= 2):
        _fail("rules.json", "guide.temperature must be a number in 0..2")
    if not isinstance(spec["max_tokens"], int) or spec["max_tokens"] <= 0:
        _fail("rules.json", "guide.max_tokens must be a positive integer")
    schema = _read_json(root, "schema/guide.schema.json")
    _check_schema("schema/guide.schema.json", schema)
    return {"guide": Card(name="guide", prompt_file="guide.md", prompt=_read_text(root, "guide.md", "guide"),
                          schema_file="schema/guide.schema.json", schema=schema, thinking=spec["thinking"],
                          max_tokens=spec["max_tokens"], voice=True,
                          temperature=float(temp) if temp is not None else None)}


def load_skill(path="skills/trip-agent", step_names=STEP_NAMES) -> SkillPackage:
    """Load the package. A relative path is tried from the cwd, then from the repository root."""
    root = Path(path)
    if not root.is_absolute() and not root.exists():
        root = DEFAULT_SKILL_PATH.parent.parent / root
    if not root.is_dir():
        _fail(str(root), "skill directory not found")
    turns = _read_json(root, "turns.json")
    if not isinstance(turns, dict) or set(turns) - {"cards", "routes"}:
        _fail("turns.json", "must be an object with keys cards and routes")
    cards = _load_cards(root, turns.get("cards"))
    routes = _load_routes(turns.get("routes"), cards, tuple(step_names))
    voice = _read_text(root, "voice.md", "voice.md")
    questions = _read_json(root, "questions.json")
    _check_questions(questions)
    segments = _read_json(root, "segments.json")
    _check_segments(segments)
    upsell = _read_json(root, "upsell.json")
    _check_upsell(upsell)
    rules = _load_rules(root)
    lessons = (root / "lessons.md").read_text(encoding="utf-8") if (root / "lessons.md").is_file() else ""
    readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").is_file() else ""
    return SkillPackage(root=root, cards=cards, routes=routes, voice=voice, questions=questions["questions"],
                        segments=segments["segments"], upsell=upsell, rules=rules, lessons=lessons, readme=readme,
                        place_confirm=questions.get("place_confirm") or {},
                        extra_cards=_load_guide_card(root, rules))
