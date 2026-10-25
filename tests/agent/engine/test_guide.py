"""Guide mode (AGENT_PLAN.md decisions 23-26, agent/guide.py) end to end on the real mock and data (fixture `live`
of test_scopes: in-process Ostrovok mock over the committed catalog, real open data); the guide model is a scripted
FakeBackend card `guide`. No network, no model.
"""
import json
import time

import pytest

from agent import guide as G
from agent.mcp_client import ToolError

from .fakes import u
from .test_scopes import live  # noqa: F401 - the fixture

GUIDE = "stage1,guide"
ISTANBUL = {"origin.city": "Москва", "destination.name": "Стамбул", "dates.start": "2026-12-08",
            "dates.end": "2026-12-12", "party.segment": "couple", "party.adults": 2}
KRASNODAR_TICKETS = {"origin.city": "Екатеринбург", "destination.name": "Краснодар", "dates.month": 8,
                     "dates.nights": 7, "party.segment": "couple", "party.adults": 2, "scope": ["flights"],
                     "prefs.transport": "plane"}
EXPECTED_KEYS = ["today", "stage", "brief", "assumed", "assumptions", "home_city", "screen", "history", "message",
                 "reading", "default_plan", "round"]


class Script:
    """The guide card's scripted replies: dicts, or callables of the parsed context (and the request)."""

    def __init__(self, h, *items):
        self.items = list(items)
        self.requests = []
        h.backend.scripts["guide"] = self

    def __call__(self, req):
        self.requests.append(req)
        if not self.items:
            raise AssertionError("guide called more often than scripted")
        item = self.items.pop(0)
        return item(json.loads(req.user), req) if callable(item) else item

    def ctx(self, n=-1):
        return json.loads(self.requests[n].user)


def reply(message, show="variants", rec=None, question=None):
    return {"calls": [], "reply": {"message": message, "show": show, "recommended_id": rec, "question": question}}


def calls(*items):
    return {"calls": [{"tool": t, "args": a} for t, a in items], "reply": None}


def guide_trace(r):
    return r["debug"]["guide"]


def first_turn(h, script_reply=None):
    """Istanbul, 8-12 December, two adults: the guide answers directly with the default plan's pick."""
    s = Script(h, script_reply or (lambda c, req: reply(
        f"Стамбул на {c['brief']['dates']['nights']} ночи — советую «{next(v['title'] for v in c['default_plan']['variants'] if v['id'] == c['default_plan']['recommended_id'])}».",
        rec=c["default_plan"]["recommended_id"])))
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert r.get("error") is None, r["message"]
    return r, s


# ---------------------------------------------------------------- context

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_context_is_compact_ordered_and_the_prefix_stable(live):
    h = live
    r, s = first_turn(h)
    assert guide_trace(r)["used"] is True
    ctx = s.ctx()
    assert list(ctx) == EXPECTED_KEYS
    size = len(s.requests[0].user)
    assert size < 24000, size
    assert ctx["default_plan"]["status"] == "ok" and len(ctx["default_plan"]["variants"]) == 3
    v = ctx["default_plan"]["variants"][0]
    assert {"id", "title", "total_rub", "dates", "nights", "out", "back", "hotel"} <= set(v)
    assert "steps" not in v and "fit" not in v              # whitelisted keys only
    assert ctx["home_city"] == "Москва" and ctx["message"] == "Стамбул, 8–12 декабря, вдвоём"
    system1 = s.requests[0].system
    assert system1.startswith((h.engine.skill.root / "guide.md").read_text(encoding="utf-8").strip()[:200])
    # a second turn: the screen shows the first turn's variants, history holds the agent's text (≤ 300 chars),
    # and the system prompt (the cacheable prefix) is byte-identical
    Script(h, lambda c, req: reply("Понял, оставляю как есть — варианты те же.", show="none"))
    h.understand(u(intent="info", info="а что с погодой?"))
    h.turn("а что с погодой?")
    s2 = h.backend.scripts["guide"]
    c2 = s2.ctx()
    assert s2.requests[0].system == system1
    assert [x["id"] for x in c2["screen"]["variants"]] == ["cheapest", "fastest", "comfort"]
    assert all(len(x["text"]) <= 301 for x in c2["history"] if x["role"] == "agent")
    assert c2["history"][0] == {"role": "user", "text": "Стамбул, 8–12 декабря, вдвоём"}


