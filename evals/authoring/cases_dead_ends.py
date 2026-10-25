"""Level-2 present cases for dead ends and invisible fees, in our own words (no text from any sealed set):

- de1: Самара → Геленджик for three friends with «билеты туда-обратно, отель и трансфер» — no plane flies there in
  the data, so code rebuilt the trip by train (the user never tied «билеты» to a plane): the card names the train,
  its time on board and the leave day (the sleeper leaves the day before check-in), never decimal hours or a
  flight.
- de2: «Летим вдвоём из Екатеринбурга в Краснодар на неделю в августе» — the user fixed the plane and no flight
  exists: the stays are shown, the card says there are no flights, names the train trip's price with the «Поехать
  поездом» button and the similar places, and never claims the trip was built by train.
- de3: an open cart Москва → Сочи by plane with a small dog: «где в корзине собака?» — the card names the ticket
  lines that hold the pet fee (includes_ru), says it is already in the price and that the total is the sum of the
  lines.
- de4: a budget-led inspiration from Екатеринбург (two people, 3 nights in November, 25 000 ₽): Казань and Самара
  by train fit, the rest does not — the card never says «ничего не укладывается» and names both.
- de5: «а с пересадкой?» after nothing could be built (tickets only, Екатеринбург → Краснодар, plane fixed): the
  card answers from what code searched — no flight, nor with a connection — with the train's price.

Writes 5 files to evals/cases/ and deletes nothing:

    .venv/bin/python -m evals.authoring.cases_dead_ends

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
DECIMAL_ROAD = r"(?<![\d,])(1[89]|[2-9]\d),\d\s*ч"
NEVER = r"билет\w*\s+не\s+нашл|не\s+смотрел\w*\s+билет|нет\s+аэропорт|аэропорт\w*\s+нет"
NOTHING_FITS = r"ничего\s+(из\s+\w+\s+)?не\s+(уклад|влеза|вписыва)|почти\s+ничего|ни\s+одно\w*\s+(\w+\s+){0,2}не\s+уклад"
GELENDZHIK = {"origin.city": "Самара", "destination.name": "Геленджик", "dates.start": "2027-07-05",
              "dates.end": "2027-07-12", "party.segment": "friends", "party.adults": 3,
              "scope": ["flights", "stay", "transfers"]}
KRASNODAR = {"origin.city": "Екатеринбург", "destination.name": "Краснодар", "dates.month": 8, "dates.nights": 7,
             "party.segment": "couple", "party.adults": 2}
SOCHI_DOG = {"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
             "party.segment": "couple", "party.adults": 2, "party.pets": ["dog"], "constraints.add": ["pet:dog"]}
EKB_BUDGET = {"origin.city": "Екатеринбург", "party.segment": "couple", "party.adults": 2, "budget_rub.max": 25000,
              "dates.month": 11, "dates.nights": 3}


def _turn(engine, backend, sid, text, intent="new_trip", info=None, **patch):
    backend.add("understand", _u(intent=intent, set_=patch, info=info))
    return engine.handle(sid, {"session_id": sid, "text": text})


def build_inputs():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Из Самары в Геленджик на 5–12 июля, нас трое, нужны билеты туда-обратно, отель "
                                    "и трансфер", **GELENDZHIK)
        out["by_train"] = _last_input(backend, "present.variants")
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Летим вдвоём из Екатеринбурга в Краснодар на неделю в августе", **KRASNODAR)
        out["plane_fixed"] = _last_input(backend, "present.variants")
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Москва — Сочи самолётом, 20–23 ноября вдвоём, с нами маленькая собака", **SOCHI_DOG)
        v = engine.get_session(sid).variants[0]
        engine.handle(sid, {"session_id": sid, "action": {"type": "choose_variant", "payload": {"variant_id": v.id}}})
        q = "А где в корзине собака? Сложил строки — доплаты за неё не вижу"
        _turn(engine, backend, sid, q, intent="info", info=q)
        out["pet_cart"] = _last_input(backend, "present.info")
        sid = engine.new_session(profile_id="demo", now=NOW)
        _turn(engine, backend, sid, "Вдвоём из Екатеринбурга на три ночи в ноябре, бюджет 25 тысяч — куда можно?",
              **EKB_BUDGET)
        out["budget"] = _last_input(backend, "present.destinations")
        sid = engine.new_session(profile_id="demo", now=NOW)
        r = _turn(engine, backend, sid, "Летим вдвоём из Екатеринбурга в Краснодар в августе, нужны только билеты",
                  **KRASNODAR, scope=["flights"])
        assert (r.get("error") or {}).get("code") == "no_data", r["message"]
        _turn(engine, backend, sid, "а с пересадкой?", intent="info", info="а с пересадкой?")
        out["connecting"] = _last_input(backend, "present.info")
    cards = {"pet_cart": "present.info", "budget": "present.destinations", "connecting": "present.info"}
    for name, inp in out.items():
        errors = validate_card_input(cards.get(name, "present.variants"), inp)
        assert not errors, (name, errors)
    for i in out["by_train"]["items"]:
        f = i["facts"]
        assert f["transport_gap"] == "train_instead" and f["out_long_train"] and f["out_days_before_checkin"] >= 1, f
    pf = out["plane_fixed"]
    assert all(i["facts"]["transport_gap"] == "plane_fixed" for i in pf["items"]) and pf["other_places"]
    assert {"kind": "train", "label": "Поехать поездом"} in pf["offers"]
    cart = next(f for f in out["pet_cart"]["facts"] if f.get("kind") == "cart")
    assert [i for i in cart["items"] if i.get("includes_ru")]
    fits = [i["name"] for i in out["budget"]["items"] if i["facts"].get("within_budget") is True]
    assert sorted(fits) == ["Казань", "Самара"] and len(out["budget"]["items"]) > 2, fits
    road = next(f for f in out["connecting"]["facts"] if f.get("kind") == "transport_search")
    assert road["code"] == "plane_fixed" and road.get("connecting_ru") and road.get("from_rub")
    return out


def _day_month(iso):
    d = date.fromisoformat(iso[:10])
    return rf"(?<!\d){d.day}\s+{MONTHS_GEN[d.month - 1]}"


def _words(text):
    return re.escape(text).replace(r"\ ", r"\s+")


def _price_re(rub):
    return f"{int(rub):,}".replace(",", " ").replace(" ", r"[\s\u00a0\u202f]?")


def cases():
    inp = build_inputs()
    bt, pf, pc, bu, cn = (inp[k] for k in ("by_train", "plane_fixed", "pet_cart", "budget", "connecting"))
    hint = next(i for i in bt["items"] if i["id"] == bt["recommended_hint"])
    f = hint["facts"]
    low = pf["items"][0]["facts"]["transport_gap_from_rub"]
    places = [p["name"] for p in pf["other_places"]]
    cart = next(x for x in pc["facts"] if x.get("kind") == "cart")
    fees = [i["pet_fee_rub"] for i in cart["items"] if i.get("pet_fee_rub")]
    road = next(x for x in cn["facts"] if x.get("kind") == "transport_search")
    return [
        case("de1_variants_no_flights_rebuilt_by_train", "C", "present.variants", bt,
             PRESENT_COMMON + [
                 a("max_items", path="/upsell", max=2), pred("recommended_in_items"),
                 pred("mentions_any", words=[r"поезд"]),
                 pred("mentions_any", words=[r"(?<![\d,])(" + _words(f["out_on_road_ru"]) + ")"]),
                 pred("mentions_any", words=[_day_month(f["out_date"]), r"за\s+день\s+до\s+заезда"]),
                 a("regex_absent", path="/message", pattern=DECIMAL_ROAD),
                 a("regex_absent", path="/message", pattern=NEVER + r"|ни\s+поездов|перел[её]т\s+(туда|обратно)"),
                 pred("no_unbacked_claims"),
                 pred("asks_at_most", n=len(bt.get("questions") or []))],
             f"Samara → Gelendzhik, 5–12 July, three friends asked for «билеты туда-обратно, отель и трансфер» (scope "
             f"flights by the user's words, no plane named): no flight exists in the data, so code rebuilt the trip by "
             f"train (risk transport_switched). The recommended «{hint['title']}» leaves {f['out_date']}, the day before "
             f"check-in: the card says the trip goes by train, «в пути {f['out_on_road_ru']}» and the leave day — "
             f"never decimal hours, never a flight, never «нет ни рейсов, ни поездов»."),
        case("de2_variants_plane_fixed_offers_train", "C", "present.variants", pf,
             PRESENT_COMMON + [
                 a("max_items", path="/upsell", max=2), pred("recommended_in_items"),
                 pred("mentions_any", words=[r"самол[её]т|рейс"]),
                 pred("mentions_any", words=[_price_re(low)]),
                 pred("mentions_any", words=[r"поезд"]),
                 pred("mentions_any", words=["|".join(n[:5] for n in places)]),
                 a("regex_absent", path="/message", pattern=NEVER + r"|собрал\w*\s+поездом|одной\s+кнопк"),
                 pred("no_unbacked_claims"),
                 pred("asks_at_most", n=len(pf.get("questions") or []))],
             f"«Летим вдвоём …» fixes the plane (code reads «летим» as prefs.transport plane): no flight Ekaterinburg → "
             f"Krasnodar exists, the stays are shown (risk no_transport with the gap line). The card says there are no "
             f"flights, names the train trip «от {low} ₽» with the «Поехать поездом» button and the similar places "
             f"({', '.join(places)}); it never says the trip was built by train."),
        case("de3_info_cart_pet_fee_line", "D", "present.info", pc,
             # the line titles carry IATA codes and flight numbers («Перелёт DME → AER, UN7545»): Latin is expected
             [a("ids_subset"), a("numbers_subset"), pred("russian_only", min_cyrillic=0.7),
              pred("tone_no_bureaucratese"), pred("at_most_one_joke"), pred("money_risks_plain"), a("max_items", path="/upsell", max=0),
              pred("mentions_any", words=["|".join(_price_re(x) for x in fees)]),
              pred("mentions_any", words=[r"в\s+цен|включ|входит|уже\s+(в|учт)"]),
              pred("mentions_any", words=[r"перел[её]т|рейс|билет"]),
              a("regex_absent", path="/message", pattern=r"не\s+(включ|входит|учтен|учтён)|нет\s+данных|доплатит"),
              pred("asks_at_most", n=0)],
             "An open cart Moscow → Sochi by plane with a small dog: «где в корзине собака?». The pet fee is inside "
             "the two flight lines (cart record items with includes_ru «в т.ч. собака в салоне — … ₽» and "
             "pet_fee_rub): the card names those lines and their fee, says it is already in the price and that the "
             "total is the sum of the lines — never that the fee is missing or must be paid separately."),
        case("de4_destinations_two_fit_the_budget", "A", "present.destinations", bu,
             [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
              pred("at_most_one_joke"), pred("money_risks_plain"), a("max_items", path="/upsell", max=0),
              a("path_equals", path="/recommended_id", value=bu["items"][0]["id"]),
              a("regex_absent", path="$text", pattern=NOTHING_FITS),
              pred("mentions_any", words=[r"Казан"]), pred("mentions_any", words=[r"Самар"]),
              pred("mentions_any", words=[r"поезд"]),
              pred("no_unbacked_claims", card="present.destinations"),
              pred("asks_at_most", n=len(bu.get("questions") or []))],
             "A budget-led inspiration from Ekaterinburg (two people, 3 nights in November, 25 000 ₽): Kazan and "
             "Samara by train have facts.within_budget true, the plane places do not. The card names both trains "
             "with their price and never says «ничего / почти ничего не укладывается» (the code guard blocks it)."),
        case("de5_info_connecting_after_no_build", "edge", "present.info", cn,
             [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
              pred("at_most_one_joke"), pred("money_risks_plain"), a("max_items", path="/upsell", max=0),
              pred("mentions_any", words=[r"пересадк"]),
              pred("mentions_any", words=[r"\bнет\b|\bни\b"]),
              pred("mentions_any", words=[_price_re(road["from_rub"])]),
              a("regex_absent", path="/message",
                pattern=NEVER + r"|есть\s+(рейс\w*\s+)?с\s+пересадк|с\s+пересадкой\s+(можно|есть|найд)"),
              pred("no_unbacked_claims", card="present.info"),
              pred("asks_at_most", n=0)],
             f"Tickets only, Ekaterinburg → Krasnodar, the plane fixed («летим»): nothing could be built; «а с "
             f"пересадкой?» is answered from the transport_search fact — no flight, nor with a connection "
             f"(connecting_ru), the train trip «от {road['from_rub']} ₽» one button away — never «билетов я не "
             f"смотрел» and never a connecting flight that does not exist."),
    ]


def main():
    for c in cases():
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
