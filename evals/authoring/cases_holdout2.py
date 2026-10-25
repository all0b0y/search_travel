"""Level-2 cases for the second held-out level-3 run (evals/runs/20261025-135109-L3-holdout):

- h01_sun_from_novosibirsk: «А хостел в нашем возрасте как-то не очень» kept a hostel as «Дешевле», the update
  card then said «убрал хостелы» without code having done it, and «…или в другом городе?» was ignored;
- h10_student_25k: a budget of 25 000 ₽ from Ekaterinburg got flights only (all above it) while Kazan / Samara fit
  by train, and «что входит в эти цены?» got «нет данных».

Writes 5 files to evals/cases/ and deletes nothing:

    .venv/bin/python -m evals.authoring.cases_holdout2

The understand input is written by hand; the present inputs are built by the real engine offline (in-process
Ostrovok mock over data/ostrovok_catalog.sqlite, real open data, a scripted FakeBackend, the clock pinned to
2026-10-25 12:00 MSK), so they carry exactly what the engine sends: destination items with facts.transport /
transport_ru / price_includes, the `destinations` info record, the constraints row of no_hostel and the
`other_places` key.
"""
import tempfile

from evals.authoring.cases_l3fix import a, brief, case, dates, party, pred, prefs, understand_input
from evals.authoring.cases_location import NOW, _engine, _last_input, _u
from evals.card_inputs import validate_card_input
from evals.common import CASES_DIR, write_json

NSK = ("Новосибирск", ["OVB"])
SHOWN = [{"label": "1", "kind": "variant", "id": "cheapest", "title": "Дешевле"},
         {"label": "2", "kind": "variant", "id": "fastest", "title": "Быстрее"}]
H01_T1 = "Устали от слякоти. Солнца бы в декабре, мы с мужем, бюджет скромный"
H01_T3 = ("Быстрее слишком дорого, у нас максимум 180 тысяч. А хостел в нашем возрасте как-то не очень. Нам вообще "
          "хотелось бы 8 ночей. Можно что-то среднее, с нормальным отелем и без долгой пересадки? Объясните попроще.")
H01_T4 = ("Хостел нам всё равно не подходит, а бюджет поднимать я не хочу. Может, есть нормальный отель в пределах "
          "180 тысяч в другие даты декабря или в другом городе? Объясните попроще.")
H10_T1 = "Студент, 25 тысяч на всё, куда можно смотаться на 3 дня из Екатеринбурга?"
H10_T2 = "а что входит в эти цены, перелёт туда-обратно и отель? можно как-то дешевле уложиться в 25"

dubai_shown = brief(origin=NSK[0], iata=NSK[1], dest="Дубай", dest_id="dubai", country="ОАЭ",
                    d=dates("2026-12-05", "2026-12-12", 12, 7), p=party("couple", 2), pr=prefs(district="quiet"),
                    traits=["warm"])

PRESENT_COMMON = [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
                  pred("at_most_one_joke"), pred("money_risks_plain"), pred("no_upsell_without_candidate"),
                  a("max_items", path="/upsell", max=2), pred("recommended_in_items")]


# ---------------------------------------------------------------- understand

UNDERSTAND = [
    case("e11_refine_hostel_not_for_us", "E", "understand",
         understand_input(H01_T3, stage="selection", b=dubai_shown, shown=SHOWN,
                          assumed=["dates.start", "dates.end", "prefs.district"],
                          assumptions=[("dates.start", "5–12 декабря"), ("prefs.district", "тихий район")]),
         [a("path_equals", path="/intent", value="refine"),
          pred("contains", path="/patch/set/constraints.add", values=["no_hostel"]),
          a("path_equals", path="/patch/set/budget_rub.max", value=180000),
          a("path_equals", path="/patch/set/dates.nights", value=8),
          a("path_absent", path="/patch/set/destination.name"),
          pred("patch_has_no_assumptions", allowed=["constraints.add", "budget_rub.max", "dates.nights",
                                                    "prefs.direct_only", "prefs.max_travel_hours", "prefs.stars_min"])],
         "h01 t3: «хостел в нашем возрасте как-то не очень» refuses hostels -> constraints.add [\"no_hostel\"] "
         "(code then drops every hostel and dorm bed); the budget and 8 nights are values said in the message.",
         unsure="«без долгой пересадки» may or may not become prefs.max_travel_hours / direct_only; both are allowed."),
]


# ---------------------------------------------------------------- present (engine-built inputs)

