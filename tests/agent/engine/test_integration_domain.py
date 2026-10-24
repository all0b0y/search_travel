"""Full path with the REAL ENGINE-B domain (ranking, checks, upsell, guards, render) on fake mock/open data.

Skipped when ENGINE-B modules are missing. The present cards are echoed (no numbers in their texts), so
the real card guard passes; everything else is the production code path.
"""
import importlib.util

import pytest

from agent.steps import Domain

from .fakes import u

pytestmark = pytest.mark.skipif(
    any(importlib.util.find_spec(m) is None for m in ("agent.ranking", "agent.checks", "agent.upsell",
                                                       "agent.guards", "agent.render")),
    reason="ENGINE-B modules not present")

TRAVELLERS = [{"name": "Анна", "birth_date": "1990-01-01"}, {"name": "Иван", "birth_date": "1989-05-05"},
              {"name": "Малыш", "birth_date": "2026-03-01"}]


def test_full_path_with_real_domain(make, tmp_path):
    h = make(domain=Domain())
    # A: first message -> destinations
    h.understand(u(set={"traits.add": ["sea", "warm"], "dates.month": 11, "party.segment": "family"}))
    r = h.turn("к тёплому морю в ноябре с ребёнком")
    assert r["stage"] == "inspiration" and r["cards"]["destinations"]
    # choose a destination -> variants (real ranking, checks, sensitivity, upsell)
    r = h.act("choose_destination", destination_id=r["cards"]["destinations"][0]["id"])
    assert r["stage"] == "selection", r["message"]
    assert r["cards"]["variants"], r["message"]
    variants = h.session().variants
    assert all(v.totals.rub == sum(int(s.price.rub or s.price.amount) for s in v.steps) or v.totals.rub > 0
               for v in variants)
    vid = r["cards"]["variants"][0]["id"]
    # cart -> checkout with an infant -> pay -> early check-in
    r = h.act("choose_variant", variant_id=vid)
    assert r["stage"] == "cart" and r["cart"]["items"]
    r = h.act("checkout", travellers=TRAVELLERS, contact={"email": "a@b.c"})
    assert r["stage"] == "checkout" and r["order"]
    assert h.session().brief.values()["party"]["children_ages"] == [0]
    if any(a["type"] == "pay" for a in r["actions"]):
        r = h.act("pay", order_id=r["order"]["order_id"])
        assert r["stage"] == "ordered"
        assert r["files"]["ics"] and r["files"]["ics"].startswith("/files/")
        h.understand(u(intent="post_purchase", pp={"kind": "early_checkin", "params": {"time": "10:00"}}))
        r = h.turn("можно заселиться пораньше?")
        assert r["stage"] == "aftercare"
