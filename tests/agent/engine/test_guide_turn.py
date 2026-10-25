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
from .test_guide import GUIDE, ISTANBUL, SOCHI, Script, _savings_turn, calls, first_turn, guide_trace, reply
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


# ---------------------------------------------------------------- dates code moved are said (unannounced_change)

ISTANBUL_EARLY = {**ISTANBUL, "dates.start": "2026-12-05", "dates.end": "2026-12-09"}


def _early_then_cheaper(h, second):
    Script(h, lambda c, req: reply("Стамбул на 4 ночи — советую «Быстрее».", rec=c["default_plan"]["recommended_id"]))
    h.understand(u(set=ISTANBUL_EARLY))
    assert h.turn("Стамбул, 5–9 декабря, вдвоём").get("error") is None
    s = Script(h, second)
    h.understand(u(intent="refine"))
    return h.turn("а можно подешевле?"), s


def _moved(c):
    """The default plan's budget fit moved the dates by itself (the setup this test needs)."""
    before = {x["dates"] for x in c["screen"]["variants"]}
    after = {x["dates"] for x in c["default_plan"]["variants"]}
    assert before == {"2026-12-05..2026-12-09"} and after and not after & before, (before, after)
    return sorted(after)[0]


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_dates_the_budget_fit_moved_are_said_by_code(live):
    h = live
    seen = {}

    def silent(c, req):
        seen["dates"] = _moved(c)
        cheapest = min(c["default_plan"]["variants"], key=lambda v: v["total_rub"])
        return reply(f"Самый дешёвый вариант — {cheapest['total_rub']} ₽ за всю поездку.", rec=cheapest["id"])

    r, s = _early_then_cheaper(h, silent)
    g = guide_trace(r)
    start, end = seen["dates"].split("..")
    assert g["used"] is True and len(s.requests) == 1 and "unannounced_change" in g["repaired"]
    assert r["message"].startswith(f"Даты теперь {G.span_ru(start, end)}"), r["message"]
    assert g["rejected"][0]["issues"][0]["code"] == "unannounced_change"


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_dates_named_by_the_guide_need_no_repair(live):
    h = live

    def named(c, req):
        start, end = _moved(c).split("..")
        cheapest = min(c["default_plan"]["variants"], key=lambda v: v["total_rub"])
        return reply(f"Сдвинул поездку на {G.span_ru(start, end)} — так дешевле: {cheapest['total_rub']} ₽.",
                     rec=cheapest["id"])

    r, s = _early_then_cheaper(h, named)
    g = guide_trace(r)
    assert g["used"] is True and "repaired" not in g and r["message"].startswith("Сдвинул поездку на ")


def _plan_ctx(dates_seen, start, end, patch=None):
    from types import SimpleNamespace
    v = SimpleNamespace(id="cheapest", facts={"check_in_date": start, "check_out_date": end}, steps=[])
    plan = SimpleNamespace(show="variants", variants=[v], rec="cheapest")
    ctx = SimpleNamespace(patch=patch or {"set": {}, "unset": []})
    ctx.__dict__["guide_screen"] = {"variants": [{"id": "cheapest", "dates": d} for d in dates_seen]}
    return ctx, plan


def test_unannounced_change_unit():
    ctx, plan = _plan_ctx(["2026-12-05..2026-12-09"], "2026-12-08", "2026-12-12")
    [i] = G.unannounced_change(ctx, plan, "Самый дешёвый — 60 140 ₽, отель 8 400 ₽.")
    assert i["code"] == "unannounced_change" and i["data"]["say"] == "Даты теперь 8–12 декабря."
    assert G.unannounced_change(ctx, plan, "С 8 по 12 декабря дешевле.") == []
    assert G.unannounced_change(ctx, plan, "Заезд 8 декабря, выезд 12-го.") == []
    # the person asked to move the dates this turn: nothing to announce
    ctx2, plan2 = _plan_ctx(["2026-12-05..2026-12-09"], "2026-12-08", "2026-12-12",
                            patch={"set": {"dates.start": "2026-12-08"}, "unset": []})
    assert G.unannounced_change(ctx2, plan2, "Советую «Дешевле».") == []
    # one night fewer from the same day: «3 ночи» is enough; else code says both
    ctx3, plan3 = _plan_ctx(["2026-12-05..2026-12-09"], "2026-12-05", "2026-12-08")
    assert G.unannounced_change(ctx3, plan3, "3 ночи вместо 4 — 59 110 ₽.") == []
    [i3] = G.unannounced_change(ctx3, plan3, "Дешевле всего — 59 110 ₽.")
    assert i3["data"]["say"] == "Даты теперь 5–8 декабря — 3 ночи вместо 4."
    # same dates as on the screen, or nothing on the screen: no issue
    assert G.unannounced_change(*_plan_ctx(["2026-12-05..2026-12-09"], "2026-12-05", "2026-12-09"), "Ок.") == []
    assert G.unannounced_change(*_plan_ctx([], "2026-12-08", "2026-12-12"), "Ок.") == []
    assert G.names_dates("с 18 по 22 декабря", "2026-12-08", "2026-12-12") is False
    assert G.span_ru("2026-12-30", "2027-01-03") == "30 декабря – 3 января"


