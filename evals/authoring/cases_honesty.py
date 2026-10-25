"""Level-2 cases for the honesty classes of an independent evaluation, written in our own words (no text from any
sealed set):

- understand: open dates stay a month («числа пока плавают»), a service not sold here is out_of_scope with no trip
  patch (a visa, a rental car «больше ничего не надо»), «без ночных поездов» at selection is prefs.no_night_flights;
- present (inputs built by the real engine offline): night trains dropped by code (the claim is allowed) and not
  droppable (only sleepers: the claim must not appear), a direct-only wish that removed every flight (the card
  names the connecting price), a place no transport reaches (other places offered), a train built because no plane
  flies there, a kitchen request.

Writes 10 files to evals/cases/ and deletes nothing:

    .venv/bin/python -m evals.authoring.cases_honesty

The understand inputs are written by hand; the present inputs come from the engine (in-process Ostrovok mock over
data/ostrovok_catalog.sqlite, real open data, a scripted FakeBackend, the clock pinned to 2026-10-25 12:00 MSK).
"""
import tempfile

from evals.authoring.cases_l3fix import MOSCOW, a, brief, case, dates, party, pred, prefs, understand_input
from evals.authoring.cases_location import NOW, _engine, _last_input, _u
from evals.card_inputs import validate_card_input
from evals.common import CASES_DIR, write_json

SPB = ("Санкт-Петербург", ["LED"])
SHOWN = [{"label": "1", "kind": "variant", "id": "cheapest", "title": "Дешевле"},
         {"label": "2", "kind": "variant", "id": "comfort", "title": "Комфортнее"}]
DATES = {"dates.start": "2026-11-20", "dates.end": "2026-11-23", "party.segment": "couple", "party.adults": 2}

PRESENT_COMMON = [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
                  pred("at_most_one_joke"), pred("money_risks_plain"), pred("no_upsell_without_candidate"),
                  a("max_items", path="/upsell", max=2), pred("recommended_in_items")]
NEVER = r"билет\w*\s+не\s+нашл|не\s+смотрел\w*\s+билет|нет\s+аэропорт|аэропорт\w*\s+нет"

karelia_shown = brief(origin=SPB[0], iata=SPB[1], dest="Карелия (Петрозаводск)", dest_id="petrozavodsk",
                      country="Россия", d=dates("2026-11-20", "2026-11-23", None, 3), p=party("couple", 2),
                      pr=prefs(transport="train"), scope=["trains", "stay", "transfers"])


# ---------------------------------------------------------------- understand

UNDERSTAND = [
    case("h1_open_dates_holiday", "B", "understand",
         understand_input("На ноябрьские в Казань из Москвы, вдвоём, но числа пока плавают"),
         [a("path_in", path="/intent", values=["new_trip"]),
          a("path_equals", path="/patch/set/destination.name", value="Казань"),
          a("path_equals", path="/patch/set/dates.month", value=11),
          pred("number_between", path="/patch/set/dates.flexible_days", min=1, max=7),
          a("path_absent", path="/patch/set/dates.start"), a("path_absent", path="/patch/set/dates.end"),
          pred("patch_has_no_assumptions", allowed=["destination.name", "dates.month", "dates.flexible_days",
                                                    "origin.city", "party.segment", "party.adults"])],
         "«числа пока плавают» with a holiday: the dates are open — dates.month 11 + dates.flexible_days, never "
         "exact start/end; the engine picks the dates as an assumption chip."),
    case("h2_visa_only", "edge", "understand", understand_input("Оформите мне, пожалуйста, визу в Китай"),
         [a("path_equals", path="/intent", value="out_of_scope"), pred("patch_empty")],
         "A visa is not sold here: out_of_scope with an empty patch (the reply says what it cannot do and what it "
         "can); no destination is set from «в Китай»."),
    case("h3_car_rental_only", "edge", "understand",
         understand_input("Машину в аренду в Калининграде на неделю, больше ничего не надо"),
         [a("path_equals", path="/intent", value="out_of_scope"), pred("patch_empty")],
         "Only a rental car (not sold) and «больше ничего не надо»: out_of_scope, no trip patch even though a city "
         "is named; code also guards this (rules.json unsold / unsold_only_cues)."),
    case("h4_refine_no_night_trains", "E", "understand",
         understand_input("Без ночных поездов, пожалуйста", stage="selection", b=karelia_shown, shown=SHOWN,
                          assumed=["party.segment"], assumptions=[("party.segment", "вдвоём")]),
         [a("path_equals", path="/intent", value="refine"),
          a("path_equals", path="/patch/set/prefs.no_night_flights", value=True),
          a("path_absent", path="/patch/set/destination.name"),
          pred("patch_has_no_assumptions", allowed=["prefs.no_night_flights", "prefs.transport"])],
         "«без ночных поездов» at selection: prefs.no_night_flights true — it covers every leg (flights and "
         "trains); ranking then drops night and overnight trains."),
]


