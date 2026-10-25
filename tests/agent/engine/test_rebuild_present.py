"""Second live level-3 run, reproduced offline: rebuild requests in the cart (s02, s04, s17), ready money numbers
instead of model arithmetic (x1), one-way travel time (s01), info facts with dates and open warnings (s20),
an impossible change after purchase answered with numbers instead of a repeated rebuild (s16)."""
import json
from datetime import datetime

import pytest

from agent import payloads as P
from agent.engine import NOOP_REBUILD_KEY, is_rebuild_request, route_intent
from agent.state import DistrictProfile, Fit, Totals, Variant, VariantHotel
from agent.steps import cancel_quote, nights_without_stay
from evals.card_inputs import validate_card_input

from .fakes import FakeOstrovok, _step, u

TRAVELLERS2 = [{"name": "Анна", "birth_date": "1990-01-01"}, {"name": "Иван", "birth_date": "1989-05-05"}]


def _to_selection(h, **extra):
    sets = {"destination.name": "Сочи", "party.adults": 2, "party.segment": "couple", "dates.start": "2026-11-12",
            "dates.end": "2026-11-19", **extra}
    h.understand(u(set=sets))
    r = h.turn("Сочи, 12–19 ноября, вдвоём")
    assert r["cards"]["variants"] and not r.get("error"), r["message"]
    return r


def _inputs(h, card):
    return [json.loads(r.user) for r in h.backend.requests if r.card == card]


def _cards(h):
    return [r.card for r in h.backend.requests]


# ---------------------------------------------------------------- 1. routing rules (unit)

@pytest.mark.parametrize("text", ["пересобери, мне нужен мужской или смешанный номер",
                                  "Да, пересоберите, чтобы мы заехали в отель сразу вечером 30-го",
                                  "соберите заново, пожалуйста", "а можно другой отель?"])
def test_rebuild_words(text):
    assert is_rebuild_request(text)


@pytest.mark.parametrize("text", ["беру второй", "там женский общий номер, я же мужчина", "оформляем"])
def test_not_rebuild_words(text):
    assert not is_rebuild_request(text)


def test_route_intent_cart_choose_with_rebuild_is_refine():   # s02 t7
    intent, patch, note = route_intent("choose", "cart", "пересобери, мне нужен мужской или смешанный номер",
                                       {"set": {}, "unset": []})
    assert intent == "refine" and patch == {"set": {}, "unset": []} and note


def test_route_intent_post_purchase_without_order_is_refine():   # s04 t5
    intent, _, _ = route_intent("post_purchase", "cart", "Да, пересоберите, чтобы мы заехали в отель сразу вечером 30-го",
                                {"set": {}, "unset": []}, paid=False,
                                post_purchase={"kind": "early_checkin", "params": {}})
    assert intent == "refine"


def test_route_intent_post_purchase_dates_go_to_patch():
    intent, patch, _ = route_intent("post_purchase", "cart", "перенесите на 14–21 ноября", {"set": {}, "unset": []},
                                    post_purchase={"kind": "change_dates", "params": {"new_dates": {
                                        "check_in": "2026-11-14", "check_out": "2026-11-21"}}})
    assert intent == "refine" and patch["set"] == {"dates.start": "2026-11-14", "dates.end": "2026-11-21"}


def test_route_intent_post_purchase_question_before_paying_is_info():
    intent, _, _ = route_intent("post_purchase", "selection", "а заселиться пораньше можно?", {"set": {}, "unset": []},
                                post_purchase={"kind": "early_checkin", "params": {}})
    assert intent == "info"


def test_route_intent_keeps_real_choices_and_paid_aftercare():
    assert route_intent("choose", "selection", "беру второй", {"set": {}, "unset": []},
                        refs=[{"kind": "variant", "ref": "2"}])[0] == "choose"
    assert route_intent("post_purchase", "ordered", "заселиться пораньше", {"set": {}, "unset": []}, paid=True,
                        post_purchase={"kind": "early_checkin", "params": {}})[0] == "post_purchase"
    # a new condition said with «беру» but without a variant: rebuild, not a cart with an unknown variant
    assert route_intent("cart", "cart", "а с завтраком?", {"set": {"prefs.meals": "breakfast"}, "unset": []})[0] == "refine"


