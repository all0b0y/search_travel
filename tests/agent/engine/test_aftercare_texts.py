"""Third live level-3 run, aftercare and text failures reproduced offline (real engine, real mock over the
real catalog, scripted FakeBackend):

- s16: change_dates on a paid order moves its transfers with the trip (each fits its new flight), and
  add_transfer works after the change;
- degraded turns: the code fallback texts are short, useful and human (no «модель», no «данные»);
- s15: the aftercare input carries the arrival time, the hotel's check-in time and its name;
- s25: the update card gets ready price deltas (delta_rub / delta_word);
- s07: info at selection gets the shown variants' tickets with `refundable`.
"""
import json
import re
from datetime import timedelta

import pytest

from agent.checks import ts

from .fakes import u
from .test_scopes import TRAVELLERS1, ask, live  # noqa: F401  (the `live` fixture)


def _last_input(h, card):
    req = next(r for r in reversed(h.backend.requests) if r.card == card)
    return json.loads(req.user)


def _flights_and_transfers(order):
    items = [i for i in order["items"] if i.get("status") != "cancelled"]
    return [i for i in items if i["kind"] == "flight"], [i for i in items if i["kind"] == "transfer"]


def _assert_transfers_fit(order):
    """Every transfer meets its flight: the arrival pickup after landing, the return one at the airport
    at least 2 hours before departure."""
    flights, transfers = _flights_and_transfers(order)
    assert transfers, "the order has no transfers"
    for tr in transfers:
        off = tr["details"]["offer"]
        iata = off["point"].get("iata")
        if off["reverse"]:
            f = next(f for f in flights if f["details"]["offer"]["origin_iata"] == iata)
            gap = (ts(f["start"]) - ts(tr["end"])).total_seconds() / 60
            assert 120 <= gap <= 300, (tr["start"], tr["end"], f["start"], gap)
        else:
            f = next(f for f in flights if f["details"]["offer"]["dest_iata"] == iata)
            gap = (ts(tr["start"]) - ts(f["end"])).total_seconds() / 60
            assert 30 <= gap <= 180, (tr["start"], f["end"], gap)


def _buy_kaliningrad(h):
    r, vs = ask(h, "Калининград, 20–23 ноября, я один",
                **{"destination.name": "Калининград", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
                   "party.segment": "solo", "party.adults": 1})
    movable = [v for v in vs if any(s.type == "transfer" for s in v.steps)]
    assert movable, [v.id for v in vs]
    order = None
    for v in movable:
        r = h.act("choose_variant", variant_id=v.id)
        r = h.act("checkout", travellers=TRAVELLERS1, contact={"email": "a@b.c"})
        r = h.act("pay", order_id=r["order"]["order_id"])
        assert r["stage"] == "ordered", r["message"]
        order = h.engine.ostrovok.call("order_get", {"order_id": r["order"]["order_id"]})
        fares = [i["details"]["offer"]["fare"] for i in order["items"] if i["kind"] == "flight"]
        if all(f.get("change_fee_rub") is not None for f in fares):
            return order
    pytest.skip("no exchangeable fare among the variants in the current data")


# ---------------------------------------------------------------- 1. s16: transfers move with the trip

def test_s16_change_dates_moves_transfers_and_add_transfer_works(live):
    h = live
    order = _buy_kaliningrad(h)
    _assert_transfers_fit(order)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    r = h.turn("слушай, а можно всё сдвинуть на день позже?")
    assert not r.get("error"), r["message"]
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"confirm": True}}))
    r = h.turn("переносим")
    assert not r.get("error"), r["message"]
    order = h.engine.ostrovok.call("order_get", {"order_id": order["order_id"]})
    assert order["status"] == "changed"
    hotel = next(i for i in order["items"] if i["kind"] == "hotel")
    assert hotel["start"][:10] == "2026-11-21"
    _assert_transfers_fit(order)
    assert not [i for i in order["issues"] + list(_last_input(h, "present.aftercare")["result"].get("issues") or [])
                if i["code"].startswith("transfer_too")]
    res = _last_input(h, "present.aftercare")["result"]
    assert res["applied"] and isinstance(res["quote"]["difference_rub"], int)
    # add_transfer after the change: re-times / adds without «не находится»
    r = h.act("order_action", order_id=order["order_id"], kind="add_transfer", params={})
    assert "не находится" not in r["message"], r["message"]
    order = h.engine.ostrovok.call("order_get", {"order_id": order["order_id"]})
    _assert_transfers_fit(order)
    assert len(_flights_and_transfers(order)[1]) == 2