def test_fit_context_cuts_to_the_cap():
    big = {"history": [{"role": "agent", "text": "а" * 300}] * 8, "screen": {"variants": [
        {"id": "cheapest", "risks": ["x" * 400] * 3, "facts": {"a": "y" * 400}, "out": {}, "back": {}}] * 3},
        "default_plan": {"variants": [{"id": "cheapest", "gives_up": ["z" * 500] * 3}] * 3,
                         "info_facts": [{"t": "w" * 3000}] * 3}}
    assert len(G._dumps(big)) > 12000
    out = G.fit_context(big, 6000)
    assert len(G._dumps(out)) <= 6000
    assert out["default_plan"]["variants"][0]["id"] == "cheapest"


# ---------------------------------------------------------------- direct reply

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_direct_reply_shows_the_default_plan(live):
    h = live
    r, s = first_turn(h)
    assert r["message"].startswith("Стамбул на 4 ночи — советую")
    ids = [c["id"] for c in r["cards"]["variants"]]
    assert ids == ["cheapest", "fastest", "comfort"]
    rec = [c["id"] for c in r["cards"]["variants"] if c["recommended"]]
    assert rec == [s.ctx()["default_plan"]["recommended_id"]]
    assert r["stage"] == "selection"
    assert any(a["type"] == "choose_variant" for a in r["actions"])
    assert [c["card"] for c in r["debug"]["calls"]] == ["understand", "guide"]   # no present card
    assert h.session().trace[-1]["guide"]["used"] is True


# ---------------------------------------------------------------- find_savings

def _savings_turn(h, *picks):
    first_turn(h)
    before = {v.id: v.totals.rub for v in h.session().variants}
    s = Script(h, *picks)
    h.understand(u(intent="refine"))
    t0 = time.perf_counter()
    r = h.turn("а можно подешевле?")
    return r, s, before, round((time.perf_counter() - t0) * 1000)


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_find_savings_one_night_fewer_is_cheaper_and_shown(live):
    h = live
    seen = {}

    def pick(c, req):
        res = c["tool_results"][0]
        assert res["tool"] == "find_savings" and res["auto"] is True      # code ran it before round 1
        lv = next(x for x in res["result"]["levers"] if x["lever"] == "nights_1")
        seen.update(result=res["result"], lever=lv)
        return reply(f"Дешевле всего — сократить поездку на ночь: {lv['change_ru']}, за {lv['total_rub']} ₽ — это на "
                     f"{lv['saving_rub']} ₽ меньше. Отдаёте одну ночь в Стамбуле.", rec=lv["variant_id"])

    r, s, before, ms = _savings_turn(h, pick)
    assert r.get("error") is None, r["message"]
    assert guide_trace(r)["used"] is True and guide_trace(r)["fallback"] is None
    res, lv = seen["result"], seen["lever"]
    cur = res["current"]
    assert cur["total_rub"] == min(before.values())                      # the trip they saw: its cheapest
    assert lv["total_rub"] < cur["total_rub"]
    assert lv["saving_rub"] == cur["total_rub"] - lv["total_rub"]       # exact, by code
    assert [x["total_rub"] for x in res["levers"]] == sorted(x["total_rub"] for x in res["levers"])
    assert sum(cur["price_parts"].values()) == cur["total_rub"]
    # the card shows the chosen lever first, recommended, with the exact total; the session holds it
    card = r["cards"]["variants"][0]
    assert card["id"] == lv["variant_id"] and card["recommended"] and card["totals"]["rub"] == lv["total_rub"]
    assert card["title"] == "На ночь короче"
    v = h.session().variant(lv["variant_id"])
    assert v is not None and v.facts["nights"] == 3 and v.facts["nights_changed_from"] == 4
    assert sum((st.price.rub or 0) for st in v.steps) == v.totals.rub
    assert h.session().brief.values()["dates"]["nights"] == 3          # the shown trip's brief
    assert f"{lv['total_rub']} ₽" in r["message"]
    assert [c["card"] for c in r["debug"]["calls"]] == ["understand", "guide"]
    # one tap books exactly that trip
    r2 = h.turn(action={"type": "choose_variant", "payload": {"variant_id": lv["variant_id"]}})
    assert r2.get("error") is None and r2["cart"]["total_rub"] == lv["total_rub"]
    tool = next(t for t in guide_trace(r)["tools"] if t["tool"] == "find_savings")
    print(f"\nfind_savings {tool['ms']} ms, levers {tool['levers_ms']}, turn {ms} ms, "
          f"context {guide_trace(r)['context_chars']}")
    assert tool["ms"] < 6000          # in process; the target is ≤ ~2 s on a dev machine (reported, not asserted)
    assert ms < 12000


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_cheaper_not_found_claim_is_blocked_then_fixed(live):
    h = live

    def lie(c, req):
        return reply("Дешевле не нашлось — это минимальная цена на эти даты.", rec=c["default_plan"]["recommended_id"])

    def fixed(c, req):
        assert "rejected by the checks" in req.system and "savings_claim" in req.system
        lv = c["tool_results"][0]["result"]["levers"][0]
        return reply(f"Можно дешевле: {lv['change_ru']} — {lv['total_rub']} ₽.", rec=lv["variant_id"])

    r, s, _, _ = _savings_turn(h, lie, fixed)
    # the first reply was blocked and the fix-up used (the script got both)
    assert len(s.requests) == 2
    assert guide_trace(r)["used"] is True
    assert [x["round"] for x in guide_trace(r)["rounds"]] == [1, "fixup"]
    assert r["message"].startswith("Можно дешевле")


