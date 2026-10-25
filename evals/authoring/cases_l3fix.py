"""Level-2 understand cases for the rules added after the first live L3 run (seasons, wishes, date ranges,
new prefs, post-purchase new_dates). Adds 7 files to evals/cases/ and deletes nothing.

    .venv/bin/python -m evals.authoring.cases_l3fix

Run it after `evals.authoring.cases_v0` (which rewrites the directory with its 40 cases).
"""
from evals.common import CASES_DIR, write_json

# helpers copied from evals/authoring/cases_v0.py (same shapes; that module is being edited by a sibling)
TODAY = "2026-10-25"
MOSCOW = ("Москва", ["SVO", "VKO", "DME"])


def prefs(**kw):
    p = {"meals": "any", "free_cancellation": None, "stars_min": None, "direct_only": None, "max_travel_hours": None,
         "district": "any", "beach_max_m": None, "transport": "any"}
    p.update(kw)
    return p


def party(segment, adults, children=0, ages=None, pets=None):
    return {"segment": segment, "adults": adults, "children": children, "children_ages": ages, "pets": pets or []}


def dates(start=None, end=None, month=None, nights=None, window=None, flex=0):
    return {"start": start, "end": end, "month": month, "window": window, "nights": nights, "flexible_days": flex}


def brief(origin=None, iata=None, dest=None, dest_id=None, country=None, traits=None, d=None, p=None, budget=None,
          scope=None, pr=None, constraints=None, mood=None):
    return {"origin": {"city": origin, "iata": iata or []} if origin else None,
            "destination": {"id": dest_id, "name": dest, "country": country} if dest else None,
            "traits": traits, "dates": d, "party": p, "budget_rub": {"max": budget} if budget is not None else None,
            "scope": scope, "prefs": pr, "constraints": constraints, "mood": mood}


def understand_input(message, stage="inspiration", b=None, shown=(), pending=(), history=(), has_image=False,
                     assumed=(), assumptions=()):
    return {"today": TODAY, "stage": stage, "brief": b or brief(), "assumed": list(assumed),
            "assumptions": [{"field": f, "label": l} for f, l in assumptions], "shown": list(shown),
            "pending_questions": list(pending), "history": list(history), "message": message, "has_image": has_image}


def a(type_, **kw):
    out = {"type": type_}
    out.update(kw)
    return out


def pred(name, **args):
    return {"type": "predicate", "name": name, **({"args": args} if args else {})}


def case(cid, category, card, inp, expect, notes, image=None, unsure=None):
    c = {"id": cid, "category": category, "card": card, "input": inp, "image": image,
         "expect": [a("schema")] + expect, "runs": 5, "notes": notes}
    if unsure:
        c["unsure"] = unsure
    return c


SHOWN = [{"label": "1", "kind": "variant", "id": "cheapest", "title": "Дешевле"},
         {"label": "2", "kind": "variant", "id": "fastest", "title": "Быстрее"},
         {"label": "3", "kind": "variant", "id": "comfort", "title": "Комфортнее"}]
ist = brief(origin="Москва", iata=MOSCOW[1], dest="Стамбул", dest_id="istanbul", country="Турция",
            d=dates("2026-11-07", "2026-11-14", 11, 7), p=party("couple", 2), pr=prefs(district="quiet"))
kgd_ordered = brief(origin="Москва", iata=MOSCOW[1], dest="Калининград", dest_id="kaliningrad", country="Россия",
                    d=dates("2026-11-20", "2026-11-23", 11, 3), p=party("solo", 1))

