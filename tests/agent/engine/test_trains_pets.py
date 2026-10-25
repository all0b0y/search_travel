"""Trains and pets end to end, offline on the real mock + real data (held-out L3 h09_corgi_sapsan,
evals/runs/20261025-125123-L3-holdout):

- t1 «С корги на выходные в Петербург на Сапсане — реально?» recommended a 11,8 h overnight train: a named
  high-speed train is a hard (relaxable) transport preference — every train leg is «Сапсан» when there is one;
- t2/t3 «А с корги в Сапсан точно пустят? Какие правила для собак?» got «нет данных» twice: train and flight
  offers carry pet terms, the variants and the info facts carry ready Russian lines, the fee is in the totals.
"""
import json

import pytest

from agent.checks import blocking, check_variant, fmt_rub
from agent.ranking import HIGH_SPEED, PETS_CARRIAGE

from .fakes import u
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)

H09_TEXT = "С корги на выходные в Петербург на Сапсане — реально?"
H09_PATCH = {"destination.name": "Санкт-Петербург", "dates.start": "2026-10-31", "dates.end": "2026-11-01",
             "party.pets": ["dog"], "prefs.transport": "train", "constraints.add": ["pet:dog"]}


def no_blocks(h):
    s = h.session()
    for v in s.variants:
        assert blocking(check_variant(v, s.brief, h.engine.skill.rules)) == [], (v.id, check_variant(v, s.brief, h.engine.skill.rules))


def trains_of(v):
    return [s for s in v.steps if s.type == "train"]


def card_input(h, card):
    req = [r for r in h.backend.requests if r.card == card][-1]
    return json.loads(req.user)


def h09_first_turn(h, text=H09_TEXT, patch=None):
    h.understand(u(set=patch or H09_PATCH))
    r = h.turn(text)
    assert r.get("error") is None, r["message"]
    assert r["stage"] == "selection" and r["cards"]["variants"], r["message"]
    return r


# ---------------------------------------------------------------- t1: «на Сапсане» is a transport preference

def test_h09_sapsan_cue_keeps_only_sapsan_legs_with_pet_fee(live):
    h = live
    h09_first_turn(h)
    s = h.session()
    assert "train:sapsan" in s.brief.values()["constraints"]
    assert s.brief.values()["prefs"]["transport"] == "train"
    for v in s.variants:
        legs = trains_of(v)
        assert len(legs) == 2 and all("Сапсан" in (st.carrier or "") for st in legs), (v.id, [st.carrier for st in legs])
        assert v.facts.get("out_high_speed") and v.facts.get("back_high_speed")
        assert "relaxed_pref" not in [n.code for n in v.risks]
        # pet-friendly hotel, the dog's fee as its own line inside the total
        assert v.facts.get("pets_allowed") is True
        fee = v.facts["price_pets_rub"]
        assert fee == v.facts["out_pet_fee_rub"] + v.facts["back_pet_fee_rub"] > 0
        assert v.totals.rub == sum(st.price.rub for st in v.steps)
        for d in ("out", "back"):
            line = v.facts[f"{d}_pets_rule"]
            assert line.startswith("В «Сапсане» — только в переноске до 180 см по сумме трёх сторон"), line
            assert "на местах для пассажиров с животными" in line and "крупных собак не возят" in line
            assert f"провоз {fmt_rub(v.facts[f'{d}_pet_fee_rub'])}" in line
    # «Дешевле» is the cheapest Sapsan option
    cheapest = next(v for v in s.variants if v.id == "cheapest")
    assert cheapest.totals.rub == min(v.totals.rub for v in s.variants)
    no_blocks(h)
    # the card input carries the lines and the fee
    item = card_input(h, "present.variants")["items"][0]
    assert item["facts"]["out_pets_rule"] and item["facts"]["price_pets_rub"]


def test_h09_cart_total_matches_variant_with_pet_fee(live):
    h = live
    h09_first_turn(h)
    v = next(v for v in h.session().variants if v.id == "cheapest")
    r = h.act("choose_variant", variant_id="cheapest")
    assert r["stage"] == "cart", r["message"]
    assert r["cart"]["total_rub"] == v.totals.rub          # the mock priced the dog into the tickets too
    trains = [i for i in r["cart"]["items"] if i["kind"] == "train"]
    assert len(trains) == 2