def test_add_transfer_finds_an_offer_for_an_order_without_transfers(live):
    """_transfer_offer_for_order read hotel_id / dest_iata from the wrong place: always None."""
    h = live
    _, vs = ask(h, "Калининград, 20–23 ноября, я один, без трансфера",
                **{"destination.name": "Калининград", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
                   "party.segment": "solo", "party.adults": 1, "scope": ["flights", "stay"]})
    v = vs[0]
    assert not any(s.type == "transfer" for s in v.steps)
    h.act("choose_variant", variant_id=v.id)
    r = h.act("checkout", travellers=TRAVELLERS1, contact={"email": "a@b.c"})
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert any(a["payload"].get("kind") == "add_transfer" for a in r["actions"])
    r = h.act("order_action", order_id=r["order"]["order_id"], kind="add_transfer", params={})
    assert "не находится" not in r["message"], r["message"]
    order = h.engine.ostrovok.call("order_get", {"order_id": h.session().order_id})
    _assert_transfers_fit(order)
    assert len(_flights_and_transfers(order)[1]) == 2


# ---------------------------------------------------------------- 2. degraded turns: useful code texts

BAD = {"message": "Трансфер подадут в 13:24, а рейс в 99:99 — доплата 77 777 ₽.", "items": [], "recommended_id": None,
       "upsell": []}
ROBOTIC = re.compile(r"модел|данн|каталог|без пересказа", re.I)


def _always_bad(h, card):
    h.backend.scripts[card] = lambda req: dict(BAD)


def _degraded(h):
    return h.session().trace[-1]["degraded"]


def test_s16_info_degraded_answers_from_the_order(live):
    h = live
    order = _buy_kaliningrad(h)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    h.turn("а можно всё сдвинуть на день позже?")
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"confirm": True}}))
    h.turn("переносим")
    _always_bad(h, "present.info")
    h.understand(u(intent="info", info="Что с трансфером к утреннему рейсу 24 ноября?"))
    r = h.turn("Так что с трансфером? Мне к утреннему рейсу 24 ноября нужно")
    assert _degraded(h), r["message"]
    assert not ROBOTIC.search(r["message"]), r["message"]
    order = h.engine.ostrovok.call("order_get", {"order_id": order["order_id"]})
    back = next(i for i in order["items"] if i["kind"] == "transfer" and i["details"]["offer"]["reverse"])
    assert back["start"][11:16] in r["message"] and "24 ноября" in r["message"], r["message"]


def test_aftercare_degraded_says_the_result(live):
    h = live
    _buy_kaliningrad(h)
    _always_bad(h, "present.aftercare")
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    r = h.turn("а можно всё сдвинуть на день позже?")
    assert _degraded(h)
    q = _last_input(h, "present.aftercare")["result"]["quote"]
    assert not ROBOTIC.search(r["message"]), r["message"]
    assert "21–24 ноября" in r["message"] and f"{q['new_total_rub']:,}".replace(",", " ") in r["message"]
    assert "переносим" in r["message"]
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"confirm": True}}))
    r = h.turn("переносим")
    assert r["message"].startswith("Перенёс: теперь 21–24 ноября"), r["message"]


def test_variants_and_update_degraded_recommend_with_price_and_hours(live):
    h = live
    _always_bad(h, "present.variants")
    r, vs = ask(h, "Калининград, 20–23 ноября, я один",
                **{"destination.name": "Калининград", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
                   "party.segment": "solo", "party.adults": 1})
    assert _degraded(h) and not ROBOTIC.search(r["message"]), r["message"]
    rec = next(c for c in r["cards"]["variants"] if c["recommended"])
    assert r["message"].startswith(f"Советую «{rec['title']}»") and "туда" in r["message"], r["message"]
    assert f"{rec['totals']['rub']:,}".replace(",", " ") in r["message"]
    _always_bad(h, "present.update")
    h.understand(u(intent="refine", set={"dates.nights": 2}))
    r = h.turn("давайте 2 ночи")
    assert _degraded(h) and r["message"].startswith("Пересобрал варианты. Советую «"), r["message"]
    assert "было" in r["message"] and not ROBOTIC.search(r["message"])


def test_destinations_degraded_lists_places_with_prices(live):
    h = live
    _always_bad(h, "present.destinations")
    h.understand(u(set={"traits.add": ["sea", "warm"], "dates.month": 11, "party.adults": 2}))
    r = h.turn("к тёплому морю в ноябре вдвоём")
    if not r["cards"]["destinations"]:
        pytest.skip("no destinations in the current data")
    assert _degraded(h) and r["message"].startswith("Куда можно поехать: "), r["message"]
    assert r["cards"]["destinations"][0]["name"] in r["message"] and "от " in r["message"]
    assert not ROBOTIC.search(r["message"])


# ---------------------------------------------------------------- 2b. s15: aftercare input has the times it needs

