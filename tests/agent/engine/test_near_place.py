"""Hotels next to a place the user names or shows (prefs.near, opendata `pois`), offline on the real mock + real
data with a scripted FakeBackend (level-3 s04_photo_landmark, level-2 e8_refine_near_hagia_sophia):

- a photo of Hagia Sophia + «Хочу сюда!» (image.landmark «Айя-София») -> the first answer already has hotels next
  to it and the facts say how far (distance_near_m);
- «Есть что-то рядом с Айя-Софией?» (understand: info, no patch) -> a refine with prefs.near, other hotels;
- «в районе Султанахмет», Moscow «рядом с Красной площадью» resolve; an unknown place or a destination without
  pois falls back to the centre with a plain note;
- «далеко ли от Эрмитажа?» -> per-variant metres computed by code (near_distance).
"""
import json
import math
import time

import pytest

from agent.checks import blocking, check_variant
from evals.card_inputs import validate_card_input

from .fakes import u
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)

DATA_URL = "data:image/jpeg;base64,AAAA"
HAGIA = (41.008583, 28.980175)   # roughly; the test reads the real point from the pois table


def istanbul_photo(landmark="Айя-София", conf=0.95):
    return {"want": "here", "place": {"name": "Стамбул", "country": "Турция", "confidence": conf},
            "landmark": landmark,
            "traits": {"landscape": ["old_town"], "water": "sea", "climate": "temperate", "setting": "city",
                       "vibe": ["culture"]}}


def last_input(h, card):
    req = [r for r in h.backend.requests if r.card == card][-1]
    return json.loads(req.user)


def recommended(h):
    from agent.engine import card_hint
    s = h.session()
    return s.variant(card_hint(s.brief, s.variants))


def no_blocks(h):
    s = h.session()
    for v in s.variants:
        assert blocking(check_variant(v, s.brief, h.engine.skill.rules)) == [], v.id


def metres(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(x))


def poi(h, dest_id, text):
    p = h.engine.opendata.find_poi(dest_id, text)
    assert p is not None, (dest_id, text)
    return p


def timed_turn(h, text, **kw):
    t0 = time.perf_counter()
    r = h.turn(text, **kw)
    return r, time.perf_counter() - t0


def istanbul_trip(h):
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "party.segment": "couple"}))
    r = h.turn("Стамбул, 1–5 декабря, вдвоём")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    return r


# ---------------------------------------------------------------- s04 t1: a photo of a landmark

def test_s04_photo_landmark_gives_hotels_next_to_it(live):
    h = live
    h.understand(u(intent="new_trip", image=istanbul_photo()))
    r, took = timed_turn(h, "Хочу сюда!", image=DATA_URL)
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    s = h.session()
    prefs = s.brief.values()["prefs"]
    assert prefs["near"] == "Айя-София" and s.brief.source_of("prefs.near") == "inferred"
    hagia = poi(h, "istanbul", "Айя-София")
    rec = recommended(h)
    assert rec.facts["near_name"] == hagia["name"] == "Собор Святой Софии"
    assert rec.facts["distance_near_m"] <= 600, (rec.facts["hotel_name"], rec.facts["distance_near_m"])
    for v in s.variants:   # every hotel within the radius, the distance measured by code from the hotel's point
        assert v.facts["distance_near_m"] <= 1500, (v.id, v.facts["distance_near_m"])
        assert abs(v.facts["distance_near_m"] - metres((v.hotel.lat, v.hotel.lon), (hagia["lat"], hagia["lon"]))) < 2
        assert "near" in v.fit.breakdown and "center" not in v.fit.breakdown, v.fit.breakdown
    # the assumption is a chip the user can drop (shown from stage 2; built here directly)
    from agent.payloads import build_chips
    chip = next(c for c in build_chips(h.engine.skill, s.brief) if c.field == "prefs.near")
    assert chip.label == "отель рядом: Айя-София" and any(o.value is None for o in chip.options)
    inp = last_input(h, "present.variants")
    assert validate_card_input("present.variants", inp) == []
    assert all(i["facts"]["near_name"] == "Собор Святой Софии" for i in inp["items"])
    no_blocks(h)
    assert took < 3.0, took        # the first turn builds the destination's poi index once


