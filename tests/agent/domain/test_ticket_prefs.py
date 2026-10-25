"""Tickets-only scopes and the ticket prefs: arrive_before, no_night_flights, refundable fares."""
import copy

from agent.checks import blocking, check_variant, check_variant_set, return_index
from agent.ranking import build_variants

from .factory import DISTRICTS, RULES, SEGMENTS, TODAY, brief, flight, sea_results, train

BASE = {"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-12",
        "dates.end": "2026-11-19", "party.adults": 2}


def build(b, results):
    return build_variants(b, results, DISTRICTS, RULES, TODAY, segments=SEGMENTS)


def codes(notes):
    return [n.code for n in notes]


def legs(v, kind=None):
    return [s for s in v.steps if s.type in ("flight", "train") and (kind is None or s.type == kind)]


def no_blocks(vs, b):
    for v in vs:
        assert blocking(check_variant(v, b, RULES)) == [], (v.id, check_variant(v, b, RULES))
    assert check_variant_set(vs) == []


# ---------------------------------------------------------------- tickets only

def test_flights_only_scope_builds_ticket_variants():
    b = brief({**BASE, "scope": ["flights"]})
    vs = build(b, sea_results())
    assert vs and vs[0].id == "cheapest"
    for v in vs:
        assert v.hotel is None and [s.type for s in v.steps] == ["flight", "flight"]
        assert v.totals.rub == sum(s.price.rub for s in v.steps)
        assert v.facts["transport_only"] is True
        assert not {"no_free_cancel", "no_transfer", "late_checkin"} & set(codes(v.risks))
    no_blocks(vs, b)
    assert len({v.totals.rub for v in vs}) == len(vs)


def test_flights_only_ignores_hotels_and_needs_both_directions():
    b = brief({**BASE, "scope": ["flights"]})
    res = sea_results()
    res["hotels"] = []
    assert build(b, res)
    res["flights_back"] = []
    assert build(b, res) == []


def _city_trains():
    tz = "+03:00"
    out = [train("TA", "Москва Казанская", "Казань-Пассажирская", f"2026-12-04T06:00:00{tz}",
                 f"2026-12-04T18:00:00{tz}", 4000, car_class="platzkart"),
           train("TB", "Москва Казанская", "Казань-Пассажирская", f"2026-12-04T06:00:00{tz}",
                 f"2026-12-04T18:00:00{tz}", 12000, car_class="sv"),
           train("TC", "Москва Казанская", "Казань-Пассажирская", f"2026-12-04T09:00:00{tz}",
                 f"2026-12-04T15:00:00{tz}", 6000, car_class="seat")]
    back = [train("RA", "Казань-Пассажирская", "Москва Казанская", f"2026-12-07T07:00:00{tz}",
                  f"2026-12-07T19:00:00{tz}", 4100, car_class="platzkart"),
            train("RB", "Казань-Пассажирская", "Москва Казанская", f"2026-12-07T07:00:00{tz}",
                  f"2026-12-07T19:00:00{tz}", 12500, car_class="sv")]
    return {"trains_out": out, "trains_back": back, "destination": {"id": "kazan", "name": "Казань"}}


def test_trains_only_scope_cheapest_fastest_comfort():
    b = brief({"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
               "dates.end": "2026-12-07", "scope": ["trains"]})
    vs = {v.id: v for v in build(b, _city_trains())}
    assert vs["cheapest"].steps[0].offer_id == "TA" and vs["cheapest"].steps[1].offer_id == "RA"
    assert vs["fastest"].steps[0].offer_id == "TC"
    comfort = vs["comfort"].facts   # SV both ways is over the comfort price cap (2x the cheapest)
    assert "sv" in (comfort["out_car_class"], comfort["back_car_class"])
    no_blocks(list(vs.values()), b)


def test_return_index_splits_a_ticket_chain():
    steps = [{"type": "flight", "start": "2026-11-12T06:00:00+03:00", "end": "2026-11-12T08:00:00+03:00"},
             {"type": "flight", "start": "2026-11-12T10:00:00+03:00", "end": "2026-11-12T12:00:00+03:00"},
             {"type": "flight", "start": "2026-11-19T10:00:00+03:00", "end": "2026-11-19T12:00:00+03:00"}]
    assert return_index(steps) == 2
    assert return_index(steps[:1]) is None