# ---------------------------------------------------------------- present (engine-built inputs)

def _turn(engine, backend, sid, text, **patch):
    backend.add("understand", _u(intent=patch.pop("intent", "new_trip"), set_=patch))
    engine.handle(sid, {"session_id": sid, "text": text})


def build_present_inputs():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        # night trains dropped: SPb -> Petrozavodsk by train, then «без ночных поездов»
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Петрозаводск поездом, 20–23 ноября вдвоём", **{
            "origin.city": "Санкт-Петербург", "destination.name": "Петрозаводск", "prefs.transport": "train", **DATES})
        _turn(engine, backend, sid, "Без ночных поездов, пожалуйста", intent="refine",
              **{"prefs.no_night_flights": True})
        out["night_dropped"] = _last_input(backend, "present.update")
        # night trains not droppable: Moscow -> Kazan by train (only sleepers)
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Казань поездом, 20–23 ноября вдвоём", **{
            "origin.city": "Москва", "destination.name": "Казань", "prefs.transport": "train", **DATES})
        _turn(engine, backend, sid, "А можно без ночных поездов?", intent="refine", **{"prefs.no_night_flights": True})
        out["night_kept"] = _last_input(backend, "present.update")
        # direct only: Ekaterinburg -> Antalya
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Анталья из Екатеринбурга 20–23 ноября, только прямые рейсы", **{
            "origin.city": "Екатеринбург", "destination.name": "Анталья", "prefs.direct_only": True, **DATES})
        out["direct_only"] = _last_input(backend, "present.variants")
        # nothing reachable: Saint Petersburg -> Gelendzhik
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Геленджик из Питера 20–23 ноября", **{
            "origin.city": "Санкт-Петербург", "destination.name": "Геленджик", **DATES})
        out["nothing"] = _last_input(backend, "present.variants")
        # no plane: Moscow -> Yaroslavl, built by train
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Ярославль 20–23 ноября вдвоём", **{
            "origin.city": "Москва", "destination.name": "Ярославль", **DATES})
        out["train_instead"] = _last_input(backend, "present.variants")
        # kitchen: Moscow -> Saint Petersburg
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Питер 20–23 ноября вдвоём, нужна кухня в номере", **{
            "origin.city": "Москва", "destination.name": "Санкт-Петербург", "constraints.add": ["kitchen"], **DATES})
        out["kitchen"] = _last_input(backend, "present.variants")
    cards = {"night_dropped": "present.update", "night_kept": "present.update"}
    for name, inp in out.items():
        errors = validate_card_input(cards.get(name, "present.variants"), inp)
        assert not errors, (name, errors)
    nd = out["night_dropped"]
    assert any(c["field"] == "prefs.no_night_flights" for c in nd["changes"]), nd["changes"]
    assert not any(i["facts"].get("out_night") or i["facts"].get("back_night") for i in nd["items"])
    nk = out["night_kept"]
    assert all(i["facts"].get("out_night") or i["facts"].get("back_night") for i in nk["items"])
    assert out["direct_only"]["items"][0]["facts"]["transport_gap"] == "direct_only"
    assert out["nothing"]["items"][0]["facts"]["transport_gap"] == "none" and out["nothing"]["other_places"]
    assert out["train_instead"]["items"][0]["facts"]["transport_gap"] == "train_instead"
    assert all(i["facts"].get("kitchen") is True for i in out["kitchen"]["items"])
    return out


