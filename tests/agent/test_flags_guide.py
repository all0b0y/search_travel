"""The opt-in flag `guide` and the guide card (guide mode, AGENT_PLAN.md decisions 23-26)."""
from agent.flags import FLAG_NAMES, PRESETS, Flags, parse_flags
from agent.skill_loader import load_skill


def test_guide_is_opt_in_and_in_no_preset():
    assert "guide" not in FLAG_NAMES and all("guide" not in p for p in PRESETS.values())
    assert parse_flags("guide").guide is True and parse_flags("guide").preset == "stage1"
    f = parse_flags("stage3,guide")
    assert f.guide and f.jev_guard and f.enabled()[-1] == "guide"
    assert parse_flags("stage5").guide is False
    assert "guide" not in Flags().as_dict() and parse_flags("guide").as_dict()["guide"] is True
    assert parse_flags("stage3,guide,-guide").guide is False


def test_guide_card_from_rules():
    skill = load_skill()
    card = skill.card("guide")
    assert "guide" not in skill.cards                       # outside turns.json routes
    assert card.thinking == skill.rules["guide"]["thinking"] == "low"
    assert card.temperature == skill.rules["guide"]["temperature"]
    assert card.schema["required"] == ["calls", "reply"]
    system = skill.system_prompt("guide")
    assert system.startswith("# guide") and skill.voice.strip() in system
    assert len((skill.root / "guide.md").read_text(encoding="utf-8").splitlines()) <= 90