# ---------------------------------------------------------------- 1. rebuild in the cart / checkout (engine)

def test_s02_rebuild_request_in_cart_goes_back_to_selection(h):
    _to_selection(h)
    r = h.act("choose_variant", variant_id="cheapest")
    assert r["stage"] == "cart"
    h.understand(u(intent="choose"))          # what the live understand returned in the cart
    r = h.turn("пересобери, мне нужен мужской или смешанный номер")
    assert not r.get("error"), r["message"]
    assert not r["message"].startswith("Положил"), r["message"]
    assert r["stage"] == "selection" and r["cards"]["variants"]
    assert _cards(h)[-1] == "present.update"
    assert any(a["type"] == "choose_variant" for a in r["actions"])
    # choosing again replaces the cart
    r = h.act("choose_variant", variant_id="comfort")
    v = h.session().variant("comfort")
    assert r["stage"] == "cart"
    assert {i["offer_id"] for i in r["cart"]["items"]} == {s.offer_id for s in v.steps if s.offer_id}


def test_s04_post_purchase_intent_in_cart_rebuilds_with_new_dates(h):
    _to_selection(h)
    h.act("choose_variant", variant_id="comfort")
    h.understand(u(intent="post_purchase", set={"dates.start": "2026-11-11"},
                   pp={"kind": "early_checkin", "params": {}}))
    r = h.turn("Да, пересоберите, чтобы мы заехали в отель сразу вечером 11-го")
    assert "сначала соберём и оплатим" not in r["message"] and not r.get("error"), r["message"]
    assert r["stage"] == "selection" and _cards(h)[-1] == "present.update"
    stay = next(s for s in r["cards"]["variants"][0]["steps"] if s["type"] == "stay")
    assert stay["start"][:10] == "2026-11-11"


def test_refine_at_checkout_before_payment_rebuilds(h):
    _to_selection(h)
    h.act("choose_variant", variant_id="cheapest")
    r = h.act("checkout", travellers=TRAVELLERS2, contact={"email": "a@b.c"})
    assert r["stage"] == "checkout" and r["order"]["status"] == "pending_payment"
    h.understand(u(intent="refine", set={"prefs.meals": "breakfast"}))
    r = h.turn("а давайте всё-таки с завтраками")
    assert r["stage"] == "selection" and r["cards"]["variants"] and _cards(h)[-1] == "present.update", r["message"]
    r = h.act("choose_variant", variant_id="fastest")
    assert r["stage"] == "cart" and r["cart"]["items"]
    r = h.act("checkout", travellers=TRAVELLERS2, contact={"email": "a@b.c"})
    assert r["order"]["status"] == "pending_payment" and r["order"]["order_id"] == h.session().order_id


def test_s17_empty_rebuild_says_it_once_then_answers_without_repeating(h):
    _to_selection(h, **{"constraints.add": ["crib"]})
    n_update = len(_inputs(h, "present.update"))
    h.understand(u(intent="refine", set={"constraints.add": ["crib"]}))
    r1 = h.turn("Да, пересоберите, пожалуйста, чтобы кроватка точно была в номере")
    ups = _inputs(h, "present.update")
    assert len(ups) == n_update + 1, _cards(h)
    assert ups[-1]["changes"] == [] and "кроватка" in ups[-1]["request"]
    assert validate_card_input("present.update", ups[-1]) == []
    h.understand(u(intent="refine", set={"constraints.add": ["crib"]}))
    r2 = h.turn("Вы же ничего не поменяли. Мне нужен отель, где кроватка точно входит в номер")
    assert len(_inputs(h, "present.update")) == n_update + 1          # no identical card the second time
    assert r2["message"] != r1["message"] and "ничего не менял" not in r2["message"]
    assert "Ближе всего" in r2["message"] and r2["cards"]["variants"] and not r2.get("error")
    assert h.session().results[NOOP_REBUILD_KEY].data["turn"] == h.session().turn


# ---------------------------------------------------------------- 3. ready money numbers (x1)

