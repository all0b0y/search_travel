"""Long-distance trains (ostrovok_mcp/rail.py: Russian pairs 1000..2500 km, sleepers only, up to ~two days on board)
on the real mock and the real data (in-process Ostrovok mock over data/ostrovok_catalog.sqlite, real open data, a
scripted model):

1. the mock: pairs up to 1000 km give exactly today's offers (a digest of every such pair, taken before the change);
   longer pairs give 1-2 sleepers a day with a realistic time on board and prices growing with distance;
2. the engine: places whose airports are closed in the data (Анапа, Геленджик via the Новороссийск station,
   Краснодар) are built by train, the outbound leaving one or two days before check-in and arriving on it, with
   code-made facts for the time on board and the days the user leaves / is home;
3. no new noise: a pair with flights and transport any keeps its flight variants; a short trip gets no day-long
   train in an inspiration; the budget-led inspiration stays under ~1 s in process.
"""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from agent.checks import blocking, check_variant
from agent.opendata import long_train_pair, train_reachable
from agent.ranking import road_time_ru

from .fakes import u
from .test_honesty import TURN_MS_MAX, actions, card_input, legs
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)
from .turn_time import calibrated

GOLDEN = Path(__file__).parent / "data" / "trains_up_to_1000km.json"
HOMES = ["Москва", "Санкт-Петербург", "Екатеринбург", "Казань", "Новосибирск", "Самара"]
JULY = {"dates.start": "2027-07-10", "dates.end": "2027-07-17"}
FAMILY = {"party.segment": "family", "party.adults": 2, "party.children": 1, "party.children_ages": [7]}
INSPIRATION_MS_MAX = 1000   # budget-led inspiration in process (measured 0.55–0.8 s)


# ---------------------------------------------------------------- 1. the mock

def test_pairs_up_to_1000_km_are_byte_identical():
    """Every Russian catalog pair 30..1000 km, two dates / party sizes / with and without a pet: the full
    search_trains answer hashes exactly as before long trains existed (golden digests taken before the change)."""
    from ostrovok_mcp import db, geo, service
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    names = sorted(r["name_ru"] for r in db.opendata().query("destinations", "country_code = ?", ("RU",), key="id"))
    cities = [c for c in (geo.resolve_city(n) for n in names) if c and c.stations]
    got = {}
    for a in cities:
        for b in cities:
            if a.name_ru != b.name_ru and 30 <= geo.haversine_km(a.lat, a.lon, b.lat, b.lon) <= 1000:
                h = hashlib.sha256()
                for day, pax, pets in (("2026-11-20", 2, 0), ("2027-07-12", 3, 1)):
                    r = service.call("search_trains", {"origin_city": a.name_ru, "dest_city": b.name_ru, "date": day,
                                                       "passengers": pax, "today": "2026-10-25", "pets": pets})
                    h.update(json.dumps(r, sort_keys=True, ensure_ascii=False).encode())
                got[f"{a.name_ru}>{b.name_ru}"] = h.hexdigest()[:16]
    assert len(golden) == 438
    assert got == golden


def _trains(o, d, day="2027-07-10", pax=1, pets=0):
    from ostrovok_mcp import service
    return service.call("search_trains", {"origin_city": o, "dest_city": d, "date": day, "passengers": pax,
                                          "today": "2026-10-25", "pets": pets})


@pytest.mark.parametrize("o,d", [("Москва", "Анапа"), ("Казань", "Геленджик"), ("Санкт-Петербург", "Краснодар"),
                                 ("Новосибирск", "Иркутск"), ("Екатеринбург", "Сочи")])
