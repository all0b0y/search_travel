import pytest

from agent.flags import FLAG_NAMES, PRESETS, Flags, parse_flags, preset


def test_presets_per_activation_order():
    assert preset("stage1").enabled() == ()
    assert preset("stage2").enabled() == ("sensitivity",)
    assert preset("stage3").enabled() == ("sensitivity", "jev_routing", "jev_ask", "jev_guard")
    assert preset("stage4").enabled() == ("sensitivity", "jev_routing", "jev_ask", "jev_guard", "prefetch")
    assert set(preset("stage5").enabled()) == set(FLAG_NAMES)
    assert list(PRESETS) == ["stage1", "stage2", "stage3", "stage4", "stage5"]


def test_parse_default_and_env(monkeypatch):
    assert parse_flags("") == Flags()
    assert parse_flags().preset == "stage1"
    monkeypatch.setenv("TRIP_AGENT_FLAGS", "stage3")
    assert parse_flags().jev_guard is True


def test_parse_modifiers_and_plain_names():
    f = parse_flags("stage3,-jev_guard,+prefetch")
    assert f.preset == "stage3" and not f.jev_guard and f.prefetch and f.sensitivity
    g = parse_flags("sensitivity, upsell")
    assert g.preset == "stage1" and g.enabled() == ("sensitivity", "upsell")
    assert parse_flags("stage5,-insights").as_dict()["insights"] is False


@pytest.mark.parametrize("spec", ["stage9", "magic", "+magic", "sensitivity,stage2"])
def test_parse_rejects_unknown(spec):
    with pytest.raises(ValueError):
        parse_flags(spec)
