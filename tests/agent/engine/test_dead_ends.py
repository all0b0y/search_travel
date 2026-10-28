"""Dead ends and invisible fees, offline on the real mock and the real data (in-process Ostrovok mock over
data/ostrovok_catalog.sqlite, real open data, a scripted model). Our own examples:

1. a trip whose required transport does not exist: the whole build used to fail with «Собрать поездку не
   получилось: … нет рейсов туда», the same sentence on every follow-up and on «а с пересадкой?», although a train
   goes there. Now the no-build path runs the same recovery as stay-only variants (steps.recover_transport
   built_nothing): «билеты» are tickets, not a plane, so the trip is rebuilt by train with the honest note; a user
   who fixed the plane («летим», «самолётом») hears that no flight exists (nor with a connection) and gets
   «Поехать поездом» with the price plus up to 3 similar reachable places; follow-ups about connections or trains
   are answered from what code searched, never with the same sentence twice in a row;
2. a pet fee priced into a ticket is named on its cart / order line (title suffix + pet_fee_rub / includes_ru),
   the cart / checkout / info answers name the line, and the lines still sum to the total.
"""

import pytest

from agent.steps import plane_cue

from .conftest import skill
from .fakes import u
from .test_honesty import TURN_MS_MAX, actions, card_input, legs
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)
from .turn_time import calibrated

RULES = skill().rules
GELENDZHIK = {"origin.city": "Самара", "destination.name": "Геленджик", "dates.start": "2027-07-05",
              "dates.end": "2027-07-12", "party.segment": "friends", "party.adults": 3}
KRASNODAR = {"origin.city": "Екатеринбург", "destination.name": "Краснодар", "dates.month": 8, "dates.nights": 7,
             "party.segment": "couple", "party.adults": 2}
SAMARA_TEXT = "Из Самары в Геленджик на 5–12 июля, нас трое, нужны билеты туда-обратно, отель и трансфер"
FLY_TEXT = "Летим вдвоём из Екатеринбурга в Краснодар на неделю в августе"
TRAVELLERS2 = [{"name": "Иван Петров", "birth_date": "1990-01-01"}, {"name": "Мария Петрова", "birth_date": "1991-02-02"}]


def timed(h, text, **understand):
    h.understand(u(**understand))
    r, ms = calibrated(h.turn, text)
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    return r


def labels(r):
    return [a["label"] for a in r["actions"]]


# ---------------------------------------------------------------- 1a. plane cues are the user's words

@pytest.mark.parametrize("text,plane", [
    (FLY_TEXT, True), ("Хотим полететь самолётом в Сочи", True), ("Летим в Анапу в июле", True),
    ("Не хочу лететь, давайте поездом", False), ("лететь не хотим", False), ("без самолёта, пожалуйста", False),
    ("Летим или едем поездом — неважно", False), (SAMARA_TEXT, False), ("Из Москвы в Казань на выходные", False),
    ("прилететь до обеда", False),
])
def test_plane_cue(text, plane):
    assert plane_cue(RULES, text) is plane


# ---------------------------------------------------------------- 1b. «билеты» without a plane: rebuilt by train

def test_tickets_only_without_flights_is_rebuilt_by_train(live):
    h = live
    r = timed(h, "Из Самары в Геленджик, только билеты туда-обратно", intent="new_trip",
              set={**GELENDZHIK, "scope": ["flights"]})
    assert r.get("error") is None, r["message"]
    vs = h.session().variants
    assert vs and all([s.type for s in legs(v)] == ["train", "train"] for v in vs)
    assert all(v.facts["transport_gap"] == "train_instead" for v in vs)
    assert all(v.risks[0].code == "transport_switched" and "собрал поездом" in v.risks[0].text for v in vs)
    assert "Собрать поездку не получилось" not in r["message"]
    assert "собрал поездом" in r["message"] and "в пути 1 сутки" in r["message"], r["message"]
    inp = card_input(h, "present.variants")
    assert any(row["field"] == "prefs.transport" and row.get("after") == "train" for row in inp.get("changes") or []) \
        or inp["items"][0]["facts"]["transport_gap"] == "train_instead"


def test_full_trip_with_user_scope_flights_goes_by_train_with_the_leave_day(live):
    """«нужны билеты туда-обратно, отель и трансфер» reads as scope flights + stay + transfers set by the user:
    before, the train was never searched and the answer said «нет ни рейсов, ни поездов» (untrue)."""
    h = live
    r = timed(h, SAMARA_TEXT, intent="new_trip", set={**GELENDZHIK, "scope": ["flights", "stay", "transfers"]})
    vs = h.session().variants
    assert vs and all([s.type for s in legs(v)] == ["train", "train"] for v in vs)
    assert all(v.facts["transport_gap"] == "train_instead" for v in vs)
    assert "ни поездов" not in r["message"]
    best = vs[0]
    assert best.facts["out_long_train"] and best.facts["out_days_before_checkin"] >= 1
    # the code text names the train's time on board and the leave day
    assert f"в пути {best.facts['out_on_road_ru']}" in r["message"] and "выезд 4 июля" in r["message"], r["message"]


