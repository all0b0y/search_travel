"""Party and rooms (third live level-3 run: s10, s19, s21).

Real engine, real ranking/checks, the in-process Ostrovok mock over the real catalog and real open data
(the `live` fixture of test_scopes); the model is a scripted FakeBackend.

- s10 / s19: an unknown party was assumed as two adults and the order blocked at checkout with one
  traveller («Путешественников 1, а … оформлено на 2 чел.»). Now a business trip means one adult, the
  profile's saved travellers give the party, and a checkout with another head count re-prices the same
  variant for the travellers in the same turn.
- s10: for one adult every variant was a hostel dorm bed. Now private rooms always reach ranking, only
  «Дешевле» may be a dorm bed (said so in its notes), the recommended pick never is.
- s21: hours given to cards are rounded to 1 decimal at the source.
"""
import json

import pytest

from agent.checks import blocking, check_variant
from agent.profiles import Profile, SavedTraveller
from agent.ranking import _is_dorm, recommended_hint

from .fakes import u
from .test_scopes import live  # noqa: F401  (fixture)

CONTACT = {"email": "oleg.morozov@example.com"}
OLEG = [{"name": "Олег Морозов", "birth_date": "1976-04-04"}]
GALINA = [{"name": "Галина Широкова", "birth_date": "1978-12-08"}]

KAZAN_STAY = {"destination.name": "Казань", "dates.start": "2026-11-20", "dates.nights": 3, "scope": ["stay"]}
NN_TRIP = {"destination.name": "Нижний Новгород", "dates.start": "2026-12-09", "dates.end": "2026-12-11"}


def ask(h, text, **patch):
    h.understand(u(set=patch))
    r = h.turn(text)
    assert r.get("error") is None, (r["message"], r.get("error"))
    assert r["stage"] == "selection" and r["cards"]["variants"], r["message"]
    return r, h.session().variants


def dorm(v):
    return bool(v.facts.get("dorm_bed")) or _is_dorm({"name": v.facts.get("room_name") or ""})


def party(h):
    return h.session().brief.values()["party"]


def pay_actions(r):
    return [a for a in r["actions"] if a["type"] == "pay"]


def last_input(h, card):
    req = next(r for r in reversed(h.backend.requests) if r.card == card)
    return json.loads(req.user)


# ---------------------------------------------------------------- 1a: the assumed party

@pytest.mark.parametrize("live", ["stage2"], indirect=True)
def test_business_trip_means_one_adult(live):
    h = live
    r, vs = ask(h, "Командировка в Нижний Новгород 9–11 декабря, нужен отель в центре и дорога туда-обратно",
                **NN_TRIP)
    b = h.session().brief
    assert party(h)["adults"] == 1 and party(h)["segment"] == "solo", party(h)
    assert b.source_of("party.segment") == "inferred"            # an assumption, shown as a chip
    assert [c["label"] for c in r["chips"] if c["field"] == "party.segment"] == ["один"], (r["chips"], r["questions"])
    assert not [q for q in r["questions"] if q["field"].startswith("party.")], r["questions"]
    hotels = [s for v in vs for s in v.steps if s.type == "stay"]
    assert hotels


def test_user_party_wins_over_business_cue(live):
    h = live
    ask(h, "Командировка в Нижний Новгород 9–11 декабря, едем вдвоём с коллегой",
        **NN_TRIP, **{"party.adults": 2})
    assert party(h)["adults"] == 2


@pytest.mark.parametrize("live", ["stage2"], indirect=True)
def test_saved_travellers_give_the_party(live):
    h = live
    h.engine.profiles.put(Profile(id="oleg", home_city="Самара", home_iata=["KUF"], age=50,
                                  travellers=[SavedTraveller(**t) for t in OLEG]))
    h.new(profile_id="oleg")
    r, _ = ask(h, "Нужен отель в Казани на 3 ночи с 20 ноября, билеты уже есть", **KAZAN_STAY)
    assert party(h)["adults"] == 1 and party(h)["segment"] == "solo", party(h)
    assert h.session().brief.source_of("party.adults") == "profile"
    assert [c["label"] for c in r["chips"] if c["field"] == "party.segment"] == ["один"], (r["chips"], r["questions"])


