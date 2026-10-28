"""Night arrival changes the code's pick for sensitive travellers, not availability."""
from types import SimpleNamespace

import pytest

from agent.night_arrival import has_night_arrival
from agent.profiles import Profile, SavedTraveller
from agent.ranking import build_variants, fit, ranking_rules, recommended_hint
from agent.state import Session
from agent.steps import rules_view

from .factory import DISTRICTS, RULES, SEGMENTS, TODAY, sea_brief, sea_results


def candidates():
    day = build_variants(sea_brief(), sea_results(), DISTRICTS, RULES, TODAY, SEGMENTS)[0]
    day = day.model_copy(deep=True, update={"id": "fastest"})
    night = day.model_copy(deep=True, update={"id": "cheapest"})
    # Same offers, price and duration: isolate the local outbound arrival time.
    night.steps[0].start = "2026-11-11T20:00:00+03:00"
    night.steps[0].end = "2026-11-12T01:10:00+03:00"
    return night, day


@pytest.mark.parametrize("patch", [
    {"mood.add": ["relax"]}, {"party.segment": "family"},
    {"party.children": 1, "party.children_ages": [5]},
])
def test_daytime_arrival_wins_for_relax_and_families(patch):
    brief = sea_brief(**patch)
    night, day = candidates()
    for variant in (night, day):
        variant.fit = fit(brief, variant, RULES, SEGMENTS)
    assert night.fit.score < day.fit.score
    assert night.fit.breakdown["arrival_time"] == 0
    assert recommended_hint([night, day]) == day.id


@pytest.mark.parametrize("profile, expected", [
    (Profile(id="p", age=65), True), (Profile(id="p", age=64), False),
    (Profile(id="p", travellers=[SavedTraveller(name="Гость", birth_date="1961-11-12")]), True),
    (Profile(id="p", travellers=[SavedTraveller(name="Гость", birth_date="1961-11-13")]), False),
    (None, False),
])
def test_profile_age_and_birthdays_affect_ranking_without_mutating_rules(profile, expected, pinned_now):
    brief = sea_brief()
    session = Session.new(now=pinned_now)
    session.brief = brief
    view = rules_view(SimpleNamespace(rules=RULES, skill=SimpleNamespace(segments=SEGMENTS),
                                     profile=profile, session=session))
    assert view["_senior_travellers"] is expected
    assert "_senior_travellers" not in RULES
    night, day = candidates()
    night.fit, day.fit = (fit(brief, variant, view, SEGMENTS) for variant in (night, day))
    assert (night.fit.score < day.fit.score) is expected
    if expected:
        assert recommended_hint([night, day]) == day.id


def test_transfer_after_midnight_counts_but_night_return_does_not():
    _, day = candidates()
    window = ranking_rules(RULES)["night_window"]
    assert not has_night_arrival(day, window)
    day.steps[-1].end = "2026-11-20T01:00:00+03:00"
    assert not has_night_arrival(day, window)
    day.steps[0].end = "2026-11-12T22:00:00+03:00"
    day.steps[1].end = "2026-11-13T00:10:00+03:00"
    assert has_night_arrival(day, window)


def test_penalty_is_configurable_and_not_accumulated():
    night, _ = candidates()
    brief = sea_brief(**{"mood.add": ["relax"], "party.segment": "family"})
    disabled = {**RULES, "ranking": {**RULES.get("ranking", {}), "night_relax_penalty": 0, "night_family_penalty": 0}}
    baseline = fit(brief, night, disabled, SEGMENTS).score
    actual = fit(brief, night, RULES, SEGMENTS).score
    assert actual == pytest.approx(baseline * 0.8, abs=0.001)


def test_hard_no_night_filter_still_removes_night_arrivals():
    brief = sea_brief(**{"mood.add": ["relax"], "prefs.no_night_flights": True})
    variants = build_variants(brief, sea_results(), DISTRICTS, RULES, TODAY, SEGMENTS)
    assert variants
    for variant in variants:
        assert not has_night_arrival(variant, ranking_rules(RULES)["night_window"])
