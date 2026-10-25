"""Hotel location and the best time to go, offline on the real mock + real data (level-3 run
evals/runs/20261025-124606-L3-main, scenario s04_photo_landmark):

- t1: a photo of Hagia Sophia + «Хочу сюда! Когда лучше поехать?» -> the question was dropped and the
  recommended hotel was Galata Design Guest House, 2 554 m from the centre (Sultanahmet has 24 hotels < 700 m);
- t5: «пересоберите … поближе к историческому центру» -> prefs.district walkable, nothing changed;
- t7: an offer to search «в районе Султанахмет» the engine cannot do.
"""
import json

import pytest

from agent.checks import blocking, check_variant
from agent.engine import Engine
from agent.opendata import OpenData
from agent.ranking import center_applies, center_score, ranking_rules
from agent.state import Brief, apply_patch
from agent.steps import location_cue, nearest_best_month
from evals.card_inputs import validate_card_input

from .fakes import u
from .test_scopes import ROOT, live  # noqa: F401  (the real-data harness fixture)

DATA_URL = "data:image/jpeg;base64,AAAA"
CENTER_KM = 1000


def istanbul_photo(conf=0.95):
    return {"want": "here", "place": {"name": "Стамбул", "country": "Турция", "confidence": conf},
            "traits": {"landscape": ["old_town"], "water": "sea", "climate": "temperate", "setting": "city",
                       "vibe": ["culture"]}}


def last_input(h, card):
    req = [r for r in h.backend.requests if r.card == card][-1]
    return json.loads(req.user)


def no_blocks(h):
    s = h.session()
    for v in s.variants:
        assert blocking(check_variant(v, s.brief, h.engine.skill.rules)) == [], v.id


def recommended(h):
    s = h.session()
    from agent.engine import card_hint
    return s.variant(card_hint(s.brief, s.variants))


def s04_first_turn(h):
    h.understand(u(intent="new_trip", image=istanbul_photo(), info="Когда лучше поехать?"))
    r = h.turn("Хочу сюда! Когда лучше поехать?", image=DATA_URL)
    assert r.get("error") is None, r["message"]
    assert r["stage"] == "selection" and r["cards"]["variants"], r["message"]
    return r


# ---------------------------------------------------------------- 1 + 3 + 4: s04 t1 replay

def test_s04_photo_question_best_time_and_central_hotel(live):
    h = live
    r = s04_first_turn(h)
    s = h.session()
    assert s.brief.values()["destination"]["id"] == "istanbul"
    # the dates are assumed in the nearest best month (no user dates), an assumption the user can change
    best = [m["month"] for m in h.engine.opendata.best_months("istanbul")]
    start = s.brief.values()["dates"]["start"]
    assert int(start[5:7]) in best, (start, best)
    assert s.brief.source_of("dates.start") != "user" and s.brief.source_of("dates.month") == "inferred"
    from agent.payloads import build_chips
    assert any(c.field == "dates.start" for c in build_chips(h.engine.skill, s.brief)), "dates are a chip"
    # the variants card carries the question and code-made answer facts
    inp = last_input(h, "present.variants")
    assert validate_card_input("present.variants", inp) == []
    q = inp["question"]
    assert q["text"] == "Когда лучше поехать?"
    bt = next(f for f in q["facts"] if f["kind"] == "best_time")["data"]
    assert bt["best_months"] == best and bt["best_months_ru"] and "днём около" in bt["summary_ru"]
    assert all(m["air_max_c"] is not None for m in bt["months"])
    assert "море" in bt["summary_ru"]                       # Istanbul is by the sea: sea temperature given
    cd = next(f for f in q["facts"] if f["kind"] == "center_distance")["items"]
    assert {i["id"] for i in cd} == {v.id for v in s.variants}
    # the recommended hotel is central (Sultanahmet), not Galata 2.5 km out
    rec = recommended(h)
    assert rec.facts["distance_center_m"] <= CENTER_KM, (rec.id, rec.facts["hotel_name"], rec.facts["distance_center_m"])
    assert rec.fit.breakdown.get("center") is not None
    no_blocks(h)