# ---------------------------------------------------------------- 1c. the user fixed the plane

def test_plane_fixed_offers_the_train_with_its_price_and_places(live):
    h = live
    r = timed(h, FLY_TEXT, intent="new_trip", set=KRASNODAR)
    s = h.session()
    assert s.brief.values()["prefs"]["transport"] == "plane" and s.brief.source_of("prefs.transport") == "user"
    vs = s.variants
    assert vs and all(v.facts.get("no_transport") and v.facts["transport_gap"] == "plane_fixed" for v in vs)
    low = vs[0].facts["transport_gap_from_rub"]
    assert "Самолётов по маршруту Екатеринбург — Краснодар в данных нет" in r["message"]
    chip = [a for a in actions(r, "set_chip") if a["payload"] == {"field": "prefs.transport", "value": "train"}]
    assert chip and chip[0]["label"] == "Поехать поездом"
    places = actions(r, "choose_destination")
    assert 1 <= len(places) <= 3
    inp = card_input(h, "present.variants")
    assert {"kind": "train", "label": "Поехать поездом"} in inp["offers"]
    assert inp["other_places"] and all(p["transport_ru"] != "поездом" for p in inp["other_places"])   # by plane
    assert low > 0
    # «а с пересадкой?» as a question: answered from what code searched, with the train button again
    r2 = timed(h, "а с пересадкой?", intent="info", info="а с пересадкой?")
    assert r2.get("error") is None
    road = next(f for f in card_input(h, "present.info")["facts"] if f.get("kind") == "transport_search")
    assert road["code"] == "plane_fixed" and "ни прямых, ни с пересадкой" in road["connecting_ru"]
    assert r2["message"].startswith("С пересадкой тоже нет: рейсов по маршруту Екатеринбург — Краснодар"), r2["message"]
    assert "Поехать поездом" in labels(r2)
    # the same question as a rebuild: the answer leads with it, never the first answer again
    r3 = timed(h, "а с пересадкой?", intent="refine", set={"prefs.direct_only": False})
    assert r3["message"] != r["message"] and r3["message"].startswith("С пересадкой тоже нет"), r3["message"]
    q = card_input(h, "present.update")["question"]
    assert any(f.get("kind") == "transport_search" for f in q["facts"])
    r4 = timed(h, "а с пересадкой?", intent="refine", set={"prefs.direct_only": False})
    assert r4["message"] != r3["message"], r4["message"]
    # the button builds it by train
    r5 = h.act("set_chip", field="prefs.transport", value="train")
    assert r5.get("error") is None, r5["message"]
    assert all([s.type for s in legs(v)] == ["train", "train"] for v in h.session().variants)


def test_plane_fixed_tickets_only_is_no_dead_end(live):
    """Nothing builds at all (tickets only, no flight to the place): the answer says no flight exists, nor with
    a connection, gives the train's price as a button and similar places; never the same sentence twice."""
    h = live
    r = timed(h, "Летим вдвоём из Екатеринбурга в Краснодар, только билеты", intent="new_trip",
              set={**KRASNODAR, "scope": ["flights"]})
    assert r["error"]["code"] == "no_data"
    assert "Собрать поездку не получилось" not in r["message"]
    assert "ни прямых, ни с пересадкой" in r["message"] and "Поездом та же поездка — от " in r["message"], r["message"]
    assert "Поехать поездом" in labels(r) and 1 <= len(actions(r, "choose_destination")) <= 3
    seen = [r["message"]]
    for kind in ("refine", "info", "refine", "refine"):
        extra = {"set": {"prefs.direct_only": False}} if kind == "refine" else {"info": "а с пересадкой?"}
        r = timed(h, "а с пересадкой?", intent=kind, **extra)
        assert r["message"] != seen[-1], (kind, r["message"])
        assert "пересадк" in r["message"] or "ещё раз" in r["message"], r["message"]
        assert "Поехать поездом" in labels(r)
        seen.append(r["message"])
    r = h.act("set_chip", field="prefs.transport", value="train")
    assert r.get("error") is None and h.session().variants, r["message"]