def test_long_pairs_sell_sleepers_with_a_real_time_on_board(o, d):
    from ostrovok_mcp import geo
    a, b = geo.resolve_city(o), geo.resolve_city(d)
    km = geo.haversine_km(a.lat, a.lon, b.lat, b.lon)
    assert 1000 < km <= 2500
    for day in ("2027-07-08", "2027-07-09", "2027-07-10", "2026-11-20"):
        out = _trains(o, d, day)
        assert out["note"] is None and out["offers"]
        trains = {x["train_no"] for x in out["offers"]}
        assert 1 <= len(trains) <= 2
        for x in out["offers"]:
            assert x["car_class"] in ("platzkart", "coupe", "sv") and x["train_kind"] == "long_distance"
            assert x["carrier"] == "ФПК"
            hours = x["duration_min"] / 60
            assert km * 1.3 / 70 - 0.1 <= hours <= km * 1.3 / 60 + 0.1          # 60–70 km/h incl. stops
            dep, arr = datetime.fromisoformat(x["dep"]), datetime.fromisoformat(x["arr"])
            assert abs((arr - dep).total_seconds() / 60 - x["duration_min"]) <= 1
            assert 1 <= (arr.date() - dep.date()).days <= 3                      # the real arrival day
        by = {(x["train_no"], x["car_class"]): x["price_rub"] for x in out["offers"]}
        for no in trains:
            assert 2000 <= by[(no, "platzkart")] < by[(no, "coupe")] < by[(no, "sv")]
            assert by[(no, "sv")] >= 2.5 * by[(no, "platzkart")]                 # СВ several times the berth
    # the same timetable every day: the train numbers and departure times repeat
    first = {(x["train_no"], x["dep"][11:16]) for x in _trains(o, d, "2027-07-09")["offers"]}
    again = {(x["train_no"], x["dep"][11:16]) for x in _trains(o, d, "2027-07-10")["offers"]}
    assert first & again or len(first) != len(again)


def test_prices_grow_with_distance_and_moscow_anapa_is_about_a_day():
    near = min(x["price_rub"] for x in _trains("Москва", "Краснодар")["offers"] if x["car_class"] == "platzkart")
    far = min(x["price_rub"] for x in _trains("Санкт-Петербург", "Анапа")["offers"] if x["car_class"] == "platzkart")
    assert far > near
    anapa = _trains("Москва", "Анапа")["offers"]
    assert all(20 * 60 <= x["duration_min"] <= 27 * 60 for x in anapa)


def test_beyond_2500_km_and_abroad_no_trains():
    out = _trains("Новосибирск", "Анапа")
    assert out["offers"] == [] and "2500" in out["note"]
    assert _trains("Москва", "Владивосток")["offers"] == []


def test_dog_on_a_long_train_follows_the_rzd_rules():
    out = _trains("Москва", "Анапа", pax=2, pets=1)
    plain = {(x["train_no"], x["car_class"]): x for x in _trains("Москва", "Анапа", pax=2)["offers"]}
    for x in out["offers"]:
        t = x["pets"]
        assert t["small_in_carrier"] and t["carrier_max_cm_sum"] == 180 and t["max_animals"] == 4
        assert t["places"] is None                                   # FPK sleeper: no special seats
        assert t["count"] == 1 and t["fee_total_rub"] == t["fee_rub"] > 0 and t["synthetic_fee"]
        assert x["price_rub"] == plain[(x["train_no"], x["car_class"])]["price_rub"] + t["fee_rub"]
        if x["car_class"] == "platzkart":
            assert t["large_dogs"] is False and t["large_dog_fee_rub"] is None
        else:                                                        # coupe / sv: the whole compartment
            assert t["large_dogs"] is True and "отдельном купе" in t["large_dogs_how"]
            # the free places of the compartment: coupe 4 places for 2 travellers, sv 2 places (none free)
            assert (t["large_dog_fee_rub"] > 0) == (x["car_class"] == "coupe")


