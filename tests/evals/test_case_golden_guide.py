"""The guide-mode cases (evals/authoring/cases_guide.py) are satisfiable: a good output passes every assertion, the
failure each case is about fails it."""
import json

from evals.common import CASES_DIR

from .test_case_golden import run


def _input(cid):
    return json.loads((CASES_DIR / f"{cid}.json").read_text(encoding="utf-8"))["input"]


def _reply(message, show="variants", rec=None, question=None):
    return {"calls": [], "reply": {"message": message, "show": show, "recommended_id": rec, "question": question}}


def _rub(n):
    return f"{n:,}".replace(",", " ") + " ₽"


def _levers(inp):
    return sorted((lv for t in inp["tool_results"] if t["tool"] == "find_savings" for lv in t["result"]["levers"]),
                  key=lambda lv: lv["total_rub"])


def test_g1_savings():
    cid = "g1_guide_savings_recommends_cheapest"
    inp = _input(cid)
    lv = _levers(inp)
    cheap, other = lv[0], lv[1]
    good = _reply(f"Дешевле всего — {cheap['change_ru']}: {_rub(cheap['total_rub'])}. Если сутки в поезде — не "
                  f"вариант, есть {other['change_ru']} за {_rub(other['total_rub'])}. Берёте?",
                  rec=other["variant_id"])
    assert run(cid, good) == (True, [])
    assert not run(cid, _reply("Дешевле не нашлось — это минимальная цена.", rec=inp["default_plan"]
                               ["recommended_id"]))[0]
    assert not run(cid, _reply(f"Советую вариант за {_rub(other['total_rub'])}.", rec="fastest"))[0]
    assert not run(cid, _reply(f"Можно за 12 345 ₽ — {cheap['change_ru']}.", rec=cheap["variant_id"]))[0]


def test_g2_further():
    cid = "g2_guide_still_expensive_goes_further"
    inp = _input(cid)
    cheap = _levers(inp)[0]
    good = _reply(f"Ещё дешевле — {cheap['change_ru']}: {_rub(cheap['total_rub'])}, но {cheap['gives_up_ru']}.",
                  rec=cheap["variant_id"])
    assert run(cid, good) == (True, [])
    assert not run(cid, _reply("Если готовы поднять бюджет, подберу что-то другое.", show="none"))[0]


def test_g3_dead_end():
    cid = "g3_guide_dead_end_acts"
    inp = _input(cid)
    rb = next(t for t in inp["tool_results"] if t["tool"] == "rebuild" and t.get("auto"))["result"]
    total = next(v["total_rub"] for v in rb["variants"] if v["id"] == rb["recommended_id"])
    good = _reply(f"Самолётов из Екатеринбурга в Краснодар в данных нет — собрал поездом: {_rub(total)} за билеты "
                  f"туда и обратно. Берёте?", rec=rb["recommended_id"])
    assert run(cid, good) == (True, [])
    # the fix is already in the input: another tool round, stays without cards or «не получилось» all fail
    again = {"calls": [{"tool": "rebuild", "args": {"set": {"prefs.transport": "train"}}}], "reply": None}
    assert not run(cid, again)[0]
    assert not run(cid, _reply("Самолётов в Краснодар нет, поездом та же поездка — от 21 310 ₽: нажмите «Поехать "
                               "поездом».", show="none"))[0]
    assert not run(cid, _reply("Собрать поездку не получилось.", rec=rb["recommended_id"]))[0]


def test_g4_g5_g6():
    inp = _input("g4_guide_vague_wish_places")
    d = inp["default_plan"]["destinations"][0]
    assert run("g4_guide_vague_wish_places", _reply(f"Советую {d['name']} — от {_rub(d['price_from_rub'])}.",
                                                    show="destinations", rec=d["id"])) == (True, [])
    assert not run("g4_guide_vague_wish_places", _reply("Вот варианты.", show="variants", rec="cheapest"))[0]
    clim = next(f for f in _input("g5_guide_weather_plain_answer")["default_plan"]["info_facts"]
                if f.get("kind") == "climate")["data"]
    msg = f"Днём около {str(clim['air_max_c']).replace('.', ',')}° — одевайтесь потеплее."
    assert run("g5_guide_weather_plain_answer", _reply(msg, show="none")) == (True, [])
    assert not run("g5_guide_weather_plain_answer", _reply("Про погоду у меня нет данных.", show="none"))[0]
    assert run("g6_guide_direct_flights_checks_the_road",
               {"calls": [{"tool": "check_road", "args": {"to": "Самара"}}], "reply": None}) == (True, [])
    assert not run("g6_guide_direct_flights_checks_the_road", _reply("Про рейсы в Самару нет данных.", show="none"))[0]
