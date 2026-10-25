"""Level-2 present.info cases for trains and pets (held-out L3 h09_corgi_sapsan: «А с корги в Сапсан точно
пустят?» was answered «нет данных» twice). Adds 5 files to evals/cases/ and deletes nothing.

    .venv/bin/python -m evals.authoring.cases_pets

The facts are built from the real mock (ostrovok_mcp.service search_trains / search_flights with `pets`, today
pinned) and the engine's own line builder (agent.ranking.pet_rule_line) and rules.json `pets.general_ru`, so the
case inputs carry exactly what step_info_lookup sends: legs with `pets_rule` / `pet_fee_rub`, `price_pets_rub`,
an order's `terms` with pet lines, and the `pet_rules` record. Run it after `evals.authoring.cases_v0`.
"""
import json
from pathlib import Path

from agent.ranking import pet_rule_line
from evals.common import CASES_DIR, write_json
from ostrovok_mcp import service

ROOT = Path(__file__).resolve().parents[2]
TODAY = "2026-10-25"
MOSCOW = ["SVO", "VKO", "DME"]
PET_RULES = json.loads((ROOT / "skills/trip-agent/rules.json").read_text(encoding="utf-8"))["pets"]["general_ru"]


def prefs(**kw):
    p = {"meals": "any", "free_cancellation": None, "stars_min": None, "direct_only": None, "max_travel_hours": None,
         "district": "any", "beach_max_m": None, "transport": "any"}
    p.update(kw)
    return p


def brief(dest, dest_id, d, pets, transport="any", constraints=None, adults=1, segment="solo"):
    return {"origin": {"city": "Москва", "iata": MOSCOW},
            "destination": {"id": dest_id, "name": dest, "country": "Россия"}, "traits": None,
            "dates": {"start": d[0], "end": d[1], "month": int(d[0][5:7]), "window": None, "nights": d[2],
                      "flexible_days": 0},
            "party": {"segment": segment, "adults": adults, "children": 0, "children_ages": None, "pets": pets},
            "budget_rub": None, "scope": None, "prefs": prefs(transport=transport), "constraints": constraints,
            "mood": None}


def info_input(b, stage, question, facts, assumed=("origin.city",)):
    return {"today": TODAY, "stage": stage, "brief": b, "assumed": list(assumed),
            "assumptions": [{"field": "origin.city", "label": "из Москвы"}], "question": question, "facts": facts}


def a(type_, **kw):
    return {"type": type_, **kw}


def pred(name, **args):
    return {"type": "predicate", "name": name, **({"args": args} if args else {})}


COMMON = [a("ids_subset"), a("numbers_subset"), pred("russian_only"), pred("tone_no_bureaucratese"),
          pred("at_most_one_joke"), pred("money_risks_plain"), pred("no_upsell_without_candidate"),
          a("max_items", path="/upsell", max=0),
          pred("max_chars", n=600),
          # facts answer it: never «нет данных», never «уточните у РЖД» instead of the answer
          pred("none_of", assertions=[pred("admits_no_data")])]


def case(cid, inp, expect, notes):
    return {"id": cid, "category": "F", "card": "present.info", "input": inp, "image": None,
            "expect": [a("schema")] + COMMON + expect, "runs": 5, "notes": notes}


# ---------------------------------------------------------------- facts from the real mock

def trains(o, d, day, pets, passengers=1):
    r = service.call("search_trains", {"origin_city": o, "dest_city": d, "date": day, "passengers": passengers,
                                       "today": TODAY, **({"pets": pets} if pets else {})})
    return r["offers"]


def flights(o, d, day, pets):
    r = service.call("search_flights", {"origin_iata": o, "dest_iata": d, "date": day, "adults": 1,
                                        "today": TODAY, **({"pets": pets} if pets else {})})
    return r["offers"]


