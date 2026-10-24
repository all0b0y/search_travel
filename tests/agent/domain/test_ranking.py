"""build_variants / fit: sea, city (train), family, friends, stay-only, coincidences, filters, notes."""
import copy

from agent.checks import check_variant, check_variant_set
from agent.ranking import build_variants, fit, recommended_hint

from .factory import (DISTRICTS, RULES, SEGMENTS, TODAY, brief, flight, hotel, rules, sea_brief, sea_results, train,
                      transfer)


def build(b, results, districts=DISTRICTS, r=RULES, today=TODAY):
    return build_variants(b, results, districts, r, today, segments=SEGMENTS)


def by_id(vs):
    return {v.id: v for v in vs}


def codes(notes):
    return [n.code for n in notes]


class TestSea:
    def setup_method(self):
        self.b = sea_brief()
        self.vs = build(self.b, sea_results())
        self.v = by_id(self.vs)

    def test_three_distinct_variants(self):
        assert [v.id for v in self.vs] == ["cheapest", "fastest", "comfort"]
        keys = {tuple(s.offer_id for s in v.steps) for v in self.vs}
        assert len(keys) == 3

    def test_cheapest_and_fastest_are_really_so(self):
        assert check_variant_set(self.vs) == []
        assert self.v["cheapest"].totals.rub == 80600  # F3 18000 + B1 21000 + H1 40000 + 2 shuttles
        assert self.v["fastest"].totals.door_to_door_hours < self.v["cheapest"].totals.door_to_door_hours

    def test_every_variant_passes_checks(self):
        for v in self.vs:
            assert [i for i in check_variant(v, self.b, RULES) if i["severity"] == "block"] == [], v.id

    def test_chain_is_door_to_door(self):
        v = self.v["comfort"]
        assert [s.type for s in v.steps] == ["flight", "transfer", "stay", "transfer", "flight"]
        assert v.steps[1].from_ == v.steps[0].to and v.steps[1].to == v.hotel.name
        assert v.steps[3].to == v.steps[4].from_
        assert sum(s.price.rub for s in v.steps) == v.totals.rub

    def test_transfer_has_taxi_margin(self):
        tr = self.v["fastest"].steps[1]
        assert tr.duration_estimate_min == 40
        assert (tr.model_dump()["end"] > tr.model_dump()["start"])

    def test_facts_cover_card_numbers(self):
        f = self.v["cheapest"].facts
        for key in ("total_rub", "price_stay_rub", "out_price_rub", "back_price_rub", "price_transfers_rub", "nights",
                    "out_duration_min", "out_stops", "transfer_out_min", "distance_beach_m", "poi_playground",
                    "district_quiet", "ref_min_rub", "delta_rub_vs_cheapest", "out_dep_time", "check_in_time"):
            assert key in f, key
        assert f["nights"] == 7 and f["out_stops"] == 1 and f["poi_playground"] == 0
        assert self.v["fastest"].facts["delta_rub_vs_cheapest"] == self.v["fastest"].totals.rub - 80600

    def test_sacrifices_are_diffs(self):
        cheap, fast, comfort = self.v["cheapest"], self.v["fastest"], self.v["comfort"]
        assert "price_more" not in codes(cheap.sacrifices)
        assert "slower" in codes(cheap.sacrifices) and "stops" in codes(cheap.sacrifices)
        assert "price_more" in codes(fast.sacrifices) and "slower" not in codes(fast.sacrifices)
        pm = next(n for n in comfort.sacrifices if n.code == "price_more")
        assert pm.data["delta_rub"] == comfort.totals.rub - cheap.totals.rub
        assert pm.text == f"+{pm.data['delta_rub']:,} ₽ к самому дешёвому".replace(",", " ")
        assert "no_free_cancel" in codes(cheap.sacrifices) and "no_free_cancel" not in codes(comfort.sacrifices)

    def test_risks(self):
        cheap = self.v["cheapest"]
        assert {"tight_layover", "night_departure", "non_refundable", "no_free_cancel"} <= set(codes(cheap.risks))
        assert "early_departure" in codes(self.v["fastest"].risks)
        assert codes(self.v["comfort"].risks) == []

    def test_not_for_from_districts(self):
        assert {"quiet_no_nightlife", "no_playground", "not_walkable"} <= set(codes(self.v["cheapest"].not_for))
        assert all(len(v.not_for) <= 4 for v in self.vs)

    def test_district_profile_on_hotel(self):
        assert self.v["cheapest"].hotel.district.labels == ["quiet"]
        assert self.v["cheapest"].hotel.district.scores["quiet"] == 0.85

    def test_fit_breakdown(self):
        bd = self.v["comfort"].fit.breakdown
        assert set(bd) >= {"budget", "time", "district", "cancellation", "stars", "beach", "rating"}
        assert self.v["cheapest"].fit.breakdown["budget"] == 1.0
        assert all(0 <= x <= 1 for v in self.vs for x in v.fit.breakdown.values())
        assert recommended_hint(self.vs) in {"cheapest", "fastest", "comfort"}

    def test_comfort_respects_budget(self):
        b = sea_brief(**{"budget_rub.max": 100000})
        vs = by_id(build(b, sea_results()))
        assert vs["comfort"].totals.rub <= 100000 if "comfort" in vs else True
        assert all(v.totals.rub <= 100000 for k, v in vs.items() if k == "comfort")

    def test_budget_fit(self):
        b = sea_brief(**{"budget_rub.max": 100000})
        vs = by_id(build(b, sea_results()))
        f = fit(b, vs["cheapest"], RULES, SEGMENTS)
        assert f.breakdown["budget"] == 1.0


