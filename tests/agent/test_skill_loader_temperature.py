from agent.skill_loader import load_skill


def test_cards_carry_the_measured_temperature():
    skill = load_skill()
    assert skill.card("understand").temperature == 0.3
    assert all(skill.card(c).temperature == 0.6 for c in skill.cards if c.startswith("present."))
