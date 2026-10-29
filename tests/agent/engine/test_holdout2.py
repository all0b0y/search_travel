"""Held-out level-3 failures (evals/runs/20261025-135109-L3-holdout), offline on the real mock + real data:

- h01: «хостел в нашем возрасте как-то не очень» kept a hostel as «Дешевле» (the room cue «хостел в » even read it
  as a hostel wish), and «в другие даты декабря или в другом городе» ignored the other city: constraint no_hostel
  removes every hostel (private rooms too) and every dorm bed, and the places that fit the budget are offered;
- h10: «Студент, 25 тысяч на всё… из Екатеринбурга» offered only flights above the budget while Kazan / Samara fit
  by train; «что входит в эти цены?» got «нет данных»: a budget-led inspiration quotes trains too, choosing a
  train place builds train variants, and every destination item carries facts.price_includes.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from agent.checks import blocking, check_variant
from agent.guards import check_card_output, hostel_claim_issues
from agent.ranking import _party_ok, refuses_hostel, wants_dorm
from agent.state import Brief, apply_patch
from agent.steps import NON_HOSTEL_KINDS, _hotel_filters, hostel_refused

from .conftest import skill
from .fakes import u
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)
from .turn_time import calibrated

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "data" / "ostrovok_catalog.sqlite"
TURN_MS_MAX = 1500   # in-process turn on the live harness, turn_time.calibrated (measured ~100–600 ms; a slow CI machine gets slack)

H01_T1 = "Устали от слякоти. Солнца бы в декабре, мы с мужем, бюджет скромный"
H01_T3 = ("Быстрее слишком дорого, у нас максимум 180 тысяч. А хостел в нашем возрасте как-то не очень. Нам вообще "
          "хотелось бы 8 ночей. Можно что-то среднее, с нормальным отелем и без долгой пересадки? Объясните попроще.")
H01_T4 = ("Хостел нам всё равно не подходит, а бюджет поднимать я не хочу. Может, есть нормальный отель в пределах "
          "180 тысяч в другие даты декабря или в другом городе? Объясните попроще.")
H10_T1 = "Студент, 25 тысяч на всё, куда можно смотаться на 3 дня из Екатеринбурга?"
H10_T2 = "а что входит в эти цены, перелёт туда-обратно и отель? можно как-то дешевле уложиться в 25"


def card_input(h, card):
    return json.loads([r for r in h.backend.requests if r.card == card][-1].user)


def hotel_kind(hotel_id):
    with sqlite3.connect(f"file:{CATALOG}?mode=ro", uri=True) as con:
        row = con.execute("select kind from hotels where hotel_id = ?", (hotel_id,)).fetchone()
    return row[0] if row else None


def no_hostels(h):
    for v in h.session().variants:
        if v.hotel is not None:
            assert hotel_kind(v.hotel.hotel_id) != "hostel", (v.id, v.hotel.name)
        assert not v.facts.get("dorm_bed"), (v.id, v.facts.get("room_name"))


def no_blocks(h):
    s = h.session()
    for v in s.variants:
        assert blocking(check_variant(v, s.brief, h.engine.skill.rules)) == [], (v.id, check_variant(v, s.brief, h.engine.skill.rules))


# ---------------------------------------------------------------- A. the refusal cue (code safety net)

ROOMS = skill().rules["rooms"]


@pytest.mark.parametrize("text", [
    "А хостел в нашем возрасте как-то не очень.",
    "Хостел нам всё равно не подходит, а бюджет поднимать я не хочу",
    "нормальный отель, не хостел",
    "без хостелов, пожалуйста",
    "не хочу жить в хостеле",
    "хостелы не предлагайте",
])
def test_hostel_refusal_cues(text):
    assert hostel_refused(ROOMS, text)


@pytest.mark.parametrize("text", [
    "Хостел в Казани на 3 ночи с 20 ноября, я один",
    "хостел подойдёт, главное дёшево",
    "отдельный номер в хостеле",
    "Устали от слякоти, бюджет скромный, не хочу далеко лететь",
    "хостел или отель — нет разницы",
    "хостел — не проблема, лишь бы чисто",
])
def test_hostel_cues_that_are_no_refusal(text):
    assert not hostel_refused(ROOMS, text)


def test_no_hostel_filters_search_and_ranking():
    brief, _ = apply_patch(Brief(), {"set": {"constraints.add": ["no_hostel", "hostel"]}})
    assert refuses_hostel(brief) and not wants_dorm(brief)        # no_hostel wins over a hostel wish
    assert _hotel_filters(brief)["kinds"] == list(NON_HOSTEL_KINDS) and "hostel" not in NON_HOSTEL_KINDS
    private_hostel = {"kind": "hostel", "room": {"name": "Двухместный номер", "bed": "double"}}
    dorm_in_guest_house = {"kind": "guest_house", "room": {"name": "Место в общем номере", "bed": "bunk"}}
    hotel = {"kind": "hotel", "room": {"name": "Стандарт", "bed": "double"}}
    assert not _party_ok(private_hostel, brief) and not _party_ok(dorm_in_guest_house, brief)
    assert _party_ok(hotel, brief)
    plain = Brief()
    assert _party_ok(private_hostel, plain) and "kinds" not in _hotel_filters(plain)


def test_hostel_claim_guard():
    base = {"brief": {"constraints": ["no_hostel"]}, "changes": []}
    assert hostel_claim_issues("present.update", base, "Убрал хостелы и перенёс на 7–15 декабря.")
    added = {"brief": {"constraints": ["no_hostel"]},
             "changes": [{"field": "constraints", "before": [], "after": ["no_hostel"]}]}
    assert hostel_claim_issues("present.update", added, "Убрал хостелы и перенёс на 7–15 декабря.") == []
    assert hostel_claim_issues("present.update", base, "Подобрал отели без хостелов.") == []
    assert hostel_claim_issues("present.variants", {"brief": {"constraints": []}}, "Варианты без хостелов.")
    issues = check_card_output("present.update", {**base, "items": []},
                               {"message": "Убрал хостелы.", "items": [], "recommended_id": None, "upsell": []},
                               skill().rules)
    assert any(i["code"] == "hostel_claim" and i["severity"] == "block" for i in issues)


# ---------------------------------------------------------------- A + C. h01 replay

def h01_to_dubai(h):
    h.understand(u(set={"origin.city": "Новосибирск", "party.segment": "couple", "party.adults": 2,
                        "dates.month": 12, "dates.nights": 7, "traits.add": ["warm", "sun"]}))
    r = h.turn(H01_T1)
    shown = {c["id"]: c["price_from_rub"] for c in r["cards"]["destinations"]}
    assert {"dubai", "bangkok"} <= set(shown), shown
    r = h.act("choose_destination", destination_id="dubai")
    assert r["cards"]["variants"], r["message"]
    assert any(hotel_kind(v.hotel.hotel_id) == "hostel" for v in h.session().variants)   # the live run's «Дешевле»
    return shown


def test_h01_hostel_refused_budget_fit_without_hostels(live):
    h = live
    h01_to_dubai(h)
    h.understand(u(intent="refine", set={"budget_rub.max": 180000, "dates.nights": 8}))   # understand missed it
    r, ms = calibrated(h.turn, H01_T3)
    assert r.get("error") is None, r["message"]
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    s = h.session()
    cons = s.brief.values()["constraints"]
    assert "no_hostel" in cons and "hostel" not in cons, cons     # «хостел в » is no hostel wish here
    no_hostels(h)
    assert not any(v.id == "custom-2" for v in s.variants)        # «Свой номер подешевле» needs a dorm «Дешевле»
    best = min(s.variants, key=lambda v: v.totals.rub)
    assert best.totals.rub <= 180000 and best.facts.get("budget_fit") in ("dates", "hotel", "dates_and_hotel")
    inp = card_input(h, "present.update")
    row = next(c for c in inp["changes"] if c["field"] == "constraints")
    assert "no_hostel" in row["after"] and "no_hostel" not in (row["before"] or []), row
    no_blocks(h)
    # t4: still no hostel, other dates, and the other city is answered with places that fit the budget
    h.understand(u(intent="refine"))
    r, ms = calibrated(h.turn, H01_T4)
    assert r.get("error") is None, r["message"]
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    no_hostels(h)
    assert h.session().brief.values()["destination"]["id"] == "dubai"          # the date search ran here
    inp = card_input(h, "present.update")
    assert not any(c["field"] == "constraints" for c in inp["changes"])         # no new «убрал хостелы» row
    places = inp["other_places"]
    assert places and all(p["within_budget"] and p["price_from_rub"] <= 180000 for p in places), places
    assert all(p["id"] != "dubai" and p["nights"] == 8 for p in places), places
    offered = [a["payload"]["destination_id"] for a in r["actions"] if a["type"] == "choose_destination"]
    assert offered == [p["id"] for p in places], (offered, places)
    from evals.card_inputs import validate_card_input
    assert validate_card_input("present.update", inp) == []


def test_h01_dubai_out_of_budget_offers_other_places(live):
    h = live
    h01_to_dubai(h)
    h.understand(u(intent="refine", set={"budget_rub.max": 150000, "dates.nights": 8}))
    r = h.turn("У нас максимум 150 тысяч, и хостел нам не подходит, 8 ночей")
    assert r.get("error") is None, r["message"]
    no_hostels(h)
    s = h.session()
    assert min(v.totals.rub for v in s.variants) > 150000                      # not reachable in Dubai
    assert s.variants[0].facts.get("budget_fit") == "not_found"
    note = next(n.text for n in s.variants[0].risks if n.code == "budget_fit")
    assert "без хостелов" in note, note
    places = card_input(h, "present.update")["other_places"]
    assert places and all(p["price_from_rub"] <= 150000 for p in places), places
    assert "bangkok" in [p["id"] for p in places]
    assert [a for a in r["actions"] if a["type"] == "choose_destination"]
    # choosing an offered place builds it (shown + remembered by code)
    r = h.act("choose_destination", destination_id=places[0]["id"])
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    no_hostels(h)


def test_other_places_without_shown_destinations(live):
    """A destination named directly (nothing shown before): a fresh destinations search with the budget."""
    h = live
    h.understand(u(set={"origin.city": "Новосибирск", "party.segment": "couple", "party.adults": 2,
                        "destination.name": "Дубай", "dates.month": 12, "dates.nights": 8,
                        "budget_rub.max": 150000}))
    r, ms = calibrated(h.turn, "Дубай в декабре на 8 ночей вдвоём, до 150 тысяч, без хостелов")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    assert ms < TURN_MS_MAX * 2, (ms, r["debug"]["timings_ms"])   # one fresh find_destinations on top
    places = card_input(h, "present.variants")["other_places"]
    assert all(p["id"] != "dubai" and p["price_from_rub"] <= 150000 for p in places), places
    assert places, r["debug"]["notes"]


def test_allow_hostel_drops_the_refusal(live):
    h = live
    h01_to_dubai(h)
    h.understand(u(intent="refine"))
    h.turn("хостел нам не подходит")
    assert "no_hostel" in h.session().brief.values()["constraints"]
    h.understand(u(intent="refine"))
    h.turn("ладно, можно и хостел, лишь бы дешевле")
    assert "no_hostel" not in (h.session().brief.values()["constraints"] or [])


# ---------------------------------------------------------------- B + D. h10 replay

def h10_first(h):
    h.understand(u(set={"origin.city": "Екатеринбург", "party.segment": "solo", "party.adults": 1,
                        "budget_rub.max": 25000, "dates.nights": 3}))
    r, ms = calibrated(h.turn, H10_T1)
    assert r.get("error") is None, r["message"]
    return r, ms


def test_h10_budget_inspiration_offers_trains_that_fit(live):
    h = live
    r, ms = h10_first(h)
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    items = card_input(h, "present.destinations")["items"]
    by_id = {i["id"]: i for i in items}
    assert {"kazan", "samara"} <= set(by_id), list(by_id)
    fitting = [i for i in items if i["facts"].get("within_budget")]
    assert items[:len(fitting)] == fitting and len(fitting) >= 2       # places that fit come first
    for pid in ("kazan", "samara"):
        f = by_id[pid]["facts"]
        assert f["transport"] == "train" and f["transport_ru"] == "поездом", f
        assert by_id[pid]["price_from_rub"] <= 25000 and f["within_budget"] is True
        assert f["train_price_rub"] + f["stay_price_rub"] == by_id[pid]["price_from_rub"]
    planes = [i for i in items if i["facts"]["transport"] == "plane"]
    assert planes and all(i["facts"]["transport_ru"] == "самолётом" for i in planes)
    for i in items:   # D: what the «от» price includes, made by code
        inc = i["facts"]["price_includes"]
        assert "туда и обратно на одного" in inc and "3 ночи" in inc and "трансферы в цену не входят" in inc, inc
        assert ("поезд" in inc) == (i["facts"]["transport"] == "train"), inc
    from evals.card_inputs import validate_card_input
    assert validate_card_input("present.destinations", card_input(h, "present.destinations")) == []


def test_h10_choose_train_place_builds_train_variants(live):
    h = live
    h10_first(h)
    quoted = {i["id"]: i["price_from_rub"] for i in card_input(h, "present.destinations")["items"]}
    r, ms = calibrated(h.act, "choose_destination", destination_id="kazan")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    assert ms < TURN_MS_MAX, (ms, r["debug"]["timings_ms"])
    s = h.session()
    assert s.brief.values()["prefs"]["transport"] == "train"
    for v in s.variants:
        legs = [st.type for st in v.steps if st.type in ("flight", "train")]
        assert legs == ["train", "train"], (v.id, legs)
    assert min(v.totals.rub for v in s.variants) <= quoted["kazan"] <= 25000   # the quoted price holds
    assert any(v.totals.rub <= 25000 and not v.facts.get("dorm_bed") for v in s.variants)
    no_blocks(h)
    # a place quoted by plane after it: back to any transport (flights searched again)
    r = h.act("choose_destination", destination_id="bukhara")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    assert h.session().brief.values()["prefs"]["transport"] == "any"
    assert all(any(st.type == "flight" for st in v.steps) for v in h.session().variants)
    # Bukhara is above 25 000 ₽ even after the budget fit: the places that fit are offered (by train)
    places = card_input(h, "present.variants")["other_places"]
    assert {"kazan", "samara"} & {p["id"] for p in places}, places
    assert all(p["within_budget"] for p in places)
    assert any(p["transport_ru"] == "поездом" for p in places)


def test_h10_what_the_price_includes_at_inspiration(live):
    h = live
    h10_first(h)
    h.understand(u(intent="info", info="Что входит в цены направлений?"))
    r = h.turn(H10_T2)
    assert r.get("error") is None, r["message"]
    facts = card_input(h, "present.info")["facts"]
    dest = next(f for f in facts if f.get("kind") == "destinations")
    assert dest["budget_rub"] == 25000
    kazan = next(i for i in dest["items"] if i["id"] == "kazan")
    assert kazan["transport_ru"] == "поездом" and kazan["within_budget"] is True
    assert kazan["price_includes"].startswith("поезд туда и обратно на одного")
    from agent.respond import _info_text

    class C:   # the degraded code text answers from the same facts
        info_facts, info_question, text = facts, "Что входит в цены?", H10_T2
        session = h.session()
    text = _info_text(C)
    assert text.startswith("В цену «от» входит") and "Казань поездом" in text, text


def test_no_trains_when_transport_is_fixed_or_no_budget(live):
    h = live
    h.understand(u(set={"origin.city": "Екатеринбург", "party.segment": "solo", "party.adults": 1,
                        "dates.nights": 3}))
    h.turn("куда можно смотаться на 3 дня из Екатеринбурга?")
    items = card_input(h, "present.destinations")["items"]
    assert all(i["facts"]["transport"] == "plane" for i in items)          # no budget: the old flight path
    h.new()
    h.understand(u(set={"origin.city": "Екатеринбург", "party.adults": 1, "dates.nights": 3,
                        "budget_rub.max": 25000, "prefs.transport": "plane"}))
    h.turn("самолётом, 25 тысяч, на 3 дня из Екатеринбурга")
    items = card_input(h, "present.destinations")["items"]
    assert all(i["facts"]["transport"] == "plane" for i in items)
