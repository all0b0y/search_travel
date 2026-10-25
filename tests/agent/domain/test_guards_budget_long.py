"""Two claims the guards used to miss (agent/guards.py budget_claim_issues, long_train_claim_issues; our own
examples):

a. present.destinations (or the variants' other_places) says «В N ₽ ничего не укладывается» / «почти ничего …»
   while code marked places within the budget (facts.within_budget true) — block;
b. a long-distance train (facts out|back_long_train, destinations facts.long_train): the time on board as decimal
   hours of 18 h and more («обратно 24,1 ч») — block; a recommended trip that leaves days before check-in
   (out_days_before_checkin) naming neither the leave day from out_road_ru nor «за день / за два дня до заезда» —
   block. Both go through check_card_output, so the engine reruns once and then uses the code text.
"""
import pytest

from agent.guards import budget_claim_issues, change_claim_issues, check_card_output, long_train_claim_issues
from agent.skill_loader import load_skill

RULES = load_skill().rules


def dest(did, name, price, within, **facts):
    return {"id": did, "label": "1", "name": name, "country": "Россия", "price_from_rub": price, "nights": 3,
            "party_label": "1 взрослый", "weather": None, "travel_time_h": 11.8, "match": [],
            "facts": {"within_budget": within, "transport": "train", "transport_ru": "поездом", **facts}}


DEST_INPUT = {"brief": {"budget_rub": {"max": 25000}},
              "items": [dest("kazan", "Казань", 18440, True), dest("samara", "Самара", 23960, True),
                        dest("sochi", "Сочи", 61200, False)]}
NONE_FITS = {"brief": {"budget_rub": {"max": 25000}},
             "items": [dest("sochi", "Сочи", 61200, False), dest("kaliningrad", "Калининград", 48100, False)]}


def out(message, items=()):
    return {"message": message, "items": list(items), "recommended_id": None, "upsell": []}


@pytest.mark.parametrize("message,inp,blocked", [
    ("В 25 000 ₽ ничего не укладывается — вот самые доступные варианты: Казань поездом — от 18 440 ₽.", DEST_INPUT, True),
    ("В 25 000 ₽ почти ничего не укладывается, ближе всего Казань.", DEST_INPUT, True),
    ("В бюджет ничего не влезает, но Казань поездом — от 18 440 ₽.", DEST_INPUT, True),
    ("Ни одно направление не укладывается в 25 000 ₽.", DEST_INPUT, True),
    ("В 25 000 ₽ укладываются Казань поездом — от 18 440 ₽ и Самара поездом — от 23 960 ₽.", DEST_INPUT, False),
    ("Сочи дороже бюджета: от 61 200 ₽.", DEST_INPUT, False),
    ("В 25 000 ₽ ничего не укладывается — вот самые доступные варианты: Калининград — от 48 100 ₽.", NONE_FITS, False),
])
def test_destinations_nothing_fits_claim(message, inp, blocked):
    found = budget_claim_issues("present.destinations", inp, out(message))
    assert bool(found) is blocked, found
    full = [i for i in check_card_output("present.destinations", inp, out(message), RULES) if i["code"] == "budget_claim"]
    assert bool(full) is blocked
    if blocked:
        assert full[0]["severity"] == "block" and "Казань" in full[0]["message"]


def test_nothing_fits_in_a_why_is_caught_too():
    bad = out("Вот куда можно.", [{"id": "kazan", "why": "Почти ничего не укладывается в бюджет, но Казань ближе всех.",
                                   "watch_out": None}])
    assert budget_claim_issues("present.destinations", DEST_INPUT, bad)


def test_other_places_claim_on_variants_needs_a_place_word():
    inp = {"items": [], "other_places": [{"id": "kazan", "name": "Казань", "price_from_rub": 18440, "nights": 3,
                                          "transport_ru": "поездом", "within_budget": True, "price_includes": None}]}
    assert budget_claim_issues("present.variants", inp, out("В бюджет не укладывается ни одно другое место."))
    # the variants themselves above the budget: honest, no place claim
    assert not budget_claim_issues("present.variants", inp, out("В 25 000 ₽ ничего из этих вариантов не укладывается."))
    inp_none = {"items": [], "other_places": [dict(inp["other_places"][0], within_budget=False)]}
    assert not budget_claim_issues("present.variants", inp_none, out("В бюджет не укладывается ни одно другое место."))