def _variant(vid, rub, fit, out=None, back=None):
    return Variant(id=vid, title=P.VARIANT_TITLES[vid], steps=[_step("s1", "stay", "H", "H", "2026-12-05T14:00+04:00",
                                                                     "2026-12-12T12:00+04:00", rub, f"o-{vid}",
                                                                     "2026-10-25")],
                   totals=Totals(rub=rub, door_to_door_hours=(out or 0) + (back or 0), out_hours=out, back_hours=back),
                   hotel=VariantHotel(hotel_id="h", offer_id=f"o-{vid}", name="H", stars=3, rating=8.0,
                                      district=DistrictProfile()), fit=Fit(score=fit))


def test_x1_gaps_and_over_budget_are_ready_numbers():
    cheap, fast = _variant("cheapest", 103200, 0.2, 6.2, 4.5), _variant("fastest", 130900, 0.15, 5.1, 5.3)
    a = P.variant_item(cheap, "1", base_rub=103200, budget_max=15000)
    b = P.variant_item(fast, "2", base_rub=103200, budget_max=15000)
    assert (a["diff_rub"], a["diff_word"], a["over_budget_rub"]) == (0, None, 88200)
    assert (b["diff_rub"], b["diff_word"], b["over_budget_rub"]) == (27700, "дороже", 115900)
    assert b["totals"]["out_hours"] == 5.1 and b["totals"]["back_hours"] == 5.3
    c = P.variant_item(cheap, "1", base_rub=130900)
    assert (c["diff_rub"], c["diff_word"], c["over_budget_rub"]) == (27700, "дешевле", None)


def test_card_hint_follows_the_budget_rule():
    vs = [_variant("cheapest", 14700, 0.69), _variant("fastest", 24900, 0.84), _variant("comfort", 41300, 0.88)]
    assert P.card_hint(vs, "comfort", 30000) == "fastest"
    assert P.card_hint(vs, "comfort", None) == "comfort"
    assert P.card_hint(vs, "comfort", 10000) == "comfort"            # nothing fits: the hint stays


def test_engine_variant_inputs_have_gaps_and_validate(h):
    _to_selection(h, **{"budget_rub.max": 60000})
    inp = _inputs(h, "present.variants")[-1]
    assert validate_card_input("present.variants", inp) == []
    hint = inp["recommended_hint"]
    by = {i["id"]: i for i in inp["items"]}
    assert by[hint]["diff_rub"] == 0 and by[hint]["diff_word"] is None
    for i in inp["items"]:
        assert i["diff_rub"] == abs(i["totals"]["rub"] - by[hint]["totals"]["rub"])
        assert i["over_budget_rub"] == (i["totals"]["rub"] - 60000 if i["totals"]["rub"] > 60000 else None)
    if any(i["totals"]["rub"] <= 60000 for i in inp["items"]):
        assert by[hint]["totals"]["rub"] <= 60000                    # the budget rule is applied by code
    h.understand(u(intent="compare"))
    h.turn("сравни варианты")
    assert validate_card_input("present.compare", _inputs(h, "present.compare")[-1]) == []


# ---------------------------------------------------------------- 4. info facts: dates and open warnings (s20)

def test_nights_without_stay_from_dates():
    v = Variant(id="comfort", title="Комфортнее", steps=[
        _step("s1", "flight", "DME", "DWC", "2026-12-14T14:00+03:00", "2026-12-14T20:29+04:00", 30000, "f1", "2026-10-25"),
        _step("s2", "stay", "R", "R", "2026-12-15T14:00+04:00", "2026-12-19T12:00+04:00", 90000, "h1", "2026-10-25"),
        _step("s3", "flight", "DWC", "DME", "2026-12-19T16:00+04:00", "2026-12-19T21:00+03:00", 30000, "f2",
              "2026-10-25")], totals=Totals(rub=150000, door_to_door_hours=12.0),
        hotel=VariantHotel(hotel_id="h", offer_id="h1", name="R", district=DistrictProfile()))
    assert nights_without_stay(v) == [{"from": "2026-12-14", "to": "2026-12-15"}]
    v.steps[0].end = "2026-12-15T06:10+04:00"           # a night flight landing on the check-in day
    assert nights_without_stay(v) == []