class TestCoincide:
    def test_single_option_says_so(self):
        res = sea_results()
        res["flights_out"] = res["flights_out"][:1]
        res["flights_back"] = res["flights_back"][:1]
        res["hotels"] = res["hotels"][:1]
        for k in ("transfers_out", "transfers_back"):
            res[k] = {h: ts[:1] for h, ts in res[k].items()}
        vs = build(sea_brief(), res)
        assert [v.id for v in vs] == ["cheapest"]
        f = vs[0].facts
        assert f["also_fastest"] is True and f["also_comfort"] is True
        assert "отдельной карточки нет" in f["coincide_note"]

    def test_cheapest_is_fastest(self):
        res = sea_results()
        res["flights_out"] = [o for o in res["flights_out"] if o["offer_id"] == "F1"]
        res["flights_back"] = [o for o in res["flights_back"] if o["offer_id"] == "B1"]
        for k in ("transfers_out", "transfers_back"):
            res[k] = {h: [t for t in ts if t["kind"] == "shuttle"] for h, ts in res[k].items()}
        vs = by_id(build(sea_brief(), res))
        assert "fastest" not in vs and "comfort" in vs
        assert vs["cheapest"].facts["also_fastest"] is True
        assert check_variant_set(list(vs.values())) == []


class TestFilters:
    def test_past_offers_are_dropped(self):
        vs = build(sea_brief(), sea_results(), today=__import__("datetime").date(2026, 11, 13))
        assert vs == [] or all(s.type == "stay" for v in vs for s in v.steps)

    def test_no_night_flights(self):
        vs = build(sea_brief(**{"constraints.add": ["no_night_flights"]}), sea_results())
        assert all(s.offer_id != "F3" for v in vs for s in v.steps)

    def test_direct_only_user(self):
        vs = build(sea_brief(**{"prefs.direct_only": True}), sea_results())
        assert all(v.facts.get("out_stops", 0) == 0 for v in vs)

    def test_user_meals_is_hard_and_relaxed_honestly(self):
        vs = build(sea_brief(**{"prefs.meals": "breakfast"}), sea_results())
        assert all(v.facts["meals"] != "none" for v in vs)
        res = sea_results()
        res["hotels"] = [h for h in res["hotels"] if h["meals"] == "none"]
        vs = build(sea_brief(**{"prefs.meals": "breakfast"}), res)
        assert vs and all("relaxed_pref" in codes(v.risks) for v in vs)

    def test_no_hotels_is_no_data(self):
        res = sea_results()
        res["hotels"] = []
        assert build(sea_brief(), res) == []

    def test_no_transport_builds_stay_only_and_says_so(self):
        res = sea_results()
        res["flights_back"] = []
        vs = build(sea_brief(), res)
        assert vs and all(v.steps[0].type == "stay" and len(v.steps) == 1 for v in vs)
        assert all(v.risks[0].code == "no_transport" for v in vs)

    def test_raw_tool_outputs_accepted(self):
        res = sea_results()
        res["hotels"] = {"offers": res["hotels"], "total_found": 5}
        assert len(build(sea_brief(), res)) == 3

    def test_without_transfers_scope(self):
        b = sea_brief(scope=["flights", "stay"])
        vs = build(b, sea_results())
        assert all(s.type != "transfer" for v in vs for s in v.steps)
        assert all("no_transfer" in codes(v.risks) for v in vs)
        for v in vs:
            issues = check_variant(v, b, RULES)
            assert {i["code"] for i in issues} <= {"no_transfer", "late_checkin", "risky_connection"}


