"""Offline fixtures for the domain tests: offers shaped (and validated) like ostrovok_mcp/contract.py,
briefs built through agent.state (user values via apply_patch, assumptions via assume), fixed rules."""
import copy
import json
import sqlite3
from datetime import date
from pathlib import Path

from agent.state import Brief, apply_patch, assume
from ostrovok_mcp import contract as C

ROOT = Path(__file__).resolve().parents[3]
TODAY = date(2026, 10, 24)

# Fixed copies so the skill builder can tune the real files without breaking these tests.
RULES = {
    "max_questions": 2, "max_upsell": 2, "max_jokes": 1, "max_variants": 3,
    "sensitivity": {"price_delta_pct": 10, "top_changes": True},
    "buffers_min": {"before_flight_international": 180, "before_flight_domestic": 120, "before_train": 30,
                    "after_flight": 45, "after_flight_with_baggage": 75},
    "taxi_time_factor": 1.2, "risk_slack_min": 60, "late_checkin": ["22:00", "05:00"], "meeting_window_min": 60,
}
SEGMENTS = {
    "solo": {"label": "один", "party": {"adults": 1, "children": 0}, "prefs": {"district": "walkable"},
             "district_weights": {"lively": 0.35, "quiet": 0.1, "family": 0.0, "walkable": 0.55}},
    "couple": {"label": "вдвоём", "party": {"adults": 2, "children": 0}, "prefs": {"district": "quiet"},
               "district_weights": {"lively": 0.15, "quiet": 0.45, "family": 0.0, "walkable": 0.4}},
    "family": {"label": "с детьми", "party": {"adults": 2, "children": 1}, "prefs": {"district": "family"},
               "district_weights": {"lively": 0.0, "quiet": 0.3, "family": 0.5, "walkable": 0.2}},
    "friends": {"label": "компанией", "party": {"adults": 3, "children": 0}, "prefs": {"district": "lively"},
                "district_weights": {"lively": 0.55, "quiet": 0.0, "family": 0.0, "walkable": 0.45}},
}
QUESTIONS = json.loads((ROOT / "skills/trip-agent/questions.json").read_text(encoding="utf-8"))["questions"]


def rules(**over):
    r = copy.deepcopy(RULES)
    r.update(over)
    return r


# ---------------------------------------------------------------- offers