# ---------------------------------------------------------------- room wishes code does not search

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_the_guide_sees_the_room_of_each_trip(live):
    h = live
    r, s = first_turn(h)
    hotel = s.ctx()["default_plan"]["variants"][0]["hotel"]
    assert hotel.get("room") and hotel.get("room_adults"), hotel


def test_a_constraint_code_does_not_know_is_a_bad_arg():
    with pytest.raises(G.ArgError, match="cannot filter by"):
        G._patch_of({"set": {"constraints.add": ["quad_room_or_adjacent"]}})
    assert G._patch_of({"set": {"constraints.add": ["kitchen", "pet:dog"]}})["set"]["constraints.add"] == ["kitchen",
                                                                                                         "pet:dog"]


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_an_unknown_constraint_goes_back_and_a_noop_rebuild_claim_is_blocked(live):
    h = live
    first_turn(h)
    seen = {}

    def second(c, req):
        res = c["tool_results"][0]["result"]
        seen["error"] = res.get("error")
        assert res["ok"] is False and "cannot filter by" in res["error"]
        return reply("Пересобрал с условием «номер на четверых» — теперь в вариантах именно такое размещение.",
                     rec=c["default_plan"]["recommended_id"])

    def fixed(c, req):
        assert "nothing_changed_claim" in req.system
        return reply("Номер на четверых код не подбирает: в «Быстрее» трёхместный номер — уточню это честно.",
                     rec=c["default_plan"]["recommended_id"])

    s = Script(h, calls(("rebuild", {"set": {"constraints.add": ["quad_room_or_adjacent"]}})), second, fixed)
    h.understand(u(intent="refine"))
    r = h.turn("нам нужен номер на четверых или два смежных")
    g = guide_trace(r)
    assert g["used"] is True and len(s.requests) == 3, g
    assert g["rejected"][0]["issues"][0]["code"] == "nothing_changed_claim"
    assert r["message"].startswith("Номер на четверых код не подбирает")


def test_nothing_changed_claim_unit():
    from types import SimpleNamespace
    st = [SimpleNamespace(offer_id="f1"), SimpleNamespace(offer_id="h1")]
    v = SimpleNamespace(id="custom-11", totals=SimpleNamespace(rub=128520), steps=st)
    plan = SimpleNamespace(show="variants", variants=[v], rec="custom-11")
    ctx = SimpleNamespace()
    ctx.__dict__["sig_before"] = [["fastest", 128520, ["f1", "h1"]]]
    assert G.nothing_changed_claim(ctx, plan, "Пересобрал с условием — теперь в вариантах такое.")[0]["code"] == \
        "nothing_changed_claim"
    assert G.nothing_changed_claim(ctx, plan, "Не пересобрал: такого условия код не ищет.") == []
    assert G.nothing_changed_claim(ctx, plan, "Советую «Быстрее» за 128 520 ₽.") == []
    ctx.__dict__["sig_before"] = [["fastest", 124780, ["f2", "h1"]]]
    assert G.nothing_changed_claim(ctx, plan, "Пересобрал с прямыми рейсами.") == []      # the trips did change


# ---------------------------------------------------------------- every priced trip is one tap away

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_a_lever_the_message_prices_is_shown_next_to_the_recommended_trip(live):
    h = live
    seen = {}

    def pick(c, req):
        res = c["tool_results"][0]["result"]
        lv = next(x for x in res["levers"] if x["lever"] == "nights_1")
        rec = c["default_plan"]["recommended_id"]
        seen.update(lever=lv, rec=rec)
        return reply(f"Оставил бы как есть. Если готовы на ночь меньше — {lv['total_rub']} ₽.", rec=rec)

    r, s, before, _ = _savings_turn(h, pick)
    g = guide_trace(r)
    ids = [v["id"] for v in r["cards"]["variants"]]
    lv = seen["lever"]
    assert g["used"] is True and ids[0] == seen["rec"] and lv["variant_id"] in ids and len(ids) <= 3, ids
    assert any(a.get("payload", {}).get("variant_id") == lv["variant_id"] for a in r["actions"])
    nights_before = h.session().brief.values()["dates"]["nights"]
    # taking the named lever: the brief becomes that trip's (3 nights), so a later rebuild starts from it
    r2 = h.turn(action={"type": "choose_variant", "payload": {"variant_id": lv["variant_id"]}})
    assert r2.get("error") is None and r2["cart"]["total_rub"] == lv["total_rub"]
    assert h.session().brief.values()["dates"]["nights"] == nights_before - 1
    assert any("brief: the conditions of" in n for n in r2["debug"]["notes"])


def test_named_variants_and_stale_ids_unit():
    from types import SimpleNamespace as NS
    a = NS(id="cheapest", totals=NS(rub=59900), steps=[NS(offer_id="f1")])
    b = NS(id="custom-11", totals=NS(rub=39380), steps=[NS(offer_id="t1")])
    state = G.GuideState(default_variants=[a], default_brief=None, sets=[G.ToolSet(call=1, tool="find_savings",
                                                                                  variants=[b])])
    assert [v.id for v in G._named_variants("поездом 39 380 ₽, сейчас 59 900 ₽", state, {"cheapest"})] == ["custom-11"]
    assert G._named_variants("ничего", state, set()) == []
    assert G._numbers_named("39\u00a0380 ₽ и 125490") == {39380, 125490}
