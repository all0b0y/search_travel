import json
from datetime import date

import pytest

from agent.state import (ALLOWED_PATCH_PATHS, ALLOWED_UNSET_PATHS, Brief, CachedResult, Chip, DateWindow, Note,
                         PatchError, Price, Question, Session, ShownItem, Step, Totals, Variant, apply_patch)


def sochi_brief():
    brief, _ = apply_patch(Brief(), {"set": {
        "destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
        "party.segment": "couple", "party.adults": 2, "origin.city": "Москва"}})
    return brief


def test_default_brief_is_empty_with_default_source():
    brief = Brief()
    assert all(v is None for v in brief.values().values())
    assert set(brief.sources().values()) == {"default"}
    assert not brief.has("destination")


def test_apply_patch_sets_values_provenance_and_changed_paths():
    brief = Brief()
    new, changed = apply_patch(brief, {"set": {"destination.name": "Сочи", "party.adults": 2,
                                               "traits.add": ["sea", "warm"]}})
    assert new.destination.value.name == "Сочи"
    assert new.destination.source == "user" and new.party.source == "user" and new.traits.source == "user"
    assert new.dates.source == "default"
    assert changed == ["destination.name", "party.adults", "traits"]
    assert brief.destination.value is None  # input not mutated


def test_apply_patch_restating_marks_user_but_no_change():
    brief = Brief.model_validate({"party": {"value": {"segment": "couple", "adults": 2}, "source": "profile",
                                            "confidence": 0.9}})
    new, changed = apply_patch(brief, {"set": {"party.adults": 2}})
    assert changed == []
    assert new.party.source == "user" and new.party.confidence is None


def test_rejects_unknown_and_disallowed_paths():
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {"hotel.name": "x"}})
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {"destination.name": "Сочи"}}, allowed_paths=("dates.start",))
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"unset": ["dates.shift_days"]})
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {"dates.start": "2026-11-12"}, "unset": ["dates.start"]})
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {}, "other": 1})


def test_rejects_bad_values():
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {"party.segment": "aliens"}})
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {"dates.start": "12 ноября"}})
    with pytest.raises(PatchError):
        apply_patch(Brief(), {"set": {"traits.add": ["volcano"]}})


def test_shift_days_moves_dates():
    new, changed = apply_patch(sochi_brief(), {"set": {"dates.shift_days": 2}})
    assert new.dates.value.start == date(2026, 11, 14) and new.dates.value.end == date(2026, 11, 21)
    assert changed == ["dates.end", "dates.start"]


def test_shift_days_without_dates_is_noop():
    new, changed = apply_patch(Brief(), {"set": {"dates.shift_days": 2}})
    assert changed == [] and new.dates.value.start is None


def test_month_and_start_consistency():
    new, _ = apply_patch(Brief(), {"set": {"dates.start": "2026-11-12"}})
    assert new.dates.value.month == 11
    moved, changed = apply_patch(sochi_brief(), {"set": {"dates.month": 12}})
    assert moved.dates.value.start is None and moved.dates.value.end is None
    assert {"dates.month", "dates.start", "dates.end"} <= set(changed)


def test_list_ops_and_constraints():
    b, _ = apply_patch(Brief(), {"set": {"constraints.add": ["pet:dog", "no_night_flights"]}})
    b, changed = apply_patch(b, {"set": {"constraints.add": ["pet:dog"]}})
    assert changed == [] and b.constraints.value == ["pet:dog", "no_night_flights"]
    b, changed = apply_patch(b, {"set": {"constraints.remove": ["pet:dog"]}})
    assert b.constraints.value == ["no_night_flights"] and changed == ["constraints"]
    b, _ = apply_patch(b, {"unset": ["constraints.add"]})
    assert b.constraints.value is None


def test_destination_rename_resets_resolved_id():
    b = Brief.model_validate({"destination": {"value": {"id": "sochi", "name": "Сочи", "country": "Россия"},
                                              "source": "user"}})
    same, changed = apply_patch(b, {"set": {"destination.name": "сочи"}})
    assert same.destination.value.id == "sochi" and changed == []
    other, changed = apply_patch(b, {"set": {"destination.name": "Анталья"}})
    assert other.destination.value.id is None and "destination.id" in changed