def test_s15_early_checkin_input_has_arrival_checkin_and_hotel(live):
    from agent.guards import bad_numbers, allowed_from
    h = live
    _, vs = ask(h, "Анталья", **{"destination.name": "Анталья", "dates.start": "2026-11-14", "dates.end": "2026-11-21",
                                "party.segment": "family", "party.adults": 2, "party.children": 1,
                                "party.children_ages": [4], "prefs.meals": "all_inclusive",
                                "prefs.arrive_before": "12:00", "prefs.no_night_flights": True})
    v = next((v for v in vs if v.id == "fastest"), vs[0])
    h.act("choose_variant", variant_id=v.id)
    r = h.act("checkout", travellers=[{"name": "Н К", "birth_date": "1991-02-02"},
                                      {"name": "А К", "birth_date": "1989-05-05"},
                                      {"name": "М К", "birth_date": "2022-03-03"}], contact={"email": "a@b.c"})
    h.act("pay", order_id=r["order"]["order_id"])
    h.understand(u(intent="post_purchase", pp={"kind": "early_checkin", "params": {}}))
    h.turn("а можно заселиться пораньше? мы прилетаем утром")
    inp = _last_input(h, "present.aftercare")
    res = inp["result"]
    flight = v.steps[0]
    assert res["lands_at"] == flight.end[11:16] and res["check_in_from"] and res["hotel"] == v.hotel.name
    text = (f"Прилетаете в {res['lands_at']}, в отеле «{res['hotel']}» будете к {res['at_hotel_at']}, обычное "
            f"заселение с {res['check_in_from']}.")
    assert bad_numbers(text, allowed_from(inp)) == []


# ---------------------------------------------------------------- 2c. s25: ready price deltas for the update card

def test_s25_update_changes_carry_ready_deltas(live):
    h = live
    ask(h, "Три подруги в Питер на 3 ночи", **{"destination.name": "Санкт-Петербург", "dates.start": "2026-12-01",
                                              "dates.end": "2026-12-04", "party.segment": "friends", "party.adults": 3,
                                              "prefs.meals": "breakfast", "prefs.district": "walkable"})
    h.understand(u(intent="refine", set={"dates.nights": 2}))
    h.turn("давайте всё-таки 2 ночи")
    inp = _last_input(h, "present.update")
    rows = [c for c in inp["changes"] if c["field"].startswith("totals.")]
    assert rows
    for c in rows:
        if c["before"] is not None and c["after"] is not None:
            assert c["delta_rub"] == abs(c["after"] - c["before"])
            assert c["delta_word"] == ("дешевле" if c["after"] < c["before"] else "дороже")
        else:
            assert c["delta_rub"] is None and c["delta_word"] is None
    from evals.card_inputs import validate_card_input
    errors = validate_card_input("present.update", inp)
    assert errors == [], errors


# ---------------------------------------------------------------- 3. s07: refundability of the shown variants

def test_s07_info_at_selection_has_refundable_after_rebuilds(live):
    h = live
    ask(h, "Калининград, недельку летом, с мужем", **{"destination.name": "Калининград", "dates.month": 7,
                                                      "dates.nights": 7, "party.segment": "couple",
                                                      "party.adults": 2})
    h.understand(u(intent="info", info="А бесплатная отмена точно есть, и на билеты тоже?"))
    h.turn("А бесплатная отмена точно есть, и на билеты тоже?")
    h.understand(u(intent="refine", set={"destination.name": "Светлогорск", "prefs.free_cancellation": True,
                                         "prefs.beach_max_m": 1000}))
    r = h.turn("Нужна бесплатная отмена и отель ближе к морю, можно Светлогорск")
    assert r["cards"]["variants"], r["message"]
    h.understand(u(intent="info", info="А билеты в этих вариантах возвратные?"))
    h.turn("А билеты в этих вариантах возвратные? Отмена бесплатная до какой даты?")
    facts = _last_input(h, "present.info")["facts"]
    shown = {i.id for i in h.session().shown.variants}
    variants = [f for f in facts if f["kind"] == "variant"]
    assert {f["id"] for f in variants} == shown
    for f in variants:
        flights = [leg for leg in f["legs"] if leg["type"] == "flight"]
        assert flights and all(isinstance(leg.get("refundable"), bool) and leg.get("flight_no") for leg in flights), f
        assert (f["stay"].get("cancellation") or {}).get("free_until"), f["stay"]
    # and the degraded text answers it too
    _always_bad(h, "present.info")
    h.understand(u(intent="info", info="А билеты в этих вариантах возвратные?"))
    r = h.turn("А билеты в этих вариантах возвратные?")
    assert "возвратные" in r["message"] and not ROBOTIC.search(r["message"]), r["message"]