# ---------------------------------------------------------------- guards and fallbacks

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_invented_number_fixup_then_pass(live):
    h = live
    first = lambda c, req: reply("Советую «Быстрее»: всего 12 345 ₽ за всё.", rec="fastest")  # noqa: E731
    second = lambda c, req: reply(  # noqa: E731
        f"Советую «Быстрее»: {next(v['total_rub'] for v in c['default_plan']['variants'] if v['id'] == 'fastest')} ₽ "
        f"за всё, прямой рейс.", rec="fastest")
    s = Script(h, first, second)
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert guide_trace(r)["used"] is True and len(s.requests) == 2
    hook = [x for x in r["debug"]["hooks"] if x.get("card") == "guide"]
    assert hook[0]["verdict"] == "block" and any(i["code"] == "invented_number" for i in hook[0]["issues"])
    assert hook[1]["verdict"] in ("ok", "warn")
    assert "12 345" not in r["message"]


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_guard_fails_twice_falls_back_to_the_pipeline(live):
    h = live
    bad = reply("Советую «Быстрее»: всего 12 345 ₽ за всё.", rec="fastest")
    s = Script(h, bad, bad)
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert len(s.requests) == 2
    g = guide_trace(r)
    assert g["used"] is False and g["fallback"].startswith("guard")
    # the pipeline's present card on the same results (here its own guard turns the echo into the code text)
    assert [c["card"] for c in r["debug"]["calls"]][:4] == ["understand", "guide", "guide", "present.variants"]
    assert "12 345" not in r["message"] and "79" in r["message"]
    assert [c["id"] for c in r["cards"]["variants"]] == ["cheapest", "fastest", "comfort"]


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_tool_error_falls_back(live, monkeypatch):
    h = live
    first_turn(h)

    def broken(*a, **k):
        raise ToolError("search_flights", "unavailable", "mock down", retryable=True)

    monkeypatch.setitem(G.TOOL_FUNCS, "check_road", broken)
    Script(h, calls(("check_road", {"to": "Анталья"})))
    h.understand(u(intent="info", info="а прямые в Анталью летают?"))
    r = h.turn("а прямые в Анталью летают?")
    g = guide_trace(r)
    assert g["used"] is False and g["fallback"].startswith("tool_error")
    assert [c["card"] for c in r["debug"]["calls"]][-1] == "present.info" and r.get("error") is None


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_schema_error_and_budget_fall_back(live, monkeypatch):
    h = live
    Script(h, "not json", "{\"calls\": [], \"reply\": {\"message\": 1}}")
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert guide_trace(r)["fallback"].startswith("llm")
    assert [c["card"] for c in r["debug"]["calls"]][-1] == "present.variants"
    # the 15 s turn budget: a turn that already used it never calls the guide
    monkeypatch.setitem(h.engine.skill.rules["guide"], "turn_budget_s", 0)
    Script(h)
    h.understand(u(intent="refine", set={"party.adults": 2}))
    r = h.turn("нас двое")
    assert guide_trace(r)["fallback"].startswith("budget")
    assert r.get("error") is None and "present.update" in [c["card"] for c in r["debug"]["calls"]]


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_unknown_recommended_id_is_a_guard_issue(live):
    h = live
    good = lambda c, req: reply("Советую «Дешевле» — самый бюджетный вариант.", rec="cheapest")  # noqa: E731
    s = Script(h, reply("Советую вот этот вариант — он лучший.", rec="custom-99"), good)
    h.understand(u(set=ISTANBUL))
    r = h.turn("Стамбул, 8–12 декабря, вдвоём")
    assert len(s.requests) == 2 and guide_trace(r)["used"] is True
    assert [c["id"] for c in r["cards"]["variants"] if c["recommended"]] == ["cheapest"]