class WarnCartOstrovok(FakeOstrovok):
    def _cart(self, sid):
        cart = super()._cart(sid)
        if cart["items"]:
            cart["issues"] = [{"code": "night_without_hotel", "severity": "warning", "item_ids": [],
                               "message": "Прибытие 11.11, а заезд только 12.11: ночь без отеля"}]
        return cart


def test_s20_info_facts_carry_stay_dates_and_cart_warnings(make):
    h = make(ostrovok=WarnCartOstrovok())
    _to_selection(h)
    r = h.act("choose_variant", variant_id="comfort")
    assert "ночь без отеля" in r["message"]
    h.understand(u(intent="info", info="Ночь с 11 на 12 входит в бронь?"))
    h.turn("Ночь с 11 на 12 входит в бронь или нет?")
    facts = _inputs(h, "present.info")[-1]["facts"]
    stay = next(f for f in facts if f["kind"] == "variant")["stay"]
    assert stay["check_in_date"] == "2026-11-12" and stay["check_in_time"] == "14:00"
    assert stay["check_out_date"] == "2026-11-19"
    leg = next(f for f in facts if f["kind"] == "variant")["legs"][0]
    assert leg["arr_date"] == "2026-11-12" and leg["arr_time"]
    warn = next(f for f in facts if f["kind"] == "warnings")
    assert warn["items"] == ["Прибытие 11.11, а заезд только 12.11: ночь без отеля"]


# ---------------------------------------------------------------- 5. aftercare: an impossible move (s16)

class NoExchangeOstrovok(FakeOstrovok):
    """Booked flights without exchange and refund; the hotel cancels with a 30 % penalty."""

    def checkout(self, p):
        out = super().checkout(p)
        for item in self.orders[out["order_id"]]["items"]:
            if item["kind"] == "flight":
                item["details"]["offer"] = {"fare": {"refundable": False, "change_fee_rub": None}}
            elif item["kind"] == "hotel":
                item["details"]["offer"] = {"cancellation": {"free_until": None, "penalty_pct": 30}}
        return out

    def order_change_dates(self, p):
        raise AssertionError("an order without exchange is never quoted for a move")


def _buy(h):
    _to_selection(h)
    h.act("choose_variant", variant_id="fastest")
    r = h.act("checkout", travellers=TRAVELLERS2, contact={"email": "a@b.c"})
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r["order"]["status"] == "paid"
    return r["order"]


@pytest.mark.parametrize("second", [
    u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}),
    # the model put the shift in the patch: it is still counted from the booked dates, not from the new variants
    u(intent="post_purchase", set={"dates.shift_days": 1}, pp={"kind": "change_dates", "params": {}}),
])
def test_s16_same_impossible_move_again_gets_numbers_not_a_second_rebuild(make, second):
    h = make(ostrovok=NoExchangeOstrovok())
    order = _buy(h)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    r1 = h.turn("слушай, а можно всё сдвинуть на день позже?")
    assert r1["stage"] == "selection" and r1["cards"]["variants"] and "не перенести" in r1["message"]
    n_variants = len(_inputs(h, "present.variants"))
    h.understand(second)
    r2 = h.turn("Погоди, мне не вторая поездка нужна, а перенос. Сколько доплачивать за сдвиг?")
    assert not r2.get("error"), r2["message"]
    assert len(_inputs(h, "present.variants")) == n_variants          # no identical rebuild
    assert _cards(h)[-1] == "present.aftercare" and r2["message"] != r1["message"]
    res = _inputs(h, "present.aftercare")[-1]["result"]
    flights = sum(i["price_rub"] for i in order["items"] if i["kind"] == "flight")
    hotel = next(i["price_rub"] for i in order["items"] if i["kind"] == "hotel")
    q = res["if_cancelled"]
    assert res["available"] is False and res["reason"] == "tickets_not_exchangeable"
    assert q["tickets_lost_rub"] == flights and q["hotel_penalty_rub"] == int(round(hotel * 0.3 / 10)) * 10
    assert q["refund_rub"] == hotel - q["hotel_penalty_rub"] and q["lost_rub"] == flights + q["hotel_penalty_rub"]
    trip = h.session().variant("fastest")
    assert res["new_trip"]["total_rub"] == trip.totals.rub and res["new_dates"]["check_in"] == "2026-11-13"
    assert res.get("extra_rub", -res.get("saving_rub", 0)) == trip.totals.rub - q["refund_rub"]
    assert h.session().order_id == order["order_id"]


