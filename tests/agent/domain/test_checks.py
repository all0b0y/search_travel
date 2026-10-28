"""Ported trip-planner finalize checks (the removed skills/trip-planner/tests/test_trip.py; its fixture lives in tests/fixtures/trip_planner/) + agent extensions."""
import copy
import json
from datetime import datetime

import pytest

from agent.checks import (check_variant, check_variant_set, connection_risks, fill_rub, parse_cbr_xml, to_rub,
                          fmt_rub, fmt_hours, nights_text)
from agent.state import Variant, assume

from .factory import ROOT, RULES, brief

FIXTURE = json.loads((ROOT / "tests/fixtures/trip_planner/berlin-lisbon.json").read_text(encoding="utf-8"))
IDS = {"v-cheap": "cheapest", "v-fast": "fastest", "v-comfort": "comfort"}
RATES = {"EUR": 95.0}


def lisbon_brief(**over):
    user = {"origin.city": "Berlin, Kreuzberg", "destination.name": "Lisbon", "dates.start": "2026-11-12",
            "dates.end": "2026-11-15", "dates.flexible_days": 1, "party.adults": 2, "budget_rub.max": 150000}
    user.update(over)
    return brief(user)


def hours(steps):
    return round(sum((datetime.fromisoformat(s["end"]) - datetime.fromisoformat(s["start"])).total_seconds()
                     for s in steps if s["type"] != "stay") / 3600, 1)


def variant(raw):
    """A trip-planner fixture variant as an agent Variant with rubles filled (trip.py fill_prices)."""
    v = Variant.model_validate({"id": IDS[raw["id"]], "title": raw["tradeoff"], "steps": raw["steps"],
                                "totals": {"rub": 0, "door_to_door_hours": hours(raw["steps"])}})
    v, problems = fill_rub(v, RATES)
    return v, problems


def doc():
    return copy.deepcopy(FIXTURE)


def raw_variant(d, vid):
    return next(v for v in d["variants"] if v["id"] == vid)


def raw_step(d, vid, sid):
    return next(s for s in raw_variant(d, vid)["steps"] if s["id"] == sid)


def run(d, b=None):
    b = b or lisbon_brief()
    out, issues = {}, []
    for raw in d["variants"]:
        v, problems = variant(raw)
        out[v.id] = v
        issues += problems + check_variant(v, b, RULES)
    issues += check_variant_set(list(out.values()))
    return out, issues


def codes(issues, severity=None):
    return {(i["code"], i["data"].get("variant"), i["data"].get("step")) for i in issues
            if severity is None or i["severity"] == severity}


class TestFixture:
    def test_fixture_passes(self):
        _, issues = run(doc())
        assert issues == []

    def test_rubles_and_totals(self):
        out, _ = run(doc())
        step = next(s for s in out["cheapest"].steps if s.id == "s2")
        assert step.price.rub == 17100
        assert out["cheapest"].totals.rub == 80085
        assert out["cheapest"].totals.door_to_door_hours == 9.0