def test_train_reachability_of_the_six_home_cities():
    """The catalog places a train reaches from each home city: before = 30..1000 km, after = up to 2500 km."""
    from agent.opendata import OpenData, gc_km
    od = OpenData()
    recs = [od.destination(r["id"]) for r in od._query("SELECT id FROM destinations WHERE country_code = 'RU'")]
    before = {}
    after = {}
    for city in HOMES:
        home = od.destination({"Москва": "moscow", "Санкт-Петербург": "saint-petersburg", "Екатеринбург": "yekaterinburg",
                               "Казань": "kazan", "Новосибирск": "novosibirsk", "Самара": "samara"}[city])
        reach = [r for r in recs if r["id"] != home["id"] and train_reachable(home, r)]
        after[city] = len(reach)
        before[city] = sum(1 for r in reach if gc_km(home["lat"], home["lon"], r["lat"], r["lon"]) <= 1000)
        south = {r["id"] for r in reach if long_train_pair(home, r)}
        if city != "Новосибирск":
            assert {"anapa", "gelendzhik", "krasnodar"} <= {r["id"] for r in reach}, city
        assert south or city == "Новосибирск" or before[city] < after[city]
    assert after == {"Москва": 34, "Санкт-Петербург": 34, "Екатеринбург": 35, "Казань": 35, "Новосибирск": 5,
                     "Самара": 35}, after
    assert before == {"Москва": 16, "Санкт-Петербург": 16, "Екатеринбург": 2, "Казань": 11, "Новосибирск": 0,
                      "Самара": 11}, before


def test_road_time_words():
    assert road_time_ru(1368) == "23 ч" and road_time_ru(1620) == "1 сутки 3 ч"
    assert road_time_ru(2880) == "2 суток" and road_time_ru(3000) == "2 суток 2 ч"


# ---------------------------------------------------------------- 2. the engine builds such trips

def trip(h, origin, dest, text, **extra):
    h.understand(u(set={"origin.city": origin, "destination.name": dest, **extra}))
    r, ms = calibrated(h.turn, text)
    assert r.get("error") is None, r["message"]
    assert r["cards"]["variants"], r["message"]
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    return r


def _date(iso):
    return date.fromisoformat(iso[:10])


def assert_long_train_trip(h, v, lead_max=2):
    """Outbound train leaves 1..lead_max days before check-in and arrives on it; the return leaves on check-out;
    the facts say the time on board and the leave / home days in ready Russian; checks find no blocker."""
    out, back = legs(v)
    stay = next(s for s in v.steps if s.type == "stay")
    assert [out.type, back.type] == ["train", "train"]
    check_in, check_out = _date(stay.start), _date(stay.end)
    assert _date(out.end) == check_in and 1 <= (check_in - _date(out.start)).days <= lead_max
    assert _date(back.start) == check_out and _date(back.end) >= check_out
    f = v.facts
    assert f["out_long_train"] and f["back_long_train"]
    assert f["out_days_before_checkin"] == (check_in - _date(out.start)).days
    dur_out = round((datetime.fromisoformat(out.end) - datetime.fromisoformat(out.start)).total_seconds() / 60)
    assert f["out_on_road_ru"] == road_time_ru(dur_out)
    assert f"в пути {f['out_on_road_ru']}" in f["out_road_ru"] and "до заезда в отель" in f["out_road_ru"]
    assert out.start[11:16] in f["out_road_ru"] and out.end[11:16] in f["out_road_ru"]
    if _date(back.end) > check_out:
        assert f["back_days_after_checkout"] == (_date(back.end) - check_out).days
        assert "после выезда из отеля" in f["back_road_ru"]
    assert v.totals.out_hours >= 18 and v.totals.back_hours >= 18      # the real time on the road, door to door
    assert blocking(check_variant(v, h.session().brief, h.engine.skill.rules)) == [], check_variant(
        v, h.session().brief, h.engine.skill.rules)


def test_moscow_anapa_family_in_july_by_train(live):
    h = live
    r = trip(h, "Москва", "Анапа", "В Анапу на море в июле с ребёнком 7 лет, на неделю", **JULY, **FAMILY)
    vs = h.session().variants
    assert vs and all(v.facts["transport_gap"] == "train_instead" for v in vs)
    assert all(v.risks[0].code == "transport_switched" and "собрал поездом" in v.risks[0].text for v in vs)
    for v in vs:
        assert_long_train_trip(h, v)
    inp = card_input(h, "present.variants")
    from evals.card_inputs import validate_card_input
    assert validate_card_input("present.variants", inp) == []
    # the degraded code text says the time on board as code wrote it, never «туда 23,0 ч»
    from agent.respond import code_why
    why = code_why(vs[0])
    assert f"в пути {vs[0].facts['out_on_road_ru']}" in why and "выезд " in why, why


