"""Level-2 cases for hotel location and the best time to go (level-3 s04_photo_landmark: the question «Когда
лучше поехать?» was dropped, «поближе к историческому центру» became district walkable and changed nothing,
the card offered a search «в районе Султанахмет» the engine cannot do). Adds 8 files to evals/cases/ and
deletes nothing.

    .venv/bin/python -m evals.authoring.cases_location

The understand inputs are written by hand; the present inputs are built by the real engine offline (in-process
Ostrovok mock over data/ostrovok_catalog.sqlite, real open data, a scripted FakeBackend, the clock pinned to
2026-10-25 12:00 MSK), so they carry exactly what the engine sends: items with facts.distance_center_m and the
`question` key ({text, facts}) with the code-made best months and distances.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.authoring.cases_l3fix import (MOSCOW, SHOWN, a, brief, case, dates, party, pred, prefs,
                                         understand_input)
from evals.card_inputs import validate_card_input
from evals.common import CASES_DIR, ROOT, write_json

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=MSK)
HAGIA = "evals/images/landmark_hagia_sophia.jpg"

# a district / sight search offered as an action (the engine filters by the distance to the centre only)
DISTRICT_OFFER_RE = (r"(поищ|подбер|найд|посмотр)\w*\s+(отел\w*\s+)?(в\s+районе|рядом\s+с|возле|у\s+)\s*"
                     r"(Султанахмет|Айя|Галат|Таксим|Диснейленд)")
PRESENT_COMMON = [
    a("ids_subset"), a("numbers_subset"),
    pred("russian_only"), pred("tone_no_bureaucratese"), pred("at_most_one_joke"), pred("money_risks_plain"),
    pred("no_upsell_without_candidate"), a("max_items", path="/upsell", max=2), pred("recommended_in_items"),
    a("regex_absent", path="$text", pattern=DISTRICT_OFFER_RE),
]

ist_shown = brief(origin="Москва", iata=MOSCOW[1], dest="Стамбул", dest_id="istanbul", country="Турция",
                  d=dates("2026-12-01", "2026-12-05", 12, 4), p=party("couple", 2), pr=prefs())


# ---------------------------------------------------------------- understand

UNDERSTAND = [
    case("b6_center_new_trip", "B", "understand",
         understand_input("Хочу в Рим на 4 ночи в декабре, вдвоём, отель в центре"),
         [a("path_equals", path="/intent", value="new_trip"),
          a("path_equals", path="/patch/set/destination.name", value="Рим", casefold=True),
          a("path_equals", path="/patch/set/prefs.center_max_m", value=1500),
          a("path_absent", path="/patch/set/prefs.district"),
          a("path_equals", path="/patch/set/dates.month", value=12),
          a("path_equals", path="/patch/set/dates.nights", value=4)],
         "«в центре» -> prefs.center_max_m 1500 (a hotel filter by the distance to the centre), never district."),
    case("e7_refine_closer_historic_center", "E", "understand",
         understand_input("Да, пересоберите, пожалуйста, чтобы отель был поближе к историческому центру",
                          stage="selection", b=ist_shown, shown=SHOWN,
                          assumed=["origin.city", "party.segment"], assumptions=[("origin.city", "из Москвы")],
                          history=[{"role": "user", "text": "А отель далеко от исторического центра, от Айя-Софии?"},
                                   {"role": "agent", "text": "Galata Design Guest House стоит в 2554 м от центра."}]),
         [a("path_equals", path="/intent", value="refine"),
          a("path_equals", path="/patch/set/prefs.center_max_m", value=1500),
          a("path_absent", path="/patch/set/prefs.district"),
          pred("patch_has_no_assumptions", allowed=["prefs.center_max_m"])],
         "L3 s04 t5: «поближе к историческому центру» was read as district walkable and the rebuild changed "
         "nothing; it is prefs.center_max_m 1500 (the code cue does the same as a safety net)."),
    case("e8_refine_near_hagia_sophia", "E", "understand",
         understand_input("Отель тот же, 2,5 км от центра. Есть что-то рядом с Айя-Софией?", stage="selection",
                          b=ist_shown, shown=SHOWN, assumed=["origin.city"],
                          assumptions=[("origin.city", "из Москвы")]),
         [a("path_in", path="/intent", values=["refine", "info"]),
          a("path_in", path="/patch/set/prefs.center_max_m", values=[800, 1500]),
          a("path_absent", path="/patch/set/prefs.district")],
         "«рядом с Айя-Софией» = a sight of the historic centre -> prefs.center_max_m (L3 s04 t6).",
         unsure="info with the patch is accepted: route_intent / the update card still rebuild with the pref."),
    case("x7_near_disneyland_no_center", "edge", "understand",
         understand_input("Париж на 3 ночи в январе, вдвоём с ребёнком, отель рядом с Диснейлендом"),
         [a("path_equals", path="/intent", value="new_trip"),
          a("path_equals", path="/patch/set/destination.name", value="Париж", casefold=True),
          a("path_absent", path="/patch/set/prefs.center_max_m")],
         "Disneyland Paris is ~30 km out of the centre: no centre pref (the card says plainly that hotels are "
         "picked by the distance to the centre only)."),
    case("ap4_photo_when_to_go", "A_photo", "understand",
         understand_input("Хочу сюда! Когда лучше поехать?", has_image=True),
         [a("path_equals", path="/intent", value="new_trip"),
          a("path_equals", path="/image/want", value="here"),
          pred("matches", path="/image/place/name", pattern="стамбул|istanbul"),
          pred("matches", path="/info_question", pattern="когда|сезон|время")],
         "L3 s04 t1: a question inside a photo request goes to info_question while the intent stays new_trip "
         "(the variants card answers it from code-made best months).",
         image=HAGIA),
]


# ---------------------------------------------------------------- present (engine-built inputs)

def _engine(tmp):
    os.environ["OSTROVOK_STATE_DB"] = str(Path(tmp) / "state.sqlite")
    os.environ["OSTROVOK_CATALOG_EXTRA_DB"] = str(Path(tmp) / "no_catalog_extra.sqlite")
    os.environ["OPENDATA_EXTRA_DB"] = str(Path(tmp) / "no_opendata_extra.sqlite")
    from agent.engine import Engine
    from agent.flags import parse_flags
    from agent.jev import FakeJev
    from agent.llm import FakeBackend
    from agent.mcp_client import InProcessOstrovokClient
    from agent.opendata import OpenData
    from agent.prefetch import Prefetcher
    from agent.profiles import ProfileStore
    from agent.sessions import SessionStore
    from ostrovok_mcp import db
    db.reset_caches()
    backend = FakeBackend()

    def echo(req):
        data = json.loads(req.user)
        items = data.get("items") or []
        return {"message": "Готово.", "items": [{"id": i["id"], "why": "—", "sacrifices": [], "risks": [],
                                                 "not_for": []} for i in items],
                "recommended_id": data.get("recommended_hint"), "upsell": []}
    for card in ("present.variants", "present.update", "present.info", "present.destinations",
                 "present.compare", "present.fallback", "present.aftercare"):
        backend.scripts[card] = echo
    engine = Engine(backend=backend, jev=FakeJev(fail=True), ostrovok=InProcessOstrovokClient(),
                    opendata=OpenData(path=str(ROOT / "data" / "opendata.sqlite"),
                                      overlay=str(Path(tmp) / "no_opendata_extra.sqlite")),
                    flags=parse_flags("stage2"), store=SessionStore(":memory:"), profiles=ProfileStore(":memory:"),
                    prefetcher=Prefetcher(synchronous=True), lazy_city=False, files_dir=str(tmp))
    return engine, backend


def _u(intent="new_trip", set_=None, image=None, info=None):
    return {"intent": intent, "patch": {"set": dict(set_ or {}), "unset": []}, "refs": [], "image": image,
            "info_question": info, "post_purchase": None}


def _last_input(backend, card):
    return json.loads([r for r in backend.requests if r.card == card][-1].user)


def build_present_inputs():
    """-> {"photo": variants input, "dates_question": variants input, "center": update input}."""
    out = {}
    photo = {"want": "here", "place": {"name": "Стамбул", "country": "Турция", "confidence": 0.95},
             "traits": {"landscape": ["old_town"], "water": "sea", "climate": "temperate", "setting": "city",
                        "vibe": ["culture"]}}
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        sid = engine.new_session(profile_id="demo", now=NOW)
        backend.add("understand", _u(image=photo, info="Когда лучше поехать?"))
        engine.handle(sid, {"session_id": sid, "text": "Хочу сюда! Когда лучше поехать?",
                            "image": "data:image/jpeg;base64,AAAA"})
        out["photo"] = _last_input(backend, "present.variants")

        sid = engine.new_session(profile_id="demo", now=NOW)
        backend.add("understand", _u(set_={"destination.name": "Стамбул", "dates.start": "2026-12-01",
                                           "dates.end": "2026-12-05", "party.segment": "couple"},
                                     info="Отели далеко от центра?"))
        engine.handle(sid, {"session_id": sid, "text": "Стамбул, 1–5 декабря, вдвоём. А отели далеко от центра?"})
        out["dates_question"] = _last_input(backend, "present.variants")
        backend.add("understand", _u(intent="refine", set_={"prefs.district": "walkable"}))
        engine.handle(sid, {"session_id": sid,
                            "text": "Да, пересоберите, пожалуйста, чтобы отель был поближе к историческому центру"})
        out["center"] = _last_input(backend, "present.update")
    for name, card in (("photo", "present.variants"), ("dates_question", "present.variants"),
                       ("center", "present.update")):
        errors = validate_card_input(card, out[name])
        assert not errors, (name, errors)
    assert out["photo"].get("question") and out["dates_question"].get("question"), "question key missing"
    assert any(c["field"] == "prefs.center_max_m" for c in out["center"]["changes"]), out["center"]["changes"]
    return out


def present_cases():
    inp = build_present_inputs()
    photo = inp["photo"]
    best = next(f for f in photo["question"]["facts"] if f["kind"] == "best_time")["data"]
    month_re = "|".join(m["month_ru"][:-1] if m["month_ru"].endswith(("ь", "й")) else m["month_ru"]
                        for m in best["months"])
    first_sentence = r"^[^.!]*(" + month_re + r")"
    return [
        case("b8_variants_answer_best_time", "B", "present.variants", photo,
             PRESENT_COMMON + [
                 pred("matches", path="/message", pattern=first_sentence),
                 pred("mentions_any", words=[r"\d+\s*°", r"\d+–\d+°"]),
                 pred("mentions_any", words=[r"мож\w+ поменять|поправ\w+|измен\w+|другие даты|сдвин"]),
                 pred("asks_at_most", n=len(photo.get("questions") or []))],
             "L3 s04 t1: «Когда лучше поехать?» inside a photo request is answered FIRST from question.facts "
             "(best_time.summary_ru: months + day temperature + sea), and the assumed dates in the best month are "
             "said to be changeable; no offer of a district / sight search."),
        case("b7_variants_answer_center_distance", "B", "present.variants", inp["dates_question"],
             PRESENT_COMMON + [
                 pred("matches", path="/message", pattern=r"^[^.!]*(центр)"),
                 pred("mentions_any", words=[r"\d[\d  ]*\s*м\b", r"\d+,\d\s*км"]),
                 pred("asks_at_most", n=len(inp["dates_question"].get("questions") or []))],
             "A question inside a search («А отели далеко от центра?») is answered in the first sentence from the "
             "center_distance facts (metres from the input), never dropped."),
        case("e9_update_center_rebuilt", "E", "present.update", inp["center"],
             PRESENT_COMMON + [
                 pred("matches", path="/message", pattern=r"^[^.!]*центр"),
                 pred("asks_at_most", n=len(inp["center"].get("questions") or [])),
                 a("regex_absent", path="/message", pattern=r"ничего не менял|подборка та же")],
             "L3 s04 t5: the centre wish is prefs.center_max_m; the update card starts with it (a prefs row in "
             "changes) and names the new hotels' distance; never «ничего не менял», never a district search offer."),
    ]


def main():
    for c in UNDERSTAND + present_cases():
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
