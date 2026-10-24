"""Offline checks of the trip-agent skill content: prompts, data files, example card I/O pairs.

No model calls. The number/id helpers mirror the deterministic guards the engine runs on
present outputs (AGENT_PLAN.md section 5, step 7) in their simplest form.
"""
import json
import re
from pathlib import Path

import jsonschema
import pytest

from agent.skill_loader import load_skill
from agent.state import ALLOWED_PATCH_PATHS, Brief, apply_patch

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "trip-agent"
EXAMPLES = SKILL / "examples"

COMMON_KEYS = {"today", "stage", "brief", "assumed", "assumptions"}
BINDING_KEYS = {
    "understand": COMMON_KEYS | {"shown", "pending_questions", "history", "message", "has_image"},
    "present.destinations": COMMON_KEYS | {"items", "questions", "image", "insight"},
    "present.variants": COMMON_KEYS | {"items", "recommended_hint", "questions", "upsell_candidates"},
    "present.update": COMMON_KEYS | {"items", "recommended_hint", "questions", "upsell_candidates", "changes"},
    "present.compare": COMMON_KEYS | {"items", "dimensions"},
    "present.info": COMMON_KEYS | {"question", "facts"},
    "present.aftercare": COMMON_KEYS | {"order", "result"},
    "present.fallback": COMMON_KEYS | {"message", "reason"},
}
DEST_ITEM_KEYS = {"id", "label", "name", "country", "price_from_rub", "nights", "party_label", "weather",
                  "travel_time_h", "match", "facts"}
VARIANT_ITEM_KEYS = {"id", "label", "title", "totals", "steps", "hotel", "facts", "sacrifices", "risks", "not_for", "fit"}
STEP_KEYS = {"type", "from", "to", "start", "end", "carrier", "price_rub"}
MIN_EXAMPLES = {"understand": 8}
PRESENT_CARDS = [c for c in BINDING_KEYS if c != "understand"]
UPSELL_ALLOWED = {"transfer", "early_checkin", "room_upgrade", "meal_upgrade", "flexible_fare"}
PROMPT_BUDGET = {"understand": 150, **{c: 90 for c in PRESENT_CARDS}}
BRIEF_TOPS = ("origin", "destination", "traits", "dates", "party", "budget_rub", "scope", "prefs", "constraints", "mood")

# ---------------------------------------------------------------- number / emoji helpers

_SPACES = "[ \u00a0\u2009\u202f]"
_NUM = re.compile(rf"\d{{1,3}}(?:{_SPACES}\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_MULT = re.compile(r"^(?:\s?(тыс\.?|тысяч[а-я]*)|к(?![а-яё]))", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]")


def numbers_in_text(text):
    """Numbers a reader sees: '90 300' -> 90300, '6,5' -> 6.5, '100к' / '100 тыс.' -> 100000."""
    out = []
    for m in _NUM.finditer(text):
        raw = re.sub(_SPACES, "", m.group(0)).replace(",", ".")
        value = float(raw)
        mult = _MULT.match(text[m.end():])
        if mult:
            value *= 1000
        out.append(value)
    return out