def test_search_with_user_dates_keeps_dates_and_answers(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05"}, info="Когда там лучше всего?"))
    h.turn("Стамбул 1–5 декабря. А вообще когда там лучше всего, в какой сезон?")
    s = h.session()
    assert s.brief.values()["dates"]["start"] == "2026-12-01"
    q = last_input(h, "present.variants")["question"]
    bt = next(f for f in q["facts"] if f["kind"] == "best_time")["data"]
    assert bt["trip_month"] == 12 and bt["trip_month_is_best"] is False and bt["dates_assumed"] is False


def test_no_question_no_question_key(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05"}))
    h.turn("Стамбул 1–5 декабря")
    inp = last_input(h, "present.variants")
    assert "question" not in inp and validate_card_input("present.variants", inp) == []


# ---------------------------------------------------------------- 2: «поближе к центру» changes the hotels

def test_s04_refine_closer_to_centre_changes_hotels(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "party.segment": "couple"}))
    h.turn("Стамбул, 1–5 декабря, вдвоём")
    before = {v.id: v.facts["distance_center_m"] for v in h.session().variants}
    sig_before = sorted(v.hotel.hotel_id for v in h.session().variants)
    # understand repeats its s04 slip (district walkable): the code cue turns it into the centre pref
    h.understand(u(intent="refine", set={"prefs.district": "walkable"}))
    r = h.turn("Да, пересоберите, пожалуйста, чтобы отель был поближе к историческому центру")
    assert r.get("error") is None, r["message"]
    s = h.session()
    prefs = s.brief.values()["prefs"]
    assert prefs["center_max_m"] == 1500 and s.brief.source_of("prefs.center_max_m") == "user"
    assert prefs["district"] != "walkable" or s.brief.source_of("prefs.district") != "user"
    after = {v.id: v.facts["distance_center_m"] for v in s.variants}
    assert all(d <= 1500 for d in after.values()), after
    assert sorted(v.hotel.hotel_id for v in s.variants) != sig_before or max(before.values()) <= 1500
    inp = last_input(h, "present.update")
    assert any(c["field"] == "prefs.center_max_m" and c["after"] == 1500 for c in inp["changes"]), inp["changes"]
    assert validate_card_input("present.update", inp) == []
    no_blocks(h)


def test_s04_t6_question_near_sight_rebuilds(live):
    """«Есть что-то ближе к Айя-Софии?» read as info + prefs.center_max_m: a rebuild, and the update card
    carries the question with the distances (never a «поищу в районе Султанахмет» dead end)."""
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "party.segment": "couple"}))
    h.turn("Стамбул, 1–5 декабря, вдвоём")
    h.understand(u(intent="info", set={"prefs.center_max_m": 1500}, info="Есть отели ближе к Айя-Софии?"))
    r = h.turn("Отель тот же, 2,5 км от центра. Есть что-то ближе к Айя-Софии?")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    assert all(v.facts["distance_center_m"] <= 1500 for v in h.session().variants)
    inp = last_input(h, "present.update")
    assert inp["question"]["text"] == "Есть отели ближе к Айя-Софии?"
    assert any(f["kind"] == "center_distance" for f in inp["question"]["facts"])


def test_understand_centre_patch_is_a_hard_filter_with_mock_filter(live, monkeypatch):
    h = live
    seen = []
    real = h.engine.ostrovok.call

    def spy(tool, payload):
        if tool == "search_hotels":
            seen.append(payload.get("filters") or {})
        return real(tool, payload)
    monkeypatch.setattr(h.engine.ostrovok, "call", spy)
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "prefs.center_max_m": 800}))
    h.turn("Стамбул 1–5 декабря, в самом центре")
    assert any(f.get("max_distance_center_m") == 800 for f in seen), seen
    assert all(v.facts["distance_center_m"] <= 800 for v in h.session().variants)


# ---------------------------------------------------------------- relaxation

def test_centre_pref_relaxed_when_nothing_so_close(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "prefs.center_max_m": 50}))
    r = h.turn("Стамбул 1–5 декабря, отель не дальше 50 метров от центра")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    s = h.session()
    nearest = min(v.facts["distance_center_m"] for v in s.variants)
    for v in s.variants:
        note = next(n for n in v.risks if n.code == "relaxed_pref")
        assert note.data["field"] == "prefs.center_max_m"
        assert note.text == "Отелей так близко к центру на эти даты нет — показываем ближайшие"
        assert v.facts["distance_center_m"] <= nearest + 1000   # the nearest ones, not the whole city
    no_blocks(h)


