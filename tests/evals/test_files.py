"""Every case and scenario file validates; case inputs match the binding card-input shapes."""
import json
import re
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

from agent.skill_loader import load_skill
from evals.assertions import ASSERTION_TYPES, PREDICATES as CASE_PREDICATES
from evals.card_inputs import CARDS, input_keys, validate_card_input
from evals.common import CASES_DIR, HOLDOUT_DIR, ROOT, SCENARIOS_DIR, load_schema
from evals.predicates import PREDICATES as MUST_PREDICATES

CASES = sorted(CASES_DIR.glob("*.json"))
SCENARIOS = sorted(SCENARIOS_DIR.glob("*.json"))
HOLDOUT = sorted(HOLDOUT_DIR.glob("*.json"))


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_counts_and_categories():
    assert len(CASES) == 47   # 40 v0 cases + 7 for the L3-fix understand rules (seasons, wishes, ranges, prefs, new_dates)
    cats = Counter(load(p)["category"] for p in CASES)
    assert cats == {"A": 8, "A_photo": 3, "B": 5, "C": 3, "D": 3, "E": 6, "F": 4, "segments": 4, "edge": 6, "tone": 5}
    assert len(SCENARIOS) == 25
    assert len(HOLDOUT) == 10


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_valid(path):
    case = load(path)
    jsonschema.validate(case, load_schema("case.schema.json"))
    assert case["id"] == path.stem
    assert case["runs"] == 5
    assert set(case["input"]) == input_keys(case["card"])
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
        assert load(p)["card"] in skill.cards


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