def test_children_ages_sets_children_and_zero_clears_ages():
    b, _ = apply_patch(Brief(), {"set": {"party.children_ages": [4, 9]}})
    assert b.party.value.children == 2
    b, _ = apply_patch(b, {"set": {"party.children": 0}})
    assert b.party.value.children_ages is None


def test_unset_resets_to_defaults():
    b, _ = apply_patch(Brief(), {"set": {"prefs.meals": "breakfast", "budget_rub.max": 100000}})
    b, changed = apply_patch(b, {"unset": ["prefs.meals", "budget_rub.max"]})
    assert b.prefs.value.meals == "any" and b.budget_rub.value.max is None
    assert changed == ["budget_rub.max", "prefs.meals"]


def test_every_allowed_path_is_applicable():
    samples = {
        "destination.name": "Стамбул", "dates.start": "2026-05-01", "dates.end": "2026-05-05", "dates.month": 5,
        "dates.nights": 4, "dates.shift_days": 1, "dates.flexible_days": 2, "party.segment": "friends",
        "party.adults": 3, "party.children": 1, "party.children_ages": [7], "party.pets": ["dog"],
        "budget_rub.max": 150000, "prefs.meals": "breakfast", "prefs.free_cancellation": True,
        "prefs.stars_min": 4, "prefs.direct_only": True, "prefs.max_travel_hours": 6.5, "prefs.district": "quiet",
        "prefs.beach_max_m": 500, "prefs.center_max_m": 1500, "prefs.transport": "plane", "prefs.arrive_before": "14:00",
        "prefs.no_night_flights": True, "prefs.domestic": True, "constraints.add": ["accessible"],
        "constraints.remove": ["accessible"], "traits.add": ["sea"], "mood.add": ["relax"],
        "origin.city": "Казань", "scope": ["stay"],
    }
    assert set(samples) == set(ALLOWED_PATCH_PATHS)
    b = Brief()
    for path in ALLOWED_PATCH_PATHS:
        b, _ = apply_patch(b, {"set": {path: samples[path]}})
    for path in ALLOWED_UNSET_PATHS:
        apply_patch(b, {"unset": [path]})


def test_has_paths():
    b = sochi_brief()
    assert b.has("destination") and b.has("dates.start") and not b.has("budget_rub")
    with pytest.raises(KeyError):
        b.has("nonsense")


def make_variant():
    price = Price(amount=11200, currency="RUB", checked_at="2026-10-24T12:00:00+03:00")
    step = Step.model_validate({"id": "s1", "type": "flight", "from": "SVO", "to": "AER",
                                "start": "2026-11-12T06:40:00+03:00", "end": "2026-11-12T09:05:00+03:00",
                                "price": price.model_dump(), "link": "mock://fl_1", "offer_id": "fl_1"})
    return Variant(id="cheapest", title="Дешевле", steps=[step], totals=Totals(rub=11200, door_to_door_hours=2.4),
                   sacrifices=[Note(code="early_flight", text="Рейс в 06:40", data={"dep": "06:40"})])


def test_step_is_trip_planner_compatible():
    d = make_variant().steps_for_checks()[0]
    assert d["from"] == "SVO" and "from_" not in d
    assert set(d) >= {"id", "type", "from", "to", "start", "end", "price", "link", "status"}
    assert "carrier" not in d  # null optionals are dropped for checks
    with pytest.raises(Exception):
        Variant(id="best", totals=Totals(rub=1, door_to_door_hours=1))


