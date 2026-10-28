"""Every case and scenario file validates; case inputs match the binding card-input shapes."""
import json
import re
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

from agent.skill_loader import load_skill
from evals.assertions import ASSERTION_TYPES, PREDICATES as CASE_PREDICATES
from evals.card_inputs import CARDS, OPTIONAL_KEYS, input_keys, validate_card_input
from evals.common import CASES_DIR, HOLDOUT_DIR, ROOT, SCENARIOS_DIR, load_schema
from evals.predicates import PREDICATES as MUST_PREDICATES

CASES = sorted(CASES_DIR.glob("*.json"))
SCENARIOS = sorted(SCENARIOS_DIR.glob("*.json"))
HOLDOUT = sorted(HOLDOUT_DIR.glob("*.json"))


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_counts_and_categories():
    # 40 v0 cases + 7 for the L3-fix understand rules (seasons, wishes, ranges, prefs, new_dates)
    # + 5 present.info pet cases (evals/authoring/cases_pets.py)
    # + 12 location / best-time / named-place cases (evals/authoring/cases_location.py)
    # + 5 held-out-2 cases: no_hostel, trains in a budget, price_includes, other places (cases_holdout2.py)
    # + 10 honesty cases: open dates, unsold services, night trains, transport gaps, kitchen (cases_honesty.py)
    # + 2 long-distance train cases: a variants card and a budget inspiration (cases_long_trains.py)
    # + 5 dead-end / pet-fee / budget cases: a no-flight trip by train, a plane-fixed trip, a cart pet line, two
    #   places in the budget, «а с пересадкой?» after nothing built (cases_dead_ends.py)
    # + 6 guide-mode cases: savings, «ещё дешевле», a dead end, a vague wish, weather, another pair (cases_guide.py)
    # + 2 daytime-arrival recommendations: relax and older travellers (cases_night_arrival.py)
    assert len(CASES) == 94
    cats = Counter(load(p)["category"] for p in CASES)
    assert cats == {"A": 11, "A_photo": 5, "B": 11, "C": 10, "D": 5, "E": 16, "F": 10, "segments": 4, "edge": 11,
                    "tone": 5, "guide": 6}
    assert len(SCENARIOS) == 25
    assert len(HOLDOUT) == 10


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_valid(path):
    case = load(path)
    jsonschema.validate(case, load_schema("case.schema.json"))
    assert case["id"] == path.stem
    assert case["runs"] == 5
    optional = set(OPTIONAL_KEYS.get(case["card"], ()))   # e.g. `question`: present only when asked
    assert set(case["input"]) - optional == input_keys(case["card"])
    assert validate_card_input(case["card"], case["input"]) == []
    if case["image"]:
        assert (ROOT / case["image"]).is_file()
    assert case["expect"][0]["type"] == "schema"
    for a in case["expect"]:
        assert a["type"] in ASSERTION_TYPES
        if a["type"] == "predicate":
            assert a["name"] in CASE_PREDICATES, a["name"]


def test_case_cards_exist_in_skill():
    skill = load_skill()
    for p in CASES:
        assert skill.card(load(p)["card"])   # turns.json cards and the guide card (rules.json "guide")


def test_photo_cases_have_images_and_flag():
    for p in CASES:
        c = load(p)
        if c["category"] == "A_photo":
            assert c["image"] and c["input"]["has_image"] is True


@pytest.mark.parametrize("path", SCENARIOS + HOLDOUT, ids=lambda p: p.stem)
def test_scenario_valid(path):
    s = load(path)
    jsonschema.validate(s, load_schema("scenario.schema.json"))
    assert s["id"] == path.stem
    for m in s["persona"]["hidden"]["must"]:
        assert m["name"] in MUST_PREDICATES, m["name"]
    if s["persona"]["image"]:
        assert (ROOT / s["persona"]["image"]).is_file()
    ages = s["persona"]["profile"]["travellers"]
    assert ages, "checkout needs travellers"


def test_scenario_coverage():
    cats = Counter(load(p)["category"] for p in SCENARIOS)
    for needed in ("A", "A_photo", "B", "C", "D", "E", "F", "family", "friends", "solo", "couple", "pets", "budget",
                   "out_of_catalog"):
        assert cats[needed] >= 1, needed
    assert sum(1 for p in SCENARIOS if load(p)["persona"]["image"]) == 2


def _ngrams(text, n=4):
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def test_holdout_never_reuses_scenario_phrases():
    main = set().union(*(_ngrams(load(p)["persona"]["start_message"]) for p in SCENARIOS))
    main_ids = {load(p)["id"] for p in SCENARIOS}
    for p in HOLDOUT:
        h = load(p)
        assert h["id"] not in main_ids
        assert not (_ngrams(h["persona"]["start_message"]) & main), h["id"]


def test_all_cards_have_input_shape():
    for card in CARDS:
        assert {"today", "stage", "brief", "assumed", "assumptions"} <= input_keys(card)


def test_validate_card_input_rejects_bad_shapes():
    case = load(CASES_DIR / "c2_sochi_variants.json")
    good = case["input"]
    assert validate_card_input("present.variants", good) == []
    extra = dict(good, foo=1)
    assert any("extra keys" in e for e in validate_card_input("present.variants", extra))
    missing = {k: v for k, v in good.items() if k != "recommended_hint"}
    assert any("missing keys" in e for e in validate_card_input("present.variants", missing))
    bad_brief = dict(good, brief=dict(good["brief"], party={"segment": "aliens", "adults": 2}))
    assert validate_card_input("present.variants", bad_brief)
    bad_step = json.loads(json.dumps(good))
    del bad_step["items"][0]["steps"][0]["carrier"]
    assert validate_card_input("present.variants", bad_step)
    bad_upsell = json.loads(json.dumps(good))
    bad_upsell["upsell_candidates"] = [{"candidate_id": "u", "variant_id": "fastest", "kind": "insurance",
                                        "label": "x", "price_rub": None, "reason": "r"}]
    assert validate_card_input("present.variants", bad_upsell)
    assert validate_card_input("present.nope", good) == ["unknown card: present.nope"]


def test_images_sources_listed():
    sources = (ROOT / "evals/images/SOURCES.md").read_text(encoding="utf-8")
    images = [p for p in (ROOT / "evals/images").iterdir() if p.suffix == ".jpg"]
    assert 1 <= len(images) <= 6
    for img in images:
        assert img.name in sources
