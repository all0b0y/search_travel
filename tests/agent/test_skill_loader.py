import json
import shutil

import jsonschema
import pytest

from agent.skill_loader import SkillLoadError, load_skill
from agent.state import (ACTION_TYPES, ALLOWED_PATCH_PATHS, ALLOWED_UNSET_PATHS, DESTINATION_TRAITS,
                         IMAGE_CLIMATE, IMAGE_LANDSCAPES, IMAGE_SETTINGS, IMAGE_VIBES, IMAGE_WATER, INTENTS,
                         MOODS, POST_PURCHASE_KINDS, REF_KINDS, Brief, apply_patch)
from agent.steps_spec import STEP_DOCS, STEP_NAMES

PRESENT_CARDS = ("present.destinations", "present.variants", "present.compare", "present.update",
                 "present.info", "present.aftercare", "present.fallback")


@pytest.fixture
def pkg():
    return load_skill()


@pytest.fixture
def skill_copy(tmp_path, skill_dir):
    target = tmp_path / "trip-agent"
    shutil.copytree(skill_dir, target)
    return target


def edit_json(path, fn):
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_step_names_documented():
    assert STEP_NAMES == tuple(STEP_DOCS)
    assert len(set(STEP_NAMES)) == len(STEP_NAMES)
    assert all(STEP_DOCS[s].strip() for s in STEP_NAMES)


def test_real_skeleton_loads(pkg):
    assert set(pkg.cards) == {"understand", *PRESENT_CARDS}
    assert pkg.card("understand").voice is False
    assert all(pkg.card(c).voice for c in PRESENT_CARDS)
    assert pkg.routes[-1].when == {} and pkg.routes[-1].present == "present.fallback"
    assert pkg.rules["max_questions"] == 2 and pkg.rules["max_upsell"] == 2 and pkg.rules["max_jokes"] == 1
    assert pkg.rules["jev"]["routing"] == 0.8
    assert set(pkg.segments) == {"solo", "couple", "family", "friends"}


def test_load_from_relative_default_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load_skill().root.name == "trip-agent"


def test_buffers_copied_from_trip_planner(pkg, root):
    old = json.loads((root / "skills/trip-planner/rules.json").read_text(encoding="utf-8"))
    for key in ("buffers_min", "taxi_time_factor", "risk_slack_min", "late_checkin", "meeting_window_min"):
        assert pkg.rules[key] == old[key]


def test_system_prompt_composition(pkg):
    text = pkg.system_prompt("present.variants")
    assert pkg.card("present.variants").prompt.strip() in text
    assert pkg.voice.strip() in text
    assert '"recommended_id"' in text
    assert pkg.voice.strip() not in pkg.system_prompt("understand")


def test_understand_schema_enums_match_python(pkg):
    schema = pkg.card("understand").schema
    patch = schema["properties"]["patch"]["properties"]
    assert tuple(patch["set"]["properties"]) == ALLOWED_PATCH_PATHS
    assert tuple(patch["unset"]["items"]["enum"]) == ALLOWED_UNSET_PATHS
    assert tuple(schema["properties"]["intent"]["enum"]) == INTENTS
    assert tuple(schema["properties"]["refs"]["items"]["properties"]["kind"]["enum"]) == REF_KINDS
    traits = schema["definitions"]["image"]["properties"]["traits"]["properties"]
    assert tuple(traits["landscape"]["items"]["enum"]) == IMAGE_LANDSCAPES
    assert tuple(traits["water"]["enum"]) == IMAGE_WATER
    assert tuple(traits["climate"]["enum"]) == IMAGE_CLIMATE
    assert tuple(traits["setting"]["enum"]) == IMAGE_SETTINGS
    assert tuple(traits["vibe"]["items"]["enum"]) == IMAGE_VIBES
    assert tuple(patch["set"]["properties"]["traits.add"]["items"]["enum"]) == DESTINATION_TRAITS
    assert tuple(patch["set"]["properties"]["mood.add"]["items"]["enum"]) == MOODS
    pp = schema["properties"]["post_purchase"]["oneOf"][1]["properties"]["kind"]["enum"]
    assert tuple(pp) == POST_PURCHASE_KINDS