def family_results():
    tz = "+03:00"
    out = [flight("SF1", "VKO", "AER", f"2026-11-12T20:30:00{tz}", f"2026-11-12T23:00:00{tz}", 30000),
           flight("SF2", "VKO", "AER", f"2026-11-12T09:00:00{tz}", f"2026-11-12T11:30:00{tz}", 42000, baggage=20)]
    back = [flight("SB1", "AER", "VKO", f"2026-11-19T12:00:00{tz}", f"2026-11-19T14:30:00{tz}", 31000)]
    hotels = [
        hotel("SH1", "s1", "Апартаменты у моря", 30000, "2026-11-12", "2026-11-19", cap_adults=2, cap_children=0,
              beach=200),
        hotel("SH2", "s2", "Бутик для взрослых", 35000, "2026-11-12", "2026-11-19", cap_adults=4,
              children_allowed=False, beach=100),
        hotel("SH3", "s3", "Семейный отель Чайка", 50000, "2026-11-12", "2026-11-19", cap_adults=2, cap_children=2,
              stars=3, beach=400, meals="breakfast", osm_id="node/f3"),
        hotel("SH4", "s4", "Парк-отель", 65000, "2026-11-12", "2026-11-19", cap_adults=2, cap_children=1, stars=4,
              rating=9.0, beach=150, meals="half_board", free_until="2026-11-10T12:00:00+03:00", penalty_pct=0,
              osm_id="node/f4"),
    ]
    tr = {h: [transfer(f"ST{h}", "taxi", 30, 1500)] for h in ("s3", "s4")}
    trb = {h: [transfer(f"SR{h}", "taxi", 30, 1500)] for h in ("s3", "s4")}
    return {"flights_out": out, "flights_back": back, "hotels": hotels, "transfers_out": tr, "transfers_back": trb,
            "destination": {"id": "sochi", "name": "Сочи", "country": "Россия"}}


FAMILY_DISTRICTS = {
    "node/f3": {"scores": {"lively": 0.2, "quiet": 0.6, "family": 0.9, "walkable": 0.5}, "counts": {"playground": 4}},
    "node/f4": {"scores": {"lively": 0.65, "quiet": 0.2, "family": 0.3, "walkable": 0.7}, "counts": {"playground": 0}},
}


class TestFamily:
    def setup_method(self):
        self.b = brief({"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-11-12",
                        "dates.end": "2026-11-19", "party.segment": "family", "party.adults": 2,
                        "party.children_ages": [4], "traits.add": ["sea"]})
        self.vs = build(self.b, family_results(), FAMILY_DISTRICTS)
        self.v = by_id(self.vs)

    def test_party_fit_filters_rooms_and_policies(self):
        names = {v.hotel.name for v in self.vs}
        assert "Апартаменты у моря" not in names and "Бутик для взрослых" not in names
        assert all(not [i for i in check_variant(v, self.b, RULES) if i["code"] in ("room_capacity", "children_policy")]
                   for v in self.vs)

    def test_family_district_weights(self):
        assert self.v["cheapest"].hotel.name == "Семейный отель Чайка"
        f3 = self.v["cheapest"].fit.breakdown["district"]
        other = next(v for v in self.vs if v.hotel.name == "Парк-отель").fit.breakdown["district"]
        assert f3 > other
        assert self.v["cheapest"].fit.breakdown["constraints"] == 1.0

    def test_night_arrival_and_late_checkin_risks(self):
        cheap = self.v["cheapest"]
        assert cheap.steps[0].offer_id == "SF1"
        assert {"late_checkin"} <= set(codes(cheap.risks))

    def test_noisy_district_not_for(self):
        park = next(v for v in self.vs if v.hotel.name == "Парк-отель")
        assert {"noisy_evening", "no_playground"} <= set(codes(park.not_for))


class TestFriends:
    def test_lively_district_wins_for_friends(self):
        from agent.ranking import district_fit
        friends = sea_brief(**{"party.segment": "friends", "party.adults": 3})
        couple = sea_brief(**{"party.segment": "couple"})
        lively, quiet = DISTRICTS["node/h3"], DISTRICTS["node/h1"]
        assert district_fit(friends, lively, SEGMENTS) > district_fit(couple, lively, SEGMENTS)
        assert district_fit(couple, quiet, SEGMENTS) > district_fit(friends, quiet, SEGMENTS)
        vs = build(friends, sea_results())
        assert all(v.fit.breakdown["district"] == district_fit(friends, DISTRICTS[f"node/{v.hotel.hotel_id}"], SEGMENTS)
                   for v in vs)

    def test_user_district_pref_overrides_segment(self):
        b = sea_brief(**{"prefs.district": "lively"})
        v = build(b, sea_results())[0]
        assert fit(b, v, RULES, SEGMENTS).breakdown["district"] == v.facts["district_lively"]


