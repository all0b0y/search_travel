"""agent/cli.py: rendering of every fixture, numbered choices, REPL, one-shot mode (offline)."""
import base64
import json

import pytest

from agent import cli
from agent.server import FIXTURE_ORDER, FixtureEngine

from .conftest import FakeEngine, fixture


@pytest.mark.parametrize("name", FIXTURE_ORDER)
def test_every_fixture_renders(name):
    resp = fixture(name)
    text, numbered = cli.render(resp)
    assert resp["message"] in text
    for a in resp["actions"]:
        assert a["label"] in text
    for d in resp["cards"]["destinations"]:
        assert d["name"] in text and d["why"] in text
    for v in resp["cards"]["variants"]:
        assert v["title"] in text and v["why"] in text
    for q in resp["questions"]:
        assert q["text"] in text
    if resp.get("error"):
        assert resp["error"]["code"] in text
    expected = sum(len(q["choices"]) for q in resp["questions"]) + len(resp["actions"]) + sum(
        sum(1 for o in c["options"] if o["value"] != c["value"]) for c in resp["chips"])
    assert len(numbered) == expected
    for action in numbered:
        assert set(action) == {"type", "payload"}


def test_inspiration_numbers_choices_and_marks_recommended():
    text, numbered = cli.render(fixture("inspiration"))
    assert "2) 2–6 лет  ★ рекомендуем" in text
    assert numbered[1] == {"type": "answer_question", "payload": {"question_id": "q_children_ages", "value": [4]}}
    chip_actions = [a for a in numbered if a["type"] == "set_chip"]
    assert {"type": "set_chip", "payload": {"field": "origin.city", "value": "Санкт-Петербург"}} in chip_actions
    assert "погод" not in text or "воздух" in text
    assert "Анталья, Турция — от 118 400 ₽" in text


def test_selection_and_ordered_details():
    text, _ = cli.render(fixture("selection"))
    assert "«Быстрее» ★" in text and "район: спокойно" in text and "перелёт 12.11 06:40" in text
    text, _ = cli.render(fixture("ordered"))
    assert "PNR-EX7K2Q" in text and "/files/o_example_1/trip.ics" in text
    text, _ = cli.render(fixture("comparison"))
    assert "Цена за двоих: 56 400 ₽ ✓" in text


def test_debug_summary_line():
    resp = fixture("selection")
    # the engine's record shapes: calls carry `cost` and `tokens`; hook verdicts ok|warn|block|error
    resp["debug"] = {"stage_path": ["understand", "present.variants"], "hooks": [{"verdict": "block"}, {"verdict": "ok"}],
                     "jev": [], "flags": {}, "timings_ms": {"total": 3990},
                     "calls": [{"latency_ms": 900, "cost": 0.0002, "ok": True},
                               {"latency_ms": 2000, "tokens": {"cost_usd": 0.0007}, "ok": False}]}
    text, _ = cli.render(resp)
    assert ("[debug] calls 2 · model 2900 ms · $0.00090 · total 3990 ms · calls failed 1 · hooks failed 1"
            " · understand→present.variants") in text
    resp["debug"]["example"] = True
    assert "[debug · ПРИМЕР, не прогон]" in cli.render(resp)[0]


def args(**kw):
    base = dict(profile="demo", debug=True, flags=None, image=None, backend=None, fixtures=True, now=None,
                json=False, message=None)
    base.update(kw)
    return type("Args", (), base)


def test_repl_number_triggers_action_and_checkout_prompts(capsys):
    engine = FixtureEngine()
    inputs = iter(["к морю в ноябре", "2", "8", "/quit"])
    prompts = []

    def read(prompt=""):
        prompts.append(prompt)
        return next(inputs)
    out = []
    cli.repl(engine, args(), read=read, write=out.append)
    sid = next(iter(engine.sessions))
    requests = engine.sessions[sid]["requests"]
    assert requests[0]["text"] == "к морю в ноябре"
    assert requests[1]["action"] == {"type": "answer_question",
                                     "payload": {"question_id": "q_children_ages", "value": [4]}}
    # selection fixture: 1-5 are chip options, 6-9 the actions; 8 is "Беру «Комфортнее»"
    assert requests[2]["action"] == {"type": "choose_variant", "payload": {"variant_id": "comfort"}}
    assert any("[debug" in line for line in out)


def test_repl_checkout_fills_travellers():
    engine = FixtureEngine(order=("cart", "checkout_issues"))
    inputs = iter(["в корзину", "1", "Иван Петров 1990-05-01; Мария Петрова 1992-07-12", "ivan@example.org",
                   "/quit"])
    out = []
    cli.repl(engine, args(debug=False), read=lambda p="": next(inputs), write=out.append)
    sid = next(iter(engine.sessions))
    action = engine.sessions[sid]["requests"][1]["action"]
    assert action == {"type": "checkout", "payload": {
        "travellers": [{"name": "Иван Петров", "birth_date": "1990-05-01"},
                       {"name": "Мария Петрова", "birth_date": "1992-07-12"}],
        "contact": {"email": "ivan@example.org"}}}


def test_repl_rejects_invalid_requests_locally():
    engine = FakeEngine()
    inputs = iter(['/action {"type": "choose_variant", "payload": {"variant_id": "nope"}}', "9", "/quit"])
    out = []
    cli.repl(engine, args(fixtures=False, debug=False), read=lambda p="": next(inputs), write=out.append)
    assert engine.requests == []
    assert any("не по договору" in line for line in out)
    assert any("нет такого пункта" in line for line in out)


def test_one_shot_with_image_and_json(tmp_path, capsys, monkeypatch):
    image = tmp_path / "beach.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    engine = FakeEngine(answer="inspiration")
    monkeypatch.setattr(cli, "make_engine", lambda a: engine)
    assert cli.main(["--fixtures", "--image", str(image), "--json", "хочу сюда"]) == 0
    sid, request = engine.requests[0]
    assert request["text"] == "хочу сюда"
    assert request["image"] == "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
    printed = capsys.readouterr().out
    assert json.loads(printed)["stage"] == "inspiration"


def test_bad_image_type_is_refused(tmp_path):
    bad = tmp_path / "x.bmp"
    bad.write_bytes(b"BM")
    with pytest.raises(SystemExit):
        cli.image_data_url(bad)
