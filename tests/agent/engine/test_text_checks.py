"""A registered blocking check makes the guide fix its reply before it is shown."""
import pytest

from agent import guards

from .fakes import u
from .test_guide import GUIDE, ISTANBUL, Script, guide_trace, reply
from .test_scopes import live  # noqa: F401 - the fixture


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_extension_blocks_guide_reply_and_accepts_fix(live, monkeypatch):
    h = live
    blocked = "Непроверенное утверждение об условиях поездки."
    fixed = "Предлагаю выбрать подходящий вариант поездки из карточек ниже."

    def check(card, card_input, output, rules):
        if output.get("message") == blocked:
            return [{"code": "extension_block", "severity": "block",
                     "message": "Утверждение не подтверждено", "data": {}}]
        return []

    monkeypatch.setattr(guards, "TEXT_CHECKS", [check])
    monkeypatch.setitem(h.engine.skill.rules["guide"], "speculative_present", False)

    def fix(context, req):
        assert "extension_block" in req.system
        return reply(fixed, rec=context["default_plan"]["recommended_id"])

    script = Script(h, lambda c, req: reply(blocked, rec=c["default_plan"]["recommended_id"]), fix)
    h.understand(u(set=ISTANBUL))
    result = h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert not result.get("error")
    assert result["message"] == fixed
    assert len(script.requests) == 2 and guide_trace(result)["used"] is True
    assert any(hook["verdict"] == "block" and any(i["code"] == "extension_block" for i in hook["issues"])
               for hook in result["debug"]["hooks"])
