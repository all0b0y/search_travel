"""Guide mode within its 15 s turn (AGENT_PLAN.md «As built: guide mode»): the pipeline's present runs
speculatively next to the guide, formal guard issues are repaired by code, dead ends get their likely fixes run by
code before round 1, guide-made changes stay proposals, the trace tells rounds / rejected replies / the speculative
present, and find_savings offers only places of the trip's class. The real mock and data (fixture `live`), a
scripted FakeBackend (it may sleep to simulate model latency). No network, no model.
"""
import json
import time

import pytest

from agent import guide as G
from agent.llm import LLMError
from agent.state import Brief, apply_patch

from .fakes import echo_present, u
from .test_guide import GUIDE, ISTANBUL, SOCHI, Script, calls, first_turn, guide_trace, reply
from .test_scopes import live  # noqa: F401 - the fixture

KALININGRAD = {"origin.city": "Новосибирск", "destination.name": "Калининград", "dates.month": 6, "dates.nights": 5,
               "party.segment": "couple", "party.adults": 2, "prefs.direct_only": True, "prefs.stars_min": 4,
               "prefs.meals": "all_inclusive"}
KALININGRAD_TEXT = "Калининград из Новосибирска на 5 ночей в июне вдвоём, только прямые рейсы, 4 звезды и всё включено"


def slow(fn, seconds):
    """The card's first call of each turn sleeps (the echo's guard rerun answers at once)."""
    def run(req):
        if not req.system.rstrip().endswith("fix exactly these problems."):
            time.sleep(seconds)
        return fn(req)
    return run


def _wait(pred, timeout=3.0):
    t0 = time.perf_counter()
    while not pred() and time.perf_counter() - t0 < timeout:
        time.sleep(0.02)
    return pred()


def _after_steps_ms(r):
    t = r["debug"]["timings_ms"]
    return t["total"] - sum(t.get(name, 0) for name in r["debug"]["steps"]) - t.get("understand", 0)


# ---------------------------------------------------------------- T1: speculative present

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_failing_guide_costs_max_not_the_sum(live, monkeypatch):
    """Guide round 1 fails after 1.2 s; the present card takes 1.2 s: with the speculative present the turn
    after the default plan takes about max(guide, present), without it their sum."""
    h = live
    h.backend.scripts["present.variants"] = slow(echo_present, 1.2)

    def failing(c, req):
        time.sleep(1.2)
        raise LLMError("guide down", card="guide")

    out = {}
    for mode in (True, False):
        monkeypatch.setitem(h.engine.skill.rules["guide"], "speculative_present", mode)
        h.new()
        Script(h, failing)
        h.understand(u(set=ISTANBUL))
        r = h.turn("Стамбул, 8–12 декабря, вдвоём")
        assert r.get("error") is None and guide_trace(r)["fallback"].startswith("llm")
        out[mode] = r
    on, off = out[True], out[False]
    print(f"\nafter the default plan: speculative {_after_steps_ms(on):.0f} ms, sequential {_after_steps_ms(off):.0f} ms")
    assert _after_steps_ms(on) < 2000 and _after_steps_ms(off) >= 2400
    spec = guide_trace(on)["speculative"]
    assert spec["used"] is True and spec["discarded"] is False and spec["card"] == "present.variants"
    assert spec["ms"] >= 1200 and spec["waited_ms"] is not None
    assert "speculative" not in guide_trace(off)
    # the same pipeline answer either way: message, cards, chips, actions; the present call is in the turn's calls
    strip = lambda r: {k: v for k, v in r.items() if k not in ("debug", "session_id")}  # noqa: E731
    assert strip(on) == strip(off)
    cards = [c["card"] for c in on["debug"]["calls"]]
    assert cards[:3] == ["understand", "guide", "present.variants"] and cards == [c["card"] for c in off["debug"]["calls"]]
    assert on["debug"]["budget"]["flash_calls"] == off["debug"]["budget"]["flash_calls"] == 2


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_used_guide_discards_the_speculative_present(live):
    h = live
    r, s = first_turn(h)
    g = guide_trace(r)
    assert g["used"] is True
    assert g["speculative"]["discarded"] is True and g["speculative"]["used"] is False
    assert g["speculative"]["ms_at_discard"] is not None
    assert [c["card"] for c in r["debug"]["calls"]] == ["understand", "guide"]   # its call is not the turn's
    assert _wait(lambda: h.backend.count("present.variants") >= 1)                 # but it was made
    assert r["debug"]["budget"]["flash_calls"] == 1


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_the_guide_gets_the_turn_budget_while_the_present_is_in_flight(live, monkeypatch):
    """turn_budget_s 3 with fallback_reserve_s 3 leaves the guide no time — unless the present already runs next to
    it (reserve 0.5 s)."""
    h = live
    monkeypatch.setitem(h.engine.skill.rules["guide"], "turn_budget_s", 3)
    first_turn(h)
    assert guide_trace({"debug": {"guide": h.session().trace[-1]["guide"]}})["used"] is True
    monkeypatch.setitem(h.engine.skill.rules["guide"], "speculative_present", False)
    Script(h, reply("Не понадобится.", show="none"))
    h.understand(u(intent="info", info="а что с погодой?"))
    r = h.turn("а что с погодой?")
    assert guide_trace(r)["fallback"].startswith("budget")


