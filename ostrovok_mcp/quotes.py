"""quote_destinations: a cheap 'from' price per destination for the party.

Per destination: the cheapest basic-fare round trip (outbound on check-in day, return on
check-out day, any origin airport x destination airport, direct or 1 stop) plus the cheapest
3-4 star hotel for the stay (any stars when there are no 3-4 star hotels with free rooms).
With `month`: check-in is the first Saturday on/after max(1st of the month, today + 7) in that
month (next year's month when this year's has passed); nights = `nights` or 7.
Missing parts give `available: false` with the reason in `notes` (Russian); `currency_notes` is only for currency conversions.
`stay_only`: flights are not searched; `available` / `total_rub` then mean the stay alone (a train quote adds fares).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import flights, geo, hotels

DEFAULT_NIGHTS = 7


def month_dates(month: int, nights: int, today: date) -> tuple[date, date]:
    earliest = today + timedelta(days=7)
    year = today.year
    while True:
        first = date(year, month, 1)
        start = max(first, earliest)
        if start.month == month and start.year == year:
            break
        year += 1
    ci = start + timedelta(days=(5 - start.weekday()) % 7)
    if ci.month != month:
        ci = start
    return ci, ci + timedelta(days=nights)


def flight_party(ages: list[int], adults: int) -> tuple[int, int, int]:
    """(adults, children, infants) by airline categories: >= 12 adult fare, 2-11 child, < 2 infant."""
    return (adults + len([a for a in ages if a >= 12]), len([a for a in ages if 2 <= a < 12]),
            len([a for a in ages if a < 2]))


def quote_one(dest_id: str, origin: geo.City | None, origin_name: str, ci: date, co: date, adults: int,
              ages: list[int], today: date, stay_only: bool = False) -> dict:
    nights = (co - ci).days
    out = {"destination_id": dest_id, "available": False, "nights": nights, "check_in": ci.isoformat(),
           "check_out": co.isoformat(), "flight_price_rub": None, "stay_price_rub": None, "total_rub": None,
           "flight_offer_id": None, "return_flight_offer_id": None, "hotel_offer_id": None, "notes": [],
           "currency_notes": [], "synthetic": True}
    notes = out["notes"]
    dest = geo.destination(dest_id)
    if dest is None:
        notes.append("Направления нет в демо-каталоге")
        return out
    if stay_only:
        pass
    elif origin is None:
        notes.append(f"Город вылета «{origin_name}» не найден в демо-каталоге")
    elif not origin.iata:
        notes.append(f"В городе вылета «{origin.name_ru}» нет аэропорта в каталоге")
    elif not dest["airports"]:
        notes.append("У направления нет аэропорта в каталоге — перелёт не посчитан")
    elif set(origin.iata) & set(dest["airports"]):
        notes.append("Город вылета совпадает с направлением — перелёт не нужен")
    else:
        a, c, i = flight_party(ages, adults)
        there = flights.search(origin.iata, dest["airports"], ci, a, c, i, 0, today, tiers=("basic",))
        back = flights.search(dest["airports"], origin.iata, co, a, c, i, 0, today, tiers=("basic",))
        if there and back:
            best_there = min(there, key=lambda o: (o["price_rub"], o["dep"]))
            best_back = min(back, key=lambda o: (o["price_rub"], o["dep"]))
            out["flight_price_rub"] = best_there["price_rub"] + best_back["price_rub"]
            out["flight_offer_id"] = best_there["offer_id"]
            out["return_flight_offer_id"] = best_back["offer_id"]
        else:
            notes.append(f"Нет рейсов из «{origin.name_ru}» на эти даты (ни прямых, ни с одной пересадкой)")
    offers, _ = hotels.search(dest_id, ci, co, adults, ages, {}, 10 ** 6, today)
    if offers:
        mid = [o for o in offers if o["stars"] in (3, 4)]
        best = min(mid or offers, key=lambda o: (o["price_total_rub"], o["hotel_id"]))
        out["stay_price_rub"] = best["price_total_rub"]
        out["hotel_offer_id"] = best["offer_id"]
        if not mid:
            notes.append("Нет свободных отелей 3–4★, взят самый доступный из найденных")
    else:
        notes.append("Нет свободных отелей для этого состава на эти даты")
    if out["stay_price_rub"] is not None and (stay_only or out["flight_price_rub"] is not None):
        out["available"] = True
        out["total_rub"] = (out["flight_price_rub"] or 0) + out["stay_price_rub"]
    return out


def quote(dest_ids: list[str], origin_city: str, month: int | None, dates: dict | None, nights: int | None,
          adults: int, ages: list[int], today: date, stay_only: bool = False) -> list[dict]:
    if dates:
        ci, co = date.fromisoformat(str(dates["check_in"])), date.fromisoformat(str(dates["check_out"]))
    else:
        ci, co = month_dates(int(month), nights or DEFAULT_NIGHTS, today)
    origin = geo.resolve_city(origin_city)
    return [quote_one(d, origin, origin_city, ci, co, adults, ages, today, stay_only) for d in dest_ids]
