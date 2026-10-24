"""render_ui on every response fixture; refs resolve to request-schema actions."""
import json

import jsonschema
import pytest

from evals.common import FIXTURES_DIR, ROOT
from evals.render_ui import build_refs, render, render_user_turn

FIXTURES = sorted(FIXTURES_DIR.glob("*.json"))
REQUEST_SCHEMA = json.loads((ROOT / "agent/contracts/request.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_render_every_fixture(path):
    resp = json.loads(path.read_text(encoding="utf-8"))
    text, refs = render(resp)
    assert resp["message"] in text
    plain, _ = render(resp, with_refs=False)
    assert "[A1]" not in plain and resp["message"] in plain
    for ref, act in refs.items():
        assert f"[{ref}]" in text, ref
        req = {"session_id": "s_x", "action": {"type": act["type"], "payload": act["payload"]}}
        if act["type"] == "checkout":
            continue  # the form (travellers, contact) is filled by persona.fill_action
        jsonschema.validate(req, REQUEST_SCHEMA)


def test_selection_rendering_details():
    resp = json.loads((FIXTURES_DIR / "selection.json").read_text(encoding="utf-8"))
    text, refs = render(resp)
    assert "«Быстрее» (рекомендуем) — 90 300 ₽, в дороге 6,5 ч" in text
    assert "Чем жертвуете: Дороже самого дешёвого на 33 900 ₽" in text
    assert refs["V2"] == {"type": "choose_variant", "payload": {"variant_id": "fastest"}, "label": "Быстрее"}
    assert refs["C1.2"]["payload"] == {"field": "prefs.district", "value": "walkable"}
    assert refs["A4"]["type"] == "compare"


def test_inspiration_questions_and_destinations():
    resp = json.loads((FIXTURES_DIR / "inspiration.json").read_text(encoding="utf-8"))
    text, refs = render(resp)
    assert "Сколько лет ребёнку?" in text and "(рекомендуем)" in text
    assert refs["Q1.2"]["payload"] == {"question_id": "q_children_ages", "value": [4]}
    assert refs["D1"]["payload"] == {"destination_id": "antalya"}
    assert "Анталья, Турция — от 118 400 ₽" in text


def test_user_turn_rendering():
    assert render_user_turn({"text": "привет"}) == "Пользователь: привет"
    assert render_user_turn({"text": "сюда", "image": "data:..."}) == "Пользователь: [прислал фото] сюда"
    assert "нажал: «Быстрее»" in render_user_turn({"action": {"type": "choose_variant", "payload": {}}}, "Быстрее")