# ---------------------------------------------------------------- T2: code repair of formal issues

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_two_questions_are_repaired_by_code(live):
    h = live
    msg = ("Стамбул на 4 ночи — советую «Быстрее»: короче дорога. Хотите, посмотрю отели поближе к центру? "
           "Отель там с хорошим рейтингом. Берёте «Быстрее»?")
    r, s = first_turn(h, reply(msg, rec="fastest"))
    g = guide_trace(r)
    assert g["used"] is True and len(s.requests) == 1 and [x["round"] for x in g["rounds"]] == [1]
    assert g["repaired"] == ["too_many_questions"]
    assert r["message"].count("?") == 1 and r["message"].endswith("Берёте «Быстрее»?")
    assert "посмотрю отели" not in r["message"] and "хорошим рейтингом" in r["message"]
    assert g["rejected"][0]["message"] == msg and g["rejected"][0]["issues"][0]["code"] == "too_many_questions"


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_reply_that_is_mostly_questions_goes_to_the_fixup(live):
    h = live
    good = reply("Советую «Быстрее» — дорога короче, отель с хорошим рейтингом.", rec="fastest")
    s = Script(h, reply("Посмотреть отели? Берёте «Быстрее»?", rec="fastest"), good)
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    g = guide_trace(r)
    assert g["used"] is True and [x["round"] for x in g["rounds"]] == [1, "fixup"] and "repaired" not in g


def test_repair_questions_unit():
    two = {"message": "Советую «Быстрее». Хотите отели ближе к центру? Он с хорошим рейтингом. Берёте?",
           "question": None}
    assert G.repair_questions(two)["message"] == "Советую «Быстрее». Он с хорошим рейтингом. Берёте?"
    # the question object is the final question: every question sentence of the message goes
    q = {"text": "Сколько ночей хотите?", "field": "dates.nights", "choices": []}
    sep = {"message": "Советую «Быстрее». Удобно? Отель хороший.", "question": q}
    assert G.repair_questions(sep)["message"] == "Советую «Быстрее». Отель хороший."
    assert G.repair_questions({"message": "Удобно? Берёте?", "question": None}) is None   # < 2 sentences left
    assert G.repair_questions({"message": "Советую «Быстрее». Берёте?", "question": None}) is None   # nothing to drop


# ---------------------------------------------------------------- T4: dead ends, T5: proposals