# ---------------------------------------------------------------- a beach resort is not pulled to the centre

@pytest.mark.parametrize("dest", ["Анталья", "Сочи"])
def test_beach_resort_not_pulled_to_centre(live, dest):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": dest, "dates.start": "2027-06-10",
                                           "dates.end": "2027-06-17", "traits.add": ["sea", "beach"]}))
    r = h.turn(f"{dest} на море, 10–17 июня")
    assert r["cards"]["variants"], r["message"]
    for v in h.session().variants:
        assert "center_fit" not in v.facts and "center" not in v.fit.breakdown, v.id


def test_beach_resort_without_traits_is_not_a_city_stay(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Анталья", "dates.start": "2027-06-10",
                                           "dates.end": "2027-06-17"}))
    h.turn("Анталья, 10–17 июня")
    for v in h.session().variants:
        assert "center_fit" not in v.facts, v.id


# ---------------------------------------------------------------- info: best time and the distance facts

def test_info_best_time_question_has_best_month_facts(live):
    h = live
    s04_first_turn(h)
    h.understand(u(intent="info", info="Когда лучше ехать в Стамбул?"))
    h.turn("А в какой сезон туда лучше ехать?")
    inp = last_input(h, "present.info")
    assert any(f["kind"] == "best_time" for f in inp["facts"])


def test_info_far_from_centre_has_distances(live):
    h = live
    s04_first_turn(h)
    h.understand(u(intent="info", info="Отель далеко от исторического центра?"))
    h.turn("А отель далеко от исторического центра, от Айя-Софии?")
    inp = last_input(h, "present.info")
    cd = next(f for f in inp["facts"] if f["kind"] == "center_distance")
    assert cd["items"] and all("distance_center_m" in i for i in cd["items"])


# ---------------------------------------------------------------- domain units

def test_location_cues():
    from agent.skill_loader import load_skill
    rules = load_skill().rules
    assert location_cue(rules, "поближе к историческому центру") == 1500
    assert location_cue(rules, "хотим жить в центре") == 1500
    assert location_cue(rules, "в старом городе") == 1500
    assert location_cue(rules, "в самом центре, пожалуйста") == 800
    assert location_cue(rules, "не в центре, там шумно") is None
    assert location_cue(rules, "рядом с Диснейлендом") is None
    assert location_cue(rules, "А отель далеко от центра?") is None


def test_nearest_best_month():
    from datetime import date
    assert nearest_best_month(date(2026, 10, 25), [5, 6, 9, 10]) == 5      # a week of October left: next May
    assert nearest_best_month(date(2026, 10, 5), [5, 6, 9, 10]) == 10
    assert nearest_best_month(date(2026, 12, 30), [1, 2]) == 1
    assert nearest_best_month(date(2026, 3, 1), []) is None


def test_best_months_city_and_sea_resort():
    od = OpenData(path=str(ROOT / "data" / "opendata.sqlite"), overlay=None)
    ist = [m["month"] for m in od.best_months("istanbul")]
    assert ist == [5, 6, 9, 10]                              # spring and autumn, not the 29° August
    ant = [m["month"] for m in od.best_months("antalya")]   # a sea resort: the sea decides
    assert set(ant) <= {6, 7, 8, 9, 10} and ant


def test_center_applies_rules():
    rr = ranking_rules({})
    b = Brief()
    assert center_applies(b, {"kind": "city"}, rr) and not center_applies(b, {"kind": "resort"}, rr)
    sea, _ = apply_patch(b, {"set": {"traits.add": ["sea"]}})
    assert not center_applies(sea, {"kind": "city"}, rr)
    user, _ = apply_patch(b, {"set": {"prefs.center_max_m": 1500}})
    assert center_applies(user, {"kind": "resort"}, rr)
    assert center_score(b, 800, rr) == 1.0 and center_score(b, 5000, rr) == 0.0
    assert 0.5 < center_score(b, 2554, rr) < 0.7
    assert center_score(user, 1400, rr) == 1.0 and center_score(user, 3000, rr) == 0.0


