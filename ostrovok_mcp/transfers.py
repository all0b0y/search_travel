"""Transfer offers between an airport / station / point and a hotel (or back with `reverse`).

Road distance = straight line x 1.3. Stations have no coordinates in the schema: a station is
placed at its city's centre (catalog destination or geo.KNOWN_CITIES) — an approximation.

Kinds (duration = road km / speed + fixed overhead):
  taxi     45 km/h + 10 min, up to 4 pax per car,   price = cars x (base + per_km x km)
  private  50 km/h +  5 min, up to 7 pax (minivan), price = max(min, 1.5 x taxi one car) per vehicle
  shuttle  35 km/h + 25 min, airports only, <= 80 km, 06:00-23:30, price = pax x (base + per_km x km)
  public   25 km/h + 20 min, <= 60 km, 06:00-23:00, price = pax x fare
Tariffs by country (RUB), TARIFFS[cc] = (taxi base, taxi per km, private min, shuttle base,
shuttle per km, public fare); DEFAULT_TARIFF for countries not listed. All synthetic.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import geo, hotels, pricing
from .db import ServiceError
from .offer_ids import encode

TARIFFS = {
    "RU": (300, 32, 2500, 450, 6, 60),
    "TR": (600, 55, 3500, 700, 9, 150),
    "AE": (700, 60, 4500, 900, 10, 250),
    "EG": (400, 35, 3000, 600, 7, 100),
    "TH": (400, 30, 3000, 600, 7, 120),
    "GE": (350, 30, 2800, 500, 6, 80),
    "AM": (350, 30, 2800, 500, 6, 80),
    "UZ": (300, 25, 2500, 450, 5, 60),
    "BY": (300, 30, 2500, 450, 6, 60),
}
DEFAULT_TARIFF = (700, 60, 4500, 900, 10, 200)
KINDS = {  # speed km/h, overhead min, capacity per vehicle (None = per seat)
    "taxi": (45, 10, 4), "private": (50, 5, 7), "shuttle": (35, 25, None), "public": (25, 20, None),
}
MEETING = {
    ("iata", "taxi"): "Выход из зоны прилёта аэропорта {place}, стойка официального такси",
    ("iata", "private"): "Зона прилёта аэропорта {place}: водитель с табличкой с вашим именем",
    ("iata", "shuttle"): "Остановка шаттлов у терминала прилёта аэропорта {place}",
    ("iata", "public"): "Остановка общественного транспорта у аэропорта {place}",
    ("station", "taxi"): "Привокзальная площадь, {place}: стоянка такси",
    ("station", "private"): "Выход в город с вокзала {place}: водитель с табличкой",
    ("station", "public"): "Остановка у вокзала {place}",
    ("point", "taxi"): "Указанная точка на карте",
    ("point", "private"): "Указанная точка на карте: водитель позвонит по прибытии",
    ("point", "public"): "Ближайшая к точке остановка",
    ("hotel", "taxi"): "У входа в отель «{hotel}»",
    ("hotel", "private"): "Лобби отеля «{hotel}»: водитель с табличкой",
    ("hotel", "shuttle"): "У входа в отель «{hotel}» (шаттл собирает гостей по пути)",
    ("hotel", "public"): "Ближайшая к отелю «{hotel}» остановка",
}


def resolve_point(point: dict) -> dict:
    """{'form', 'lat', 'lon', 'place', 'country_code'} for an airport, a station or coordinates."""
    if point.get("iata"):
        ap = geo.airport(point["iata"])
        if not ap:
            raise ServiceError("unknown_airport", f"airport {point['iata']!r} not in open data")
        return {"form": "iata", "lat": ap["lat"], "lon": ap["lon"], "place": ap["name"],
                "country_code": ap["country_code"]}
    if point.get("station"):
        name = point["station"]
        city = geo.station_city(name) or geo.resolve_city(name)
        if city is None:  # "Москва Казанская" -> "Москва", "Казань-Пассажирская" -> "Казань"
            for sep in (" ", "-"):
                city = geo.resolve_city(name.split(sep)[0])
                if city:
                    break
        if city is None:
            from . import db
            for d in db.opendata().query("destinations", cols="id", key="id"):
                dd = geo.destination(d["id"])
                if dd and name in dd["stations"]:
                    city = geo.resolve_city(dd["id"])
                    break
        if city is None:
            raise ServiceError("unknown_station", f"station {name!r} not found")
        return {"form": "station", "lat": city.lat, "lon": city.lon, "place": name,
                "country_code": city.country_code}
    return {"form": "point", "lat": float(point["lat"]), "lon": float(point["lon"]), "place": "точка",
            "country_code": None}


def build(params: dict) -> dict | None:
    h = hotels.hotel(params["h"])
    if not h:
        return None
    kind, pax, reverse = params["k"], int(params["pax"]), bool(params["rv"])
    pt = resolve_point(params["fr"])
    dest = geo.destination(h["destination_id"])
    cc = pt["country_code"] or (dest["country_code"] if dest else None)
    km = geo.haversine_km(pt["lat"], pt["lon"], h["lat"], h["lon"]) * 1.3
    at = geo.parse_iso(params["at"])
    today = date.fromisoformat(params["t"])
    if at.date() < today or (at.date() - today).days > pricing.MAX_HORIZON_DAYS:
        return None
    if kind not in KINDS:
        return None
    speed, overhead, cap = KINDS[kind]
    duration = max(1, int(round(km / speed * 60 + overhead)))
    # service hours are local to the place: `at` may come with any offset (a UTC 20:53 is 23:53 in Minsk,
    # where public transport no longer runs), and a public ride must also end before midnight
    tz = geo.destination_tz(dest) if dest else None
    at_local = at.astimezone(tz) if tz is not None and at.tzinfo is not None else at
    local_min = at_local.hour * 60 + at_local.minute
    if kind == "shuttle" and (pt["form"] != "iata" or km > 80 or not 360 <= local_min <= 1410):
        return None
    if kind == "public" and (km > 60 or not 360 <= local_min <= 1380 or local_min + duration > 1440):
        return None
    if km > 400:
        return None
    tb, tkm, pmin, sb, skm, pub = TARIFFS.get((cc or "").upper(), DEFAULT_TARIFF)
    one_taxi = tb + tkm * km
    if kind == "taxi":
        price = math.ceil(pax / cap) * one_taxi
    elif kind == "private":
        price = math.ceil(pax / cap) * max(pmin, 1.5 * one_taxi)
    elif kind == "shuttle":
        price = pax * (sb + skm * km)
    else:
        price = pax * pub
    side = "hotel" if reverse else pt["form"]
    template = MEETING.get((side, kind)) or MEETING[(side, "taxi")]
    meeting = template.format(place=pt["place"], hotel=h["name"])
    return {"offer_id": encode("transfer", params), "kind": kind, "duration_min": duration,
            "price_rub": pricing.round10(price), "meeting_point": meeting, "synthetic": True,
            # extra fields for the cart (not part of TransferOffer; stripped by the service)
            "_start": params["at"], "_end": geo.iso(at + timedelta(minutes=duration)), "_km": round(km, 1),
            "_point": params["fr"], "_hotel_id": h["hotel_id"], "_hotel_name": h["name"], "_reverse": reverse,
            "_pax": pax}


def search(point: dict, hotel_id: str, at: str, pax: int, kinds: list[str] | None, reverse: bool,
           today: date) -> list[dict]:
    if not hotels.hotel(hotel_id):
        raise ServiceError("hotel_not_found", f"no hotel {hotel_id!r} in the catalog")
    resolve_point(point)
    clean_point = {k: v for k, v in point.items() if v is not None}
    offers = []
    for kind in (kinds or list(KINDS)):
        params = {"fr": clean_point, "h": hotel_id, "at": at, "pax": pax, "k": kind, "rv": reverse,
                  "t": today.isoformat()}
        offer = build(params)
        if offer:
            offers.append(offer)
    offers.sort(key=lambda o: (o["price_rub"], o["duration_min"]))
    return offers
