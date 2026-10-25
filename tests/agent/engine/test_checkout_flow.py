"""Order and checkout end to end (second live level-3 run, s03 / s09): train trips reach payment, and no
checkout response is a dead end.

Real engine, real ranking/checks, the in-process Ostrovok mock over the real catalog and real open data
(the `live` fixture of test_scopes); the model is a scripted FakeBackend. The origin comes from a profile
with airports, as in the live run, so the origin check of agent/checks.check_chain really runs (with an
empty `origin.iata` it is skipped, which is why the first offline harness never saw the live failure:
in the live run the after_variants check blocked the train variants as well, and the checkout re-check
repeated the same verdict).
"""
import json

from agent import checks
from agent.checks import blocking, check_chain, check_variant
from agent.profiles import Profile
from agent.state import Brief, apply_patch
from agent.steps import Domain, attention

from .fakes import fake_domain, u
from .test_scopes import live  # noqa: F401  (fixture)

TRAVELLERS2 = [{"name": "Ксения Белова", "birth_date": "2002-01-15"},
               {"name": "Дарья Фомина", "birth_date": "2001-12-01"}]
CONTACT = {"email": "a@b.c"}


# ---------------------------------------------------------------- helpers

def kinds(v):
    return [s.type for s in v.steps]


def pay_actions(r):
    return [a for a in r["actions"] if a["type"] == "pay"]


def last_input(h, card):
    req = next(r for r in reversed(h.backend.requests) if r.card == card)
    return json.loads(req.user)


def select(h, text, **patch):
    h.understand(u(set=patch))
    r = h.turn(text)
    assert r.get("error") is None and r["stage"] == "selection" and r["cards"]["variants"], r["message"]
    return r, h.session().variants


def assert_no_origin_issue(h, variants):
    s = h.session()
    for v in variants:
        found = check_variant(v, s.brief, h.engine.skill.rules)
        assert not [i for i in found if i["code"] == "chain_origin"], (v.id, found)
        assert blocking(found) == [], (v.id, found)


def checkout_and_pay(h, variant_id, travellers=TRAVELLERS2):
    r = h.act("choose_variant", variant_id=variant_id)
    assert r["stage"] == "cart" and r["cart"], r["message"]
    r = h.act("checkout", travellers=travellers, contact=CONTACT)
    assert r["stage"] == "checkout" and r["order"]["status"] == "pending_payment", r["message"]
    assert not [i for i in r["order"]["issues"] if i["severity"] == "blocking"], r["order"]["issues"]
    assert "вылет" not in r["message"].lower() and "Маршрут начинается" not in r["message"], r["message"]
    pay = pay_actions(r)
    assert len(pay) == 1 and pay[0]["payload"] == {"order_id": r["order"]["order_id"]}, r["actions"]
    assert pay[0]["label"] in r["message"], r["message"]            # the text names the button the screen has
    # the same variant passes the re-check at checkout (after_travellers hook) as at ranking
    s = h.session()
    assert blocking(check_variant(s.variant(variant_id), s.brief, h.engine.skill.rules)) == []
    checked = r
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r.get("error") is None and r["stage"] == "ordered", r["message"]
    assert r["order"]["status"] == "paid" and all(i["booking_number"] for i in r["order"]["items"])
    return checked, r


# ---------------------------------------------------------------- s09: Moscow -> Kazan, overnight train + hotel

def test_s09_moscow_kazan_overnight_train_is_payable(live):
    h = live                                   # demo profile: Москва with SVO/DME/VKO (the origin check runs)
    _, vs = select(h, "Казань на поезде, 4–7 декабря, едем вдвоём с подругой",
                   **{"destination.name": "Казань", "dates.start": "2026-12-04", "dates.end": "2026-12-07",
                      "party.adults": 2, "prefs.transport": "train"})
    assert h.session().brief.origin.value.iata, "the profile's airports must be in the brief"
    for v in vs:
        assert kinds(v)[0] == "train" and kinds(v)[-1] == "train" and "stay" in kinds(v)
        assert v.steps[0].from_ == v.steps[-1].to                     # a Moscow station both ways
    assert_no_origin_issue(h, vs)
    assert not [n for v in vs for n in v.risks if n.code == "chain_origin"]
    overnight = [v for v in vs if v.steps[0].start[:10] == "2026-12-03"]
    chosen = (overnight or vs)[-1]
    assert overnight, "an overnight train the evening before check-in"
    checkout_and_pay(h, chosen.id)