def city_results():
    tz = "+03:00"
    return {
        "trains_out": [train("TR1", "Москва Казанская", "Казань-Пассажирская", f"2026-11-12T22:00:00{tz}",
                             f"2026-11-13T07:30:00{tz}", 6000),
                       train("TR2", "Москва Казанская", "Казань-Пассажирская", f"2026-11-13T07:00:00{tz}",
                             f"2026-11-13T13:30:00{tz}", 9000, car_class="seat", train_no="Ласточка")],
        "trains_back": [train("TB1", "Казань-Пассажирская", "Москва Казанская", f"2026-11-16T15:00:00{tz}",
                              f"2026-11-16T21:30:00{tz}", 9000, car_class="seat")],
        "flights_out": [flight("KF1", "SVO", "KZN", f"2026-11-13T08:00:00{tz}", f"2026-11-13T09:35:00{tz}", 14000)],
        "flights_back": [flight("KB1", "KZN", "SVO", f"2026-11-16T18:00:00{tz}", f"2026-11-16T19:35:00{tz}", 13000)],
        "hotels": [hotel("KH1", "k1", "Отель на Баумана", 21000, "2026-11-13", "2026-11-16", stars=3,
                         osm_id="node/k1"),
                   hotel("KH2", "k2", "Гранд Отель Казань", 39000, "2026-11-13", "2026-11-16", stars=5, rating=9.4,
                         meals="breakfast", free_until="2026-11-11T12:00:00+03:00", penalty_pct=0)],
        "transfers_out": {"k1": [transfer("KT1", "taxi", 20, 600)], "k2": [transfer("KT2", "taxi", 25, 700)]},
        "transfers_back": {"k1": [transfer("KR1", "taxi", 20, 600)], "k2": [transfer("KR2", "taxi", 25, 700)]},
        "destination": {"id": "kazan", "name": "Казань", "country": "Россия"},
    }


class TestCity:
    def setup_method(self):
        self.b = brief({"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-11-13",
                        "dates.end": "2026-11-16", "party.segment": "solo", "party.adults": 1,
                        "traits.add": ["city"]})
        self.vs = by_id(build(self.b, city_results(), {}))

    def test_overnight_train_is_cheapest_and_checks_pass(self):
        cheap = self.vs["cheapest"]
        assert cheap.steps[0].type == "train" and cheap.steps[0].offer_id == "TR1"
        assert [i for i in check_variant(cheap, self.b, RULES) if i["severity"] == "block"] == []

    def test_flight_is_fastest(self):
        assert self.vs["fastest"].steps[0].type == "flight"

    def test_origin_enforced_with_station_names(self):
        cheap = self.vs["cheapest"]
        assert cheap.steps[0].from_.startswith("Москва")

    def test_user_transport_train(self):
        b = brief({"origin.city": "Москва", "dates.start": "2026-11-13", "dates.end": "2026-11-16",
                   "prefs.transport": "train"})
        vs = build(b, city_results(), {})
        assert all(s.type != "flight" for v in vs for s in v.steps)

    def test_no_district_data_no_district_criterion(self):
        assert "district" not in self.vs["cheapest"].fit.breakdown


class TestStayOnly:
    def setup_method(self):
        self.b = brief({"destination.name": "Анталья", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
                        "party.adults": 2, "scope": ["stay"], "traits.add": ["sea"]})
        self.vs = build(self.b, sea_results())

    def test_stay_only_variants(self):
        assert [v.id for v in self.vs][:2] == ["cheapest", "comfort"]
        assert all(len(v.steps) == 1 and v.steps[0].type == "stay" for v in self.vs)
        assert all(v.totals.door_to_door_hours == 0 for v in self.vs)
        assert self.vs[0].totals.rub == 40000

    def test_location_variant_when_distinct(self):
        ids = [v.id for v in self.vs]
        if "custom-1" in ids:
            v = by_id(self.vs)["custom-1"]
            assert v.hotel.hotel_id not in {by_id(self.vs)[i].hotel.hotel_id for i in ("cheapest",)}

    def test_checks_pass(self):
        for v in self.vs:
            assert [i for i in check_variant(v, self.b, RULES) if i["severity"] == "block"] == []

    def test_no_time_criterion(self):
        assert "time" not in self.vs[0].fit.breakdown


def test_rules_override_thresholds():
    r = rules(ranking={"tight_layover_min": 30})
    vs = by_id(build(sea_brief(), sea_results(), r=r))
    assert "tight_layover" not in codes(vs["cheapest"].risks)


def test_inputs_not_mutated():
    res = sea_results()
    snap = copy.deepcopy(res)
    build(sea_brief(), res)
    assert res == snap
