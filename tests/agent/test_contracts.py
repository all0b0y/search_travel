import json

import jsonschema
import pytest

from agent.state import ACTION_TYPES, ALLOWED_PATCH_PATHS, STAGES

FIXTURES = ("inspiration", "selection", "comparison", "cart", "checkout_issues", "ordered", "aftercare", "error")


def load_fixture(root, name):
    return json.loads((root / "agent/contracts/fixtures" / f"{name}.json").read_text(encoding="utf-8"))


def test_schemas_are_draft07(response_schema, request_schema):
    for schema in (response_schema, request_schema):
        assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
        jsonschema.Draft7Validator.check_schema(schema)


def test_fixture_set_is_complete(root):
    names = sorted(p.stem for p in (root / "agent/contracts/fixtures").glob("*.json"))
    assert names == sorted(FIXTURES)


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_validates(root, response_schema, name):
    jsonschema.Draft7Validator(response_schema).validate(load_fixture(root, name))


def test_fixture_numbers_consistent(root):
    sel = load_fixture(root, "selection")
    for v in sel["cards"]["variants"]:
        assert v["totals"]["rub"] == sum(s["price_rub"] for s in v["steps"])
    cart = load_fixture(root, "cart")["cart"]
    assert cart["total_rub"] == sum(i["price_rub"] for i in cart["items"])
    for name in ("checkout_issues", "ordered", "aftercare"):
        order = load_fixture(root, name)["order"]
        assert order["total_rub"] == sum(i["price_rub"] for i in order["items"])
    assert load_fixture(root, "error")["error"]["code"] == "tool_unavailable"


def test_enums_match_python(response_schema):
    assert tuple(response_schema["properties"]["stage"]["enum"]) == STAGES
    action_types = [ref["$ref"].rsplit("_", 1)[0] for ref in response_schema["definitions"]["action"]["oneOf"]]
    types = tuple(response_schema["definitions"][f"action_{t}"]["properties"]["type"]["const"] for t in ACTION_TYPES)
    assert types == ACTION_TYPES and len(action_types) == len(ACTION_TYPES)
    assert tuple(response_schema["definitions"]["question"]["properties"]["field"]["enum"]) == ALLOWED_PATCH_PATHS


def test_action_payloads_shared_with_request(response_schema, request_schema):
    for t in ACTION_TYPES:
        if t == "checkout":
            continue  # response: what is known so far; request: complete traveller data
        assert response_schema["definitions"][f"payload_{t}"] == request_schema["definitions"][f"payload_{t}"], t


def test_response_rejects_bad_documents(root, response_schema):
    v = jsonschema.Draft7Validator(response_schema)
    doc = load_fixture(root, "selection")
    assert not list(v.iter_errors(doc))
    bad_stage = dict(doc, stage="shopping")
    assert list(v.iter_errors(bad_stage))
    too_many_q = dict(doc, questions=[load_fixture(root, "inspiration")["questions"][0]] * 3)
    assert list(v.iter_errors(too_many_q))
    bad_action = dict(doc, actions=[{"type": "teleport", "label": "x", "payload": {}}])
    assert list(v.iter_errors(bad_action))
    no_offset = json.loads(json.dumps(doc))
    no_offset["cards"]["variants"][0]["steps"][0]["start"] = "2026-11-12T06:40"
    assert list(v.iter_errors(no_offset))
    debug = dict(doc, debug={"stage_path": ["selection"], "calls": [], "hooks": [], "timings_ms": {"total": 812},
                             "jev": [], "flags": {"sensitivity": True}})
    assert not list(v.iter_errors(debug))


@pytest.mark.parametrize("body", [
    {"session_id": "s_abc", "text": "хочу на море в ноябре"},
    {"session_id": "s_abc", "image": "data:image/jpeg;base64,/9j/4AAQ"},
    {"session_id": "s_abc", "action": {"type": "choose_variant", "payload": {"variant_id": "fastest"}}},
    {"session_id": "s_abc", "action": {"type": "answer_question", "payload": {"question_id": "q_party", "value": "couple"}}},
    {"session_id": "s_abc", "action": {"type": "set_chip", "payload": {"field": "prefs.meals", "value": "breakfast"}}},
    {"session_id": "s_abc", "action": {"type": "add_to_cart", "payload": {"variant_id": "comfort", "extras": ["tr_1"]}}},
    {"session_id": "s_abc", "action": {"type": "checkout", "payload": {
        "travellers": [{"name": "Иван Петров", "birth_date": "1990-04-01"}], "contact": {"email": "ivan@example.com"}}}},
    {"session_id": "s_abc", "action": {"type": "order_action", "payload": {
        "order_id": "o_1", "kind": "early_checkin", "params": {"time": "10:00"}}}},
])
def test_request_valid(request_schema, body):
    jsonschema.Draft7Validator(request_schema).validate(body)


@pytest.mark.parametrize("body", [
    {"session_id": "s_abc"},
    {"text": "привет"},
    {"session_id": "s_abc", "image": "https://example.com/x.jpg"},
    {"session_id": "s_abc", "action": {"type": "checkout", "payload": {"travellers": [], "contact": {"email": "a@b"}}}},
    {"session_id": "s_abc", "action": {"type": "compare", "payload": {"variant_ids": ["cheapest"]}}},
    {"session_id": "s_abc", "action": {"type": "set_chip", "payload": {"field": "hotel.name", "value": 1}}},
    {"session_id": "s_abc", "text": "x", "extra": 1},
])
def test_request_invalid(request_schema, body):
    assert list(jsonschema.Draft7Validator(request_schema).iter_errors(body))