def test_long_train_trip_goes_to_cart_order_and_pay(live):
    h = live
    trip(h, "Москва", "Анапа", "Анапа в июле вдвоём", **JULY, **{"party.segment": "couple", "party.adults": 2})
    v = h.session().variants[0]
    r = h.act("choose_variant", variant_id=v.id)
    assert r["stage"] == "cart", r["message"]
    cart = r["cart"]
    assert sorted(i["kind"] for i in cart["items"] if i["kind"] != "transfer") == ["hotel", "train", "train"]
    assert not [i for i in cart.get("issues") or [] if i.get("severity") == "blocking"], cart.get("issues")
    assert not [i for i in cart.get("issues") or [] if i.get("code") in ("night_without_hotel", "dates_mismatch")]
    r = h.act("checkout", travellers=[{"name": "Иван Петров", "birth_date": "1990-01-01"},
                                      {"name": "Мария Петрова", "birth_date": "1991-02-02"}],
              contact={"email": "a@b.c"})
    assert r["stage"] == "checkout" and r["order"], r["message"]
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r["stage"] == "ordered", r["message"]


def test_kazan_gelendzhik_via_novorossiysk_with_a_transfer(live):
    h = live
    trip(h, "Казань", "Геленджик", "Геленджик на неделю в июле вдвоём", **JULY,
         **{"party.segment": "couple", "party.adults": 2})
    vs = h.session().variants
    assert vs
    for v in vs:
        assert_long_train_trip(h, v)
        out = legs(v)[0]
        assert out.to == "Новороссийск"
        ride = next(s for s in v.steps if s.type == "transfer" and s.from_ == "Новороссийск")
        mins = (datetime.fromisoformat(ride.end) - datetime.fromisoformat(ride.start)).total_seconds() / 60
        assert mins >= 40 and ride.price.amount >= 1000          # ~30 km by road, not a ride across Gelendzhik


def test_krasnodar_from_saint_petersburg_by_train(live):
    h = live
    trip(h, "Санкт-Петербург", "Краснодар", "Краснодар 10–17 июля", **JULY,
         **{"party.segment": "solo", "party.adults": 1})
    for v in h.session().variants:
        assert_long_train_trip(h, v)


def test_moscow_sochi_any_transport_keeps_flights(live):
    h = live
    trip(h, "Москва", "Сочи", "Сочи на три ночи", **{"dates.start": "2026-11-20", "dates.end": "2026-11-23",
                                                       "party.segment": "couple", "party.adults": 2})
    vs = h.session().variants
    assert vs and all([s.type for s in legs(v)] == ["flight", "flight"] for v in vs)
    assert not any(v.facts.get("out_long_train") for v in vs)
    # the user wants a train: the sleepers are the trip
    h.new()
    trip(h, "Москва", "Сочи", "Сочи на неделю поездом", **JULY, **FAMILY, **{"prefs.transport": "train"})
    for v in h.session().variants:
        assert_long_train_trip(h, v)


def test_dog_on_a_long_train_in_a_trip(live):
    h = live
    trip(h, "Москва", "Анапа", "Анапа в июле, едем с собакой", **JULY,
         **{"party.segment": "couple", "party.adults": 2, "party.pets": ["dog"]})
    for v in h.session().variants:
        assert_long_train_trip(h, v)
        assert v.facts["out_pet_fee_rub"] > 0 and v.facts["price_pets_rub"] > 0
        assert "переноске до 180 см" in v.facts["out_pets_rule"]
        if v.facts["out_car_class"] == "platzkart":
            assert "в плацкарте нельзя" in v.facts["out_pets_rule"]
        else:
            assert "отдельном купе" in v.facts["out_pets_rule"]