# ---------------------------------------------------------------- dead ends and other tools

@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_dead_end_is_solved_by_a_rebuild(live):
    """Tickets only, plane fixed, no flight Екатеринбург — Краснодар in the data: the default plan fails (no_data);
    the guide rebuilds by train and shows it instead of «не получилось»."""
    h = live

    def first(c, req):
        assert c["default_plan"]["status"] == "failed" and c["default_plan"]["failure"]
        return calls(("check_road", {}), ("rebuild", {"set": {"prefs.transport": "train", "scope": ["trains"]}}))

    def second(c, req):
        res = {t["tool"]: t["result"] for t in c["tool_results"]}
        assert res["check_road"]["ok"] and "line_ru" in res["check_road"]
        assert res["rebuild"]["ok"], res["rebuild"]
        rec = res["rebuild"]["recommended_id"]
        total = next(v["total_rub"] for v in res["rebuild"]["variants"] if v["id"] == rec)
        return reply(f"Самолётов туда нет, зато есть поезд: {total} ₽ за билеты туда и обратно.", rec=rec)

    Script(h, first, second)
    h.understand(u(set=KRASNODAR_TICKETS))
    r = h.turn("Летим вдвоём из Екатеринбурга в Краснодар в августе, нужны только билеты")
    assert r.get("error") is None, r["message"]
    assert guide_trace(r)["used"] is True
    cards = r["cards"]["variants"]
    assert cards and all(c["id"].startswith("custom-") for c in cards)
    assert all(st["type"] == "train" for c in cards for st in c["steps"])
    assert h.session().brief.values()["prefs"]["transport"] == "train"


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_dead_end_falls_back_to_the_same_no_data_answer(live):
    h = live
    Script(h, "broken", "broken")
    h.understand(u(set=KRASNODAR_TICKETS))
    r = h.turn("Летим вдвоём из Екатеринбурга в Краснодар в августе, нужны только билеты")
    assert r["error"]["code"] == "no_data"
    assert guide_trace(r)["fallback"].startswith("llm")


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_check_road_and_place_info_and_compare(live):
    h = live
    first_turn(h)
    got = {}

    def first(c, req):
        return calls(("check_road", {"to": "Анталья"}), ("place_info", {"question": "Что посмотреть?"}),
                     ("compare", {"ids": ["cheapest", "fastest"]}))

    def second(c, req):
        got.update({t["tool"]: t["result"] for t in c["tool_results"]})
        return reply("Сравнил «Дешевле» и «Быстрее» — разница в дороге и отеле, советую «Быстрее».",
                     show="comparison", rec="fastest")

    Script(h, first, second)
    h.understand(u(intent="info", info="что выбрать?"))
    r = h.turn("что выбрать, и летают ли прямые в Анталью?")
    assert guide_trace(r)["used"] is True, json.dumps([x["issues"] for x in r["debug"]["hooks"] if x.get("card") == "guide"], ensure_ascii=False)
    road = got["check_road"]
    assert road["ok"] and road["route"].endswith("Анталья") and road["line_ru"].startswith("Москва — Анталья")
    assert road["ways"][0]["flights"] is not None
    assert got["place_info"]["ok"] and got["place_info"]["facts"]
    assert got["compare"]["ok"] and got["compare"]["ids"] == ["cheapest", "fastest"]
    assert r["comparison"]["variant_ids"] == ["cheapest", "fastest"] and r["comparison"]["recommended_id"] == "fastest"
    assert len(guide_trace(r)["tools"]) == 3


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_bad_tool_args_go_back_to_the_model(live):
    h = live
    first_turn(h)

    def second(c, req):
        res = c["tool_results"][0]["result"]
        assert res["ok"] is False and res["error"].startswith("bad args")
        return reply("Оставил варианты как есть — советую «Быстрее».", rec="fastest")

    Script(h, calls(("rebuild", {"set": {"hotel.pool": True}})), second)
    h.understand(u(intent="refine"))
    r = h.turn("а с бассейном есть?")
    assert guide_trace(r)["used"] is True


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_question_with_a_field_becomes_buttons(live):
    h = live
    q = {"text": "Сколько ночей хотите?", "field": "dates.nights",
         "choices": [{"label": "3 ночи", "value": 3}, {"label": "4 ночи", "value": 4}]}
    first_turn(h, lambda c, req: reply("Стамбул — советую «Быстрее». Сколько ночей хотите?", rec="fastest",
                                       question=q))
    r = h.engine.handle(h.sid, {"session_id": h.sid, "action": {"type": "answer_question", "payload": {
        "question_id": "q_guide", "value": 3}}})
    assert r.get("error") is None, r["message"]
    assert h.session().brief.values()["dates"]["nights"] == 3