def test_understand_schema_accepts_example_and_patch_applies(pkg):
    out = {"intent": "new_trip",
           "patch": {"set": {"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
                             "party.segment": "couple", "party.adults": 2, "traits.add": ["sea"]},
                     "unset": []},
           "refs": [], "image": None, "info_question": None, "post_purchase": None}
    jsonschema.validate(out, pkg.card("understand").schema)
    brief, changed = apply_patch(Brief(), out["patch"])
    assert "destination.name" in changed
    bad = dict(out, patch={"set": {"hotel.name": "x"}, "unset": []})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, pkg.card("understand").schema)
    image = dict(out, image={"want": "similar", "place": None, "traits": {"landscape": ["beach"], "water": "sea", "climate": "warm",
                                                       "setting": "resort", "vibe": ["relax"]}})
    jsonschema.validate(image, pkg.card("understand").schema)


def test_present_schemas_shape(pkg):
    for name in PRESENT_CARDS:
        schema = pkg.card(name).schema
        assert schema["required"] == ["message", "items", "recommended_id", "upsell"]
        assert schema["properties"]["upsell"]["maxItems"] <= pkg.rules["max_upsell"]
    variants = pkg.card("present.variants").schema
    item = variants["properties"]["items"]["items"]
    assert set(item["required"]) == {"id", "why", "sacrifices", "risks", "not_for"}
    example = {"message": "Вот три варианта.", "items": [
        {"id": "cheapest", "why": "Дешевле всех", "sacrifices": ["ранний рейс"], "risks": [], "not_for": []}],
        "recommended_id": "cheapest", "upsell": [{"candidate_id": "u1", "line": "Трансфер ночью"}]}
    jsonschema.validate(example, variants)


def test_routes_cover_entries_and_actions(pkg):
    sochi, _ = apply_patch(Brief(), {"set": {"destination.name": "Сочи"}})
    empty = Brief()
    m = pkg.match_route
    assert m(intent="new_trip", stage="inspiration", brief=empty).present == "present.destinations"  # A
    assert "image_match" in m(intent="new_trip", stage="inspiration", brief=empty).steps                  # A'
    assert m(intent="new_trip", stage="inspiration", brief=sochi).present == "present.variants"       # B, C, D
    assert m(intent="refine", stage="selection", brief=sochi).present == "present.update"              # E
    assert m(intent="post_purchase", stage="ordered", brief=sochi).present == "present.aftercare"      # F
    assert m(intent="choose", stage="inspiration", brief=empty).present == "present.variants"
    assert m(intent="choose", stage="selection", brief=sochi).present is None
    assert m(intent="compare", stage="selection", brief=sochi).present == "present.compare"
    assert m(intent="info", stage="selection", brief=sochi).present == "present.info"
    assert m(intent="smalltalk", stage="selection", brief=sochi).present == "present.fallback"
    assert m(intent="out_of_scope", stage="inspiration", brief=empty).present == "present.fallback"
    for intent in INTENTS:
        assert m(intent=intent, stage="inspiration", brief=empty) is not None
    for action in ACTION_TYPES:
        route = m(action=action, stage="selection", brief=sochi)
        assert route is not None and "action" in route.when, action
    assert m(action="pay", stage="checkout").steps == ("pay",)
    assert m(action="checkout", stage="cart").present is None


def test_questions_choices_fit_understand_schema(pkg):
    set_props = pkg.card("understand").schema["properties"]["patch"]["properties"]["set"]["properties"]
    assert set(pkg.questions) == {"party.segment", "origin.city", "party.children_ages", "dates.nights",
                                  "budget_rub.max"}
    for path, q in pkg.questions.items():
        for c in q["choices"]:
            jsonschema.validate(c["value"], set_props[path])


# ---------------------------------------------------------------- failures

def test_missing_prompt_fails(skill_copy):
    (skill_copy / "present" / "compare.md").unlink()
    with pytest.raises(SkillLoadError, match="present/compare.md"):
        load_skill(skill_copy)