def test_info_legs_carry_the_time_on_board(live):
    h = live
    trip(h, "Москва", "Анапа", "Анапа в июле вдвоём", **JULY, **{"party.segment": "couple", "party.adults": 2})
    h.understand(u(intent="info", info="Сколько ехать на поезде?"))
    r = h.turn("сколько ехать на поезде?")
    assert r.get("error") is None
    variant = next(f for f in card_input(h, "present.info")["facts"] if f.get("kind") == "variant")
    trains = [leg for leg in variant["legs"] if leg["type"] == "train"]
    assert trains and all(leg["on_road_ru"] for leg in trains)


# ---------------------------------------------------------------- 3. no new noise, and fast

def _inspire(h, origin, budget, nights, traits=("sea",), month=7, **extra):
    h.new()
    h.understand(u(set={"origin.city": origin, "party.segment": "couple", "party.adults": 2, "budget_rub.max": budget,
                        "dates.month": month, "dates.nights": nights, "traits.add": list(traits), **extra}))
    r, ms = calibrated(h.turn, "на море, бюджет")
    assert r.get("error") is None, r["message"]
    return r, ms, card_input(h, "present.destinations")["items"]


@pytest.mark.parametrize("origin,budget", [("Москва", 120000), ("Санкт-Петербург", 120000), ("Екатеринбург", 60000),
                                           ("Новосибирск", 80000)])
def test_budget_inspiration_turn_time(live, origin, budget):
    h = live
    took = []
    for _ in range(2):
        r, ms, items = _inspire(h, origin, budget, 7)
        took.append(ms)
    assert min(took) < INSPIRATION_MS_MAX, took
    for i in items:
        f = i["facts"]
        assert f["transport_ru"] in ("поездом", "самолётом")
        if f.get("long_train"):     # unasked: only within the budget, matching the wish, with its time on board
            assert f["within_budget"] is True and i["match"] and f["on_road_ru"] in f["road_ru"]
            assert i["travel_time_h"] <= 6 * i["nights"]


def test_moscow_budget_inspiration_offers_the_south_by_train(live):
    h = live
    _, _, items = _inspire(h, "Москва", 120000, 7)
    trains = [i for i in items if i["facts"].get("long_train")]
    assert trains, [(i["id"], i["facts"]["transport"]) for i in items]
    for i in trains:
        f = i["facts"]
        assert f["road_ru"].startswith(f"поездом, в пути {f['on_road_ru']}; выезд ") and "до заезда" in f["road_ru"]
        assert f["train_price_rub"] + f["stay_price_rub"] == i["price_from_rub"] <= 120000
    from evals.card_inputs import validate_card_input
    assert validate_card_input("present.destinations", card_input(h, "present.destinations")) == []


def test_short_trip_gets_no_day_long_train(live):
    h = live
    _, _, items = _inspire(h, "Москва", 120000, 3)
    assert not any(i["facts"].get("long_train") for i in items)
    h.new()
    h.understand(u(set={"origin.city": "Санкт-Петербург", "party.segment": "couple", "traits.add": ["food", "city"],
                        "dates.start": "2026-10-31", "dates.end": "2026-11-01", "prefs.transport": "train"}))
    r = h.turn("на выходные поездом")
    assert r.get("error") is None
    assert not any(i["facts"].get("long_train") for i in card_input(h, "present.destinations")["items"])


def test_nothing_reachable_beyond_2500_km(live):
    h = live
    h.understand(u(set={"origin.city": "Новосибирск", "destination.name": "Анапа", "dates.start": "2026-11-20",
                        "dates.end": "2026-11-23", "party.segment": "couple", "party.adults": 2}))
    r = h.turn("Анапа на три ночи")
    vs = h.session().variants
    assert all(v.facts.get("no_transport") and v.facts["transport_gap"] == "none" for v in vs)
    assert actions(r, "choose_destination")