def test_photo_landmark_not_in_pois_falls_back_to_centre(live):
    h = live
    h.understand(u(intent="new_trip", image=istanbul_photo(landmark="Эйфелева башня")))
    r = h.turn("Хочу сюда!", image=DATA_URL)
    assert r["cards"]["variants"], r["message"]
    s = h.session()
    assert not s.brief.values()["prefs"].get("near")
    assert all("near_name" not in v.facts and v.facts.get("center_fit") is not None for v in s.variants)
    assert "Эйфелев" not in r["message"]     # the photo's landmark was not asked for: nothing is claimed


# ---------------------------------------------------------------- s04 t6 / e8: «есть что-то рядом с Айя-Софией?»

def test_refine_near_hagia_sophia_changes_hotels(live):
    h = live
    istanbul_trip(h)
    before = sorted(v.hotel.hotel_id for v in h.session().variants)
    h.understand(u(intent="refine", set={"prefs.near": "Айя-София"}))
    r, took = timed_turn(h, "Хочу жить рядом с Айя-Софией")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    s = h.session()
    assert s.brief.values()["prefs"]["near"] == "Айя-София" and s.brief.source_of("prefs.near") == "user"
    after = sorted(v.hotel.hotel_id for v in s.variants)
    assert after != before
    assert all(v.facts["distance_near_m"] <= 1500 for v in s.variants)
    inp = last_input(h, "present.update")
    assert validate_card_input("present.update", inp) == []
    assert any(c["field"] == "prefs.near" and c["after"] == "Айя-София" for c in inp["changes"]), inp["changes"]
    no_blocks(h)
    assert took < 1.0, f"turn took {took:.2f} s"


def test_info_question_near_sight_is_a_refine_with_distances(live):
    """e8: understand reads «Есть что-то рядом с Айя-Софией?» as a bare info question (no patch): the code cue
    sets prefs.near, the turn rebuilds, and the update card answers with code-made metres."""
    h = live
    istanbul_trip(h)
    h.understand(u(intent="info", info="Есть что-то рядом с Айя-Софией?"))
    r = h.turn("Отель тот же, 2,5 км от центра. Есть что-то рядом с Айя-Софией?")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    s = h.session()
    assert s.brief.values()["prefs"]["near"] == "Айя-Софией"
    assert s.brief.values()["prefs"].get("center_max_m") is None          # «2,5 км от центра» is no pref
    inp = last_input(h, "present.update")
    nd = next(f for f in inp["question"]["facts"] if f["kind"] == "near_distance")
    assert nd["found"] and nd["place"] == "Собор Святой Софии"
    assert {i["id"] for i in nd["items"]} == {v.id for v in s.variants}
    assert all(i["distance_near_m"] <= 1500 for i in nd["items"])


def test_distance_question_stays_info(live):
    h = live
    istanbul_trip(h)
    h.understand(u(intent="info", info="Далеко ли отель от Айя-Софии?"))
    h.turn("А далеко ли отель от Айя-Софии?")
    s = h.session()
    assert not s.brief.values()["prefs"].get("near")
    inp = last_input(h, "present.info")
    nd = next(f for f in inp["facts"] if f["kind"] == "near_distance")
    assert nd["found"] and nd["place"] == "Собор Святой Софии" and nd["items"]


def test_district_sultanahmet_resolves(live):
    h = live
    istanbul_trip(h)
    h.understand(u(intent="refine"))          # understand slips: no patch; the code cue reads the district
    r = h.turn("Пересоберите, пожалуйста, в районе Султанахмет")
    assert r["cards"]["variants"], r["message"]
    s = h.session()
    assert s.brief.values()["prefs"]["near"] == "Султанахмет"
    sq = poi(h, "istanbul", "Султанахмет")
    assert sq["dist_center_m"] < 1000
    for v in s.variants:
        assert v.facts["near_name"] == sq["name"] and v.facts["distance_near_m"] <= 1500


def test_unknown_place_falls_back_to_centre_with_note(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "prefs.near": "Диснейленд"}))
    r = h.turn("Стамбул 1–5 декабря, отель рядом с Диснейлендом")
    assert r["cards"]["variants"], r["message"]
    s = h.session()
    assert not s.brief.values()["prefs"].get("near")
    assert "«Диснейленд» не нашёл" in r["message"]
    assert all("near_name" not in v.facts and v.facts.get("center_fit") is not None for v in s.variants)