@pytest.mark.parametrize("live", [GUIDE], indirect=True)
def test_find_destinations_shows_places(live):
    h = live

    def first(c, req):
        return calls(("find_destinations", {"set": {"traits.add": ["sea", "warm"], "dates.month": 3}}))

    def second(c, req):
        d = c["tool_results"][0]["result"]["destinations"]
        return reply(f"К тёплому морю в марте советую {d[0]['name']} — от {d[0]['price_from_rub']} ₽.",
                     show="destinations", rec=d[0]["id"])

    Script(h, first, second)
    h.understand(u(set={"origin.city": "Москва", "party.segment": "couple", "party.adults": 2}))
    r = h.turn("Хочу куда-нибудь, не знаю куда")
    assert guide_trace(r)["used"] is True and r["stage"] == "inspiration"
    assert r["cards"]["destinations"]
    assert all(a["type"] == "choose_destination" for a in r["actions"][:len(r["cards"]["destinations"])])


# ---------------------------------------------------------------- flag off: today's engine

SCENARIO = [("Стамбул, 8–12 декабря, вдвоём", u(set=ISTANBUL)),
            ("а можно подешевле?", u(intent="refine")),
            ("а что с погодой?", u(intent="info", info="а что с погодой?"))]


def _run(h):
    out = []
    for text, und in SCENARIO:
        h.understand(und)
        r = h.turn(text)
        r = {k: v for k, v in r.items() if k not in ("debug", "session_id")}
        out.append(r)
    return out


@pytest.mark.parametrize("live", ["stage1"], indirect=True)
def test_flag_off_never_touches_the_guide(live, monkeypatch):
    h = live
    for name in ("run_guide", "screen_snapshot", "eligible"):
        monkeypatch.setattr(G, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("guide used")))
    rs = _run(h)
    assert all(r.get("error") is None for r in rs)
    t = h.session().trace[-1]
    assert "guide" not in t and "guide" not in t.get("flags", {})
    assert "guide" not in h.engine.flags.as_dict()


@pytest.mark.parametrize("live", ["stage1"], indirect=True)
def test_flag_off_and_guide_fallback_answer_the_same(live, monkeypatch):
    h = live
    off = _run(h)
    # the same engine with the guide on, every guide call failing -> the pipeline path on every turn
    monkeypatch.setattr(h.engine, "flags", h.engine.flags.__class__(**{**h.engine.flags.as_dict(), "guide": True}))
    h.backend.scripts["guide"] = lambda req: (_ for _ in ()).throw(TimeoutError("down"))
    h.new()
    on = _run(h)
    assert all((t.get("guide") or {}).get("fallback") for t in h.session().trace)   # the guide ran and fell back
    assert off == on