# ---------------------------------------------------------------- s03: St Petersburg -> Moscow by Sapsan

def test_s03_spb_moscow_sapsan_is_payable(live):
    h = live
    h.engine.profiles.put(Profile(id="spb", home_city="Санкт-Петербург", home_iata=["LED"], age=30))
    h.new(profile_id="spb")
    _, vs = select(h, "Выходные 20–22 ноября в Москве, вдвоём, поездом туда-обратно",
                   **{"destination.name": "Москва", "dates.start": "2026-11-20", "dates.end": "2026-11-22",
                      "party.adults": 2, "prefs.transport": "train"})
    assert h.session().brief.origin.value.city == "Санкт-Петербург" and h.session().brief.origin.value.iata
    for v in vs:
        assert kinds(v)[0] == "train" and kinds(v)[-1] == "train"
    assert_no_origin_issue(h, vs)
    sapsan = [v for v in vs if any("Сапсан" in (s.carrier or "") for s in v.steps)]
    assert sapsan, [(v.id, [s.carrier for s in v.steps]) for v in vs]
    checked, _ = checkout_and_pay(h, sapsan[0].id)
    assert "добираться самим" not in checked["message"] or checked["message"].count("трансфер") <= 1


# ---------------------------------------------------------------- never a dead end at checkout

def test_info_question_and_smalltalk_at_checkout_keep_order_and_pay(live):
    h = live
    _, vs = select(h, "Казань на поезде, 4–7 декабря, вдвоём",
                   **{"destination.name": "Казань", "dates.start": "2026-12-04", "dates.end": "2026-12-07",
                      "party.adults": 2, "prefs.transport": "train"})
    h.act("choose_variant", variant_id=vs[0].id)
    r = h.act("checkout", travellers=TRAVELLERS2, contact=CONTACT)
    order_id, label = r["order"]["order_id"], pay_actions(r)[0]["label"]

    h.understand(u(intent="info", info="где кнопка оплаты?"))
    r = h.turn("Я не вижу кнопки оплаты, где её нажать?")
    assert r["stage"] == "checkout" and r["order"] and r["order"]["order_id"] == order_id, r
    assert [a["label"] for a in pay_actions(r)] == [label]
    facts = last_input(h, "present.info")["facts"]
    order_fact = next(f for f in facts if f["kind"] == "order")
    assert order_fact["data"]["payment"]["pay_button"] == label and order_fact["data"]["payment"]["can_pay"]

    h.understand(u(intent="smalltalk"))
    r = h.turn("спасибо, вы очень помогли")
    assert r["order"] and r["order"]["status"] == "pending_payment" and pay_actions(r), r

    r = h.act("pay", order_id=order_id)
    assert r["stage"] == "ordered" and r["order"]["status"] == "paid"