def test_destination_without_pois_falls_back(live, monkeypatch):
    h = live
    monkeypatch.setattr(h.engine.opendata, "pois_rows", lambda dest_id: [])
    h.understand(u(intent="new_trip", image=istanbul_photo()))
    r = h.turn("Хочу сюда!", image=DATA_URL)
    assert r["cards"]["variants"] and not h.session().brief.values()["prefs"].get("near")
    h.understand(u(intent="refine", set={"prefs.near": "Айя-София"}))
    r = h.turn("Хочу жить рядом с Айя-Софией")
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    assert not h.session().brief.values()["prefs"].get("near")
    assert "не нашёл" in r["message"]


def test_near_relaxed_names_the_nearest_distance(live, monkeypatch):
    h = live
    monkeypatch.setitem(h.engine.skill.rules["ranking"], "near_max_m", 10)
    h.understand(u(intent="new_trip", set={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "prefs.near": "Галатская башня"}))
    r = h.turn("Стамбул 1–5 декабря, у Галатской башни")
    assert r["cards"]["variants"], r["message"]
    s = h.session()
    nearest = min(v.facts["distance_near_m"] for v in s.variants)
    for v in s.variants:
        note = next(n for n in v.risks if n.code == "relaxed_pref" and n.data.get("field") == "prefs.near")
        assert "ближайший в" in note.text and "Галатская башня" in note.text
        assert v.facts["distance_near_m"] <= nearest + 700
    no_blocks(h)


def test_destination_change_clears_near(live):
    h = live
    istanbul_trip(h)
    h.understand(u(intent="refine", set={"prefs.near": "Айя-София"}))
    h.turn("рядом с Айя-Софией")
    assert h.session().brief.values()["prefs"]["near"]
    h.understand(u(intent="new_trip", set={"destination.name": "Санкт-Петербург", "scope": ["stay"]}))
    h.turn("А давайте лучше в Питер, только отель")
    s = h.session()
    assert s.brief.values()["destination"]["id"] == "saint-petersburg"
    assert not s.brief.values()["prefs"].get("near")
    assert all("near_name" not in v.facts for v in s.variants)


def test_centre_wish_replaces_near(live):
    h = live
    istanbul_trip(h)
    h.understand(u(intent="refine", set={"prefs.near": "Галатская башня"}))
    h.turn("хочу у Галатской башни")
    h.understand(u(intent="refine", set={"prefs.center_max_m": 1500}))
    h.turn("Нет, лучше в центре")
    prefs = h.session().brief.values()["prefs"]
    assert prefs["center_max_m"] == 1500 and not prefs.get("near")


# ---------------------------------------------------------------- Moscow, Saint Petersburg

def test_moscow_near_red_square(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Москва", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-04", "scope": ["stay"],
                                           "prefs.near": "Красная площадь"}))
    r = h.turn("Только отель в Москве 1–4 декабря рядом с Красной площадью")
    assert r["cards"]["variants"], r["message"]
    rs = poi(h, "moscow", "Красной площадью")
    assert rs["name"] == "Красная площадь"
    for v in h.session().variants:
        assert v.facts["near_name"] == "Красная площадь" and v.facts["distance_near_m"] <= 1500