def test_session_json_round_trip_is_lossless(pinned_now):
    s = Session.new(now=pinned_now, profile_id="p1", debug=True)
    s.brief = sochi_brief()
    s.brief, _ = apply_patch(s.brief, {"set": {"dates.flexible_days": 2}})
    s.brief.dates.value.window = DateWindow.model_validate({"from": "2026-11-10", "to": "2026-11-25"})
    s.stage = "selection"
    s.shown.variants.append(ShownItem(label="1", kind="variant", id="cheapest", title="Дешевле"))
    s.variants.append(make_variant())
    s.pending_questions.append(Question(id="q_party", field="party.segment", text="Кто едет?",
                                        choices=[{"label": "вдвоём", "value": "couple", "recommended": True}]))
    s.results["k1"] = CachedResult(key="k1", kind="search_hotels", params={"a": 1}, fetched_at=pinned_now,
                                   data={"offers": [{"x": 1}]})
    for i in range(9):
        s.remember_user(f"msg {i}")
        s.add_trace({"turn": i})
    text = s.to_json()
    back = Session.from_json(text)
    assert back == s
    assert json.loads(back.to_json()) == json.loads(text)
    assert '"from"' in text and "from_" not in text
    assert len(back.history.user) == 6 and back.history.user[-1] == "msg 8"
    assert back.clock() == pinned_now and back.today() == date(2026, 10, 24)


def test_session_requires_aware_datetimes():
    from datetime import datetime
    with pytest.raises(Exception):
        Session(created_at=datetime(2026, 1, 1))


def test_find_shown(session):
    session.shown.destinations = [ShownItem(label="1", kind="destination", id="antalya", title="Анталья"),
                                  ShownItem(label="2", kind="destination", id="hurghada", title="Хургада")]
    assert session.find_shown("destination", "2").id == "hurghada"
    assert session.find_shown("destination", "antalya").label == "1"
    assert session.find_shown("variant", "1") is None
    assert session.shown_titles()["destinations"] == {"1": "Анталья", "2": "Хургада"}


def test_question_field_must_be_patch_path():
    with pytest.raises(Exception):
        Question(id="q", field="hotel.name", text="?")
    Chip(field="prefs.meals", label="с завтраком", value="breakfast")


# ---------------------------------------------------------------- per-sub-field provenance (orchestrator)

from agent.state import assume  # noqa: E402


def test_patch_on_new_object_marks_only_patched_subfields_user():
    new, _ = apply_patch(Brief(), {"set": {"party.adults": 2}})
    assert new.source_of("party.adults") == "user"
    assert new.source_of("party.segment") == "default"
    assert new.source_of("party") == "user"


def test_patch_keeps_assumed_subfield_provenance():
    brief = assume(Brief(), "party.segment", "family", "pattern", confidence=0.83)
    assert brief.source_of("party.segment") == "pattern" and brief.confidence_of("party.segment") == 0.83
    new, _ = apply_patch(brief, {"set": {"party.adults": 2}})
    assert new.source_of("party.segment") == "pattern"
    assert new.confidence_of("party.segment") == 0.83
    assert new.source_of("party.adults") == "user"


def test_existing_profile_object_keeps_profile_on_untouched_subfields():
    brief = Brief.model_validate({"party": {"value": {"segment": "couple", "adults": 2}, "source": "profile",
                                            "confidence": 0.9}})
    new, _ = apply_patch(brief, {"set": {"party.children_ages": [5]}})
    assert new.source_of("party.segment") == "profile" and new.confidence_of("party.segment") == 0.9
    assert new.source_of("party.children") == "user" and new.source_of("party.children_ages") == "user"


def test_whole_value_replacement_is_user_and_dates_start_marks_month():
    brief = assume(Brief(), "origin.city", "Москва", "profile")
    assert brief.source_of("origin") == "profile"
    new, _ = apply_patch(brief, {"set": {"origin.city": "Казань", "dates.start": "2026-11-12"}})
    assert new.source_of("origin") == "user" and new.origin.sub_sources == {}
    assert new.source_of("dates.month") == "user" and new.source_of("dates.nights") == "default"


def test_assume_rejects_user_source_and_round_trips():
    with pytest.raises(ValueError):
        assume(Brief(), "party.segment", "solo", "user")
    brief = assume(Brief(), "prefs.district", "quiet", "pattern", confidence=0.98)
    assert Brief.model_validate_json(brief.model_dump_json()) == brief
