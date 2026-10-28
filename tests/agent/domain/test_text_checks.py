"""Extension checks run after built-in guards and also gate the guide's reply."""
from types import SimpleNamespace

from agent import claims, guards, guide
from agent.skill_loader import load_skill


def output(message="Вот варианты."):
    return {"message": message, "items": [], "recommended_id": None, "upsell": []}


def issue(code, severity="block"):
    return {"code": code, "severity": severity, "message": "Проверка из реестра", "data": {}}


def test_registered_checks_receive_inputs_and_keep_issue_order(monkeypatch):
    card_input, result, rules = {}, output(""), {}
    seen = []

    def first(*args):
        seen.append(args)
        return [issue("first", "warn"), issue("second")]

    def last(*args):
        seen.append(args)
        return [issue("last")]

    assert guards.TEXT_CHECKS is claims.TEXT_CHECKS
    monkeypatch.setattr(guards, "TEXT_CHECKS", [first, last])
    found = guards.check_card_output("present.info", card_input, result, rules)
    assert [i["code"] for i in found] == ["message_empty", "first", "second", "last"]
    assert len(seen) == 2
    for args in seen:
        assert args[0] == "present.info"
        assert args[1] is card_input and args[2] is result and args[3] is rules


def test_non_applicable_check_leaves_builtin_verdict_unchanged(monkeypatch):
    result = output("Цена — 987654321 ₽.")
    expected = guards.check_card_output("present.info", {}, result, {})
    monkeypatch.setattr(guards, "TEXT_CHECKS", [lambda *args: []])
    assert guards.check_card_output("present.info", {}, result, {}) == expected
    assert "invented_number" in [i["code"] for i in expected]


def test_registering_in_claims_adds_check_to_guards():
    marker = issue("registered_in_claims")

    def check(*args):
        return [marker]

    claims.TEXT_CHECKS.append(check)
    try:
        assert marker in guards.check_card_output("present.info", {}, output(), {})
    finally:
        claims.TEXT_CHECKS.remove(check)


def test_guide_reply_uses_registered_text_checks(monkeypatch):
    rules = load_skill().rules
    pool = {"items": [], "questions": []}
    reply = {"message": "Проверяю условия поездки и предлагаю подходящий вариант.", "question": None}
    plan = guide.Plan("none", "present.info")
    marker = issue("extension_block")
    seen = []

    def check(card, card_input, result, actual_rules):
        seen.append((card, card_input, result, actual_rules))
        return [marker]

    monkeypatch.setattr(guards, "TEXT_CHECKS", [check])
    found = guide.check_reply(SimpleNamespace(skill=SimpleNamespace(rules=rules)),
                             SimpleNamespace(changes=[]), reply, plan, pool, [])
    assert any(i["code"] == "extension_block" and i["severity"] == "block" for i in found)
    assert len(seen) == 1
    card, card_input, result, actual_rules = seen[0]
    assert card == "present.info" and card_input is pool and actual_rules is rules
    assert result == output(reply["message"])