def _kaliningrad_round1(c, req, seen):
    seen.append(c)
    res = [t for t in c.get("tool_results") or [] if t["tool"] == "rebuild"]
    rb = res[0]["result"]
    total = next(v["total_rub"] for v in rb["variants"] if v["id"] == rb["recommended_id"])
    return reply(f"Прямых рейсов из Новосибирска в Калининград в июне нет, а «всё включено» в 4 звезды там нет. "
                 f"С пересадкой и без «всё включено» вся поездка — {total} ₽. Берёте?", rec=rb["recommended_id"])


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_dead_end_gets_its_fixes_before_round_1_and_shows_whole_trips(live):
    h = live
    seen = []
    s = Script(h, lambda c, req: _kaliningrad_round1(c, req, seen))
    h.understand(u(set=KALININGRAD))
    r = h.turn(KALININGRAD_TEXT)
    assert r.get("error") is None, r["message"]
    c = seen[0]
    assert c["default_plan"]["status"] == "failed"
    assert c["default_plan"]["dead_end"]["why"] == ["no_direct_flights", "no_hotel_under_filters"]
    auto = c["tool_results"]
    assert all(t["auto"] for t in auto) and len(guide_trace(r)["tools"]) <= 3
    assert [t["tool"] for t in auto] == ["check_road", "rebuild"]          # the stars rebuild built nothing: left out
    assert auto[1]["args"]["set"]["prefs.direct_only"] is False and auto[1]["args"]["set"]["prefs.meals"] == "any"
    assert "прямых нет" in auto[0]["result"]["line_ru"]
    g = guide_trace(r)
    assert g["used"] and len(s.requests) == 1 and [x["round"] for x in g["rounds"]] == [1]   # one model call
    cards = r["cards"]["variants"]
    assert cards and all(any(st["type"] == "flight" for st in cd["steps"]) for cd in cards)   # whole trips
    assert all(any(st["type"] == "stay" for st in cd["steps"]) for cd in cards)
    assert r["stage"] == "selection"
    # T5: what the guide changed is a proposal; what the person said stays theirs
    b = h.session().brief
    assert b.values()["prefs"]["direct_only"] is False and b.source_of("prefs.direct_only") == "inferred"
    assert b.source_of("prefs.meals") == "inferred" and b.source_of("prefs.stars_min") == "user"
    assert b.source_of("destination") == "user" and b.source_of("dates.nights") == "user"


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_question_about_a_standing_failure_gets_the_fixes_too(live):
    h = live
    Script(h, "broken", "broken")
    h.understand(u(set=KALININGRAD))
    r = h.turn(KALININGRAD_TEXT)
    assert r["error"]["code"] == "no_data"          # the guide fell back: the pipeline's dead end
    seen = []
    Script(h, lambda c, req: _kaliningrad_round1(c, req, seen))
    h.understand(u(intent="info", info="а почему не получилось?"))
    r = h.turn("а почему не получилось?")
    assert r.get("error") is None, r["message"]
    assert seen[0]["default_plan"]["dead_end"]["why"] == ["no_direct_flights", "no_hotel_under_filters"]
    assert guide_trace(r)["used"] is True and r["cards"]["variants"]
    assert all(any(st["type"] == "flight" for st in cd["steps"]) for cd in r["cards"]["variants"])


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_no_flights_with_a_train_is_rebuilt_by_train_before_round_1(live):
    from .test_guide import KRASNODAR_TICKETS
    h = live
    seen = []

    def first(c, req):
        seen.append(c)
        rb = next(t["result"] for t in c["tool_results"] if t["tool"] == "rebuild")
        total = next(v["total_rub"] for v in rb["variants"] if v["id"] == rb["recommended_id"])
        return reply(f"Самолётов туда в данных нет, зато есть поезд: {total} ₽ за билеты туда и обратно.",
                     rec=rb["recommended_id"])

    Script(h, first)
    h.understand(u(set=KRASNODAR_TICKETS))
    r = h.turn("Летим вдвоём из Екатеринбурга в Краснодар в августе, нужны только билеты")
    assert r.get("error") is None and guide_trace(r)["used"]
    auto = seen[0]["tool_results"]
    assert auto[0]["auto"] and auto[0]["args"]["set"] == {"prefs.transport": "train", "scope": ["trains"]}
    assert all(st["type"] == "train" for cd in r["cards"]["variants"] for st in cd["steps"])
    assert h.session().brief.source_of("prefs.transport") == "inferred"


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_rebuild_marks_only_the_guides_changes_as_proposals(live):
    h = live
    first_turn(h)

    def second(c, req):
        res = c["tool_results"][0]["result"]
        return reply(f"В Анталью с завтраками — от {min(v['total_rub'] for v in res['variants'])} ₽.",
                     rec=res["recommended_id"])

    Script(h, calls(("rebuild", {"set": {"destination.name": "Анталья", "prefs.meals": "breakfast"}})), second)
    h.understand(u(intent="refine", set={"prefs.meals": "breakfast"}))
    r = h.turn("а с завтраками? может, тогда в Анталью")
    assert guide_trace(r)["used"] is True
    b = h.session().brief
    assert b.values()["destination"]["name"] == "Анталья" and b.source_of("destination") == "inferred"
    assert b.source_of("prefs.meals") == "user"                     # the person said it this turn
    assert b.source_of("party.adults") == "user"                    # untouched
    assert "destination.name" in __import__("agent.payloads", fromlist=["x"]).assumed_paths(b)


def test_as_proposals_unit():
    before, _ = apply_patch(Brief(), {"set": {"prefs.direct_only": True, "prefs.meals": "all_inclusive",
                                              "destination.name": "Калининград"}, "unset": []})
    patch = {"set": {"prefs.direct_only": False, "prefs.meals": "all_inclusive", "scope": ["flights", "stay"]},
             "unset": []}
    after, _ = apply_patch(before, patch)
    out = G.as_proposals(before, after, patch, {"set": {}, "unset": []})
    assert out.source_of("prefs.direct_only") == "inferred" and out.values()["prefs"]["direct_only"] is False
    assert out.source_of("prefs.meals") == "user"                   # the same value the person gave: still theirs
    assert out.source_of("scope") == "inferred" and out.source_of("destination") == "user"
    # «только прямые» said this turn and overridden by the guide: a proposal; the value the person said: theirs
    over = G.as_proposals(before, after, patch, {"set": {"prefs.direct_only": True}, "unset": []})
    assert over.source_of("prefs.direct_only") == "inferred"
    said = G.as_proposals(before, after, patch, {"set": {"prefs.direct_only": False}, "unset": []})
    assert said.source_of("prefs.direct_only") == "user"


