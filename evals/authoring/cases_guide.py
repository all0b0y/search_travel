"""Level-2 cases for the `guide` card (guide mode, AGENT_PLAN.md decisions 23-26), our own invented trips:

- g1: Москва → Сочи, 5 nights in December for two; «дороговато, а можно подешевле?» → the input holds the auto
  find_savings result: the reply recommends a savings lever and names the cheapest option's total, never «дешевле не
  нашлось».
- g2: the same trip after the guide showed the cheapest lever; «всё равно дорого, давай ещё дешевле» → savings from
  the new trip: a cheaper lever or a cheaper place, never «если готовы поднять бюджет» alone.
- g3: Екатеринбург → Краснодар, tickets only, plane fixed: the default plan failed (no flight in the data) and code
  rebuilt it by train before round 1 (auto) → the guide shows the train trips in one reply, never «не получилось».
- g4: «хочу к тёплому морю в феврале, не знаю куда» from Москва → places: the default plan's or find_destinations'.
- g5: a shown Kazan trip, «а какая там погода будет?» → a plain answer (show none or the shown variants) from the
  info facts, no tools needed.
- g6: a shown Kazan trip, «а в Самару прямые рейсы в эти дни есть?» → check_road for Самара (not «нет данных»).

Writes 6 files to evals/cases/ and deletes nothing:

    .venv/bin/python -m evals.authoring.cases_guide

The inputs are built by the real engine offline (in-process Ostrovok mock over data/ostrovok_catalog.sqlite, real
open data, a scripted FakeBackend whose guide card records its input, the clock pinned to 2026-10-25 12:00 MSK).
"""
import json
import os
import tempfile
from pathlib import Path

from evals.authoring.cases_l3fix import a, case, pred
from evals.authoring.cases_location import NOW, _u
from evals.card_inputs import validate_card_input
from evals.common import CASES_DIR, ROOT, write_json

SOCHI = {"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-12-14", "dates.end": "2026-12-19",
         "party.segment": "couple", "party.adults": 2}
KAZAN = {"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
         "party.segment": "friends", "party.adults": 3}
KRASNODAR = {"origin.city": "Екатеринбург", "destination.name": "Краснодар", "dates.month": 8, "dates.nights": 7,
             "party.segment": "couple", "party.adults": 2, "scope": ["flights"], "prefs.transport": "plane"}
NOT_FOUND = r"дешевле\s+(\S+\s+){0,2}(не\s+нашл|нет\b|не\s+найт)|не\s+нашл\w*\s+(\S+\s+){0,2}дешевле|ничего\s+дешевле"
RAISE_ONLY = r"^[^.]*готовы\s+поднять\s+бюджет[^.]*\.?\s*$"
DEAD_REPEAT = r"не\s+получилось|не\s+удалось\s+собрать"
NO_DATA = r"нет\s+данных|не\s+знаю"
REPLY_COMMON = [a("numbers_subset", path="/reply"), pred("guide_recommended_shown"), pred("guide_one_question"),
                pred("guide_on_reply", check="russian_only"), pred("guide_on_reply", check="tone_no_bureaucratese"),
                pred("guide_on_reply", check="at_most_one_joke"), pred("guide_on_reply", check="money_risks_plain")]


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
    engine = Engine(backend=backend, jev=FakeJev(fail=True), ostrovok=InProcessOstrovokClient(),
                    opendata=OpenData(path=str(ROOT / "data" / "opendata.sqlite"),
                                      overlay=str(Path(tmp) / "no_opendata_extra.sqlite")),
                    flags=parse_flags("stage2,guide"), store=SessionStore(":memory:"), profiles=ProfileStore(":memory:"),
                    prefetcher=Prefetcher(synchronous=True), lazy_city=False, files_dir=str(tmp))
    return engine, backend


class Guide:
    """The guide card's scripted turn: records the round-1 input, then answers with `fn(input)`."""

    def __init__(self, backend):
        self.inputs, self.next = [], None
        backend.scripts["guide"] = self

    def __call__(self, req):
        data = json.loads(req.user)
        self.inputs.append(data)
        return self.next(data)


def _default_reply(data):
    dp = data["default_plan"]
    if dp.get("variants"):
        return {"calls": [], "reply": {"message": "Вот варианты — советую тот, что отмечен.", "show": "variants",
                                       "recommended_id": dp["recommended_id"], "question": None}}
    if dp.get("destinations"):
        return {"calls": [], "reply": {"message": "Вот места, которые подходят.", "show": "destinations",
                                       "recommended_id": dp["destinations"][0]["id"], "question": None}}
    return {"calls": [], "reply": {"message": "Понял, подскажите чуть подробнее.", "show": "none",
                                   "recommended_id": None, "question": None}}


def _cheapest_lever(data):
    lv = min((x for t in data.get("tool_results") or [] for x in t["result"].get("levers") or []),
             key=lambda x: x["total_rub"])
    leave = (lv["variant"].get("facts") or {}).get("out_leave_ru")
    return {"calls": [], "reply": {"message": f"Дешевле всего — {lv['change_ru']}: {lv['total_rub']} ₽."
                                              + (f" Важно: {leave}." if leave and leave not in lv["change_ru"] else ""),
                                   "show": "variants", "recommended_id": lv["variant_id"], "question": None}}