def test_info_far_from_hermitage_per_variant_metres(live):
    h = live
    h.understand(u(intent="new_trip", set={"destination.name": "Санкт-Петербург", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-04", "scope": ["stay"]}))
    h.turn("Только отель в Питере 1–4 декабря")
    h.understand(u(intent="info", info="Далеко ли от Эрмитажа?"))
    r, took = timed_turn(h, "А далеко ли от Эрмитажа?")
    inp = last_input(h, "present.info")
    assert validate_card_input("present.info", inp) == []
    nd = next(f for f in inp["facts"] if f["kind"] == "near_distance")
    herm = poi(h, "saint-petersburg", "Эрмитаж")
    assert nd["found"] and nd["place"] == herm["name"] == "Государственный Эрмитаж"
    s = h.session()
    shown = {i.id for i in s.shown.variants} or {v.id for v in s.variants}
    assert {i["id"] for i in nd["items"]} == shown
    for item in nd["items"]:
        v = s.variant(item["id"])
        assert isinstance(item["distance_near_m"], int)
        assert abs(item["distance_near_m"] - metres((v.hotel.lat, v.hotel.lon), (herm["lat"], herm["lon"]))) < 2
    assert took < 1.0, f"turn took {took:.2f} s"


def test_info_unknown_place_says_unknown_and_gives_centre(live):
    h = live
    istanbul_trip(h)
    h.understand(u(intent="info", info="Далеко ли отель от Диснейленда?"))
    h.turn("Далеко ли отель от Диснейленда?")
    facts = last_input(h, "present.info")["facts"]
    nd = next(f for f in facts if f["kind"] == "near_distance")
    assert nd == {"kind": "near_distance", "place": "Диснейленда", "found": False, "items": []}
    assert any(f["kind"] == "center_distance" for f in facts)


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


def test_near_understand_cases_golden():
    good = _und(sets={"prefs.near": "Айя-София"}, info="Есть отели рядом с Айя-Софией?")
    assert _run_case("e8_refine_near_hagia_sophia", good)[0]
    assert not _run_case("e8_refine_near_hagia_sophia", _und(sets={"prefs.center_max_m": 2500}))[0]
    assert not _run_case("e8_refine_near_hagia_sophia",
                         _und(sets={"prefs.near": "Айя-София", "prefs.center_max_m": 2500}))[0]
    assert _run_case("e10_refine_district_sultanahmet", _und(sets={"prefs.near": "Султанахмет"}))[0]
    assert not _run_case("e10_refine_district_sultanahmet", _und(sets={"prefs.center_max_m": 1500}))[0]
    photo = dict(istanbul_photo(), place={"name": "Стамбул", "country": "Турция", "confidence": 0.95})
    assert _run_case("ap5_photo_landmark", _und("new_trip", image=photo))[0]
    assert not _run_case("ap5_photo_landmark", _und("new_trip", image=dict(photo, landmark=None)))[0]


def _present_out(inp, message):
    items = inp["items"]
    return {"message": message, "recommended_id": inp.get("recommended_hint"), "upsell": [],
            "items": [{"id": i["id"], "why": i["title"], "sacrifices": [n["text"] for n in i["sacrifices"]],
                       "risks": [n["text"] for n in i["risks"]], "not_for": [n["text"] for n in i["not_for"]]}
                      for i in items]}


def test_near_present_cases_golden():
    _, _, inp = _run_case("b10_variants_near_landmark", {"message": "", "items": [], "recommended_id": None,
                                                         "upsell": []})
    rec = next(i for i in inp["items"] if i["id"] == inp["recommended_hint"])
    d = rec["facts"]["distance_near_m"]
    message = (f"Советую «{rec['title']}»: отель {rec['facts']['hotel_name']} — до Айя-Софии {d} м. "
               f"Могу подобрать отели рядом с Айя-Софией и подешевле, если нужно.")
    ok, failed, _ = _run_case("b10_variants_near_landmark", _present_out(inp, message))
    assert ok, failed
    centre_only = message.replace(f"до Айя-Софии {d} м", "отели подбираю только по расстоянию до центра")
    assert not _run_case("b10_variants_near_landmark", _present_out(inp, centre_only))[0]
    other = message + " Могу поискать отели в районе Султанахмет."
    assert not _run_case("b10_variants_near_landmark", _present_out(inp, other))[0]

    _, _, inp = _run_case("b11_info_far_from_hermitage", {"message": "", "items": [], "recommended_id": None,
                                                          "upsell": []})
    nd = next(f for f in inp["facts"] if f["kind"] == "near_distance")
    rows = "; ".join(f"{i['hotel']} — {i['distance_near_m']} м" for i in nd["items"])
    good = {"message": f"Недалеко: от каждого из предложенных отелей до Эрмитажа можно дойти пешком, {rows}. "
                       f"Если хотите ещё ближе, скажите — подберу отели рядом с Эрмитажем.",
            "items": [], "recommended_id": None, "upsell": []}
    ok, failed, _ = _run_case("b11_info_far_from_hermitage", good)
    assert ok, failed
    bad = dict(good, message="Про расстояние до Эрмитажа у меня нет данных, знаю только до центра.")
    assert not _run_case("b11_info_far_from_hermitage", bad)[0]