def numbers_in_json(obj):
    """Every number of a card input: numeric values plus numbers inside strings (dates, times, notes)."""
    found = set()
    if isinstance(obj, bool) or obj is None:
        return found
    if isinstance(obj, (int, float)):
        found.add(float(abs(obj)))
    elif isinstance(obj, str):
        found.update(numbers_in_text(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= numbers_in_json(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= numbers_in_json(v)
    return found


def output_texts(out):
    texts = [out["message"]]
    for item in out["items"]:
        texts.append(item.get("why") or "")
        texts.append(item.get("watch_out") or "")
        for key in ("sacrifices", "risks", "not_for"):
            texts.extend(item.get(key, []))
    texts.extend(u["line"] for u in out["upsell"])
    return [t for t in texts if t]


# ---------------------------------------------------------------- example loading

def _card_of(stem):
    name = stem.split(".")[0]
    return "understand" if name == "understand" else f"present.{name}"


def load_examples():
    pairs = []
    for inp in sorted(EXAMPLES.glob("*.input.json")):
        stem = inp.name[: -len(".input.json")]
        out = inp.with_name(stem + ".output.json")
        pairs.append((stem, _card_of(stem), json.loads(inp.read_text(encoding="utf-8")),
                      json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None))
    return pairs


EXAMPLE_PAIRS = load_examples()
PRESENT_PAIRS = [p for p in EXAMPLE_PAIRS if p[1] != "understand"]
UNDERSTAND_PAIRS = [p for p in EXAMPLE_PAIRS if p[1] == "understand"]


@pytest.fixture(scope="module")
def pkg():
    return load_skill()


# ---------------------------------------------------------------- package

def test_load_skill_and_prompts_written(pkg):
    assert set(pkg.cards) == {"understand", *PRESENT_CARDS}
    for name, card in pkg.cards.items():
        assert "TODO" not in card.prompt, name
        assert card.prompt.startswith(f"# {name}\n"), name
        assert pkg.system_prompt(name)
    assert "TODO" not in pkg.voice


@pytest.mark.parametrize("card", list(PROMPT_BUDGET))
def test_prompt_length_budget(pkg, card):
    lines = pkg.card(card).prompt.strip().splitlines()
    assert len(lines) <= PROMPT_BUDGET[card], f"{card}: {len(lines)} lines"


def test_prompts_mention_only_allowed_patch_paths(pkg):
    pattern = re.compile(rf"\b(?:{'|'.join(BRIEF_TOPS)})\.[a-z_]+\b")
    allowed = set(ALLOWED_PATCH_PATHS)
    for name, card in pkg.cards.items():
        for path in set(pattern.findall(card.prompt)):
            assert path in allowed, f"{name} mentions {path}"


def test_understand_prompt_lists_every_patch_path(pkg):
    prompt = pkg.card("understand").prompt
    for path in ALLOWED_PATCH_PATHS:
        assert path in prompt, path


def test_voice_has_do_dont_pairs(pkg):
    lines = [l.strip() for l in pkg.voice.splitlines()]
    dont = [i for i, l in enumerate(lines) if l.startswith("Не так:")]
    do = [i for i, l in enumerate(lines) if l.startswith("Так:")]
    assert 6 <= len(dont) <= 10 and len(do) == len(dont)
    assert all(i + 1 in do for i in dont), "every «Не так:» line is followed by a «Так:» line"
    assert "«вы»" in pkg.voice


def test_upsell_kinds_are_mock_products(pkg):
    data = json.loads((SKILL / "upsell.json").read_text(encoding="utf-8"))
    rules = data["rules"] + data.get("rules_pending", [])
    kinds = {r["kind"] for r in rules}
    assert kinds <= UPSELL_ALLOWED
    assert kinds == UPSELL_ALLOWED, "every mock add-on kind has at least one rule"
    assert all("placeholder" not in r and r.get("offer") for r in rules)
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))


def test_rules_thresholds(pkg):
    r = pkg.rules
    assert r["image"] == {"place_min_conf": 0.8, "confirm_min_conf": 0.5}
    assert r["max_questions"] == 2 and r["max_upsell"] == 2
    g = r["guard"]
    assert g["max_jokes"] == r["max_jokes"] == 1 and g["max_emoji"] == 2
    assert set(g["message_chars"]) == set(PRESENT_CARDS)
    for lo, hi in g["message_chars"].values():
        assert 0 < lo < hi <= 1500
    for key in ("routing", "ask_or_assume", "guard_fail", "segment", "district"):
        assert 0 <= r["jev"][key] <= 1


def test_segment_defaults(pkg):
    s = pkg.segments
    assert s["solo"]["party"] == {"adults": 1, "children": 0}
    assert s["couple"]["party"] == {"adults": 2, "children": 0}
    assert s["family"]["party"]["children"] >= 1
    assert s["friends"]["party"]["adults"] >= 3
    for seg in s.values():
        assert seg["jev_criteria"].strip() and seg["jev_district"].strip()
    district = json.loads((SKILL / "segments.json").read_text(encoding="utf-8"))["district"]
    assert set(district) == {"lively", "quiet", "family", "walkable"}


def test_place_confirm_question():
    data = json.loads((SKILL / "questions.json").read_text(encoding="utf-8"))
    pc = data["place_confirm"]
    assert pc["field"] in ALLOWED_PATCH_PATHS and "{place}" in pc["text"]
    for q in data["questions"].values():
        assert q.get("hint") and all(c.get("chip") for c in q["choices"])


# ---------------------------------------------------------------- examples

def test_example_counts():
    by_card = {}
    for _, card, _, out in EXAMPLE_PAIRS:
        assert out is not None
        by_card[card] = by_card.get(card, 0) + 1
    for card in BINDING_KEYS:
        assert by_card.get(card, 0) >= MIN_EXAMPLES.get(card, 3), card


@pytest.mark.parametrize("stem, card, inp, out", EXAMPLE_PAIRS, ids=[p[0] for p in EXAMPLE_PAIRS])
def test_example_input_binding_keys(stem, card, inp, out):
    assert BINDING_KEYS[card] <= set(inp), BINDING_KEYS[card] - set(inp)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", inp["today"])
    Brief.model_validate({k: {"value": v} for k, v in inp["brief"].items()})
    assert all({"field", "label"} == set(a) for a in inp["assumptions"])
    if card == "present.destinations":
        for item in inp["items"]:
            assert set(item) == DEST_ITEM_KEYS, stem
    if card in ("present.variants", "present.update", "present.compare"):
        for item in inp["items"]:
            assert set(item) == VARIANT_ITEM_KEYS, stem
            assert all(set(s) == STEP_KEYS for s in item["steps"]), stem
            assert set(item["totals"]) == {"rub", "door_to_door_hours"}
    if card in ("present.variants", "present.update"):
        for c in inp["upsell_candidates"]:
            assert set(c) == {"candidate_id", "kind", "label", "price_rub", "reason"}
            assert c["kind"] in UPSELL_ALLOWED
    if card == "understand":
        for s in inp["shown"]:
            assert set(s) == {"label", "kind", "id", "title"}
        for q in inp["pending_questions"]:
            assert set(q) == {"id", "field", "text", "choices"} and q["field"] in ALLOWED_PATCH_PATHS


@pytest.mark.parametrize("stem, card, inp, out", EXAMPLE_PAIRS, ids=[p[0] for p in EXAMPLE_PAIRS])
def test_example_output_validates(pkg, stem, card, inp, out):
    jsonschema.validate(out, pkg.card(card).schema)
    assert list(out) == list(pkg.card(card).schema["required"]), "fields in schema order"


@pytest.mark.parametrize("stem, card, inp, out", UNDERSTAND_PAIRS, ids=[p[0] for p in UNDERSTAND_PAIRS])
def test_understand_example_semantics(stem, card, inp, out):
    brief = Brief.model_validate({k: {"value": v} for k, v in inp["brief"].items()})
    apply_patch(brief, out["patch"])          # the patch applies cleanly
    labels = {s["label"] for s in inp["shown"]} | {s["id"] for s in inp["shown"]}
    qids = {q["id"] for q in inp["pending_questions"]}
    for ref in out["refs"]:
        assert ref["ref"] in (qids if ref["kind"] == "question" else labels), ref
    assert (out["image"] is None) == (not inp["has_image"])
    if out["intent"] in ("out_of_scope", "smalltalk"):
        assert out["patch"] == {"set": {}, "unset": []} and out["refs"] == []
    if out["intent"] == "post_purchase":
        assert inp["stage"] in ("ordered", "aftercare") and out["post_purchase"] is not None
    if out["image"] and re.search(r"так же|похож|в таком духе", inp["message"]):
        assert out["image"]["place"] is None


@pytest.mark.parametrize("stem, card, inp, out", PRESENT_PAIRS, ids=[p[0] for p in PRESENT_PAIRS])
def test_present_numbers_come_from_input(stem, card, inp, out):
    allowed = numbers_in_json(inp)
    for text in output_texts(out):
        for n in numbers_in_text(text):
            assert n in allowed, f"{stem}: {n} in «{text}» is not in the input"


@pytest.mark.parametrize("stem, card, inp, out", PRESENT_PAIRS, ids=[p[0] for p in PRESENT_PAIRS])
def test_present_ids_come_from_input(stem, card, inp, out):
    item_ids = [i["id"] for i in inp.get("items", [])]
    out_ids = [i["id"] for i in out["items"]]
    if card in ("present.destinations", "present.variants", "present.update", "present.compare"):
        assert out_ids == item_ids, "one output item per input item, same order"
    else:
        assert set(out_ids) <= set(item_ids)
    assert out["recommended_id"] is None or out["recommended_id"] in item_ids
    candidates = {c["candidate_id"] for c in inp.get("upsell_candidates", [])}
    assert {u["candidate_id"] for u in out["upsell"]} <= candidates
    if card == "present.variants" and inp["recommended_hint"] in item_ids:
        budget = (inp["brief"].get("budget_rub") or {}).get("max")
        hinted = next(i for i in inp["items"] if i["id"] == inp["recommended_hint"])
        if not budget or hinted["totals"]["rub"] <= budget:
            assert out["recommended_id"] == inp["recommended_hint"]


@pytest.mark.parametrize("stem, card, inp, out", PRESENT_PAIRS, ids=[p[0] for p in PRESENT_PAIRS])
def test_present_voice_guards(pkg, stem, card, inp, out):
    g = pkg.rules["guard"]
    msg = out["message"]
    lo, hi = g["message_chars"][card]
    assert lo <= len(msg) <= hi, f"{stem}: {len(msg)} chars"
    full = " ".join(output_texts(out))
    assert len(_EMOJI.findall(full)) <= g["max_emoji"]
    assert msg.count("!") <= g["max_exclamations"]
    for phrase in g["banned_phrases"]:
        assert phrase.casefold() not in full.casefold(), phrase
    if card in g["question_mark_needs_questions"] and not inp.get("questions"):
        assert "?" not in msg, f"{stem}: asks a question the engine did not ask"
    for item in out["items"]:
        source = next((i for i in inp.get("items", []) if i["id"] == item["id"]), None)
        if source and "sacrifices" in item:
            for key in ("sacrifices", "risks", "not_for"):
                assert len(item[key]) == len(source[key]), f"{stem}: {item['id']}.{key} count differs from input notes"


def test_numbers_helper():
    assert numbers_in_text("90 300 ₽ и 6,5 ч, до 100к, 2 500 ₽, 100 тыс.") == [90300, 6.5, 100000, 2500, 100000]
    assert numbers_in_text("в 06:40, 12–19 ноября") == [6, 40, 12, 19]
    assert 2026 in numbers_in_json({"d": "2026-11-12"}) and 12 in numbers_in_json({"d": "2026-11-12"})