LONG_FACTS = {"out_long_train": True, "back_long_train": True, "out_on_road_ru": "1 сутки 3 ч",
              "back_on_road_ru": "1 сутки", "out_days_before_checkin": 1,
              "out_road_ru": "туда поездом: отправление 4 июля в 10:30, прибытие 5 июля в 12:35, в пути 1 сутки 3 ч — "
                             "выезжаете за день до заезда в отель",
              "back_road_ru": "обратно поездом: отправление 12 июля в 15:45, прибытие 13 июля в 17:05, в пути 1 сутки"}


def variant(vid="cheapest", facts=None):
    return {"id": vid, "title": "Дешевле", "steps": [{"type": "train"}], "facts": dict(facts or {}),
            "risks": [], "sacrifices": [], "not_for": []}


VAR_INPUT = {"items": [variant(facts=LONG_FACTS)], "recommended_hint": "cheapest", "changes": []}


def vout(message, why="Поездом, в пути 1 сутки 3 ч."):
    return {"message": message, "items": [{"id": "cheapest", "why": why, "sacrifices": [], "risks": [], "not_for": []}],
            "recommended_id": "cheapest", "upsell": []}


@pytest.mark.parametrize("message,kinds", [
    ("Советую «Дешевле»: туда 27,1 ч, обратно 24,1 ч.", ["decimal_hours", "leave_day"]),
    ("Советую «Дешевле»: поездом, в пути 1 сутки 3 ч, обратно 24,1 ч; выезд 4 июля.", ["decimal_hours"]),
    ("Советую «Дешевле»: поездом, в пути 1 сутки 3 ч.", ["leave_day"]),
    ("Советую «Дешевле»: поездом, в пути 1 сутки 3 ч, выезд 4 июля — за день до заезда.", []),
    ("Советую «Дешевле»: поездом, в пути 1 сутки 3 ч; выезжаете за день до заезда в отель.", []),
    ("Советую «Дешевле»: поездом в пути 1 сутки 3 ч, отправление 4 июля в 10:30.", []),
    ("Трансфер 1,5 ч, поезд — в пути 1 сутки 3 ч, выезд 4 июля.", []),
])
def test_long_train_claims(message, kinds):
    found = long_train_claim_issues("present.variants", VAR_INPUT, vout(message))
    assert [i["data"]["kind"] for i in found] == kinds, found
    assert all(i["code"] == "long_train_claim" and i["severity"] == "block" for i in found)
    via = [i["data"]["kind"] for i in change_claim_issues("present.variants", VAR_INPUT, vout(message))
           if i["code"] == "long_train_claim"]
    assert via == kinds


def test_long_train_claims_only_with_a_long_leg():
    short = {"items": [variant(facts={"out_kind": "train"})], "recommended_hint": "cheapest"}
    assert long_train_claim_issues("present.variants", short, vout("Туда 11,8 ч, обратно 9,4 ч.")) == []
    # a decimal under 18 h next to a long leg is fine (a transfer, a short flight)
    assert long_train_claim_issues("present.update", VAR_INPUT, vout("Трансфер 1,5 ч; выезд 4 июля.")) == []


def test_leave_day_in_the_recommended_items_why_counts():
    found = long_train_claim_issues("present.variants", VAR_INPUT,
                                    vout("Советую «Дешевле».", why="Поездом, в пути 1 сутки 3 ч, выезд 4 июля."))
    assert found == []


def test_destinations_long_train_decimal_hours():
    inp = {"brief": {}, "items": [dest("anapa", "Анапа", 61000, None, long_train=True, on_road_ru="1 сутки 4 ч",
                                       road_ru="поездом, в пути 1 сутки 4 ч; выезд 2 июля — за день до заезда")]}
    assert long_train_claim_issues("present.destinations", inp, out("Анапа поездом 28,2 ч — от 61 000 ₽."))
    assert not long_train_claim_issues("present.destinations", inp,
                                       out("Анапа поездом, в пути 1 сутки 4 ч; выезд 2 июля — от 61 000 ₽."))