CASES = [
    case("a6_wish_south_sea", "A", "understand",
         understand_input("тянет на юг, к морю, в январе на 10 ночей, мы с женой"),
         [a("path_equals", path="/intent", value="new_trip"),
          a("path_absent", path="/patch/set/destination.name"),
          pred("contains", path="/patch/set/traits.add", values=["sea"]),
          a("path_equals", path="/patch/set/dates.month", value=1),
          a("path_equals", path="/patch/set/dates.nights", value=10),
          a("path_equals", path="/patch/set/party.segment", value="couple"),
          pred("patch_has_no_assumptions", allowed=["traits.add", "dates.month", "dates.nights", "party.segment",
                                                    "party.adults"])],
         "«на юг», «к морю» are wishes (traits), never destination.name (L3 s02: a wish was geocoded to a town)."),
    case("a7_mountains_spring", "A", "understand",
         understand_input("в горы бы весной, ночей на 5, вдвоём с подругой"),
         [a("path_absent", path="/patch/set/destination.name"),
          pred("contains", path="/patch/set/traits.add", values=["mountains"]),
          a("path_equals", path="/patch/set/dates.month", value=4),
          a("path_equals", path="/patch/set/dates.nights", value=5),
          a("path_absent", path="/patch/set/dates.start"),
          a("path_in", path="/patch/set/party.segment", values=["other"])],
         "«в горы» is a trait; «весной» -> dates.month 4 by the table (L3 s07: a season was ignored)."),
    case("a8_domestic_december", "A", "understand",
         understand_input("куда-нибудь по России в декабре на 4 ночи, вдвоём"),
         [a("path_equals", path="/intent", value="new_trip"),
          a("path_absent", path="/patch/set/destination.name"),
          a("path_equals", path="/patch/set/prefs.domestic", value=True),
          a("path_equals", path="/patch/set/dates.month", value=12),
          a("path_equals", path="/patch/set/dates.nights", value=4),
          a("path_equals", path="/patch/set/party.segment", value="couple"),
          pred("patch_has_no_assumptions", allowed=["prefs.domestic", "dates.month", "dates.nights", "party.segment",
                                                    "party.adults"])],
         "«по России» -> prefs.domestic true; «Россия» is not a destination."),
    case("b5_season_autumn", "B", "understand",
         understand_input("хочу в Казань осенью, на 3 ночи, вдвоём"),
         [a("path_equals", path="/patch/set/destination.name", value="Казань", casefold=True),
          a("path_equals", path="/patch/set/dates.month", value=11),
          a("path_equals", path="/patch/set/dates.nights", value=3),
          a("path_absent", path="/patch/set/dates.start"),
          a("path_equals", path="/patch/set/party.segment", value="couple")],
         "«осенью» with today 2026-10-25 -> the first autumn month after October = 11 (the code applies the same rule)."),
    case("e5_refine_mid_month_range", "E", "understand",
         understand_input("давайте лучше в середине декабря, ночи на 3–4", stage="selection", b=ist, shown=SHOWN,
                          assumed=["origin.city", "dates.start", "dates.end", "prefs.district"],
                          assumptions=[("origin.city", "из Москвы"), ("dates.start", "7–14 ноября")]),
         [a("path_equals", path="/intent", value="refine"),
          a("path_equals", path="/patch/set/dates.start", value="2026-12-15"),
          a("path_equals", path="/patch/set/dates.nights", value=3),
          a("path_absent", path="/patch/set/dates.end"),
          a("path_absent", path="/patch/set/destination.name"),
          pred("patch_has_no_assumptions", allowed=["dates.start", "dates.nights", "dates.flexible_days",
                                                    "dates.month"])],
         "«в середине декабря» -> dates.start the 15th (+ flexible days); a range of nights -> its lower number "
         "(L3 s04: such a refine ended in bad_request).",
         unsure="dates.flexible_days 3 is in the table but not asserted; a missing flexible_days still passes."),
    case("e6_arrive_before_no_night", "E", "understand",
         understand_input("и чтобы прилёт был до 12, ночные рейсы не нужны", stage="selection", b=ist, shown=SHOWN,
                          assumed=["origin.city", "prefs.district"], assumptions=[("origin.city", "из Москвы")]),
         [a("path_equals", path="/intent", value="refine"),
          a("path_equals", path="/patch/set/prefs.arrive_before", value="12:00"),
          a("path_equals", path="/patch/set/prefs.no_night_flights", value=True),
          pred("patch_has_no_assumptions", allowed=["prefs.arrive_before", "prefs.no_night_flights",
                                                    "constraints.add"])],
         "New prefs: an arrival deadline as \"HH:MM\" and no night flights (L3 s24)."),
    case("f4_change_dates_new_dates", "F", "understand",
         understand_input("перенесите, пожалуйста, на 5–8 декабря — сколько будет доплата?", stage="aftercare",
                          b=kgd_ordered, history=[{"role": "user", "text": "можно сдвинуть поездку?"},
                                                  {"role": "agent", "text": "На какие даты перенести?"}]),
         [a("path_equals", path="/intent", value="post_purchase"),
          a("path_equals", path="/post_purchase/kind", value="change_dates"),
          a("path_equals", path="/post_purchase/params/new_dates/check_in", value="2026-12-05"),
          a("path_equals", path="/post_purchase/params/new_dates/check_out", value="2026-12-08")],
         "After purchase new dates travel as post_purchase.params.new_dates {check_in, check_out} (L3 s16 looped "
         "because the dates were not carried)."),
]


def main():
    for c in CASES:
        write_json(CASES_DIR / f"{c['id']}.json", c)
        print("wrote", c["id"])


if __name__ == "__main__":
    main()