def test_blocker_at_checkout_offers_a_way_forward(make):
    """A blocking re-check at checkout hides the pay button but never leaves the user stuck."""
    h = make(domain=Domain(**fake_domain(check_variant=lambda v, b, r: [
        {"code": "infant_bed", "severity": "block", "message": "В номере нет места для младенца", "data": {}}]
        if (b.values()["party"].get("children_ages") or []) else [])))
    h.understand(u(set={"destination.name": "Сочи", "party.adults": 2}))
    assert h.turn("Сочи, двое")["cards"]["variants"]
    h.act("choose_variant", variant_id="fastest")
    travellers = TRAVELLERS2 + [{"name": "Малыш", "birth_date": "2026-03-01"}]
    r = h.act("checkout", travellers=travellers, contact=CONTACT)
    assert not pay_actions(r)
    others = [a for a in r["actions"] if a["type"] == "choose_variant"]
    assert others and "fastest" not in {a["payload"]["variant_id"] for a in others}
    assert "оплатить его пока нельзя" in r["message"] and "места для младенца" in r["message"], r["message"]

    h.understand(u(intent="info", info="почему нельзя оплатить?"))
    r = h.turn("почему нельзя оплатить?")
    assert r["order"] and not pay_actions(r) and [a for a in r["actions"] if a["type"] == "choose_variant"]

    r = h.act("pay", order_id=h.session().order_id)                  # a stale button: refused, plainly
    assert r["error"] and "нельзя" in r["message"] and r["order"] and not pay_actions(r)
    r = h.act("choose_variant", variant_id=others[0]["payload"]["variant_id"])
    assert r["stage"] == "cart" and r["cart"], r["message"]


# ---------------------------------------------------------------- units: stations of the origin city, texts

def _brief(city, iata=()):
    b, _ = apply_patch(Brief(), {"set": {"origin.city": city}})
    b.origin.value.iata = list(iata)
    return b


def _train_steps(out_from, back_to):
    return [
        {"id": "s1", "type": "train", "from": out_from, "to": "Казань-Пассажирская",
         "start": "2026-12-03T20:15:00+03:00", "end": "2026-12-04T09:04:00+03:00"},
        {"id": "s2", "type": "stay", "from": "Отель", "to": "Отель",
         "start": "2026-12-04T15:00:00+03:00", "end": "2026-12-07T12:00:00+03:00"},
        {"id": "s3", "type": "train", "from": "Казань-Пассажирская", "to": back_to,
         "start": "2026-12-07T22:20:00+03:00", "end": "2026-12-08T10:00:00+03:00"},
    ]


def test_origin_city_stations_count_as_origin(monkeypatch):
    places = {"москва": {"stations": ["Казанский вокзал", "Ленинградский вокзал"], "airports": ["SVO"]},
              "санкт-петербург": {"stations": ["Московский вокзал"], "airports": ["LED"]}}
    monkeypatch.setattr(checks, "PLACE_SOURCE", lambda city: places.get(checks.norm(city), {}))
    steps = _train_steps("Казанский вокзал", "Казанский вокзал")
    for b in (_brief("Москва", ["SVO", "DME"]), _brief("Москва")):     # with airports, and found by its station
        assert [i for i in check_chain(steps, b, "v") if i["code"] == "chain_origin"] == []
    found = [i for i in check_chain(steps, _brief("Санкт-Петербург", ["LED"]), "v") if i["code"] == "chain_origin"]
    assert [i["message"] for i in found] == ["Отправление туда — Казанский вокзал, а это не город Санкт-Петербург",
                                             "Прибытие обратно — Казанский вокзал, а это не город Санкт-Петербург"]


def test_real_catalog_knows_the_stations():
    assert "Казанский вокзал" in checks.city_places("Москва")["stations"]
    assert "Московский вокзал" in checks.city_places("Санкт-Петербург")["stations"]
    assert checks.city_places("Нет-Такого-Города") in ({}, {"stations": [], "airports": []})


def test_attention_is_short_and_blockers_first():
    issues = [{"code": "no_transfer", "severity": "warn", "message": "Трансфер от «A» до «B» не найден — добираться самим"},
              {"code": "no_transfer", "severity": "warn", "message": "Трансфер от «B» до «A» не найден — добираться самим"},
              {"code": "early_arrival", "severity": "info", "message": "Прибытие в 09:04"},
              {"code": "late_checkin", "severity": "warn", "message": "Заселение поздно, в 23:10"},
              {"code": "child_bed", "severity": "blocking", "message": "Номер не вмещает состав"}]
    assert attention(issues) == ("номер не вмещает состав; трансферов до отеля и обратно нет — добираться самим.")
    assert attention(issues[:1]) == "трансфер от «A» до «B» не найден — добираться самим."
    assert attention([issues[2]]) == ""
