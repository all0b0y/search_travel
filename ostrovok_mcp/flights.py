"""Flight generator: deterministic schedules and fares over the real OpenFlights route graph.

- Routing: a direct offer when `routes` has origin->dest; otherwise one stop via a hub that has
  routes to both (preferred hubs geo.HUBS first, then shortest detour), at most 2 hubs. When a
  direct route exists, one cheaper 1-stop alternative is added if the detour is <= 1.6x.
- Schedule: 4 stable slots per leg (05:00-23:30 local, seeded by the leg), 1-4 of them fly on
  a given date (seeded by leg|date). Duration = great-circle km / 800 km/h + 30 min taxi.
  Connections: layover 1.5-6 h (seeded).
- Fare per adult = (2500 + 5 RUB/km [+3500 international]) x season(date, destination profile)
  x lead time(today -> date) x weekday (Fri/Sun x1.08) x demand noise (0.9-1.15) x tier
  (x0.82 for 1-stop). Children (2-11) 75 %, infants (< 2, no seat) 10 %.
- Tiers: basic (cabin bag only, non-refundable, no changes), standard (23 kg bag, change fee),
  flex (23 kg bag, refundable, free changes) — `flex` is the `flexible_fare` upsell.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

from . import db, geo, pricing, rng
from .offer_ids import encode

CARRIER_NAMES = {
    "SU": "Аэрофлот", "S7": "S7 Airlines", "U6": "Уральские авиалинии", "DP": "Победа", "UT": "ЮТэйр",
    "N4": "Nordwind", "ZF": "Azur Air", "5N": "Smartavia", "WZ": "Red Wings", "A4": "Азимут",
    "FV": "Россия", "R3": "Якутия", "6R": "Алроса", "IO": "ИрАэро", "Y7": "NordStar", "EO": "Pegas Fly",
    "TK": "Turkish Airlines", "PC": "Pegasus", "XQ": "SunExpress", "EK": "Emirates", "FZ": "flydubai",
    "QR": "Qatar Airways", "EY": "Etihad", "G9": "Air Arabia", "HY": "Uzbekistan Airways", "KC": "Air Astana",
    "J2": "AZAL", "B2": "Belavia", "MS": "EgyptAir", "TG": "Thai Airways", "MU": "China Eastern",
    "CZ": "China Southern", "CA": "Air China", "UL": "SriLankan", "AI": "Air India", "VN": "Vietnam Airlines",
    "SV": "Saudia", "WY": "Oman Air", "GF": "Gulf Air", "A9": "Georgian Airways", "RM": "Aircompany Armenia",
    "KR": "Air Manas", "OZ": "Asiana", "KE": "Korean Air", "JL": "JAL", "IR": "Iran Air",
    "8Q": "Onur Air", "UN": "Трансаэро", "7R": "РусЛайн", "YC": "Ямал", "KL": "KLM", "LH": "Lufthansa",
    "AF": "Air France", "BA": "British Airways", "AZ": "Alitalia", "OS": "Austrian", "LO": "LOT",
    "A3": "Aegean", "OA": "Olympic Air", "CY": "Cyprus Airways", "VY": "Vueling", "IB": "Iberia",
    "SQ": "Singapore Airlines", "CX": "Cathay Pacific", "HU": "Hainan Airlines", "PS": "МАУ",
}
# Routes come from OpenFlights (last updated 2014): some carriers no longer fly; the names are
# what the data says, the schedules and fares on top of them are synthetic.
TIERS = {
    #          price x, checked kg, cabin kg, refundable, change fee per pax (dom, intl) or None
    "basic":    (1.00, None, 10, False, None),
    "standard": (1.18, 23, 10, False, (2500, 4500)),
    "flex":     (1.45, 23, 10, True, (0, 0)),
}
SLOTS = 4
CHILD_SHARE, INFANT_SHARE = 0.75, 0.10


def carrier_name(code: str) -> str:
    return CARRIER_NAMES.get(code, code)


def _key() -> tuple:
    return db._cache_key(db.opendata_paths())


@lru_cache(maxsize=4096)
def _routes(cache_key: tuple, column: str, iata: str) -> dict[str, list[str]]:
    other = "dest_iata" if column == "origin_iata" else "origin_iata"
    out: dict[str, list[str]] = {}
    for r in db.opendata().query("routes", f"{column} = ?", (iata,)):
        carriers = [c for c in db.jload(r.get("carriers"), []) if c]
        lst = out.setdefault(r[other], [])
        for c in carriers:
            if c not in lst:
                lst.append(c)
    return out


def routes_from(iata: str) -> dict[str, list[str]]:
    return _routes(_key(), "origin_iata", iata)


def routes_to(iata: str) -> dict[str, list[str]]:
    return _routes(_key(), "dest_iata", iata)


def route_carriers(o: str, d: str) -> list[str] | None:
    r = routes_from(o)
    if d not in r:
        return None
    return sorted(r[d]) or ["XX"]


def _dist(a: dict, b: dict) -> float:
    return geo.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])


def plans(o: str, d: str) -> list[str | None]:
    """Hub list for o->d: None = direct."""
    ao, ad = geo.airport(o), geo.airport(d)
    if not ao or not ad or o == d:
        return []
    direct = route_carriers(o, d) is not None
    out_from, in_to = routes_from(o), routes_to(d)
    cands = []
    for h in set(out_from) & set(in_to):
        if h in (o, d):
            continue
        ah = geo.airport(h)
        if not ah:
            continue
        domestic = ao["country_code"] == ad["country_code"]
        if domestic and ah["country_code"] != ao["country_code"]:
            continue  # a domestic trip never connects abroad
        detour = (_dist(ao, ah) + _dist(ah, ad)) / max(1.0, _dist(ao, ad))
        pref = geo.HUBS.index(h) if h in geo.HUBS else len(geo.HUBS)
        cands.append((pref, detour, h))
    cands.sort()
    if direct:
        alt = [h for pref, detour, h in cands if detour <= 1.6 and pref < len(geo.HUBS)][:1]
        return [None, *alt]
    good = [h for pref, detour, h in cands if detour <= 2.5]
    return good[:2]


def _slot(o: str, d: str, i: int) -> tuple[int, str, str]:
    """Stable slot i of leg o->d: (local departure minutes, carrier code, flight number)."""
    carriers = route_carriers(o, d) or ["XX"]
    minutes = rng.rint(60, 282, "flight", o, d, "slot", i) * 5  # 05:00 .. 23:30
    carrier = rng.pick(carriers, "flight", o, d, "carrier", i)
    number = rng.rint(100, 9899, "flight", o, d, carrier, "number", i)
    return minutes, carrier, f"{carrier}{number}"


def active_slots(o: str, d: str, day: date) -> list[int]:
    n = rng.rint(1, SLOTS, "flight", o, d, "departures", day.isoformat())
    order = sorted(range(SLOTS), key=lambda i: rng.unit("flight", o, d, "slotpick", day.isoformat(), i))
    return sorted(order[:n], key=lambda i: _slot(o, d, i)[0])


def _leg(o: str, d: str, dep: datetime, carrier: str, flight_no: str) -> dict:
    ao, ad = geo.airport(o), geo.airport(d)
    km = _dist(ao, ad)
    duration = int(round(km / 800 * 60 + 30))
    arr = (dep + timedelta(minutes=duration)).astimezone(geo.airport_tz(ad))
    return {"origin_iata": o, "dest_iata": d, "dep": dep, "arr": arr, "carrier": carrier,
            "flight_no": flight_no, "km": km}


def _dest_profile(d: str) -> str:
    ad = geo.airport(d) or {}
    return pricing.profile(ad.get("dest_kind"), ad.get("dest_tags"), ad.get("country_code"))


def build(params: dict) -> dict | None:
    """Rebuild one flight offer from its params (the offer-id payload). None if it does not fly."""
    o, d, h = params["o"], params["d"], params.get("h")
    day = date.fromisoformat(params["dt"])
    today = date.fromisoformat(params["t"])
    s, tier = int(params["s"]), params["f"]
    adults, children, infants = int(params["a"]), int(params["c"]), int(params["i"])
    if tier not in TIERS:
        return None
    lead = (day - today).days
    if lead < 0 or lead > pricing.MAX_HORIZON_DAYS:
        return None
    ao, ad = geo.airport(o), geo.airport(d)
    if not ao or not ad:
        return None
    first_to = h or d
    if route_carriers(o, first_to) is None or s not in active_slots(o, first_to, day):
        return None
    minutes, carrier, flight_no = _slot(o, first_to, s)
    dep = geo.local_dt(day, minutes, geo.airport_tz(ao))
    legs = [_leg(o, first_to, dep, carrier, flight_no)]
    if h:
        if h not in plans(o, d):
            return None
        layover = rng.rint(18, 72, "flight", o, h, d, "layover", day.isoformat(), s) * 5  # 1.5 .. 6 h
        dep2 = legs[0]["arr"] + timedelta(minutes=layover)
        _, carrier2, flight_no2 = _slot(h, d, rng.rint(0, SLOTS - 1, "flight", o, h, d, "leg2", day.isoformat(), s))
        legs.append(_leg(h, d, dep2.astimezone(geo.airport_tz(geo.airport(h))), carrier2, flight_no2))
    elif route_carriers(o, d) is None:
        return None
    international = ao["country_code"] != ad["country_code"]
    km = sum(leg["km"] for leg in legs)
    mult, checked, cabin, refundable, fee = TIERS[tier]
    per_adult = (2500 + 5.0 * km + (3500 if international else 0))
    per_adult *= pricing.season_factor(day, _dest_profile(d))
    per_adult *= pricing.lead_factor(lead, pricing.FLIGHT_LEAD)
    per_adult *= 1.08 if day.weekday() in (4, 6) else 1.0
    per_adult *= rng.between(0.90, 1.15, "flight", o, h or "-", d, "demand", day.isoformat(), s)
    per_adult *= mult * (0.82 if h else 1.0)
    price = pricing.round10(per_adult * (adults + CHILD_SHARE * children + INFANT_SHARE * infants))
    change_fee = None if fee is None else fee[1 if international else 0] * (adults + children)
    dep_dt, arr_dt = legs[0]["dep"], legs[-1]["arr"]
    offer = {
        "offer_id": encode("flight", params),
        "carrier": carrier_name(legs[0]["carrier"]),
        "flight_no": legs[0]["flight_no"],
        "origin_iata": o, "dest_iata": d,
        "segments": [{"origin_iata": leg["origin_iata"], "dest_iata": leg["dest_iata"], "dep": geo.iso(leg["dep"]),
                      "arr": geo.iso(leg["arr"]), "carrier": carrier_name(leg["carrier"]),
                      "flight_no": leg["flight_no"]} for leg in legs],
        "dep": geo.iso(dep_dt), "arr": geo.iso(arr_dt),
        "duration_min": max(1, int((arr_dt - dep_dt).total_seconds() // 60)),
        "stops": len(legs) - 1,
        "international": international,
        "baggage": {"checked_kg": checked, "cabin_kg": cabin},
        "fare": {"name": tier, "refundable": refundable, "change_fee_rub": change_fee},
        "price_rub": price,
        "date": day.isoformat(),
        "synthetic": True,
    }
    return offer


def search(origin: list[str], dest: list[str], day: date, adults: int, children: int, infants: int,
           flex_days: int, today: date, tiers: tuple[str, ...] = tuple(TIERS)) -> list[dict]:
    offers = []
    origins, dests = geo.expand_iata(origin), geo.expand_iata(dest)
    for delta in range(-flex_days, flex_days + 1):
        d0 = day + timedelta(days=delta)
        if d0 < today:
            continue
        for o in origins:
            for d in dests:
                for h in plans(o, d):
                    first_to = h or d
                    for s in active_slots(o, first_to, d0):
                        for tier in tiers:
                            params = {"o": o, "d": d, "h": h, "dt": d0.isoformat(), "s": s, "f": tier,
                                      "a": adults, "c": children, "i": infants, "t": today.isoformat()}
                            offer = build(params)
                            if offer:
                                offers.append(offer)
    offers.sort(key=lambda x: (x["date"], x["dep"], x["price_rub"], x["offer_id"]))
    return offers