class TestProblems:
    def test_tight_airport_buffer(self):
        d = doc()
        raw_step(d, "v-cheap", "s1")["end"] = "2026-11-12T06:10:00+01:00"
        assert ("buffer", "cheapest", "s2") in codes(run(d)[1], "block")

    def test_taxi_without_margin(self):
        d = doc()
        raw_step(d, "v-cheap", "s1")["duration_estimate_min"] = 40
        assert ("taxi_margin", "cheapest", "s1") in codes(run(d)[1], "block")

    def test_broken_chain(self):
        d = doc()
        raw_step(d, "v-cheap", "s3")["to"] = "Another hotel"
        assert ("chain_break", "cheapest", "s4") in codes(run(d)[1], "block")

    def test_chain_must_start_and_end_at_origin(self):
        d = doc()
        raw_step(d, "v-cheap", "s7")["to"] = "Potsdam"
        assert ("chain_origin", "cheapest", "s7") in codes(run(d)[1], "block")

    def test_missing_rate(self):
        d = doc()
        raw_step(d, "v-cheap", "s2")["price"]["currency"] = "USD"
        assert ("price_no_rate", "cheapest", "s2") in codes(run(d)[1], "block")

    def test_over_budget(self):
        # an overrun warns even for a user budget: the user may pick it knowingly and must be able to pay
        _, issues = run(doc(), lisbon_brief(**{"budget_rub.max": 100000}))
        assert ("budget", "comfort", None) in codes(issues, "warn")
        assert ("budget", "comfort", None) not in codes(issues, "block")
        assert ("budget", "cheapest", None) not in codes(issues)

    def test_assumed_budget_only_warns(self):
        b = assume(lisbon_brief(), "budget_rub.max", 100000, "pattern")
        _, issues = run(doc(), b)
        assert ("budget", "comfort", None) in codes(issues, "warn")
        assert ("budget", "comfort", None) not in codes(issues, "block")

    def test_departure_outside_window(self):
        _, issues = run(doc(), lisbon_brief(**{"dates.start": "2026-11-13", "dates.flexible_days": 0,
                                                "dates.end": "2026-11-15"}))
        assert ("dates", "cheapest", "s2") in codes(issues, "block")

    def test_cheapest_label_must_be_cheapest(self):
        d = doc()
        raw_step(d, "v-cheap", "s4")["price"]["amount"] = 1000
        assert ("variants_order", "cheapest", None) in codes(run(d)[1], "block")

    def test_fastest_label_must_be_fastest(self):
        d = doc()
        raw_step(d, "v-fast", "f2")["start"] = "2026-11-12T07:00:00+01:00"  # 5 h flight
        raw_step(d, "v-fast", "f1")["start"] = "2026-11-12T03:30:00+01:00"
        raw_step(d, "v-fast", "f1")["end"] = "2026-11-12T04:00:00+01:00"
        assert ("variants_order", "fastest", None) in codes(run(d)[1], "block")

    def test_overlap(self):
        d = doc()
        raw_step(d, "v-cheap", "s3")["start"] = "2026-11-12T11:00:00+00:00"
        assert ("time_overlap", "cheapest", "s3") in codes(run(d)[1], "block")

    def test_ends_before_it_starts(self):
        d = doc()
        raw_step(d, "v-cheap", "s1")["end"] = "2026-11-12T05:00:00+01:00"
        assert ("time_order", "cheapest", "s1") in codes(run(d)[1], "block")

    def test_after_flight_with_baggage_needs_more(self):
        d = doc()  # comfort: baggage, 75 min needed; c3 starts 75 min after landing -> fine; make it 60
        raw_step(d, "v-comfort", "c3")["start"] = "2026-11-12T12:05:00+00:00"
        assert ("buffer", "comfort", "c3") in codes(run(d)[1], "block")

    def test_wrong_totals(self):
        v, _ = variant(raw_variant(doc(), "v-cheap"))
        v.totals.rub += 1
        assert ("totals", "cheapest", None) in codes(check_variant(v, lisbon_brief(), RULES))


class TestRiskyConnection:
    """Land in Lisbon, taxi to the station, catch a train: the train is missable."""

    def setup_method(self):
        self.doc = doc()
        steps = raw_variant(self.doc, "v-cheap")["steps"]
        steps[2:3] = [
            {"id": "t1", "type": "taxi", "from": "LIS", "to": "Oriente", "start": "2026-11-12T11:50:00+00:00",
             "end": "2026-11-12T12:10:00+00:00", "duration_estimate_min": 15,
             "price": {"amount": 15, "currency": "EUR", "estimate": True, "checked_at": "x"},
             "link": "https://m.uber.com/ul/", "status": "planned"},
            {"id": "t2", "type": "train", "from": "Oriente", "to": "Hotel Alfama Lisboa",
             "start": "2026-11-12T12:50:00+00:00", "end": "2026-11-12T13:20:00+00:00",
             "price": {"amount": 5, "currency": "EUR", "checked_at": "x"}, "link": "https://www.omio.com/x",
             "status": "planned"},
        ]

    def test_risky_train_is_reported(self):
        out, issues = run(self.doc)
        assert ("risky_connection", "cheapest", "t2") in codes(issues, "warn")
        risks = connection_risks(out["cheapest"].steps_for_checks(), RULES)
        assert risks["t2"] == {"slack_min": 10, "risky": True}

    def test_plan_b_clears_it_and_gets_rubles(self):
        train = raw_variant(self.doc, "v-cheap")["steps"][3]
        backup = dict(train, id="t2b", start="2026-11-12T13:50:00+00:00", end="2026-11-12T14:20:00+00:00")
        train["plan_b"] = {"trigger": "рейс задержан больше чем на 15 мин", "step": backup}
        out, issues = run(self.doc)
        assert ("risky_connection", "cheapest", "t2") not in codes(issues)
        t2 = next(s for s in out["cheapest"].steps if s.id == "t2")
        assert t2.plan_b["step"]["price"]["rub"] == 475


class TestStay:
    def test_late_arrival_flags_checkin(self):
        d = doc()
        s3 = raw_step(d, "v-cheap", "s3")
        s3["start"], s3["end"] = "2026-11-12T22:40:00+00:00", "2026-11-12T23:10:00+00:00"
        _, issues = run(d)
        assert ("late_checkin", "cheapest", "s4") in codes(issues, "warn")

    def test_lost_night_blocks(self):
        d = doc()
        for sid, (a, b) in {"s2": ("2026-11-13T09:00:00+01:00", "2026-11-13T11:05:00+00:00"),
                            "s3": ("2026-11-13T11:55:00+00:00", "2026-11-13T12:25:00+00:00"),
                            "s1": ("2026-11-13T05:10:00+01:00", "2026-11-13T05:50:00+01:00")}.items():
            raw_step(d, "v-cheap", sid).update(start=a, end=b)
        _, issues = run(d)
        assert ("night_lost", "cheapest", "s4") in codes(issues, "block")

    def test_nights_must_match(self):
        _, issues = run(doc(), lisbon_brief(**{"dates.nights": 4}))
        assert ("nights", "cheapest", "s4") in codes(issues, "block")

    def test_missing_transfer_only_warns(self):
        d = doc()
        steps = raw_variant(d, "v-cheap")["steps"]
        del steps[2]  # flight LIS -> stay at the hotel, no transfer
        _, issues = run(d)
        assert ("no_transfer", "cheapest", "s4") in codes(issues, "warn")
        assert not [i for i in issues if i["data"].get("variant") == "cheapest" and i["code"] == "chain_break"]


