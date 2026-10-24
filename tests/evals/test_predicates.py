"""Level-3 must predicates on a final state: true and false examples."""
import copy
import json
from datetime import datetime

import pytest

from agent.state import Session, apply_patch
from evals.common import FIXTURES_DIR
from evals.predicates import PREDICATES, FinalState, build_final_state, check_all, check_must, normalize_variant
from evals.testing.fakes import variants_from_cards

NOW = datetime.fromisoformat("2026-10-25T12:00:00+03:00")


def fx(name):
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def make_state(chosen="fastest", patch=None, facts=None, extra_responses=()):
    s = Session.new(now=NOW, session_id="s_t")
    sel = fx("selection")
    s.variants = variants_from_cards(sel["cards"]["variants"], NOW.isoformat())
    if facts:
        for v in s.variants:
            v.facts.update(facts)
    s.chosen_variant_id = chosen
    s.brief, _ = apply_patch(s.brief, patch or {"set": {"destination.name": "Сочи", "party.adults": 2,
                                                         "dates.start": "2026-11-12", "dates.end": "2026-11-19"}})
    responses = [sel, fx("cart"), fx("checkout_issues"), fx("ordered"), *extra_responses]
    return build_final_state(s, responses)


def ok(name, state, **args):
    return PREDICATES[name](state, **args)[0]


def test_order_and_total():
    st = make_state()
    assert ok("order_paid", st)
    assert ok("total_le", st, rub=203600) and not ok("total_le", st, rub=200000)
    assert not ok("order_paid", FinalState())
    r = check_must({"name": "total_le", "args": {"rub": 1}}, FinalState())
    assert r["ok"] is False and r["detail"].startswith("no data")


def test_hotel_predicates():
    st = make_state()
    assert ok("district_label_in", st, labels=["walkable"]) and not ok("district_label_in", st, labels=["lively"])
    assert ok("stars_at_least", st, n=4) and not ok("stars_at_least", st, n=5)
    assert ok("meals_at_least", st, level="breakfast") and not ok("meals_at_least", st, level="half_board")
    assert not ok("free_cancellation", st)               # no data in facts / titles for «Быстрее»
    assert ok("free_cancellation", make_state(chosen="comfort"))   # title says «бесплатная отмена»
    assert not ok("hotel_allows_pets", st)                # no data
    assert ok("hotel_allows_pets", make_state(facts={"pets_allowed": True}))
    assert not ok("hotel_allows_pets", make_state(facts={"pets_allowed": False}))
    looked = make_state()
    looked.lookup = lambda tool, payload: {"policies": {"pets_allowed": True}}
    assert ok("hotel_allows_pets", looked)


def test_transport_predicates():
    st = make_state()
    assert ok("direct_flights_only", st)
    assert ok("arrival_before", st, hour=13) and not ok("arrival_before", st, hour=12)
    assert ok("departure_after", st, hour=10) and not ok("departure_after", st, hour=11)
    assert ok("no_night_arrival", st)
    assert ok("transport_is", st, kind="flight") and not ok("transport_is", st, kind="train")
    assert ok("has_transfer", st)
    connecting = make_state()
    steps = connecting.chosen["steps"]
    hop = dict(steps[0], to="LED", end="2026-11-12T11:30+03:00")
    hop2 = dict(steps[0], **{"from": "LED", "start": "2026-11-12T12:30+03:00"})
    connecting.chosen["steps"] = [hop, hop2] + steps[1:]
    assert not ok("direct_flights_only", connecting)
    night = make_state()
    night.chosen["steps"][0]["end"] = "2026-11-13T01:10+03:00"
    assert not ok("no_night_arrival", night)


def test_dates_and_party():
    st = make_state()
    assert ok("nights_between", st, min=7, max=7) and not ok("nights_between", st, min=2, max=3)
    assert ok("stay_starts_on", st, day="2026-11-12") and not ok("stay_starts_on", st, day="2026-11-14")
    assert ok("dates_within", st, start="2026-11-10", end="2026-11-20") and not ok("dates_within", st, start="2026-11-13", end="2026-11-20")
    assert ok("party_matches", st, adults=2) and not ok("party_matches", st, adults=3)
    fam = make_state(patch={"set": {"party.adults": 2, "party.children_ages": [5]}})
    assert ok("party_matches", fam, adults=2, children_ages=[4])       # same age bucket 2-6
    assert not ok("party_matches", fam, adults=2, children_ages=[1])
    assert not ok("party_matches", fam, adults=2)
    assert ok("destination_in", st, names=["сочи"]) and not ok("destination_in", st, names=["Казань"])
    assert not ok("origin_is", st, city="Москва")  # no origin in this brief
    st2 = make_state(patch={"set": {"origin.city": "Москва", "budget_rub.max": 90000, "traits.add": ["sea"],
                                    "constraints.add": ["pet:dog"]}})
    assert ok("origin_is", st2, city="москва")
    assert ok("budget_le", st2, rub=90000) and not ok("budget_le", st2, rub=80000)
    assert ok("brief_traits_any", st2, traits=["sea", "beach"]) and not ok("brief_traits_any", st2, traits=["ski"])
    assert ok("brief_has_constraint", st2, token="pet:dog") and not ok("brief_has_constraint", st, token="pet:dog")


def test_cart_and_order_predicates():
    st = make_state()
    assert ok("cart_has_kinds", st, kinds=["flight", "hotel"])
    assert not ok("cart_has_kinds", st, kinds=["hotel"], exactly=True)
    assert ok("no_blocking_issues", st)
    bad = make_state()
    bad.order = copy.deepcopy(bad.order)
    bad.order["issues"].append({"code": "x", "message": "m", "severity": "blocking", "item_ids": []})
    assert not ok("no_blocking_issues", bad)
    assert not ok("order_action_done", st, kind="early_checkin")
    after = make_state(extra_responses=[fx("aftercare")])
    assert ok("order_action_done", after, kind="early_checkin")
    assert ok("agent_said", st, pattern="оплачено") and not ok("agent_said", st, pattern="тромс")
    assert ok("agent_never_said", st, pattern="тромс") and not ok("agent_never_said", st, pattern="оплачено")


def test_final_state_from_cards_only():
    """Without a session the chosen variant comes from the response cards (by chosen id)."""
    sel = fx("selection")
    state = build_final_state({"brief": {"party": {"value": {"adults": 2, "children": 0}}}, "chosen_variant_id": "comfort"},
                              [sel, fx("ordered")])
    assert state.chosen["id"] == "comfort" and state.chosen["hotel"]["stars"] == 5
    assert ok("party_matches", state, adults=2)


def test_check_all_and_unknown():
    st = make_state()
    res = check_all([{"name": "order_paid"}, {"name": "nope"}, {"name": "total_le", "args": {"wrong": 1}}], st)
    assert [r["ok"] for r in res] == [True, False, False]
    assert "unknown predicate" in res[1]["detail"] and "predicate error" in res[2]["detail"]


def test_normalize_variant_none():
    assert normalize_variant(None) is None