def _turn(engine, backend, g, sid, text, reply, intent="new_trip", info=None, **patch):
    backend.add("understand", _u(intent=intent, set_=patch, info=info))
    g.next = reply
    n = len(g.inputs)
    r = engine.handle(sid, {"session_id": sid, "text": text})
    assert r.get("error") is None or intent == "new_trip", r["message"]
    return g.inputs[n] if len(g.inputs) > n else None, r


def build_inputs():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        g = Guide(backend)
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, g, sid, "Москва — Сочи, 14–19 декабря, вдвоём", _default_reply, **SOCHI)
        out["savings"], _ = _turn(engine, backend, g, sid, "Дороговато выходит, а можно подешевле?", _cheapest_lever,
                                  intent="refine")
        out["still"], _ = _turn(engine, backend, g, sid, "Всё равно дорого, давай ещё дешевле", _default_reply,
                                intent="refine")
        sid = engine.new_session(profile_id="demo", now=NOW)
        out["dead_end"], _ = _turn(engine, backend, g, sid, "Летим вдвоём из Екатеринбурга в Краснодар в августе на "
                                                            "неделю, нужны только билеты", _default_reply, **KRASNODAR)
        sid = engine.new_session(profile_id="demo", now=NOW)
        out["vague"], _ = _turn(engine, backend, g, sid, "Хочу к тёплому морю в феврале, вдвоём, не знаю куда",
                                _default_reply, **{"origin.city": "Москва", "traits.add": ["sea", "warm"],
                                                   "dates.month": 2, "party.segment": "couple", "party.adults": 2})
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, g, sid, "Казань на выходные 20–23 ноября, нас трое", _default_reply, **KAZAN)
        q = "А какая там погода будет в эти дни?"
        out["weather"], _ = _turn(engine, backend, g, sid, q, _default_reply, intent="info", info=q)
        q = "А в Самару прямые рейсы в эти дни есть?"
        out["road"], _ = _turn(engine, backend, g, sid, q, _default_reply, intent="info", info=q)
    return out


def cases(inputs):
    sav = inputs["savings"]
    assert sav["tool_results"][0]["tool"] == "find_savings" and sav["tool_results"][0]["result"]["levers"], \
        "the Sochi trip must have savings levers"
    return [
        case("g1_guide_savings_recommends_cheapest", "guide", "guide", sav,
             [pred("guide_replies"), pred("guide_cheapest_option"), a("path_equals", path="/reply/show", value="variants"),
              a("regex_absent", path="/reply/message", pattern=NOT_FOUND)] + REPLY_COMMON,
             "«Дороговато, а можно подешевле?» on a Sochi trip: code already ran find_savings (auto); the guide "
             "recommends a lever and names the cheapest option's total with what it costs, never «дешевле не нашлось»."),
        case("g2_guide_still_expensive_goes_further", "guide", "guide", inputs["still"],
             [pred("guide_replies"), pred("guide_cheapest_option"),
              a("regex_absent", path="/reply/message", pattern=RAISE_ONLY),
              a("regex_absent", path="/reply/message", pattern=NOT_FOUND)] + REPLY_COMMON,
             "«Всё равно дорого, давай ещё дешевле» after the cheapest lever was shown: savings from the new trip "
             "(fewer nights, a cheaper hotel, a cheaper place) — never only «если готовы поднять бюджет».",
             unsure="When find_savings finds no lever below the shown trip, a cheaper place or an honest «дешевле этой "
                    "поездки нет» with the price parts is right; the regex then only bans the claim when places exist."),
        case("g3_guide_dead_end_acts", "guide", "guide", inputs["dead_end"],
             [pred("guide_replies"), a("path_equals", path="/reply/show", value="variants"),
              a("regex_absent", path="/reply/message", pattern=DEAD_REPEAT)] + REPLY_COMMON,
             "Tickets only, plane fixed, no flight Екатеринбург — Краснодар: the default plan failed and code already "
             "rebuilt it by train before round 1 (auto, default_plan.dead_end). The guide replies in this one call, "
             "shows the train trips and says plainly that it is the train — never «не получилось» again."),
        case("g4_guide_vague_wish_places", "guide", "guide", inputs["vague"],
             [pred("guide_calls", tools=["find_destinations"], reply_ok=True),
              a("path_in", path="/reply/show", values=["destinations"])] + REPLY_COMMON,
             "A vague wish (warm sea in February, no place): places from the default plan or find_destinations, one "
             "recommended with its «от» price."),
        case("g5_guide_weather_plain_answer", "guide", "guide", inputs["weather"],
             [pred("guide_replies"), a("path_in", path="/reply/show", values=["none", "variants"]),
              a("regex_absent", path="/reply/message", pattern=NO_DATA)] + REPLY_COMMON,
             "A weather question on a shown Kazan trip: the climate facts are in the default plan — a plain answer, "
             "no tools, numbers from the facts."),
        case("g6_guide_direct_flights_checks_the_road", "guide", "guide", inputs["road"],
             [pred("guide_calls", tools=["check_road"])],
             "«А в Самару прямые рейсы в эти дни есть?» on a Kazan trip: another pair the default plan never searched "
             "→ check_road for Самара, never «нет данных»."),
    ]


def main():
    built = cases(build_inputs())
    for c in built:
        errors = validate_card_input(c["card"], c["input"])
        assert not errors, (c["id"], errors)
        write_json(CASES_DIR / f"{c['id']}.json", c)
    print(f"wrote {len(built)} cases: {[c['id'] for c in built]}")


if __name__ == "__main__":
    main()