def test_cancel_quote_free_cancellation_and_refundable_fares():
    now = datetime.fromisoformat("2026-10-25T12:00:00+03:00")
    order = {"items": [
        {"kind": "hotel", "price_rub": 40000, "details": {"offer": {"cancellation": {
            "free_until": "2026-11-10T23:59:00+03:00", "penalty_pct": 100}}}},
        {"kind": "flight", "price_rub": 12000, "details": {"offer": {"fare": {"refundable": True}}}},
        {"kind": "flight", "price_rub": 9000, "details": {"offer": {"fare": {"refundable": False}}}},
        {"kind": "transfer", "price_rub": 1500, "details": {}}]}
    q = cancel_quote(order, now)
    assert q == {"hotel_refund_rub": 40000, "hotel_penalty_rub": 0, "tickets_refund_rub": 12000,
                 "tickets_lost_rub": 9000, "other_rub": 1500, "refund_rub": 52000, "lost_rub": 9000}


def test_info_after_purchase_has_cancel_numbers(make):
    h = make(ostrovok=NoExchangeOstrovok())
    _buy(h)
    h.understand(u(intent="info", info="Сколько вернут, если отменить?"))
    h.turn("а если отменить, сколько вернут?")
    order_fact = next(f for f in _inputs(h, "present.info")[-1]["facts"] if f["kind"] == "order")
    assert order_fact["if_cancelled"]["lost_rub"] > 0


# ---------------------------------------------------------------- live: real ranking + the in-process mock

from .test_scopes import ask, live  # noqa: E402,F401  (the `live` fixture)


def test_s01_variant_items_carry_one_way_hours(live):
    ask(live, "Анталья, 14–21 ноября, двое взрослых и ребёнок 4 лет",
        **{"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-14",
           "dates.end": "2026-11-21", "party.adults": 2, "party.children": 1, "party.children_ages": [4]})
    inp = _inputs(live, "present.variants")[-1]
    assert validate_card_input("present.variants", inp) == []
    for i in inp["items"]:
        t = i["totals"]
        assert t["out_hours"] and t["back_hours"], t
        assert abs(t["out_hours"] + t["back_hours"] - t["door_to_door_hours"]) <= 0.2, t


def test_s16_live_unmovable_order_answers_with_numbers(live):
    h = live
    _, vs = ask(h, "Калининград, 20–23 ноября, я один",
                **{"origin.city": "Москва", "destination.name": "Калининград", "dates.start": "2026-11-20",
                   "dates.end": "2026-11-23", "party.adults": 1, "party.segment": "solo"})
    fixed = [v for v in vs if v.facts.get("out_refundable") is False]
    pick = (fixed or vs)[0]
    h.act("choose_variant", variant_id=pick.id)
    r = h.act("checkout", travellers=[{"name": "Евгений Тихонов", "birth_date": "1982-07-07"}],
              contact={"email": "e@x.ru"})
    order = h.act("pay", order_id=r["order"]["order_id"])["order"]
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    r1 = h.turn("слушай, а можно всё сдвинуть на день позже?")
    assert not r1.get("error"), r1["message"]
    if _cards(h)[-1] == "present.aftercare":
        pytest.skip("this booked fare can be exchanged: the mock moves it (no rebook path)")
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    r2 = h.turn("Погоди, мне не вторая поездка нужна, а перенос. Сколько доплачивать за сдвиг?")
    assert _cards(h)[-1] == "present.aftercare" and not r2.get("error"), r2["message"]
    res = _inputs(h, "present.aftercare")[-1]["result"]
    q = res["if_cancelled"]
    paid = sum(i["price_rub"] for i in order["items"])
    assert q["refund_rub"] + q["lost_rub"] + q["other_rub"] == paid
    assert q["tickets_lost_rub"] > 0 and res["new_trip"]["total_rub"] > 0
    assert validate_card_input("present.aftercare", _inputs(h, "present.aftercare")[-1]) == []