def flight(offer_id, frm, to, dep, arr, price, stops=0, baggage=None, refundable=False, change_fee=None,
           carrier="Пример Авиа", flight_no=None, international=False, layover_min=None, segments=None):
    from datetime import datetime, timedelta
    d, a = datetime.fromisoformat(dep), datetime.fromisoformat(arr)
    if segments is None:
        if stops:
            mid = d + (a - d) / 2
            lay = timedelta(minutes=layover_min or 90)
            segments = [{"origin_iata": frm, "dest_iata": "IST", "dep": dep, "arr": (mid - lay / 2).isoformat(),
                         "carrier": carrier, "flight_no": (flight_no or offer_id) + "a"},
                        {"origin_iata": "IST", "dest_iata": to, "dep": (mid + lay / 2).isoformat(), "arr": arr,
                         "carrier": carrier, "flight_no": (flight_no or offer_id) + "b"}]
        else:
            segments = [{"origin_iata": frm, "dest_iata": to, "dep": dep, "arr": arr, "carrier": carrier,
                         "flight_no": flight_no or offer_id}]
    o = {"offer_id": offer_id, "carrier": carrier, "flight_no": flight_no or offer_id, "origin_iata": frm,
         "dest_iata": to, "segments": segments, "dep": dep, "arr": arr,
         "duration_min": int((a - d).total_seconds() // 60), "stops": stops, "international": international,
         "baggage": {"checked_kg": baggage, "cabin_kg": 10}, "fare": {"refundable": refundable, "change_fee_rub": change_fee},
         "price_rub": price, "date": dep[:10]}
    return C.FlightOffer.model_validate(o).model_dump(mode="json")


def train(offer_id, frm, to, dep, arr, price, car_class="coupe", train_no="002"):
    from datetime import datetime
    d, a = datetime.fromisoformat(dep), datetime.fromisoformat(arr)
    o = {"offer_id": offer_id, "carrier": "РЖД", "train_no": train_no, "from_station": frm, "to_station": to,
         "dep": dep, "arr": arr, "duration_min": int((a - d).total_seconds() // 60), "car_class": car_class,
         "price_rub": price, "date": dep[:10]}
    return C.TrainOffer.model_validate(o).model_dump(mode="json")


def hotel(offer_id, hotel_id, name, price, check_in, check_out, stars=3, rating=8.0, meals="none",
          free_until=None, penalty_pct=100, cap_adults=2, cap_children=0, pets=False, children_allowed=True,
          min_child_age=None, early=(False, None), beach=None, center=1500, osm_id=None, kind="hotel",
          room_id="std", room_name="Стандарт", tz="+03:00", in_time="14:00", out_time="12:00"):
    o = {"offer_id": offer_id, "hotel_id": hotel_id, "osm_id": osm_id or f"node/{hotel_id}", "name": name,
         "kind": kind, "stars": stars, "rating": rating, "reviews": 120, "lat": 36.88, "lon": 30.70,
         "room": {"room_id": room_id, "name": room_name, "capacity_adults": cap_adults,
                  "capacity_children": cap_children},
         "meals": meals, "cancellation": {"free_until": free_until, "penalty_pct": penalty_pct},
         "price_total_rub": price, "pets_allowed": pets,
         "kids_policy": {"children_allowed": children_allowed, "min_child_age": min_child_age},
         "early_checkin": {"available": early[0], "price_rub": early[1]},
         "check_in": f"{check_in}T{in_time}:00{tz}", "check_out": f"{check_out}T{out_time}:00{tz}",
         "distance_beach_m": beach, "distance_center_m": center}
    return C.HotelOffer.model_validate(o).model_dump(mode="json")


def transfer(offer_id, kind, minutes, price, point=None):
    o = C.TransferOffer.model_validate({"offer_id": offer_id, "kind": kind, "duration_min": minutes,
                                        "price_rub": price, "meeting_point": "У выхода"}).model_dump(mode="json")
    if point:
        o["point"] = point
    return o


# ---------------------------------------------------------------- briefs

def brief(user=None, assumed=None):
    """`user`: {patch path: value} (source user); `assumed`: {path: (value, source)}."""
    b = Brief()
    if user:
        b, _ = apply_patch(b, {"set": user})
    for path, (value, source) in (assumed or {}).items():
        b = assume(b, path, value, source)
    return b


# ---------------------------------------------------------------- scenarios

def sea_results():
    """Antalya, 12–19 Nov, couple. Cheap night flight with a stop, cheap and comfortable direct ones."""
    tz = "+03:00"
    out = [
        flight("F1", "SVO", "AYT", f"2026-11-12T06:40:00{tz}", f"2026-11-12T10:50:00{tz}", 22000, international=True),
        flight("F2", "SVO", "AYT", f"2026-11-12T10:15:00{tz}", f"2026-11-12T14:25:00{tz}", 30000, baggage=23,
               refundable=True, change_fee=0, international=True),
        flight("F3", "VKO", "AYT", f"2026-11-12T01:30:00{tz}", f"2026-11-12T11:30:00{tz}", 18000, stops=1,
               layover_min=50, international=True),
    ]
    back = [
        flight("B1", "AYT", "SVO", f"2026-11-19T19:20:00{tz}", f"2026-11-19T23:30:00{tz}", 21000, international=True),
        flight("B2", "AYT", "SVO", f"2026-11-19T15:00:00{tz}", f"2026-11-19T19:10:00{tz}", 29000, baggage=23,
               refundable=True, change_fee=0, international=True),
    ]
    hotels = [
        hotel("H1", "h1", "Гостевой дом Лаванда", 40000, "2026-11-12", "2026-11-19", stars=2, rating=7.8, beach=900,
              early=(True, 1500)),
        hotel("H2", "h2", "Отель Ривьера", 70000, "2026-11-12", "2026-11-19", stars=4, rating=8.6, meals="breakfast",
              free_until="2026-11-10T23:59:00+03:00", penalty_pct=0, beach=300),
        hotel("H3", "h3", "Резорт Палмс", 120000, "2026-11-12", "2026-11-19", stars=5, rating=9.3,
              meals="all_inclusive", free_until="2026-11-10T23:59:00+03:00", penalty_pct=0, beach=50),
        hotel("H2b", "h2", "Отель Ривьера", 82000, "2026-11-12", "2026-11-19", stars=4, rating=8.6,
              meals="breakfast", free_until="2026-11-10T23:59:00+03:00", penalty_pct=0, beach=300,
              room_id="sea", room_name="Номер с видом на море"),
        hotel("H2c", "h2", "Отель Ривьера", 79000, "2026-11-12", "2026-11-19", stars=4, rating=8.6,
              meals="half_board", free_until="2026-11-10T23:59:00+03:00", penalty_pct=0, beach=300),
    ]
    tr = {h: [transfer(f"T{h}s", "shuttle", 60, 800), transfer(f"T{h}t", "taxi", 40, 2500)] for h in ("h1", "h2", "h3")}
    trb = {h: [transfer(f"R{h}s", "shuttle", 60, 800), transfer(f"R{h}t", "taxi", 40, 2500)] for h in ("h1", "h2", "h3")}
    return {"flights_out": out, "flights_back": back, "hotels": hotels, "transfers_out": tr, "transfers_back": trb,
            "destination": {"id": "antalya", "name": "Анталья", "country": "Турция"}}


DISTRICTS = {
    "node/h1": {"labels": ["quiet"], "scores": {"lively": 0.1, "quiet": 0.85, "family": 0.4, "walkable": 0.2},
                "counts": {"playground": 0, "bar": 0, "cafe": 3}, "nearest_m": {"beach": 900}},
    "node/h2": {"labels": ["walkable"], "scores": {"lively": 0.4, "quiet": 0.5, "family": 0.6, "walkable": 0.8},
                "counts": {"playground": 2, "bar": 4, "cafe": 12}, "nearest_m": {"beach": 300}},
    "node/h3": {"labels": ["lively", "family"], "scores": {"lively": 0.7, "quiet": 0.3, "family": 0.7, "walkable": 0.6},
                "counts": {"playground": 3, "bar": 14, "cafe": 20}, "nearest_m": {"beach": 50}},
}


def sea_brief(**extra_user):
    user = {"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-12",
            "dates.end": "2026-11-19", "party.segment": "couple", "party.adults": 2, "traits.add": ["sea"]}
    user.update(extra_user)
    return brief(user)


# ---------------------------------------------------------------- open data fixture DB

def make_opendata(path, destinations, climate=(), airports=(), routes=(), districts=()):
    """Create an opendata sqlite from data/schema/opendata.sql with the given rows (dicts)."""
    con = sqlite3.connect(path)
    con.executescript((ROOT / "data/schema/opendata.sql").read_text(encoding="utf-8"))
    for d in destinations:
        row = {"country_code": "XX", "kind": "resort", "tags": [], "airports": [], "stations": [], "timezone": None,
               "description_ru": None, "popularity": 0.5, "source": "test", "fetched_at": "2026-10-24T00:00:00Z", **d}
        for k in ("tags", "airports", "stations"):
            row[k] = json.dumps(row[k], ensure_ascii=False)
        con.execute("INSERT INTO destinations (id,name_ru,name_en,country,country_code,lat,lon,kind,tags,airports,stations,"
                    "timezone,description_ru,popularity,source,fetched_at) VALUES (:id,:name_ru,:name_en,:country,"
                    ":country_code,:lat,:lon,:kind,:tags,:airports,:stations,:timezone,:description_ru,:popularity,"
                    ":source,:fetched_at)", row)
    for c in climate:
        row = {"air_min_c": None, "sea_c": None, "rain_days": 0, "sunny_days": 20, "source": "test",
               "fetched_at": "2026-10-24T00:00:00Z", **c}
        con.execute("INSERT INTO climate VALUES (:destination_id,:month,:air_max_c,:air_min_c,:sea_c,:rain_days,"
                    ":sunny_days,:source,:fetched_at)", row)
    for a in airports:
        row = {"city": None, "type": "large_airport", "source": "test", "fetched_at": "2026-10-24T00:00:00Z", **a}
        row.setdefault("ident", row["iata"])
        con.execute("INSERT INTO airports VALUES (:ident,:iata,:name,:city,:country_code,:lat,:lon,:type,:source,:fetched_at)", row)
    for r in routes:
        row = {"stops": 0, "carriers": "[]", "source": "test", "fetched_at": "2026-10-24T00:00:00Z", **r}
        con.execute("INSERT INTO routes VALUES (:origin_iata,:dest_iata,:stops,:carriers,:source,:fetched_at)", row)
    for d in districts:
        row = {"destination_id": None, "poi_counts": "{}", "nearest_m": "{}", "source": "test",
               "fetched_at": "2026-10-24T00:00:00Z", **d}
        for k in ("poi_counts", "nearest_m"):
            if not isinstance(row[k], str):
                row[k] = json.dumps(row[k])
        con.execute("INSERT INTO districts VALUES (:osm_id,:destination_id,:lat,:lon,:poi_counts,:nearest_m,:lively,"
                    ":quiet,:family,:walkable,:source,:fetched_at)", row)
    con.commit()
    con.close()
    return path