def test_saved_family_travellers_ages_on_trip_start(live):
    h = live
    fam = [{"name": "Анна", "birth_date": "1990-05-05"}, {"name": "Пётр", "birth_date": "1988-01-01"},
           {"name": "Маша", "birth_date": "2019-11-25"}]                 # 6 on 20 Nov 2026, 7 five days later
    h.engine.profiles.put(Profile(id="fam", home_city="Москва", home_iata=["SVO"],
                                  travellers=[SavedTraveller(**t) for t in fam]))
    h.new(profile_id="fam")
    ask(h, "Отель в Казани на 3 ночи с 20 ноября", **KAZAN_STAY)
    p = party(h)
    assert (p["segment"], p["adults"], p["children"], p["children_ages"]) == ("family", 2, 1, [6]), p


def test_no_profile_travellers_keeps_default(live):
    h = live                                                  # demo profile: no saved travellers
    ask(h, "Нужен отель в Казани на 3 ночи с 20 ноября, билеты уже есть", **KAZAN_STAY)
    assert party(h)["adults"] == 2


# ---------------------------------------------------------------- 1b: checkout re-prices for the travellers

def test_s10_checkout_with_one_traveller_reprices_same_hotel(live):
    h = live
    _, vs = ask(h, "Нужен отель в Казани на 3 ночи с 20 ноября, билеты уже есть", **KAZAN_STAY)
    assert party(h)["adults"] == 2
    chosen = next(v for v in vs if not dorm(v))
    old_hotel = chosen.hotel.hotel_id
    r = h.act("choose_variant", variant_id=chosen.id)
    old_total = r["cart"]["total_rub"]
    r = h.act("checkout", travellers=OLEG, contact=CONTACT)
    assert r.get("error") is None and r["stage"] == "checkout", r["message"]
    order = r["order"]
    assert order["status"] == "pending_payment"
    assert not [i for i in order["issues"] if i["severity"] == "blocking"], order["issues"]
    pay = pay_actions(r)
    assert len(pay) == 1 and pay[0]["label"] in r["message"], (r["actions"], r["message"])
    assert r["message"].startswith("Пересчитал на одного: "), r["message"]
    from agent.payloads import fmt_rub
    assert f"{fmt_rub(order['total_rub'])} вместо {fmt_rub(old_total)}" in r["message"], r["message"]
    assert party(h)["adults"] == 1
    s = h.session()
    v = s.variant(chosen.id)
    assert v.hotel.hotel_id == old_hotel and v.totals.rub == order["total_rub"]
    assert blocking(check_variant(v, s.brief, h.engine.skill.rules)) == []
    r = h.act("pay", order_id=order["order_id"])
    assert r["stage"] == "ordered" and r["order"]["status"] == "paid", r["message"]


def test_s19_checkout_with_one_traveller_reprices_flights_and_hotel(live):
    h = live
    _, vs = ask(h, "Нижний Новгород 9–11 декабря, нужен отель в центре и дорога туда-обратно", **NN_TRIP)
    assert party(h)["adults"] == 2
    chosen = vs[-1]
    numbers = [(s.type, s.carrier, s.start) for s in chosen.steps if s.type in ("flight", "train")]
    r = h.act("choose_variant", variant_id=chosen.id)
    old_total = r["cart"]["total_rub"]
    r = h.act("checkout", travellers=GALINA, contact={"email": "g@example.com"})
    order = r["order"]
    assert r["stage"] == "checkout" and order["status"] == "pending_payment", r["message"]
    assert not [i for i in order["issues"] if i["severity"] == "blocking"], order["issues"]
    assert pay_actions(r) and r["message"].startswith("Пересчитал на одного: "), r["message"]
    assert order["total_rub"] < old_total
    v = h.session().variant(chosen.id)
    assert [(s.type, s.carrier, s.start) for s in v.steps if s.type in ("flight", "train")] == numbers