def stay_variant(**facts):
    return Variant.model_validate({
        "id": "cheapest", "steps": [{"id": "s1", "type": "stay", "from": "Отель", "to": "Отель",
                                     "start": "2026-11-12T14:00:00+03:00", "end": "2026-11-15T12:00:00+03:00",
                                     "price": {"amount": 30000, "currency": "RUB", "checked_at": "2026-10-24", "rub": 30000},
                                     "link": "ostrovok-mock:stay/x"}],
        "totals": {"rub": 30000, "door_to_door_hours": 0}, "hotel": {"hotel_id": "h", "name": "Отель"}, "facts": facts})


class TestPartyFit:
    def family(self, **extra):
        u = {"party.segment": "family", "party.adults": 2, "party.children_ages": [4], "dates.start": "2026-11-12",
             "dates.end": "2026-11-15"}
        u.update(extra)
        return brief(u)

    def test_capacity(self):
        issues = check_variant(stay_variant(room_capacity_adults=2, room_capacity_children=0), self.family(), RULES)
        assert {"room_capacity"} == {i["code"] for i in issues}

    def test_capacity_ok_with_child_bed(self):
        assert check_variant(stay_variant(room_capacity_adults=2, room_capacity_children=1), self.family(), RULES) == []

    def test_children_not_allowed(self):
        issues = check_variant(stay_variant(room_capacity_adults=4, children_allowed=False), self.family(), RULES)
        assert [i["code"] for i in issues] == ["children_policy"]

    def test_min_child_age(self):
        issues = check_variant(stay_variant(room_capacity_adults=4, min_child_age=6), self.family(), RULES)
        assert issues[0]["data"]["min_child_age"] == 6

    def test_pets(self):
        b = brief({"constraints.add": ["pet:dog"], "dates.start": "2026-11-12", "dates.end": "2026-11-15"})
        assert [i["code"] for i in check_variant(stay_variant(pets_allowed=False), b, RULES)] == ["pets_policy"]
        assert check_variant(stay_variant(pets_allowed=True), b, RULES) == []

    def test_unknown_facts_are_not_guessed(self):
        assert check_variant(stay_variant(), self.family(), RULES) == []

    def test_stay_only_dates(self):
        b = brief({"dates.start": "2026-11-13", "dates.end": "2026-11-15"})
        assert ("dates", "cheapest", "s1") in codes(check_variant(stay_variant(), b, RULES), "block")

    def test_month(self):
        b = brief({"dates.month": 12})
        assert [i["code"] for i in check_variant(stay_variant(), b, RULES)] == ["dates"]


CBR_XML = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="24.10.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name><Value>92,5012</Value><VunitRate>92,5012</VunitRate></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal><Name>Евро</Name><Value>99,1000</Value><VunitRate>99,1</VunitRate></Valute>
<Valute ID="R01700J"><NumCode>949</NumCode><CharCode>TRY</CharCode><Nominal>10</Nominal><Name>Турецких лир</Name><Value>27,0500</Value><VunitRate>2,705</VunitRate></Valute>
</ValCurs>
""".encode("cp1251")


class TestRates:
    def test_parse_recorded_cbr_xml(self):
        rates = parse_cbr_xml(CBR_XML)
        assert rates["date"] == "2026-10-24"
        assert rates["rub_per_unit"] == {"USD": 92.5012, "EUR": 99.1, "TRY": 2.705}

    def test_bad_xml(self):
        with pytest.raises(ValueError):
            parse_cbr_xml(b"<html>down</html>")

    def test_to_rub(self):
        rates = parse_cbr_xml(CBR_XML)
        assert to_rub(100, "TRY", rates) == 270
        assert to_rub(1500, "RUB", rates) == 1500
        assert to_rub(10, "GBP", rates) is None


def test_formatting():
    assert fmt_rub(84000) == "84 000 ₽"
    assert fmt_hours(6.5) == "6,5" and fmt_hours(7.0) == "7"
    assert nights_text(1) == "1 ночь" and nights_text(3) == "3 ночи" and nights_text(7) == "7 ночей"
    assert nights_text(11) == "11 ночей" and nights_text(22) == "22 ночи"
