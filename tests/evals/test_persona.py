"""Persona: fake `claude` executable (structured_output / result / malformed), click resolution, prompts."""
import json
import os
import sys
from pathlib import Path

import pytest

from evals.common import FIXTURES_DIR, ROOT, SCENARIOS_DIR
from evals.persona import (ButtonPersona, ClaudeCodePersona, FakePersona, PersonaError, parse_claude_output,
                           render_system_prompt, render_turn_prompt, resolve_reply)

FAKE = ROOT / "evals" / "testing" / "fake_claude"
SCENARIO = json.loads((SCENARIOS_DIR / "s08_sochi_exact.json").read_text(encoding="utf-8"))


def fx(name):
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def persona(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    p = ClaudeCodePersona(claude_bin=sys.executable, cwd=tmp_path / "cwd")
    (tmp_path / "cwd").mkdir()
    # run the fake script through the current interpreter
    p.command_prefix = [sys.executable, str(FAKE)]
    orig = p.command
    p.command = lambda f: [sys.executable, str(FAKE)] + orig(f)[1:]
    p.log = log
    return p


def transcript(resp):
    return [{"role": "user", "text": "Пользователь: Сочи"}, {"role": "agent", "text": "..."}]


def test_structured_output_click(persona, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", json.dumps({"type": "click", "ref": "A2"}))
    resp = fx("selection")
    out = persona.reply(SCENARIO, transcript(resp), resp)
    assert out["type"] == "action" and out["action"] == {"type": "choose_variant", "payload": {"variant_id": "fastest"}}
    assert out["label"] == "Беру «Быстрее»"
    assert out["meta"]["model"] == "claude-sonnet-5-5" and out["meta"]["duration_ms"] == 1234
    call = json.loads(persona.log.read_text(encoding="utf-8").splitlines()[0])
    argv = call["argv"]
    for flag in ("-p", "--model", "claude-sonnet-5-5", "--system-prompt-file", "--tools", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "--no-session-persistence", "--output-format",
                 "json", "--json-schema"):
        assert flag in argv, flag
    assert "--bare" not in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert not Path(call["cwd"]).resolve().is_relative_to(ROOT.resolve())
    assert "Сейчас на экране" in call["stdin"] and "[A2]" in call["stdin"]
    prompt_file = Path(argv[argv.index("--system-prompt-file") + 1])
    assert "s08_sochi_exact" in prompt_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["result", "fenced", "events"])
def test_result_forms(persona, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", json.dumps({"type": "text", "text": "а с завтраком есть?"}))
    resp = fx("selection")
    out = persona.reply(SCENARIO, transcript(resp), resp)
    assert out == {"type": "text", "text": "а с завтраком есть?", "meta": out["meta"]}


@pytest.mark.parametrize("mode", ["malformed", "error", "empty"])
def test_bad_outputs_raise(persona, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    resp = fx("selection")
    with pytest.raises(PersonaError):
        persona.reply(SCENARIO, transcript(resp), resp)


def test_invalid_reply_and_unknown_ref(persona, monkeypatch):
    resp = fx("selection")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", json.dumps({"type": "dance"}))
    with pytest.raises(PersonaError):
        persona.reply(SCENARIO, transcript(resp), resp)
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", json.dumps({"type": "click", "ref": "A9"}))
    with pytest.raises(PersonaError, match="unknown reference"):
        persona.reply(SCENARIO, transcript(resp), resp)


def test_cwd_inside_repo_is_refused():
    with pytest.raises(ValueError):
        ClaudeCodePersona(cwd=ROOT / "evals")


def test_parse_claude_output_variants():
    data, meta = parse_claude_output(json.dumps({"type": "result", "subtype": "success", "structured_output": {"type": "stop"},
                                                 "modelUsage": {"claude-sonnet-5-5": {}}}))
    assert data == {"type": "stop"} and meta["model"] == "claude-sonnet-5-5"
    with pytest.raises(PersonaError):
        parse_claude_output("[]")
    with pytest.raises(PersonaError):
        parse_claude_output(json.dumps({"type": "result", "result": "not json"}))


def test_checkout_and_pay_are_filled():
    out = resolve_reply({"type": "click", "ref": "A1"}, fx("cart"), SCENARIO)
    assert out["action"]["type"] == "checkout"
    assert out["action"]["payload"]["travellers"] == SCENARIO["persona"]["profile"]["travellers"]
    assert out["action"]["payload"]["contact"] == SCENARIO["persona"]["profile"]["contact"]
    pay = resolve_reply({"type": "click", "ref": "A1"}, fx("checkout_issues"), SCENARIO)
    assert pay["action"] == {"type": "pay", "payload": {"order_id": "o_example_1"}}


def test_prompts():
    sp = render_system_prompt(SCENARIO)
    assert "{{scenario}}" not in sp and "нужен завтрак" in sp and "must" not in sp.split("## Your scenario")[1]
    tp = render_turn_prompt([{"role": "user", "text": "Пользователь: Сочи"}, {"role": "agent", "text": "x"}],
                            fx("selection"), 1, 10)
    assert "ход 1 из 10" in tp and "[V2]" in tp


def test_fake_and_button_personas():
    fp = FakePersona([{"type": "click", "ref": "V1"}, {"type": "text", "text": "ок"}])
    r1 = fp.reply(SCENARIO, [], fx("selection"))
    assert r1["action"]["payload"] == {"variant_id": "cheapest"}
    assert fp.reply(SCENARIO, [], fx("selection"))["text"] == "ок"
    assert fp.reply(SCENARIO, [], fx("selection"))["type"] == "stop"
    bp = ButtonPersona()
    assert bp.reply(SCENARIO, [], fx("selection"))["action"]["payload"] == {"variant_id": "fastest"}
    assert bp.reply(SCENARIO, [], fx("inspiration"))["action"]["type"] == "choose_destination"
    assert bp.reply(SCENARIO, [], fx("cart"))["action"]["type"] == "checkout"
    assert bp.reply(SCENARIO, [], fx("checkout_issues"))["action"]["type"] == "pay"
    assert bp.reply(SCENARIO, [], fx("ordered"))["type"] == "stop"
    f_scen = dict(SCENARIO, stop_on_paid=False)
    assert bp.reply(f_scen, [], fx("ordered"))["action"]["type"] == "order_action"
    assert bp.reply(f_scen, [], fx("aftercare"))["type"] == "stop"