def leg(kind, o):
    """One leg as steps._leg_fact + _pet_fact write it."""
    out = {"type": kind, "from": o.get("from_station") or o.get("origin_iata"), "to": o.get("to_station") or o.get("dest_iata"),
           "carrier": o["carrier"], "dep_date": o["dep"][:10], "dep_time": o["dep"][11:16], "arr_date": o["arr"][:10],
           "arr_time": o["arr"][11:16], "price_rub": o["price_rub"], "duration_min": o["duration_min"]}
    if kind == "train":
        out.update(train_no=o["train_no"], car_class=o["car_class"])
        if o.get("train_kind") == "high_speed":
            out["high_speed"] = True
    else:
        out.update(flight_no=o["flight_no"], direct=o["stops"] == 0, stops=o["stops"],
                   baggage_checked_kg=o["baggage"]["checked_kg"], baggage_cabin_kg=o["baggage"]["cabin_kg"],
                   refundable=o["fare"]["refundable"], fare=o["fare"]["name"])
    out["pets_rule"] = pet_rule_line(kind, o)
    if o["pets"]["count"]:
        out["pet_fee_rub"] = o["pets"]["fee_total_rub"]
    return {k: v for k, v in out.items() if v is not None}


def cheapest(offers, **match):
    return min((o for o in offers if all(o.get(k) == v for k, v in match.items())), key=lambda o: o["price_rub"])


def variant(vid, title, legs, stay_price, hotel):
    fee = sum(l.get("pet_fee_rub", 0) for l in legs)
    return {"kind": "variant", "id": vid, "title": title,
            "total_rub": sum(l["price_rub"] for l in legs) + stay_price, "legs": legs,
            "stay": {"name": hotel, "check_in_date": legs[0]["arr_date"], "check_in_time": "14:00",
                     "check_out_date": legs[-1]["dep_date"], "check_out_time": "12:00", "pets_allowed": True},
            **({"price_pets_rub": fee} if fee else {})}


