"""Honesty classes found by an independent evaluation (reproduced here in our own words), offline on the real mock
and the real data (in-process Ostrovok mock over data/ostrovok_catalog.sqlite, real open data, a scripted model):

1. a reply must not claim what code did not do — «без ночных» covers trains too (night_leg), and a table-driven
   guard (guards.change_claim_issues) checks every «убрал / теперь все / с кухней / прямые / перенёс / пересчитал»
   against this turn's `changes` rows and the shown items' facts;
2. no transport → an honest recovery (steps.recover_transport): connecting flights behind a direct-only wish, a
   train where no plane flies, or plainly nothing — with real alternatives as buttons;
3. no promise without the means: «одной кнопкой» only with an add_road offer, the repeated rebuild names the unmet
   wish (engine.noop_rebuild_text);
4. kitchen / step-free constraints act on the mock's synthetic room.kitchen / accessibility;
5. understanding: a service the agent does not sell is said once (or is out_of_scope alone), open dates stay a month.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.guards import change_claim_issues, check_card_output
from agent.ranking import night_leg, ranking_rules
from agent.steps import not_sold_fact, unsold_cues, unsold_only

from .conftest import skill
from .fakes import u
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)
from .turn_time import calibrated

TURN_MS_MAX = 1500   # in-process turn on the live harness, turn_time.calibrated (measured 60–710 ms; slack for a slow machine)
RULES = skill().rules
DATES = {"dates.start": "2026-11-20", "dates.end": "2026-11-23", "party.segment": "couple", "party.adults": 2}


def card_input(h, card):
    return json.loads([r for r in h.backend.requests if r.card == card][-1].user)


def trip(h, origin, dest, text, **extra):
    h.understand(u(set={"origin.city": origin, "destination.name": dest, **DATES, **extra}))
    r, ms = calibrated(h.turn, text)
    assert r.get("error") is None, r["message"]
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    return r


def legs(v):
    return [s for s in v.steps if s.type in ("flight", "train")]


def is_night(step):
    dep, arr = datetime.fromisoformat(step.start), datetime.fromisoformat(step.end)
    return night_leg(SimpleNamespace(kind=step.type, dep=dep, arr=arr), ranking_rules(RULES))


def actions(r, kind=None):
    return [a for a in r["actions"] if kind is None or a["type"] == kind]


# ---------------------------------------------------------------- 1a. «без ночных» covers trains

MSK = timezone(timedelta(hours=3))


@pytest.mark.parametrize("dep,arr,night", [
    ("2026-11-20T21:30", "2026-11-21T07:40", True),    # an overnight sleeper: on the move at 03:00
    ("2026-11-20T20:15", "2026-11-21T00:43", True),    # arrives in the night window
    ("2026-11-20T23:15", "2026-11-21T04:37", True),    # leaves in the night window
    ("2026-11-20T07:50", "2026-11-20T10:58", False),   # a day train
    ("2026-11-20T18:30", "2026-11-20T22:40", False),   # an evening train before the window
])
def test_night_leg_rule_for_trains(dep, arr, night):
    leg = SimpleNamespace(kind="train", dep=datetime.fromisoformat(dep).replace(tzinfo=MSK),
                          arr=datetime.fromisoformat(arr).replace(tzinfo=MSK))
    assert night_leg(leg, ranking_rules(RULES)) is night


def test_night_trains_moscow_yaroslavl_dropped(live):
    h = live
    trip(h, "Москва", "Ярославль", "Ярославль, 20–23 ноября", **{"prefs.transport": "train"})
    assert any(is_night(st) for v in h.session().variants for st in legs(v))   # the 20:15 → 00:43 train was «Дешевле»
    h.new()
    trip(h, "Москва", "Ярославль", "Ярославль, 20–23 ноября, без ночных поездов",
         **{"prefs.transport": "train", "prefs.no_night_flights": True})
    vs = h.session().variants
    assert vs and all(legs(v) and not any(is_night(st) for st in legs(v)) for v in vs)
    assert all(not v.facts.get("out_night") and not v.facts.get("back_night") for v in vs)


def test_night_trains_spb_petrozavodsk_dropped(live):
    h = live
    trip(h, "Санкт-Петербург", "Петрозаводск", "Петрозаводск поездом", **{"prefs.transport": "train"})
    assert any(is_night(st) for v in h.session().variants for st in legs(v))   # 23:15 → 04:37 was «Комфортнее»
    h.new()
    trip(h, "Санкт-Петербург", "Петрозаводск", "Петрозаводск поездом, ночью ехать не хотим",
         **{"prefs.transport": "train", "prefs.no_night_flights": True})
    vs = h.session().variants
    assert vs and all(legs(v) == [s for s in legs(v) if s.type == "train"] and not any(is_night(s) for s in legs(v))
                      for v in vs)


def test_only_sleepers_relaxed_with_a_plain_note(live):
    h = live
    trip(h, "Москва", "Казань", "Казань поездом, без ночных", **{"prefs.transport": "train",
                                                                 "prefs.no_night_flights": True})
    for v in h.session().variants:
        note = next(n for n in v.risks if n.code == "relaxed_pref" and n.data.get("field") == "prefs.no_night_flights")
        assert note.text == "Дневных поездов на эти даты нет — в варианте ночной поезд"
        assert v.facts.get("out_night") or v.facts.get("back_night")
    # the card may not claim the night legs are gone
    inp = card_input(h, "present.variants")
    out = {"message": "Казань без ночных поездов: туда и обратно днём.", "items": [], "recommended_id": None,
           "upsell": []}
    assert any(i["code"] == "change_claim" for i in check_card_output("present.variants", inp, out, RULES))


# ---------------------------------------------------------------- 1b. the change-claim guard, per kind

def item(vid="cheapest", facts=None, steps=None, risks=()):
    steps = steps if steps is not None else [
        {"type": "flight", "from": "SVO", "to": "LED", "start": "2026-11-20T11:20:00+03:00",
         "end": "2026-11-20T12:35:00+03:00", "carrier": "А", "price_rub": 1},
        {"type": "stay", "from": "H", "to": "H", "start": "2026-11-20T15:00:00+03:00",
         "end": "2026-11-23T12:00:00+03:00", "carrier": None, "price_rub": 1}]
    return {"id": vid, "title": vid, "steps": steps, "facts": dict(facts or {}), "risks": list(risks),
            "sacrifices": [], "not_for": []}


def blocked(card, inp, message, code="change_claim"):
    out = {"message": message, "items": [], "recommended_id": None, "upsell": []}
    return [i for i in change_claim_issues(card, inp, out) if i["code"] == code]


NIGHT_ROW = {"field": "prefs.no_night_flights", "before": None, "after": True}


@pytest.mark.parametrize("message,inp,ok", [
    # night legs: removed only with this turn's row and no night item left
    ("Убрал ночные поезда — теперь все поезда днём.", {"changes": [NIGHT_ROW], "items": [item()]}, True),
    ("Убрал ночные поезда.", {"changes": [NIGHT_ROW], "items": [item(facts={"back_night": True})]}, False),
    ("Убрал ночные поезда.", {"changes": [], "items": [item()]}, False),
    ("Варианты без ночных поездов.", {"changes": [], "items": [item(facts={"out_night": True})]}, False),
    ("Дневных поездов на эти даты нет — в варианте ночной поезд.", {"changes": [], "items": [item()]}, True),
    # kitchen: some / all
    ("Все варианты с кухней в номере.", {"changes": [], "items": [item(facts={"kitchen": True}),
                                                                  item("comfort", {"kitchen": False})]}, False),
    ("Все варианты с кухней в номере.", {"changes": [], "items": [item(facts={"kitchen": True}),
                                                                  item("comfort", {"kitchen": True})]}, True),
    ("Номер с кухней.", {"changes": [], "items": [item(facts={"kitchen": False})]}, False),
    ("С кухней в номере на эти даты ничего нет — показываю без неё.", {"changes": [], "items": [item()]}, True),
    # cot, step-free, lift
    ("Номер с кроваткой для малыша.", {"changes": [], "items": [item(facts={"infant_cot": "on_request"})]}, False),
    ("Номер с кроваткой для малыша.", {"changes": [], "items": [item(facts={"infant_cot": "free"})]}, True),
    ("Отель без ступенек, удобно с коляской.", {"changes": [], "items": [item(facts={"step_free": False})]}, False),
    ("Отель без ступенек.", {"changes": [], "items": [item(facts={"step_free": True})]}, True),
    ("В отеле есть лифт.", {"changes": [], "items": [item(facts={"lift": False})]}, False),
    # direct flights
    ("Только прямые рейсы.", {"changes": [], "items": [item(facts={"out_kind": "flight", "out_stops": 1})]}, False),
    ("Только прямые рейсы.", {"changes": [], "items": [item(facts={"out_kind": "flight", "out_stops": 0})]}, True),
    ("Прямых рейсов нет, с пересадкой — дороже.", {"changes": [], "items": [item()]}, True),
    # dates moved / party recount: a changes row of that field this turn
    ("Перенёс на 22–25 ноября.", {"changes": [], "items": [item()]}, False),
    ("Перенёс на 22–25 ноября.", {"changes": [{"field": "dates.start", "before": "2026-11-20",
                                               "after": "2026-11-22"}], "items": [item()]}, True),
    ("Пересчитал на троих.", {"changes": [], "items": [item()]}, False),
    ("Пересчитал на троих.", {"changes": [{"field": "party.adults", "before": 2, "after": 3}], "items": [item()]}, True),
    # no airport only when the record has none; «билеты не нашлись» never after a transport search
    ("В Бодруме нет аэропорта.", {"changes": [], "items": [item(facts={"dest_airports": True})]}, False),
    ("Билеты не нашлись.", {"changes": [], "items": [item(facts={"transport_gap": "none"})]}, False),
])
def test_change_claim_guard(message, inp, ok):
    assert (blocked("present.update", inp, message) == []) is ok, change_claim_issues(
        "present.update", inp, {"message": message, "items": []})


def test_claims_in_item_texts_use_that_items_facts():
    inp = {"changes": [], "items": [item(facts={"kitchen": True}), item("comfort", {"kitchen": False})]}
    good = {"message": "Готово.", "items": [{"id": "cheapest", "why": "Студия с кухней.", "sacrifices": [],
                                             "risks": [], "not_for": []}]}
    bad = {"message": "Готово.", "items": [{"id": "comfort", "why": "Номер с кухней.", "sacrifices": [],
                                            "risks": [], "not_for": []}]}
    assert change_claim_issues("present.variants", inp, good) == []
    assert [i["data"]["kind"] for i in change_claim_issues("present.variants", inp, bad)] == ["kitchen"]


def test_info_card_no_tickets_claim_after_a_search():
    inp = {"question": "А с пересадкой?", "facts": [{"kind": "transport_search", "searched": True,
                                                     "line": "Прямых рейсов… с пересадкой поездка — от 76 090 ₽"}]}
    assert blocked("present.info", inp, "Билетов я пока не смотрел.")
    assert blocked("present.info", {"question": "?", "facts": []}, "Билетов я пока не смотрел.") == []


@pytest.mark.parametrize("message,offers,ok", [
    ("Дорогу до Казани добавлю одной кнопкой, если нужна.", None, False),
    ("Дорогу до Казани добавлю одной кнопкой, если нужна.", [{"kind": "add_road", "label": "добавить дорогу"}], True),
    ("Скажите — пересоберу с видом на море.", None, False),
    ("Могу подобрать отели с бассейном.", None, False),
    ("Могу подобрать отели ближе к центру.", None, True),
])
def test_promise_guard(message, offers, ok):
    inp = {"items": [item()], **({"offers": offers} if offers else {})}
    assert (blocked("present.variants", inp, message, code="promise_claim") == []) is ok


def test_promise_guard_rebuild_by_an_amenity_already_found_nowhere():
    note = {"code": "relaxed_pref", "text": "С кухней в номере на эти даты ничего нет — показываю без неё",
            "data": {"field": "constraints.kitchen"}}
    inp = {"items": [item(risks=[note])]}
    assert blocked("present.variants", inp, "Скажите — пересоберу с кухней.", code="promise_claim")


# ---------------------------------------------------------------- 2. no transport: honest recovery

def test_ekb_antalya_direct_only_names_connecting(live):
    h = live
    r = trip(h, "Екатеринбург", "Анталья", "Анталья, только прямые", **{"prefs.direct_only": True})
    vs = h.session().variants
    assert vs and all(v.facts["transport_gap"] == "direct_only" for v in vs)
    line = vs[0].facts["transport_gap_ru"]
    low = vs[0].facts["transport_gap_from_rub"]
    assert line.startswith("Прямых рейсов по маршруту Екатеринбург — Анталья") and "с пересадкой" in line, line
    assert "не нашлись" not in line and all(n.text == line for v in vs for n in v.risks if n.code == "no_transport")
    act = [a for a in actions(r, "set_chip") if a["payload"]["field"] == "prefs.direct_only"]
    assert act and act[0]["payload"]["value"] is False
    inp = card_input(h, "present.variants")
    assert {"kind": "connecting", "label": act[0]["label"]} in inp["offers"]
    from evals.card_inputs import validate_card_input
    assert validate_card_input("present.variants", inp) == []
    # the button shows the connecting flights, at the price said
    r, ms = calibrated(h.act, "set_chip", field="prefs.direct_only", value=False)
    assert r.get("error") is None and ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    vs = h.session().variants
    assert all(legs(v) for v in vs) and min(v.totals.rub for v in vs) == low
    assert any((v.facts.get("out_stops") or 0) > 0 for v in vs)


def test_ekb_antalya_question_after_the_gap_answered_from_the_search(live):
    h = live
    trip(h, "Екатеринбург", "Анталья", "Анталья, только прямые", **{"prefs.direct_only": True})
    h.understand(u(intent="info", info="А с пересадкой есть?"))
    r = h.turn("а с пересадкой есть?")
    assert r.get("error") is None
    road = next(f for f in card_input(h, "present.info")["facts"] if f.get("kind") == "transport_search")
    assert road["code"] == "direct_only" and "с пересадкой" in road["line"] and road["from_rub"]
    h.backend.scripts["present.info"] = [
        {"message": "Билетов я пока не смотрел.", "items": [], "recommended_id": None, "upsell": []},
        {"message": "Билетов я пока не смотрел.", "items": [], "recommended_id": None, "upsell": []}]
    h.understand(u(intent="info", info="А с пересадкой есть?"))
    r = h.turn("а с пересадкой есть?")
    assert "не смотрел" not in r["message"]          # guard -> rerun -> code text


def test_yaroslavl_without_flights_builds_by_train(live):
    h = live
    r = trip(h, "Москва", "Ярославль", "Ярославль на выходные")
    vs = h.session().variants
    assert vs and all([s.type for s in legs(v)] == ["train", "train"] for v in vs)
    assert all(v.risks[0].code == "transport_switched" and "собрал поездом" in v.risks[0].text for v in vs)
    assert all(v.facts["transport_gap"] == "train_instead" for v in vs)
    assert not actions(r, "set_chip")
    assert "offers" not in card_input(h, "present.variants")
    # another place goes back to any transport (flights searched again)
    h.understand(u(intent="new_trip", set={"destination.name": "Калининград"}))
    r = h.turn("а давайте лучше Калининград")
    assert r.get("error") is None and h.session().brief.values()["prefs"]["transport"] == "any"
    assert all(any(s.type == "flight" for s in legs(v)) for v in h.session().variants)


def test_yaroslavl_by_plane_says_so_and_offers_the_train(live):
    h = live
    r = trip(h, "Москва", "Ярославль", "Ярославль, только самолётом", **{"prefs.transport": "plane"})
    vs = h.session().variants
    assert all(v.facts.get("no_transport") and v.facts["transport_gap"] == "plane_fixed" for v in vs)
    line = vs[0].facts["transport_gap_ru"]
    assert line.startswith("Самолётов по маршруту Москва — Ярославль в данных нет; поездом поездка — от "), line
    act = next(a for a in actions(r, "set_chip") if a["payload"]["field"] == "prefs.transport")
    assert act["payload"]["value"] == "train"
    r = h.act("set_chip", field="prefs.transport", value="train")
    assert r.get("error") is None
    assert all([s.type for s in legs(v)] == ["train", "train"] for v in h.session().variants)
    assert min(v.totals.rub for v in h.session().variants) == vs[0].facts["transport_gap_from_rub"]


@pytest.mark.parametrize("origin,dest,dest_id", [("Новосибирск", "Анапа", "anapa"),        # ~3400 km: no train
                                                 ("Москва", "Суздаль", "suzdal")])
def test_nothing_reachable_offers_places_that_are(live, origin, dest, dest_id):
    h = live
    h.understand(u(set={"origin.city": origin, "destination.name": dest, **DATES}))
    r, ms = calibrated(h.turn, f"{dest} на три ночи")
    assert r.get("error") is None, r["message"]
    assert ms < TURN_MS_MAX, ms
    vs = h.session().variants
    assert vs and all(v.facts.get("no_transport") and v.facts["transport_gap"] == "none" for v in vs)
    assert vs[0].facts["transport_gap_ru"] == (f"По маршруту {origin} — {dest} в данных нет ни рейсов, ни поездов — "
                                               f"показываю только проживание")
    inp = card_input(h, "present.variants")
    places = inp["other_places"]
    assert 1 <= len(places) <= 3 and all(p["id"] != dest_id and p["price_from_rub"] > 0 for p in places)
    offered = [a["payload"]["destination_id"] for a in actions(r, "choose_destination")]
    assert offered == [p["id"] for p in places]
    assert [o["kind"] for o in inp["offers"]] == ["place"] * len(places)
    # a place offered really has the road
    r = h.act("choose_destination", destination_id=places[0]["id"])
    assert r.get("error") is None and all(legs(v) for v in h.session().variants), r["message"]


def test_degraded_answer_keeps_the_gap_line(live):
    h = live
    bad = {"message": "Билеты не нашлись, но отель есть.", "items": [], "recommended_id": None, "upsell": []}
    h.backend.scripts["present.variants"] = [bad, bad]
    r = trip(h, "Самара", "Бодрум", "Бодрум на три ночи")
    assert r["message"].startswith("По маршруту Самара — Бодрум в данных нет ни рейсов, ни поездов"), r["message"]
    assert "не нашлись" not in r["message"]
    assert h.session().variants[0].facts["dest_airports"] is True   # Bodrum has an airport: never «нет аэропорта»


# ---------------------------------------------------------------- 3. promises and the repeated rebuild

def test_one_tap_road_offer_only_for_stay_only_variants(live):
    h = live
    trip(h, "Москва", "Казань", "отель в Казани на 3 ночи", **{"scope": ["stay"]})
    assert card_input(h, "present.variants")["offers"] == [{"kind": "add_road", "label": "добавить дорогу"}]
    h.new()
    trip(h, "Москва", "Казань", "Казань на 3 ночи")
    inp = card_input(h, "present.variants")
    assert "offers" not in inp
    assert blocked("present.variants", inp, "Дорогу до Казани добавлю одной кнопкой, если нужна.",
                   code="promise_claim")


def test_repeated_rebuild_names_the_unmet_wish(live):
    h = live
    trip(h, "Москва", "Ростов-на-Дону", "Ростов, нужна кухня в номере", **{"constraints.add": ["kitchen"]})
    vs = h.session().variants
    assert all(any(n.code == "relaxed_pref" and n.data.get("field") == "constraints.kitchen" for n in v.risks)
               for v in vs)
    assert all(v.facts.get("kitchen") is False for v in vs)
    for text in ("пересоберите, чтобы с кухней", "вы ничего не поменяли, нужна кухня"):
        h.understand(u(intent="refine", set={"constraints.add": ["kitchen"]}))
        r = h.turn(text)
        assert r.get("error") is None
    assert "С кухней в номере на эти даты ничего нет" in r["message"] or \
        "с кухней в номере на эти даты ничего нет" in r["message"], r["message"]
    assert "где это гарантировано" not in r["message"] and "Ближе всего" in r["message"]


def test_repeated_rebuild_offers_only_real_buttons(live):
    h = live
    trip(h, "Екатеринбург", "Анталья", "Анталья, только прямые", **{"prefs.direct_only": True})
    for text in ("пересоберите, только прямые", "опять то же, нужны прямые"):
        h.understand(u(intent="refine", set={"prefs.direct_only": True}))
        r = h.turn(text)
    assert "прямых рейсов по маршруту екатеринбург — анталья" in r["message"].casefold(), r["message"]
    assert "«Показать с пересадкой»" in r["message"] and "за проживание" in r["message"]
    assert any(a["type"] == "set_chip" for a in r["actions"])


# ---------------------------------------------------------------- 4. kitchen / step-free act

def test_mock_amenities_are_synthetic_and_deterministic():
    from ostrovok_mcp import hotels as H
    apt = {"hotel_id": "x1", "kind": "apartment", "stars": None}
    hotel = {"hotel_id": "x2", "kind": "hotel", "stars": 4}
    assert H.room_kitchen(apt, {"name": "Двухместный номер"}) and H.room_kitchen(hotel, {"name": "Студия"})
    assert H.room_kitchen(hotel, {"name": "Апартаменты с одной спальней"})
    assert not H.room_kitchen(hotel, {"name": "Стандартный двухместный"})
    assert H.accessibility({"hotel_id": "h", "kind": "hostel", "stars": 3}) == {"step_free": False, "lift": False,
                                                                               "synthetic": True}
    a1 = H.accessibility(hotel)
    assert a1 == H.accessibility(dict(hotel)) and a1["synthetic"] is True and (a1["lift"] or not a1["step_free"])
    from ostrovok_mcp import db
    rows = db.catalog().query("hotels", "kind = ?", ("hotel",), key="hotel_id")
    high = [h for h in rows if (h.get("stars") or 0) >= 3]
    low = [h for h in rows if (h.get("stars") or 0) < 3]
    share = lambda hs: sum(H.accessibility(h)["step_free"] for h in hs) / max(1, len(hs))  # noqa: E731
    assert share(high) > share(low) + 0.2


@pytest.mark.parametrize("cons,fact", [("kitchen", "kitchen"), ("accessible", "step_free")])
def test_amenity_filters_in_a_city_with_apartments(live, cons, fact):
    h = live
    trip(h, "Москва", "Санкт-Петербург", "Питер", **{"constraints.add": [cons]})
    vs = h.session().variants
    assert vs and all(v.facts.get(fact) is True for v in vs), [(v.id, v.facts.get(fact)) for v in vs]
    assert not any(n.code in ("relaxed_pref", "accessibility_unknown") for v in vs for n in v.risks
                   if n.data.get("field", "").startswith("constraints.") or n.code == "accessibility_unknown")
    # info: «есть ли кухня / можно ли без ступенек» from the stay facts
    h.understand(u(intent="info", info="Есть ли кухня и можно ли без ступенек?", refs=[{"kind": "variant", "ref": "1"}]))
    h.turn("есть ли там кухня и можно ли без ступенек?")
    stays = [f["stay"] for f in card_input(h, "present.info")["facts"] if f.get("kind") == "variant" and f.get("stay")]
    assert stays and all(s.get(fact) is True for s in stays)


def test_without_the_wish_variants_carry_the_amenity_facts(live):
    h = live
    trip(h, "Москва", "Санкт-Петербург", "Питер")
    for v in h.session().variants:
        assert isinstance(v.facts.get("kitchen"), bool) and isinstance(v.facts.get("step_free"), bool)


# ---------------------------------------------------------------- 5. understanding: services not sold, open dates

@pytest.mark.parametrize("text,found,only", [
    ("Нужна машина в аренду в Калининграде, больше ничего не надо", ["аренду машин"], True),
    ("Оформите визу в Китай", ["визы"], False),
    ("Казань на выходные вдвоём, и экскурсию по Кремлю бы", ["экскурсии"], False),
    ("Казань на выходные вдвоём", [], False),
    ("Сочи на неделю, страховку и прокат машины тоже", ["аренду машин", "страховку"], False),
])
def test_unsold_cues(text, found, only):
    assert unsold_cues(RULES, text) == found
    assert unsold_only(RULES, text) is only
    if found:
        assert not_sold_fact(found)["line"].endswith("я не оформляю — подбираю дорогу, отели и трансферы")


def test_unsold_only_is_out_of_scope_without_a_trip_patch(live):
    h = live
    h.understand(u(set={"destination.name": "Калининград"}))      # the model read a trip into it
    r = h.turn("Нужна машина в аренду в Калининграде, больше ничего не надо")
    assert r.get("error") is None and not r["cards"]["variants"]
    assert h.session().brief.values().get("destination") is None
    inp = card_input(h, "present.fallback")
    assert inp["reason"] == "out_of_scope"


def test_unsold_next_to_a_trip_is_one_sentence_and_the_trip_goes_on(live):
    h = live
    r = trip(h, "Москва", "Казань", "Казань 20–23 ноября вдвоём, и экскурсию по Кремлю бы")
    assert r["cards"]["variants"]
    q = card_input(h, "present.variants")["question"]
    assert q["facts"][0]["kind"] == "not_sold" and q["facts"][0]["items_ru"] == "экскурсии"


def test_open_dates_stay_a_month_and_are_assumed(live):
    h = live
    h.understand(u(set={"origin.city": "Москва", "destination.name": "Казань", "dates.month": 11,
                        "dates.flexible_days": 3, "party.segment": "couple", "party.adults": 2}))
    r = h.turn("на ноябрьские в Казань, но числа пока плавают")
    assert r.get("error") is None and r["cards"]["variants"]
    b = h.session().brief
    assert b.values()["dates"]["start"][:7] == "2026-11" and b.source_of("dates.start") != "user"
    assert "dates.start" in card_input(h, "present.variants")["assumed"]


def test_airport_question_answered_from_the_record(live):
    h = live
    trip(h, "Самара", "Бодрум", "Бодрум на три ночи")
    h.understand(u(intent="info", info="Есть ли в Бодруме аэропорт?"))
    h.turn("а там вообще есть аэропорт?")
    inp = card_input(h, "present.info")
    place = next(f for f in inp["facts"] if f.get("kind") == "place")
    assert place["dest_airports"] is True and place["airports"]
    assert blocked("present.info", inp, "В Бодруме нет аэропорта.")
