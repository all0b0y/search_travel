"""Short breaks: pay for the same nights, but keep more time at the destination."""
from datetime import datetime, timezone

import pytest

from agent.ranking import build_variants

from .factory import RULES, SEGMENTS, TODAY, brief, flight, hotel, transfer


def weekend_brief(**changes):
    return brief({"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                  "dates.start": "2026-11-20", "dates.end": "2026-11-22", "dates.nights": 2,
                  "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000,
                  **changes})


def weekend_results():
    return {
        "destination": {"id": "spb", "name": "Санкт-Петербург", "country": "Россия"},
        "flights_out": [
            flight("late-out", "SVO", "LED", "2026-11-20T20:00:00+03:00", "2026-11-20T22:00:00+03:00", 10000),
            flight("early-out", "SVO", "LED", "2026-11-20T08:00:00+03:00", "2026-11-20T10:00:00+03:00", 11000),
        ],
        "flights_back": [
            flight("early-back", "LED", "SVO", "2026-11-22T08:00:00+03:00", "2026-11-22T10:00:00+03:00", 10000),
            flight("late-back", "LED", "SVO", "2026-11-22T20:00:00+03:00", "2026-11-22T22:00:00+03:00", 11000),
        ],
        "hotels": [hotel("h1-offer", "h1", "Отель в центре", 10000, "2026-11-20", "2026-11-22")],
        "transfers_out": {"h1": [transfer("t-out", "taxi", 20, 500)]},
        "transfers_back": {"h1": [transfer("t-back", "taxi", 20, 500)]},
    }


def build(b=None, results=None):
    return build_variants(b or weekend_brief(), results or weekend_results(), {}, RULES, TODAY, segments=SEGMENTS)


def test_weekend_has_an_option_with_more_time_without_losing_the_cheapest():
    variants = build()
    assert len(variants) <= 3
    assert next(v for v in variants if v.id == "cheapest").totals.rub == 31000
    longer = next(v for v in variants if v.title == "Больше времени на месте")
    assert longer.steps[0].offer_id == "early-out" and longer.steps[-1].offer_id == "late-back"
    assert longer.totals.rub == 33000
    assert longer.totals.door_to_door_hours == variants[0].totals.door_to_door_hours


def test_destination_time_uses_transfers_and_airport_buffers_not_the_number_of_nights():
    variants = build()
    for variant in variants:
        arrival = datetime.fromisoformat(variant.steps[1].end)
        departure = datetime.fromisoformat(variant.steps[-2].start)
        assert variant.facts["destination_hours"] == round((departure - arrival).total_seconds() / 3600, 1)
    assert max(v.facts["destination_hours"] for v in variants) > 48
    assert min(v.facts["destination_hours"] for v in variants) < 48


def test_more_time_stays_inside_the_budget():
    variants = build(weekend_brief(**{"budget_rub.max": 32000}))
    assert all(v.totals.rub <= 32000 for v in variants)
    assert any(v.facts["destination_hours"] > variants[0].facts["destination_hours"] for v in variants)


def test_more_time_does_not_add_a_long_detour():
    results = weekend_results()
    results["flights_out"].append(flight("detour", "SVO", "LED", "2026-11-20T00:00:00+03:00",
                                          "2026-11-20T09:00:00+03:00", 11000, stops=1))
    variants = build(results=results)
    assert not any(s.offer_id == "detour" for v in variants for s in v.steps)


@pytest.mark.parametrize("changes", [{"party.segment": "family", "party.children": 0},
                                     {"dates.end": "2026-11-27", "dates.nights": 7}])
def test_special_card_is_only_for_a_short_break_for_two(changes):
    variants = build(weekend_brief(**changes))
    assert variants
    assert all(v.title != "Больше времени на месте" for v in variants)


def test_missing_transport_does_not_claim_time_at_the_destination():
    results = weekend_results()
    results["flights_out"] = []
    variants = build(results=results)
    assert variants and all("destination_hours" not in v.facts for v in variants)


def test_no_duplicate_when_the_longest_stay_is_already_the_cheapest():
    results = weekend_results()
    results["flights_out"] = results["flights_out"][1:]
    results["flights_back"] = results["flights_back"][1:]
    variants = build(results=results)
    assert len(variants) == 1 and variants[0].id == "cheapest"


def test_pruning_keeps_the_early_arrival_and_late_return():
    results = weekend_results()
    results["flights_out"] = [
        flight(f"out-{i}", "SVO", "LED", f"2026-11-20T{12 + i:02d}:00:00+03:00",
               f"2026-11-20T{14 + i:02d}:00:00+03:00", 10000) for i in range(9)
    ] + results["flights_out"][1:]
    results["flights_back"] = [
        flight(f"back-{i}", "LED", "SVO", f"2026-11-22T{7 + i:02d}:00:00+03:00",
               f"2026-11-22T{9 + i:02d}:00:00+03:00", 10000) for i in range(9)
    ] + results["flights_back"][1:]
    variants = build(results=results)
    assert any(v.steps[0].offer_id == "early-out" and v.steps[-1].offer_id == "late-back" for v in variants)


def test_missing_transfer_does_not_claim_complete_destination_time():
    results = weekend_results()
    results["transfers_out"] = {}
    variants = build(results=results)
    assert variants and all("destination_hours" not in v.facts for v in variants)


def test_longer_stay_does_not_replace_the_fastest_card():
    results = weekend_results()
    results["flights_out"].append(flight("quick-out", "SVO", "LED", "2026-11-20T19:00:00+03:00",
                                         "2026-11-20T20:00:00+03:00", 11000))
    variants = build(results=results)
    assert {v.id for v in variants} == {"cheapest", "fastest", "custom-3"}
    assert next(v for v in variants if v.id == "fastest").steps[0].offer_id == "quick-out"


def test_small_card_limit_is_still_respected():
    rules = {**RULES, "max_variants": 1}
    variants = build_variants(weekend_brief(), weekend_results(), {}, rules, TODAY, segments=SEGMENTS)
    assert len(variants) == 1 and variants[0].id == "cheapest"


def test_destination_time_uses_elapsed_time_across_timezones():
    results = weekend_results()
    for offer in results["flights_out"]:
        instant = datetime.fromisoformat(offer["arr"])
        offer["arr"] = instant.astimezone(timezone.utc).isoformat()
    variants = build(results=results)
    for variant in variants:
        arrival = datetime.fromisoformat(variant.steps[1].end)
        departure = datetime.fromisoformat(variant.steps[-2].start)
        assert variant.facts["destination_hours"] == round((departure - arrival).total_seconds() / 3600, 1)