def test_place_without_any_road_still_says_so_once(live):
    """Новосибирск — Анапа: no flight, no train (beyond 2500 km) — tickets only: the honest line, places."""
    h = live
    r = timed(h, "Новосибирск — Анапа, только билеты", intent="new_trip",
              set={"origin.city": "Новосибирск", "destination.name": "Анапа", "dates.start": "2026-11-20",
                   "dates.end": "2026-11-23", "party.segment": "couple", "party.adults": 2, "scope": ["flights"]})
    assert r["error"]["code"] == "no_data"
    assert "в данных нет ни рейсов, ни поездов" in r["message"] and actions(r, "choose_destination"), r["message"]
    again = timed(h, "Новосибирск — Анапа, только билеты", intent="refine",
                  set={"destination.name": "Анапа", "scope": ["flights"]})
    assert again["message"] != r["message"]


def test_flights_both_ways_keep_their_old_no_build_text(live):
    """No new noise: a pair with flights that simply do not stack keeps the old no-build answer."""
    h = live
    h.understand(u(set={"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-11-20",
                        "dates.end": "2026-11-23", "party.segment": "couple", "party.adults": 2}))
    r = h.turn("Сочи на три ночи")
    assert r.get("error") is None and all([s.type for s in legs(v)] == ["flight", "flight"]
                                          for v in h.session().variants)
    assert not any(v.facts.get("transport_gap") for v in h.session().variants)


# ---------------------------------------------------------------- 2. pet fees visible on their lines

def _sum_ok(box):
    assert sum(i["price_rub"] for i in box["items"]) == box["total_rub"]


def _pet_lines(box):
    return [i for i in box["items"] if i.get("pet_fee_rub")]


@pytest.mark.parametrize("patch,text,kind,word,where", [
    ({"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
      "party.segment": "couple", "party.adults": 2, "party.pets": ["dog"], "constraints.add": ["pet:dog"]},
     "Москва — Сочи самолётом, с нами маленькая собака в салоне", "flight", "собака", "в салоне"),
    ({"origin.city": "Санкт-Петербург", "destination.name": "Москва", "dates.start": "2026-12-04",
      "dates.end": "2026-12-07", "party.segment": "couple", "party.adults": 2, "party.pets": ["cat"],
      "constraints.add": ["pet:cat"], "prefs.transport": "train"},
     "Из Петербурга в Москву на Сапсане с котом", "train", "кот", "в переноске"),
])
def test_pet_fee_is_named_on_its_line(live, patch, text, kind, word, where):
    h = live
    r = timed(h, text, intent="new_trip", set=patch)
    assert r.get("error") is None, r["message"]
    v = h.session().variants[0]
    fee = v.facts["price_pets_rub"]
    assert fee > 0
    r = h.act("choose_variant", variant_id=v.id)
    cart = r["cart"]
    _sum_ok(cart)
    assert cart["total_rub"] == v.totals.rub
    pets = _pet_lines(cart)
    assert [i["kind"] for i in pets] == [kind, kind]
    assert sum(i["pet_fee_rub"] for i in pets) == fee
    for i in pets:
        assert i["includes_ru"].startswith(f"в т.ч. {word} {where} — ")
        assert i["title"].endswith(f"({i['includes_ru']})")
    assert not [i for i in cart["items"] if i["kind"] in ("hotel", "transfer") and i.get("pet_fee_rub")]
    assert "Провоз питомца уже в цене билетов: «" in r["message"] and pets[0]["includes_ru"] in r["message"], r["message"]
    # an info question in the cart names the line, never a separate pet line
    r2 = timed(h, "а где в корзине доплата за питомца?", intent="info", info="а где в корзине доплата за питомца?")
    facts = card_input(h, "present.info")["facts"]
    cart_fact = next(f for f in facts if f.get("kind") == "cart")
    assert [i["pet_fee_rub"] for i in cart_fact["items"] if i.get("pet_fee_rub")] == [i["pet_fee_rub"] for i in pets]
    assert r2["message"].startswith("Провоз питомца уже в цене билетов"), r2["message"]
    assert "сумма строк корзины" in r2["message"]
    # checkout: the order lines carry it too and still sum to the total
    r3 = h.act("checkout", travellers=TRAVELLERS2, contact={"email": "a@b.c"})
    order = r3["order"]
    _sum_ok(order)
    assert [i["pet_fee_rub"] for i in _pet_lines(order)] == [i["pet_fee_rub"] for i in pets]
    assert "Провоз питомца уже в цене билетов" in r3["message"], r3["message"]
    r4 = timed(h, "а за кота / собаку точно заплачено?", intent="info", info="за питомца точно заплачено?")
    order_fact = next(f for f in card_input(h, "present.info")["facts"] if f.get("kind") == "order")
    assert [i.get("pet_fee_rub") for i in order_fact["data"]["items"] if i.get("pet_fee_rub")]
    assert "сумма строк заказа" in r4["message"], r4["message"]