def test_tickets_only_return_from_another_airport_is_not_a_chain_break():
    b = brief({**BASE, "scope": ["flights"]})
    res = sea_results()
    tz = "+03:00"
    res["flights_back"] = [flight("BG", "GZP", "SVO", f"2026-11-19T12:00:00{tz}", f"2026-11-19T16:00:00{tz}",
                                  15000, international=True)]
    vs = build(b, res)
    assert vs and all(v.steps[-1].from_ == "GZP" for v in vs)
    no_blocks(vs, b)


# ---------------------------------------------------------------- arrive_before / night flights

def test_arrive_before_filters_outbound_arrival():
    b = brief({**BASE, "prefs.arrive_before": "11:00"})
    vs = build(b, sea_results())
    assert vs
    for v in vs:
        out = legs(v)[0]
        assert out.end[11:16] <= "11:00"
        assert "relaxed_pref" not in codes(v.risks)


def test_arrive_before_relaxed_with_a_risk_when_nothing_passes():
    b = brief({**BASE, "prefs.arrive_before": "08:00"})
    vs = build(b, sea_results())
    assert vs
    for v in vs:
        relaxed = [n for n in v.risks if n.code == "relaxed_pref"]
        assert relaxed and relaxed[0].data["field"] == "prefs.arrive_before" and "08:00" in relaxed[0].text


def test_no_night_flights_pref():
    b = brief({**BASE, "prefs.no_night_flights": True})
    vs = build(b, sea_results())
    used = {s.offer_id for v in vs for s in legs(v)}
    assert vs and "F3" not in used and "B1" not in used   # F3 leaves 01:30, B1 lands 23:30


def test_no_night_flights_relaxed_when_only_night_flights():
    b = brief({**BASE, "prefs.no_night_flights": True})
    res = sea_results()
    res["flights_back"] = [f for f in res["flights_back"] if f["offer_id"] == "B1"]
    vs = build(b, res)
    assert vs and all("relaxed_pref" in codes(v.risks) for v in vs)


def test_pref_set_by_profile_is_not_a_filter():
    b = brief(BASE, assumed={"prefs.arrive_before": ("08:00", "profile")})
    vs = build(b, sea_results())
    assert vs and not any("relaxed_pref" in codes(v.risks) for v in vs)


# ---------------------------------------------------------------- refundable tickets

def test_free_cancellation_prefers_refundable_fares():
    b = brief({**BASE, "prefs.free_cancellation": True})
    vs = build(b, sea_results())
    assert vs
    for v in vs:
        assert {s.offer_id for s in legs(v)} == {"F2", "B2"}
        assert v.facts["out_refundable"] is True and v.facts["back_refundable"] is True
        assert "non_refundable" not in codes(v.risks)


def test_free_cancellation_without_refundable_fares_says_so():
    b = brief({**BASE, "prefs.free_cancellation": True})
    res = sea_results()
    for key in ("flights_out", "flights_back"):
        res[key] = [dict(copy.deepcopy(f), fare={"refundable": False, "change_fee_rub": None}) for f in res[key]]
    vs = build(b, res)
    assert vs
    for v in vs:
        assert codes(v.risks)[0] == "non_refundable_only"
        assert "non_refundable" in codes(v.risks)


def test_arrive_before_means_the_travel_day_not_the_clock_alone():
    """s11: a 21:35 departure landing at 01:53 the next night is not «до 14:00»; the 08:20 -> 12:37 one is."""
    from types import SimpleNamespace
    from datetime import date, datetime, timedelta, timezone
    from agent.ranking import _soft_leg_filters
    from agent.state import Brief, apply_patch
    brief, _ = apply_patch(Brief(), {"set": {"dates.start": "2026-11-20", "dates.end": "2026-11-23",
                                             "prefs.arrive_before": "14:00"}})
    keep = dict(_soft_leg_filters(brief, "out", {"night_window": ["23:00", "06:00"]}))["prefs.arrive_before"]
    msk, ekb = timezone(timedelta(hours=3)), timezone(timedelta(hours=5))
    night = SimpleNamespace(dep=datetime(2026, 11, 20, 21, 35, tzinfo=msk), arr=datetime(2026, 11, 21, 1, 53, tzinfo=ekb))
    morning = SimpleNamespace(dep=datetime(2026, 11, 20, 8, 20, tzinfo=msk), arr=datetime(2026, 11, 20, 12, 37, tzinfo=ekb))
    assert keep(morning) and not keep(night)