def _price_re(rub):
    s = f"{int(rub):,}".replace(",", " ")
    return s.replace(" ", r"[\s\u00a0\u202f]?")


def present_cases():
    inp = build_present_inputs()
    nd, nk, do, no, ti, kt = (inp[k] for k in ("night_dropped", "night_kept", "direct_only", "nothing",
                                                "train_instead", "kitchen"))
    low = do["items"][0]["facts"]["transport_gap_from_rub"]
    places = [p["name"] for p in no["other_places"]]
    return [
        case("h5_update_night_trains_dropped", "E", "present.update", nd,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[r"ночн", r"дн[её]м", r"дневн"]),
                 pred("no_unbacked_claims", card="present.update"),
                 a("regex_absent", path="/message", pattern=NEVER),
                 pred("asks_at_most", n=len(nd.get("questions") or []))],
             "«без ночных поездов»: code dropped the 23:15 train (a prefs.no_night_flights row, no item with "
             "facts.out_night / back_night) — the card may say the night trains are gone; every claim it makes is "
             "backed (no_unbacked_claims)."),
        case("h6_update_night_trains_kept", "E", "present.update", nk,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[r"ночн"]),
                 a("regex_absent", path="/message",
                   pattern=r"убрал\w*\s+(все\s+)?ночн|без\s+ночных\s+(поезд|рейс)|все\s+[^.!?]{0,40}дн[её]м"),
                 pred("no_unbacked_claims", card="present.update"),
                 pred("asks_at_most", n=len(nk.get("questions") or []))],
             "Moscow → Kazan by train has only sleepers on these dates: the filter is relaxed (risk «Дневных поездов "
             "на эти даты нет — в варианте ночной поезд»); the card says so plainly and never claims the night "
             "trains were removed."),
        case("h7_variants_direct_only_connecting", "C", "present.variants", do,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[r"пересадк"]),
                 pred("mentions_any", words=[_price_re(low)]),
                 a("regex_absent", path="/message", pattern=NEVER),
                 pred("no_unbacked_claims"),
                 pred("asks_at_most", n=len(do.get("questions") or []))],
             f"«только прямые» removed every flight Ekaterinburg → Antalya, connecting ones exist: the card says "
             f"there are no direct flights and names the connecting trip «от {low} ₽» (facts.transport_gap_ru, the "
             f"«Показать с пересадкой» button), never «билеты не нашлись»."),
        case("h8_variants_nothing_reachable", "edge", "present.variants", no,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[r"ни\s+рейсов|ни\s+поездов|нет\s+ни"]),
                 pred("mentions_any", words=["|".join(n[:5] for n in places)]),
                 a("regex_absent", path="/message", pattern=NEVER + r"|одной\s+кнопк"),
                 pred("no_unbacked_claims"),
                 pred("asks_at_most", n=len(no.get("questions") or []))],
             f"Saint Petersburg → Gelendzhik: no flight and no train in the data (Gelendzhik has an airport, so "
             f"never «нет аэропорта»); the stays are what they are, and the places with transport ({', '.join(places)}) "
             f"are named with their price — buttons below."),
        case("h9_variants_train_instead", "C", "present.variants", ti,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[r"поезд"]),
                 a("regex_absent", path="/message", pattern=NEVER),
                 pred("no_unbacked_claims"),
                 pred("asks_at_most", n=len(ti.get("questions") or []))],
             "Moscow → Yaroslavl: no plane flies there, so code built the trip by train (risk transport_switched "
             "«Самолётов … нет — собрал поездом»); the card says it goes by train."),
        case("h10_variants_kitchen", "D", "present.variants", kt,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[r"кухн"]),
                 pred("no_unbacked_claims"),
                 a("regex_absent", path="/message", pattern=r"не\s+провер|нет\s+данных\s+о\s+кухн"),
                 pred("asks_at_most", n=len(kt.get("questions") or []))],
             "«нужна кухня в номере»: constraint kitchen filtered the stays (every item has facts.kitchen true — "
             "studios and apartments); the card says the rooms have a kitchen."),
    ]


def main():
    for c in UNDERSTAND + present_cases():
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