def test_no_pet_no_fee_fields(live):
    h = live
    timed(h, "Сочи на три ночи", intent="new_trip",
          set={"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-11-20",
               "dates.end": "2026-11-23", "party.segment": "couple", "party.adults": 2})
    r = h.act("choose_variant", variant_id=h.session().variants[0].id)
    assert not _pet_lines(r["cart"]) and "питом" not in r["message"]
    assert all("в т.ч." not in i["title"] for i in r["cart"]["items"])


# ---------------------------------------------------------------- 3. guards in the turn: one rerun, then code text

EKB_BUDGET = {"origin.city": "Екатеринбург", "party.segment": "couple", "party.adults": 2, "budget_rub.max": 25000,
              "dates.month": 11, "dates.nights": 3}


def _scripted(bad, bad_n):
    """A present.destinations script: the message `bad` for the first bad_n calls, then a true one."""
    calls = []

    def script(req):
        import json as _json
        items = _json.loads(req.user)["items"]
        calls.append(req)
        msg = bad if len(calls) <= bad_n else "В 25 000 ₽ укладываются Казань поездом и Самара поездом."
        return {"message": msg, "items": [{"id": i["id"], "why": i["name"], "watch_out": None} for i in items],
                "recommended_id": items[0]["id"], "upsell": []}
    return script, calls


@pytest.mark.parametrize("bad_calls", [1, 2])
def test_nothing_fits_claim_is_rerun_then_replaced(live, bad_calls):
    h = live
    script, calls = _scripted("В 25 000 ₽ ничего не укладывается — вот самые доступные варианты.", bad_calls)
    h.backend.scripts["present.destinations"] = script
    r = timed(h, "Вдвоём из Екатеринбурга на три ночи в ноябре, бюджет 25 тысяч — куда можно?", intent="new_trip",
              set=EKB_BUDGET)
    items = card_input(h, "present.destinations")["items"]
    fits = [i["name"] for i in items if i["facts"].get("within_budget") is True]
    assert sorted(fits) == ["Казань", "Самара"] and len(items) > 2
    assert len(calls) == 2                                   # one rerun, never more
    assert "ничего не укладывается" not in r["message"]
    if bad_calls == 2:   # the guard failed twice: the code text names the places that fit
        assert r["message"].startswith("В 25 000 ₽ укладываются: "), r["message"]
        assert "Казань поездом — от " in r["message"] and "Самара поездом — от " in r["message"]


@pytest.mark.parametrize("bad_n", [1, 2])
def test_long_train_decimal_hours_are_rerun_then_replaced(live, bad_n):
    h = live
    calls = []

    def script(req):
        import json as _json
        inp = _json.loads(req.user)
        calls.append(req)
        rec = inp["recommended_hint"]
        f = next(i for i in inp["items"] if i["id"] == rec)["facts"]
        msg = (f"Советую этот вариант: туда {f['out_on_road_ru']}, обратно 24,1 ч." if len(calls) <= bad_n else
               f"Советую этот вариант: поездом, в пути {f['out_on_road_ru']}, выезд за день до заезда.")
        return {"message": msg, "items": [{"id": i["id"], "why": "Поездом.", "sacrifices": [], "risks": [],
                                           "not_for": []} for i in inp["items"]],
                "recommended_id": rec, "upsell": []}
    h.backend.scripts["present.variants"] = script
    r = timed(h, SAMARA_TEXT, intent="new_trip", set={**GELENDZHIK, "scope": ["flights", "stay", "transfers"]})
    assert len(calls) == 2 and "24,1 ч" not in r["message"]
    assert "24,1 ч" in calls[1].system           # the rerun was told what to fix
    best = h.session().variants[0]
    assert f"в пути {best.facts['out_on_road_ru']}" in r["message"]
    assert ("за день до заезда" in r["message"]) if bad_n == 1 else ("выезд 4 июля" in r["message"]), r["message"]


# ---------------------------------------------------------------- 4. a condition that is met is said (code text too)

def test_kitchen_met_is_confirmed_in_the_code_text(live):
    h = live
    r = timed(h, "Питер 20–23 ноября вдвоём, нужна кухня в номере", intent="new_trip",
              set={"origin.city": "Москва", "destination.name": "Санкт-Петербург", "constraints.add": ["kitchen"],
                   "dates.start": "2026-11-20", "dates.end": "2026-11-23", "party.segment": "couple",
                   "party.adults": 2})
    assert all(v.facts.get("kitchen") is True for v in h.session().variants)
    assert "в номере есть кухня" in r["message"], r["message"]