def test_checkout_same_party_is_unchanged(live):
    h = live
    _, vs = ask(h, "Отель в Казани на 3 ночи с 20 ноября, вдвоём", **KAZAN_STAY, **{"party.adults": 2})
    r = h.act("choose_variant", variant_id=vs[0].id)
    total = r["cart"]["total_rub"]
    r = h.act("checkout", travellers=[{"name": "А", "birth_date": "1990-01-01"},
                                      {"name": "Б", "birth_date": "1991-01-01"}], contact=CONTACT)
    assert r["order"]["total_rub"] == total and "Пересчитал" not in r["message"] and pay_actions(r)


# ---------------------------------------------------------------- 2: private rooms for one adult

def test_solo_kazan_private_rooms_not_dorms(live):
    h = live
    r, vs = ask(h, "Отель в Казани на 3 ночи с 20 ноября, я один", **KAZAN_STAY,
                **{"party.segment": "solo", "party.adults": 1})
    assert len(vs) >= 2, [v.id for v in vs]
    privates = [v for v in vs if not dorm(v)]
    assert privates, [(v.id, v.facts.get("room_name")) for v in vs]
    for v in vs:
        if v.id != "cheapest":
            assert not dorm(v), (v.id, v.facts.get("room_name"))
        if dorm(v):
            texts = " ".join(n.text for n in list(v.risks) + list(v.not_for))
            assert "общем номере" in texts, texts
    assert not dorm(next(v for v in vs if v.id == recommended_hint(vs)))
    assert last_input(h, "present.variants")["recommended_hint"] in {v.id for v in privates}


def test_hostel_wish_keeps_dorms(live):
    h = live
    _, vs = ask(h, "Хостел в Казани на 3 ночи с 20 ноября, я один", **KAZAN_STAY,
                **{"party.segment": "solo", "party.adults": 1})
    assert "hostel" in (h.session().brief.values()["constraints"] or [])
    assert any(dorm(v) for v in vs)


def test_private_room_wish_drops_dorms(live):
    h = live
    _, vs = ask(h, "Тихий отель в Казани на 3 ночи с 20 ноября, отдельный номер, не хостел", **KAZAN_STAY,
                **{"party.segment": "solo", "party.adults": 1})
    assert vs and not any(dorm(v) for v in vs), [(v.id, v.facts.get("room_name")) for v in vs]


# ---------------------------------------------------------------- 3: hours for cards, 1 decimal

def _one_decimal(x):
    return x is None or round(float(x), 1) == float(x)


def test_destination_train_hours_rounded(live):
    h = live
    h.engine.profiles.put(Profile(id="spb", home_city="Санкт-Петербург", home_iata=["LED"]))
    h.new(profile_id="spb")
    h.understand(u(set={"traits.add": ["nature"], "prefs.transport": "train", "dates.start": "2026-12-04",
                        "dates.end": "2026-12-07", "party.adults": 1}))
    r = h.turn("недалеко от Питера, на поезде, природа")
    assert r.get("error") is None and r["cards"]["destinations"], r["message"]
    items = last_input(h, "present.destinations")["items"]
    for i in items:
        assert _one_decimal(i["travel_time_h"]), i
        w = i.get("weather") or {}
        assert all(_one_decimal(w.get(k)) for k in ("air_max_c", "sea_c", "rain_days")), w


def test_variant_hours_rounded(live):
    h = live
    ask(h, "Нижний Новгород 9–11 декабря, отель и дорога", **NN_TRIP)
    for i in last_input(h, "present.variants")["items"]:
        assert all(_one_decimal(i["totals"].get(k)) for k in ("door_to_door_hours", "out_hours", "back_hours")), i


def test_reprice_failure_keeps_the_honest_blocker(live, monkeypatch):
    from agent import steps
    h = live
    _, vs = ask(h, "Нужен отель в Казани на 3 ночи с 20 ноября, билеты уже есть", **KAZAN_STAY)
    r = h.act("choose_variant", variant_id=vs[0].id)
    monkeypatch.setattr(steps, "build", lambda *a, **k: [])          # nothing builds for the travellers
    r = h.act("checkout", travellers=OLEG, contact=CONTACT)
    assert not pay_actions(r) and "оплатить его пока нельзя" in r["message"], r["message"]
    assert "Пересчитал" not in r["message"]
    assert [a for a in r["actions"] if a["type"] == "choose_variant"], r["actions"]