def build_present_inputs():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        # h10: budget-led inspiration from Ekaterinburg, then «что входит в эти цены?»
        sid = engine.new_session(profile_id="demo", now=NOW)
        backend.add("understand", _u(set_={"origin.city": "Екатеринбург", "party.segment": "solo",
                                           "party.adults": 1, "budget_rub.max": 25000, "dates.nights": 3}))
        engine.handle(sid, {"session_id": sid, "text": H10_T1})
        out["h10_destinations"] = _last_input(backend, "present.destinations")
        backend.add("understand", _u(intent="info", info="Что входит в цены направлений?"))
        engine.handle(sid, {"session_id": sid, "text": H10_T2})
        out["h10_info"] = _last_input(backend, "present.info")
        # h01: Dubai, then the hostel refusal with the budget, then «другие даты или другой город»
        sid = engine.new_session(profile_id="demo", now=NOW)
        backend.add("understand", _u(set_={"origin.city": "Новосибирск", "party.segment": "couple",
                                           "party.adults": 2, "dates.month": 12, "dates.nights": 7,
                                           "traits.add": ["warm", "sun"]}))
        engine.handle(sid, {"session_id": sid, "text": H01_T1})
        engine.handle(sid, {"session_id": sid, "action": {"type": "choose_destination",
                                                           "payload": {"destination_id": "dubai"}}})
        backend.add("understand", _u(intent="refine", set_={"budget_rub.max": 180000, "dates.nights": 8,
                                                            "constraints.add": ["no_hostel"]}))
        engine.handle(sid, {"session_id": sid, "text": H01_T3})
        out["h01_hostels_removed"] = _last_input(backend, "present.update")
        backend.add("understand", _u(intent="refine"))
        engine.handle(sid, {"session_id": sid, "text": H01_T4})
        out["h01_other_city"] = _last_input(backend, "present.update")
    for name, card in (("h10_destinations", "present.destinations"), ("h10_info", "present.info"),
                       ("h01_hostels_removed", "present.update"), ("h01_other_city", "present.update")):
        errors = validate_card_input(card, out[name])
        assert not errors, (name, errors)
    trains = [i for i in out["h10_destinations"]["items"] if i["facts"].get("transport") == "train"]
    assert {"kazan", "samara"} <= {i["id"] for i in trains} and all(i["facts"]["within_budget"] for i in trains)
    assert any(f.get("kind") == "destinations" for f in out["h10_info"]["facts"]), out["h10_info"]["facts"]
    row = next(c for c in out["h01_hostels_removed"]["changes"] if c["field"] == "constraints")
    assert "no_hostel" in row["after"], row
    assert not any(c["field"] == "constraints" for c in out["h01_other_city"]["changes"])
    assert out["h01_other_city"].get("other_places"), "other_places missing"
    return out


def present_cases():
    inp = build_present_inputs()
    dest, info = inp["h10_destinations"], inp["h10_info"]
    removed, other = inp["h01_hostels_removed"], inp["h01_other_city"]
    places = [p["name"] for p in other["other_places"]]
    place_re = "|".join(n[:5] for n in places)
    return [
        case("a9_destinations_trains_in_budget", "A", "present.destinations", dest,
             [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
              pred("at_most_one_joke"), pred("money_risks_plain"), a("max_items", path="/upsell", max=0),
              a("path_equals", path="/recommended_id", value=dest["items"][0]["id"]),
              pred("mentions_any", words=[r"поезд"]), pred("mentions_any", words=[r"Казан", r"Самар"]),
              a("regex_absent", path="/message", pattern=r"ничего не укладывается"),
              pred("asks_at_most", n=len(dest.get("questions") or []))],
             "h10 t1: with a budget and no transport fixed, code also quoted train-reachable places — Kazan and "
             "Samara by train fit 25 000 ₽ and come first (facts.transport train, transport_ru «поездом»); the "
             "card names the train with the price («Казань поездом — от 16 480 ₽»), never «ничего не "
             "укладывается»."),
        case("f10_info_price_includes", "F", "present.info", info,
             [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
              pred("at_most_one_joke"), a("max_items", path="/upsell", max=0), pred("max_chars", n=600),
              pred("none_of", assertions=[pred("admits_no_data")]),
              pred("mentions_any", words=[r"трансфер"]),
              pred("mentions_any", words=[r"отел"]),
              pred("mentions_any", words=[r"поезд|перел[её]т|туда[- ]и[- ]обратно|туда-обратно"]),
              pred("asks_at_most", n=0)],
             "h10 t2: «что входит в эти цены?» at the inspiration stage is answered from the `destinations` "
             "record: facts.price_includes (the cheapest round trip for one and 3 nights in a 3–4 star hotel, "
             "transfers not included) and the places within 25 000 ₽; never «нет данных»."),
        case("e12_update_hostels_removed", "E", "present.update", removed,
             PRESENT_COMMON + [
                 pred("matches", path="/message", pattern=r"^[^.!?]*(хостел|180\s?000)"),
                 pred("mentions_any", words=[r"убрал\w*\s+(все\s+)?хостел", r"хостел\w*\s+(все\s+)?убрал", r"без\s+хостел"]),
                 a("regex_absent", path="/message", pattern=r"это хостел|без хостела не вышло|остался хостел"),
                 pred("asks_at_most", n=len(removed.get("questions") or []))],
             "h01 t3: the constraints row adds no_hostel (code removed every hostel and dorm bed) and the budget "
             "fit moved the dates to fit 180 000 ₽; the change sentence says hostels are removed and what the "
             "budget fit did, no item is called a hostel."),
        case("e13_update_other_city_places", "E", "present.update", other,
             PRESENT_COMMON + [
                 pred("mentions_any", words=[place_re]),
                 a("regex_absent", path="/message", pattern=r"убрал\w*\s+(все\s+)?хостел"),
                 a("regex_absent", path="/message", pattern=r"другие города не (ищу|подбираю)|про другой город"),
                 pred("asks_at_most", n=len(other.get("questions") or []))],
             f"h01 t4: «в другие даты декабря или в другом городе?» — the date search ran in Dubai and code priced "
             f"other places for the trip (`other_places`: {', '.join(places)}); the card names them with their "
             f"price (buttons below) and never repeats «убрал хостелы» (no constraints row this turn)."),
    ]


def main():
    for c in UNDERSTAND + present_cases():
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