def test_bad_step_name_fails(skill_copy):
    edit_json(skill_copy / "turns.json", lambda d: d["routes"][0]["steps"].append("teleport"))
    with pytest.raises(SkillLoadError, match="teleport"):
        load_skill(skill_copy)


def test_custom_step_names_are_used(skill_copy):
    with pytest.raises(SkillLoadError, match="unknown step"):
        load_skill(skill_copy, step_names=("apply_patch",))


def test_invalid_schema_fails(skill_copy):
    edit_json(skill_copy / "schema" / "present.info.schema.json", lambda d: d.update(type="banana"))
    with pytest.raises(SkillLoadError, match="present.info.schema.json"):
        load_skill(skill_copy)


def test_draft_2020_schema_rejected(skill_copy):
    edit_json(skill_copy / "schema" / "present.info.schema.json",
              lambda d: d.update({"$schema": "https://json-schema.org/draft/2020-12/schema"}))
    with pytest.raises(SkillLoadError, match="draft-07"):
        load_skill(skill_copy)


def test_missing_schema_file_fails(skill_copy):
    (skill_copy / "schema" / "present.update.schema.json").unlink()
    with pytest.raises(SkillLoadError, match="present.update.schema.json"):
        load_skill(skill_copy)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["cards"]["understand"].update(thinking="medium"), "thinking"),
    (lambda d: d["cards"]["understand"].update(max_tokens="900"), "max_tokens"),
    (lambda d: d["routes"][0].update(present="present.nope"), "unknown card"),
    (lambda d: d["routes"][0]["when"].update(weather=["sunny"]), "unknown condition"),
    (lambda d: d["routes"][0]["when"].update(stage=["dreaming"]), "unknown value"),
    (lambda d: d["routes"][0]["when"].update(action=["fly"]), "unknown value"),
    (lambda d: d["routes"][-1]["when"].update(has=["hotel"]), "unknown value"),
    (lambda d: d["routes"][-1]["when"].update(intent=["sing"]), "unknown value"),
    (lambda d: d["cards"].pop("understand"), "understand"),
])
def test_turns_validation(skill_copy, mutate, message):
    edit_json(skill_copy / "turns.json", mutate)
    with pytest.raises(SkillLoadError, match=message):
        load_skill(skill_copy)


@pytest.mark.parametrize("file, mutate, message", [
    ("questions.json", lambda d: d["questions"].update({"hotel.name": d["questions"]["origin.city"]}), "hotel.name"),
    ("questions.json", lambda d: d["questions"]["dates.nights"].update(choices=[]), "choices"),
    ("segments.json", lambda d: d["segments"].update(aliens=d["segments"]["solo"]), "aliens"),
    ("segments.json", lambda d: d["segments"]["solo"]["district_weights"].update(lively=0.9), "sum"),
    ("upsell.json", lambda d: d["rules"][0].update(kind="spaceship"), "spaceship"),
    ("upsell.json", lambda d: d["rules"][0]["when"].update(moon_phase="full"), "moon_phase"),
    ("rules.json", lambda d: d.pop("max_questions"), "max_questions"),
    ("rules.json", lambda d: d["jev"].update(routing=3), "jev.routing"),
])
def test_data_files_validation(skill_copy, file, mutate, message):
    edit_json(skill_copy / file, mutate)
    with pytest.raises(SkillLoadError, match=message):
        load_skill(skill_copy)


def test_broken_json_names_file(skill_copy):
    (skill_copy / "upsell.json").write_text("{", encoding="utf-8")
    with pytest.raises(SkillLoadError, match="upsell.json"):
        load_skill(skill_copy)


def test_prompts_are_written(skill_dir):
    """The skill-content builder replaced the foundation placeholders (content is checked in tests/skill)."""
    files = [skill_dir / "understand.md", skill_dir / "voice.md", *sorted((skill_dir / "present").glob("*.md"))]
    assert len(files) == 9
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "TODO: written by the skill-content builder" not in text, f
        assert len([l for l in text.splitlines() if l.strip()]) > 5, f