def test_mock_filter_max_distance_center_before_limit():
    from ostrovok_mcp import service
    base = {"destination_id": "rome", "check_in": "2026-12-01", "check_out": "2026-12-05", "adults": 2,
            "children_ages": [], "limit": 5, "today": "2026-10-25"}
    near = service.call("search_hotels", {**base, "filters": {"max_distance_center_m": 1500}})
    assert near["offers"] and all(o["distance_center_m"] <= 1500 for o in near["offers"])
    top = service.call("search_hotels", {**base, "filters": {}})
    assert {o["hotel_id"] for o in near["offers"]} != {o["hotel_id"] for o in top["offers"]}


# ---------------------------------------------------------------- level-2 cases are satisfiable (golden)

def _run_case(case_id, output):
    from agent.skill_loader import load_skill
    from evals.assertions import evaluate_all
    from evals.common import CASES_DIR
    case = json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    ok, res = evaluate_all(case["expect"], output, case["input"], load_skill().card(case["card"]).schema)
    return ok, [r for r in res if not r["ok"]], case["input"]


def _und(intent="refine", sets=None, info=None, image=None):
    return {"intent": intent, "patch": {"set": sets or {}, "unset": []}, "refs": [], "image": image,
            "info_question": info, "post_purchase": None}


def _rub(n):
    return f"{n:,}".replace(",", " ") + " ₽"


def test_location_understand_cases_golden():
    assert _run_case("e7_refine_closer_historic_center", _und(sets={"prefs.center_max_m": 1500}))[0]
    ok, failed, _ = _run_case("e7_refine_closer_historic_center", _und(sets={"prefs.district": "walkable"}))
    assert not ok                                               # the s04 slip fails the case
    assert _run_case("x7_near_disneyland_no_center",
                     _und("new_trip", {"destination.name": "Париж", "prefs.near": "Диснейленд"}))[0]
    assert not _run_case("x7_near_disneyland_no_center", _und("new_trip", {"destination.name": "Париж"}))[0]
    assert not _run_case("x7_near_disneyland_no_center",
                         _und("new_trip", {"destination.name": "Париж", "prefs.near": "Диснейленд",
                                           "prefs.center_max_m": 1500}))[0]


def test_location_present_cases_golden():
    _, _, inp = _run_case("b8_variants_answer_best_time", {"message": "", "items": [], "recommended_id": None,
                                                           "upsell": []})
    bt = next(f for f in inp["question"]["facts"] if f["kind"] == "best_time")["data"]
    items = {i["id"]: i for i in inp["items"]}
    rec = items[inp["recommended_hint"]]
    others = [i for i in inp["items"] if i["id"] != rec["id"]]
    d = inp["brief"]["dates"]
    message = (f"{bt['summary_ru']}, поэтому даты поставил на {int(d['start'][8:])}–{int(d['end'][8:])} мая — их "
               f"можно поменять. Советую «{rec['title']}»: {_rub(rec['totals']['rub'])} за всё, отель в "
               f"{rec['facts']['distance_center_m']} м от центра. "
               + " ".join(f"«{o['title']}» — {_rub(o['totals']['rub'])}." for o in others))
    out = {"message": message, "recommended_id": rec["id"], "upsell": [],
           "items": [{"id": i["id"], "why": i["title"], "sacrifices": [], "risks": [], "not_for": []}
                     for i in inp["items"]]}
    out["items"] = [dict(it, sacrifices=[n["text"] for n in items[it["id"]]["sacrifices"]],
                         risks=[n["text"] for n in items[it["id"]]["risks"]],
                         not_for=[n["text"] for n in items[it["id"]]["not_for"]]) for it in out["items"]]
    ok, failed, _ = _run_case("b8_variants_answer_best_time", out)
    assert ok, failed
    dropped = dict(out, message=message.split(". ", 1)[1])     # the question dropped: the case fails
    assert not _run_case("b8_variants_answer_best_time", dropped)[0]
    offer = dict(out, message=message + " Скажите — поищу отели в районе Султанахмет.")
    assert not _run_case("b8_variants_answer_best_time", offer)[0]
