"""Level-2 present cases for long-distance trains (the mock sells Russian pairs up to 2500 km; over 1000 km only
sleepers, up to ~two days on board), in our own words:

- lt1: Москва → Анапа in July for a family — no plane flies there in the data, so code built the trip by train: the
  outbound sleeper leaves the day before check-in and arrives on it, the return leaves on check-out and arrives the
  next day. The card says the time on board and the leave day from the code-made facts (out_on_road_ru,
  out_road_ru, back_road_ru), never hours like «туда 23,0 ч», and no number it was not given.
- lt2: a budget-led inspiration from Москва to the sea in July (7 nights, 120 000 ₽): the south fits by train —
  every such item carries facts.long_train / on_road_ru / road_ru, and the card names the train with its time on
  board («Сочи поездом, в пути 1 сутки 4 ч»), never «поездом 25,6 ч».

Writes 2 files to evals/cases/ and deletes nothing:

    .venv/bin/python -m evals.authoring.cases_long_trains

The inputs are built by the real engine offline (in-process Ostrovok mock over data/ostrovok_catalog.sqlite, real
open data, a scripted FakeBackend, the clock pinned to 2026-10-25 12:00 MSK).
"""
import re
import tempfile
from datetime import date

from agent.payloads import MONTHS_GEN
from evals.authoring.cases_l3fix import a, case, pred
from evals.authoring.cases_location import NOW, _engine, _last_input, _u
from evals.card_inputs import validate_card_input
from evals.common import CASES_DIR, write_json

PRESENT_COMMON = [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
                  pred("at_most_one_joke"), pred("money_risks_plain"), pred("no_upsell_without_candidate")]
# a long train's time said as decimal hours (the card must use the ready «1 сутки 3 ч» line instead)
DECIMAL_ROAD = r"(?<![\d,])(1[89]|[2-9]\d),\d\s*ч"


def build_inputs():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        sid = engine.new_session(profile_id="demo", now=NOW)
        backend.add("understand", _u(set_={
            "origin.city": "Москва", "destination.name": "Анапа", "dates.start": "2027-07-10",
            "dates.end": "2027-07-17", "party.segment": "family", "party.adults": 2, "party.children": 1,
            "party.children_ages": [7]}))
        engine.handle(sid, {"session_id": sid, "text": "В Анапу на море с ребёнком 7 лет, 10–17 июля"})
        out["anapa"] = _last_input(backend, "present.variants")
        sid = engine.new_session(profile_id="demo", now=NOW)
        backend.add("understand", _u(set_={
            "origin.city": "Москва", "party.segment": "couple", "party.adults": 2, "budget_rub.max": 120000,
            "dates.month": 7, "dates.nights": 7, "traits.add": ["sea"]}))
        engine.handle(sid, {"session_id": sid, "text": "Хотим на море в июле на неделю, вдвоём, до 120 тысяч"})
        out["inspiration"] = _last_input(backend, "present.destinations")
    assert not validate_card_input("present.variants", out["anapa"])
    assert not validate_card_input("present.destinations", out["inspiration"])
    for i in out["anapa"]["items"]:
        f = i["facts"]
        assert f["out_long_train"] and f["out_days_before_checkin"] >= 1 and f["transport_gap"] == "train_instead", f
    assert any(i["facts"].get("long_train") for i in out["inspiration"]["items"])
    return out


def _day_month(iso):
    d = date.fromisoformat(iso[:10])
    return rf"(?<!\d){d.day}\s+{MONTHS_GEN[d.month - 1]}"


def _words(text):
    return re.escape(text).replace(r"\ ", r"\s+")


def cases():
    inp = build_inputs()
    anapa, insp = inp["anapa"], inp["inspiration"]
    hint = next(i for i in anapa["items"] if i["id"] == anapa["recommended_hint"])
    f = hint["facts"]
    road = [_words(f["out_on_road_ru"]), _words(f["back_on_road_ru"])]
    leave = _day_month(f["out_date"])
    long_items = [i for i in insp["items"] if i["facts"].get("long_train")]
    long_road = [_words(i["facts"]["on_road_ru"]) for i in long_items]
    names = "|".join(i["name"][:4] for i in long_items)
    return [
        case("lt1_variants_long_train_anapa", "C", "present.variants", anapa,
             PRESENT_COMMON + [
                 a("max_items", path="/upsell", max=2), pred("recommended_in_items"),
                 pred("mentions_any", words=[r"поезд"]),
                 pred("mentions_any", words=[r"(?<![\d,])(" + "|".join(road) + ")"]),
                 pred("mentions_any", words=[leave, r"за\s+день\s+до\s+заезда"]),
                 a("regex_absent", path="/message", pattern=DECIMAL_ROAD),
                 a("regex_absent", path="/message", pattern=r"перел[её]т|\bлететь|\bвылет"),
                 pred("no_unbacked_claims"),
                 pred("asks_at_most", n=len(anapa.get("questions") or []))],
             f"Moscow → Anapa, 10–17 July, a family: no plane flies to Anapa in the data, so code built the trip by "
             f"train (risk transport_switched). The recommended «{hint['title']}» leaves {f['out_date']} and arrives on "
             f"the check-in day; the card says it goes by train, the time on board from out_on_road_ru / "
             f"back_on_road_ru («в пути {f['out_on_road_ru']}») and the leave day from out_road_ru — never decimal "
             f"hours for these legs and never a flight."),
        case("lt2_destinations_south_by_train_in_budget", "A", "present.destinations", insp,
             [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
              pred("at_most_one_joke"), pred("money_risks_plain"), a("max_items", path="/upsell", max=0),
              a("path_equals", path="/recommended_id", value=insp["items"][0]["id"]),
              pred("mentions_any", words=[r"поезд"]), pred("mentions_any", words=[names]),
              pred("mentions_any", words=[r"в\s+пути\s+(" + "|".join(long_road) + ")"]),
              a("regex_absent", path="/message", pattern=DECIMAL_ROAD),
              a("regex_absent", path="/message", pattern=r"ничего не укладывается"),
              pred("asks_at_most", n=len(insp.get("questions") or []))],
             "A budget-led inspiration from Moscow (sea, July, 7 nights, 120 000 ₽): the south fits by long-distance "
             "train (facts.long_train, on_road_ru, road_ru «поездом, в пути … ; выезд … — за день до заезда»). The "
             "card names each train place with its transport and its time on board as written, never decimal "
             "hours, and never «ничего не укладывается»."),
    ]


def main():
    for c in cases():
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