def build():
    cases = []
    spb = ("2026-10-31", "2026-11-01", 1)
    # 1. h09 t2: the corgi and «Сапсан», both legs high-speed with the fee in the price
    out = cheapest(trains("Москва", "Санкт-Петербург", spb[0], 1), train_kind="high_speed")
    back = cheapest(trains("Санкт-Петербург", "Москва", spb[1], 1), train_kind="high_speed")
    b = brief("Санкт-Петербург", "saint-petersburg", spb, ["dog"], "train", ["pet:dog", "train:sapsan"])
    v = variant("cheapest", "Дешевле", [leg("train", out), leg("train", back)], 2710, "Исаакиевская площадь")
    q = "А с корги в Сапсан пустят? Какие правила для собак?"
    cases.append(case("p1_info_corgi_sapsan", info_input(b, "selection", q, [v, {"kind": "pet_rules", "lines": PET_RULES}]),
                      [pred("matches", path="/message", pattern=r"^\W*Да\b"),
                       pred("mentions_any", words=["переноск"]),
                       pred("mentions_any", words=["180"]),
                       pred("mentions_any", path="$text", words=[str(out["pets"]["fee_rub"]),
                                                               f"{out['pets']['fee_rub']:,}".replace(",", " ")])],
                      "h09 t2: the leg's pets_rule answers it — «да, в переноске до 180 см…» with the fee; no «нет "
                      "данных», no size of the corgi the user never gave."))
    # 2. a large dog asked about on an overnight FPK train: compartment terms
    kzn = ("2026-12-04", "2026-12-07", 3)
    coupe = cheapest(trains("Москва", "Казань", kzn[0], 1), car_class="coupe")
    coupe_back = cheapest(trains("Казань", "Москва", kzn[1], 1), car_class="coupe")
    b = brief("Казань", "kazan", kzn, ["dog"], "train", ["pet:dog"])
    v = variant("comfort", "Комфортнее", [leg("train", coupe), leg("train", coupe_back)], 14800, "Отель «Пример» (пример)")
    q = "У нас лабрадор, его в купе возьмут?"
    cases.append(case("p2_info_large_dog_coupe", info_input(b, "selection", q, [v, {"kind": "pet_rules", "lines": PET_RULES}]),
                      [pred("mentions_any", words=["купе"]),
                       pred("mentions_any", path="$text", words=["намордник", "выкуп", "все свободные места"])],
                      "A large dog on an FPK train: only a separate compartment with every free place bought, muzzled "
                      "and leashed — copied from pets_rule; the lab is not called «небольшая»."))
    # 3. a cat on a flight
    kgd = ("2026-11-20", "2026-11-23", 3)
    fl = cheapest(flights(["SVO"], ["KGD"], kgd[0], 1))
    fl_back = cheapest(flights(["KGD"], ["SVO"], kgd[1], 1))
    b = brief("Калининград", "kaliningrad", kgd, ["cat"], "plane", ["pet:cat"])
    v = variant("cheapest", "Дешевле", [leg("flight", fl), leg("flight", fl_back)], 9600, "Отель «Пример» (пример)")
    q = "Кошку можно взять с собой в салон?"
    cases.append(case("p3_info_cat_flight_cabin", info_input(b, "selection", q, [v]),
                      [pred("matches", path="/message", pattern=r"^\W*(Да|Можно)\b"),
                       pred("mentions_any", words=["8 кг"]),
                       pred("mentions_any", path="$text", words=["салон"])],
                      "Flight pet terms: cabin in a carrier up to 8 kg together, heavier in the hold — from pets_rule."))
    # 4. a paid order: the dog question after payment reads order.terms
    lines = [leg("train", out), leg("train", back)]
    order = {"kind": "order", "id": "o_ex_spb",
             "data": {"order_id": "o_ex_spb", "status": "paid", "total_rub": sum(l["price_rub"] for l in lines) + 2710,
                      "items": [{"kind": "train", "title": f"Поезд {out['train_no']} {out['from_station']} → {out['to_station']}, сидячий",
                                 "start": out["dep"], "booking_number": "RZ1234"},
                                {"kind": "hotel", "title": "Исаакиевская площадь: Одноместный, 1 ноч., без питания",
                                 "start": "2026-10-31T13:00+03:00", "booking_number": "HT5678"},
                                {"kind": "train", "title": f"Поезд {back['train_no']} {back['from_station']} → {back['to_station']}, сидячий",
                                 "start": back["dep"], "booking_number": "RZ4321"}]},
             "terms": [{"item_id": "oi_1", "title": "Поезд туда", "pets_rule": lines[0]["pets_rule"],
                        "pet_fee_rub": lines[0]["pet_fee_rub"]},
                       {"item_id": "oi_3", "title": "Поезд обратно", "pets_rule": lines[1]["pets_rule"],
                        "pet_fee_rub": lines[1]["pet_fee_rub"]}]}
    b = brief("Санкт-Петербург", "saint-petersburg", spb, ["dog"], "train", ["pet:dog", "train:sapsan"])
    q = "Билеты уже оплатил — собаку точно пустят в поезд?"
    cases.append(case("p4_info_paid_order_dog", info_input(b, "ordered", q, [order]),
                      [pred("matches", path="/message", pattern=r"^\W*Да\b"),
                       pred("mentions_any", words=["переноск"])],
                      "After payment the order's terms carry the same pet lines; the answer comes from them."))
    # 5. only the general rules (no leg answers it): «Ласточка» with a cat
    b = brief("Санкт-Петербург", "saint-petersburg", spb, ["cat"], "train", ["pet:cat"])
    q = "А в «Ласточку» с котом можно?"
    cases.append(case("p5_info_general_pet_rules", info_input(b, "selection", q, [{"kind": "pet_rules", "lines": PET_RULES}]),
                      [pred("mentions_any", words=["переноск"]),
                       pred("mentions_any", path="$text", words=["местах для пассажиров с животными", "места для пассажиров с животными"])],
                      "No leg is a «Ласточка», the pet_rules lines answer it (high-speed trains: carrier, designated seats)."))
    return cases


def main():
    for c in build():
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