def test_without_sapsan_cue_overnight_trains_stay(live):
    """The same trip without the cue: the cheapest is the overnight train, as before (no regression)."""
    h = live
    h09_first_turn(h, text="С корги на выходные в Петербург поездом", patch=H09_PATCH)
    s = h.session()
    assert not any(t.startswith("train:") for t in s.brief.values()["constraints"])
    cheapest = next(v for v in s.variants if v.id == "cheapest")
    assert not all("Сапсан" in (st.carrier or "") for st in trains_of(cheapest))
    assert cheapest.facts["out_pets_rule"]       # pet lines come with any train


def test_any_train_cue_drops_sapsan(live):
    h = live
    h09_first_turn(h)
    h.understand(u("refine"))
    r = h.turn("ладно, можно и не Сапсан, любой поезд")
    assert r.get("error") is None, r["message"]
    s = h.session()
    assert "train:sapsan" not in (s.brief.values()["constraints"] or [])
    carriers = {st.carrier for v in s.variants for st in trains_of(v)}
    assert any("Сапсан" not in (c or "") for c in carriers), carriers


def test_no_sapsan_on_route_relaxes_with_plain_risk(live):
    h = live
    h09_first_turn(h, text="В Казань на Сапсане с собакой, 4–7 декабря",
                   patch={"destination.name": "Казань", "dates.start": "2026-12-04", "dates.end": "2026-12-07",
                          "party.pets": ["dog"], "constraints.add": ["pet:dog"]})
    s = h.session()
    assert "train:sapsan" in s.brief.values()["constraints"]
    for v in s.variants:
        assert trains_of(v), v.id
        notes = [n for n in v.risks if n.code == "relaxed_pref" and n.data.get("field") == HIGH_SPEED]
        assert notes and notes[0].text == "«Сапсана» на эти даты нет — показываем другие поезда", [n.text for n in v.risks]
        assert HIGH_SPEED + ".out" in v.facts["relaxed"]
    no_blocks(h)


# ---------------------------------------------------------------- t2: «пустят ли корги в Сапсан» from data

def test_h09_info_pet_question_answered_from_facts(live):
    h = live
    h09_first_turn(h)
    h.understand(u("info", info="А с корги в Сапсан точно пустят? Какие правила для собак?"))
    r = h.turn("А с корги в Сапсан точно пустят? Какие правила для собак?")
    assert r.get("error") is None, r["message"]
    facts = card_input(h, "present.info")["facts"]
    variants = [f for f in facts if f["kind"] == "variant"]
    assert variants
    for f in variants:
        trains = [leg for leg in f["legs"] if leg["type"] == "train"]
        assert trains and all(leg["pets_rule"].startswith("В «Сапсане» — только в переноске") for leg in trains)
        assert all(leg["high_speed"] is True and leg["pet_fee_rub"] > 0 for leg in trains)
        assert f["price_pets_rub"] == sum(leg["pet_fee_rub"] for leg in trains)
    rules = next(f for f in facts if f["kind"] == "pet_rules")
    assert any("Сапсан" in line and "кроме собак-проводников" in line for line in rules["lines"])


def test_pet_question_without_pet_in_brief_gets_lines(live):
    h = live
    patch = {k: v for k, v in H09_PATCH.items() if k not in ("party.pets", "constraints.add")}
    h09_first_turn(h, text="На выходные в Петербург на Сапсане", patch=patch)
    assert h.session().variants[0].facts.get("price_pets_rub") is None     # no pet: nothing priced
    h.understand(u("info", info="а кошку в Сапсан можно?"))
    h.turn("а кошку в Сапсан можно?")
    facts = card_input(h, "present.info")["facts"]
    legs = [leg for f in facts if f["kind"] == "variant" for leg in f["legs"] if leg["type"] == "train"]
    assert legs and all("pets_rule" in leg and "pet_fee_rub" not in leg for leg in legs)
    assert any(f["kind"] == "pet_rules" for f in facts)


def test_paid_order_info_has_pet_terms(live):
    h = live
    h09_first_turn(h)
    h.act("choose_variant", variant_id="cheapest")
    r = h.act("checkout", travellers=[{"name": "Иван Петров", "birth_date": "1990-01-01"}], contact={"email": "a@b.c"})
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r["stage"] == "ordered", r["message"]
    h.understand(u("info", info="а собаку точно пустят в поезд?"))
    h.turn("а собаку точно пустят в поезд?")
    facts = card_input(h, "present.info")["facts"]
    order = next(f for f in facts if f["kind"] == "order")
    lines = [t["pets_rule"] for t in order["terms"] if "pets_rule" in t]
    assert len(lines) == 2 and all(line.startswith("В «Сапсане»") for line in lines)