# ---------------------------------------------------------------- T6: trace

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_trace_rounds_usage_and_rejected_replies(live):
    h = live
    bad = reply("Советую «Быстрее»: всего 12 345 ₽ за всё.", rec="fastest")
    good = lambda c, req: reply("Советую «Быстрее» — дорога короче.", rec="fastest")  # noqa: E731
    Script(h, bad, good)
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    g = guide_trace(r)
    assert g["used"] is True
    for rnd in g["rounds"]:
        assert set(rnd["usage"]) == {"input", "cached", "output", "reasoning"} and "latency_ms" in rnd
    assert g["rejected"] == [{"round": 1, "message": bad["reply"]["message"], "question": None, "show": "variants",
                              "recommended_id": "fastest", "issues": g["rejected"][0]["issues"]}]
    assert any(i["code"] == "invented_number" for i in g["rejected"][0]["issues"])
    assert json.loads(json.dumps(h.session().trace[-1]["guide"]))["rejected"]
    # a non-debug session keeps no rejected text
    h.new(debug=False)
    Script(h, bad, good)
    h.understand(u(set=ISTANBUL))
    h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert "rejected" not in h.session().trace[-1]["guide"]


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_timed_out_round_is_in_the_trace(live, monkeypatch):
    h = live
    monkeypatch.setitem(h.engine.skill.rules["guide"], "turn_budget_s", 1.5)
    Script(h, lambda c, req: (time.sleep(2.0), reply("Поздно.", show="none"))[1])
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    g = guide_trace(r)
    assert g["fallback"].startswith("budget") and g["rounds"][-1]["error"] == "timeout"


# ---------------------------------------------------------------- T8: similar places of the trip's class

def _rec(cc, kind, tags):
    return {"country_code": cc, "kind": kind, "tags": tags}


def test_similar_places_keep_the_trips_class():
    moscow = {"country_code": "RU"}
    brief = Brief()
    istanbul = _rec("TR", "city", ["city", "sea", "history"])
    assert G.main_trait(istanbul) == "city" and G.main_trait(_rec("TR", "resort", ["sea", "city"])) == "sea"
    assert not G.same_class(_rec("RU", "city", ["city", "river"]), istanbul, moscow, brief)      # Казань: at home
    assert G.same_class(_rec("GE", "city", ["city", "food"]), istanbul, moscow, brief)           # Тбилиси
    assert not G.same_class(_rec("TR", "resort", ["sea", "beach", "city"]), istanbul, moscow, brief)  # Анталья: sea
    sochi = _rec("RU", "resort", ["sea", "beach", "mountains"])
    assert G.same_class(_rec("RU", "resort", ["sea", "beach"]), sochi, moscow, brief)
    assert not G.same_class(_rec("RU", "city", ["city"]), sochi, moscow, brief)
    # the person asked otherwise: «по России» makes a domestic place fit an abroad trip
    home, _ = apply_patch(brief, {"set": {"prefs.domestic": True}, "unset": []})
    assert G.same_class(_rec("RU", "city", ["city", "river"]), istanbul, moscow, home)
    sea, _ = apply_patch(brief, {"set": {"traits.add": ["sea"]}, "unset": []})
    assert G.same_class(_rec("TR", "resort", ["sea", "beach"]), istanbul, moscow, sea)


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_istanbul_savings_offer_no_domestic_cities(live):
    h = live
    first_turn(h)
    seen = []
    Script(h, lambda c, req: (seen.append(c), reply("Дешевле — на ночь короче.", show="none"))[1])
    h.understand(u(intent="refine"))
    h.turn("а можно подешевле?")
    places = seen[0]["tool_results"][0]["result"]["places"]
    assert not {p["id"] for p in places} & {"kazan", "nizhny-novgorod", "kaliningrad"}, places
    assert all(p["id"] not in ("kazan", "nizhny-novgorod") for p in places)


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_sochi_savings_offer_domestic_sea_places(live):
    h = live
    seen = []
    Script(h, reply("Сочи — советую отмеченный.", show="none"),
           lambda c, req: (seen.append(c), reply("Дешевле — на ночь короче.", show="none"))[1])
    h.understand(u(set=SOCHI))
    h.turn("Москва — Сочи, 14–19 декабря, вдвоём")
    h.understand(u(intent="refine"))
    h.turn("а можно подешевле?")
    places = seen[0]["tool_results"][0]["result"]["places"]
    assert places
    from agent.opendata import OpenData
    from .test_scopes import ROOT
    od = OpenData(path=str(ROOT / "data" / "opendata.sqlite"))
    for p in places:
        rec = dict(od.destination(p["id"]))
        assert rec["country_code"] == "RU" and G.main_trait(rec) == "sea", (p["name"], rec.get("kind"), rec.get("tags"))