# ---------------------------------------------------------------- flights with a cat

def test_flight_trip_with_cat_carries_flight_pet_terms(live):
    h = live
    h.understand(u(set={"destination.name": "Калининград", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
                        "party.segment": "solo", "party.adults": 1, "party.pets": ["cat"],
                        "constraints.add": ["pet:cat"], "prefs.transport": "plane"}))
    r = h.turn("в Калининград с кошкой, 20–23 ноября, лечу один")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    for v in h.session().variants:
        flights = [s for s in v.steps if s.type == "flight"]
        assert flights
        line = v.facts["out_pets_rule"]
        assert "в салоне в переноске, если вместе с ней до 8 кг" in line and "в багажном отсеке" in line, line
        assert v.facts["price_pets_rub"] == v.facts["out_pet_fee_rub"] + v.facts["back_pet_fee_rub"] > 0
        assert v.facts.get("pets_allowed") is True
    no_blocks(h)


# ---------------------------------------------------------------- ranking units

def _leg(kind="train", train_kind="high_speed", small=True, max_animals=2, carrier="РЖД «Сапсан»"):
    from datetime import datetime, timedelta, timezone
    from agent.ranking import Leg
    dep = datetime(2026, 10, 31, 9, 0, tzinfo=timezone(timedelta(hours=3)))
    offer = {"offer_id": "x", "carrier": carrier, "train_no": "751А", "from_station": "A", "to_station": "B",
             "dep": dep.isoformat(), "arr": (dep + timedelta(hours=4)).isoformat(), "duration_min": 240,
             "car_class": "seat", "price_rub": 4000, "train_kind": train_kind,
             "pets": {"small_in_carrier": small, "max_animals": max_animals, "fee_rub": 700, "count": 1,
                      "fee_total_rub": 700, "carrier_max_cm_sum": 180, "large_dogs": False}}
    return Leg.train(offer)


def _brief(**values):
    from agent.state import Brief, apply_patch
    b, _ = apply_patch(Brief(), {"set": values, "unset": []})
    return b


def test_soft_filters_pet_and_high_speed():
    from agent.ranking import _soft_leg_filters
    brief = _brief(**{"party.pets": ["dog"], "constraints.add": ["pet:dog", "train:sapsan"]})
    filters = dict(_soft_leg_filters(brief, "back", {"night_window": ["23:00", "06:00"]}))
    assert set(filters) == {PETS_CARRIAGE, HIGH_SPEED + ".back"}
    assert filters[PETS_CARRIAGE](_leg()) and not filters[PETS_CARRIAGE](_leg(small=False))
    assert filters[HIGH_SPEED + ".back"](_leg()) and not filters[HIGH_SPEED + ".back"](_leg(train_kind="long_distance", carrier="ФПК"))
    # three dogs on one ticket: more than the train takes
    many = _brief(**{"party.pets": ["dog", "dog", "dog"]})
    assert not dict(_soft_leg_filters(many, "out", {"night_window": ["23:00", "06:00"]}))[PETS_CARRIAGE](_leg())


def test_pet_rule_lines_follow_the_terms():
    from agent.ranking import pet_rule_line
    sapsan = _leg().offer
    sapsan["pets"]["places"] = "места для пассажиров с животными"
    assert pet_rule_line("train", sapsan) == ("В «Сапсане» — только в переноске до 180 см по сумме трёх сторон, на "
                                              "местах для пассажиров с животными; провоз 700 ₽; крупных собак "
                                              "не возят, кроме собак-проводников")
    coupe = {"carrier": "ФПК", "train_no": "054А", "car_class": "coupe", "train_kind": "long_distance",
             "pets": {"small_in_carrier": True, "carrier_max_cm_sum": 180, "large_dogs": True,
                      "large_dogs_how": "только в отдельном купе: выкупить все свободные места, собака в наморднике и на поводке",
                      "large_dog_fee_rub": 9300, "fee_rub": 760, "count": 0}}
    line = pet_rule_line("train", coupe) or ""
    assert line.startswith("В купе поезда 054А — только в переноске до 180 см") and "в наморднике и на поводке" in line
    assert pet_rule_line("train", {"carrier": "ФПК"}) is None

