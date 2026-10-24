"""Search tools of the Ostrovok mock over the fixture DBs: shapes, determinism, pricing effects."""
from datetime import date, datetime, timedelta

import pytest

from ostrovok_mcp import contract as c
from ostrovok_mcp import service as s
from ostrovok_mcp.db import ServiceError

TODAY = "2026-10-25"


def flights(**kw):
    payload = {"origin_iata": ["SVO"], "dest_iata": ["AER"], "date": "2026-11-14", "adults": 2, "today": TODAY}
    payload.update(kw)
    out = s.search_flights(payload)
    c.SearchFlightsOut.model_validate(out)
    return out["offers"]


def hotels(**kw):
    payload = {"destination_id": "sochi", "check_in": "2026-11-14", "check_out": "2026-11-21", "adults": 2,
               "today": TODAY}
    payload.update(kw)
    out = s.search_hotels(payload)
    c.SearchHotelsOut.model_validate(out)
    return out


def test_every_contract_tool_has_a_plain_function():
    assert tuple(s.IMPL) == c.TOOL_NAMES
    for name in c.TOOL_NAMES:
        assert callable(getattr(s, name))


def test_unknown_tool_and_invalid_payload(mock_env):
    with pytest.raises(ServiceError):
        s.call("book_spaceship", {})
    with pytest.raises(ValueError):
        s.call("search_flights", {"origin_iata": [], "dest_iata": ["AER"], "date": "2026-11-14", "adults": 1,
                                  "today": TODAY})


# ---------------------------------------------------------------- flights

def test_direct_flights_shape_and_local_times(mock_env):
    offers = flights()
    assert offers
    for o in offers:
        assert o["stops"] == 0 and len(o["segments"]) == 1
        assert o["origin_iata"] == "SVO" and o["dest_iata"] == "AER"
        assert o["dep"].endswith("+03:00") and o["arr"].endswith("+03:00")
        assert o["international"] is False
        dep, arr = datetime.fromisoformat(o["dep"]), datetime.fromisoformat(o["arr"])
        assert (arr - dep) == timedelta(minutes=o["duration_min"])
        assert 120 <= o["duration_min"] <= 145  # ~1360 km / 800 km/h + 30 min
        assert 5 * 60 <= dep.hour * 60 + dep.minute <= 23 * 60 + 30
        assert o["carrier"] == "Аэрофлот" and o["flight_no"].startswith("SU")
    tiers = {(o["baggage"]["checked_kg"], o["fare"]["refundable"], o["fare"]["change_fee_rub"]) for o in offers}
    assert (None, False, None) in tiers          # basic
    assert (23, True, 0) in tiers                # flex (flexible_fare upsell)
    # 1-4 departures a day, three fare tiers each
    assert len(offers) % 3 == 0 and 3 <= len(offers) <= 12


def test_flights_deterministic(mock_env):
    assert flights() == flights()
    assert flights(flex_days=2) == flights(flex_days=2)


def test_metro_code_expands_to_moscow_airports(mock_env):
    origins = {o["origin_iata"] for o in flights(origin_iata=["MOW"])}
    assert origins == {"SVO", "VKO"}  # DME has no route to AER in the fixture


def test_one_stop_via_hub(mock_env):
    offers = flights(dest_iata=["AYT"])
    assert offers
    for o in offers:
        assert o["stops"] == 1
        assert [seg["dest_iata"] for seg in o["segments"]] == ["IST", "AYT"]
        assert o["international"] is True
        arr1 = datetime.fromisoformat(o["segments"][0]["arr"])
        dep2 = datetime.fromisoformat(o["segments"][1]["dep"])
        assert timedelta(minutes=90) <= dep2 - arr1 <= timedelta(hours=6)
        assert o["arr"].endswith("+03:00")  # Europe/Istanbul


def test_domestic_trip_connects_only_at_home(mock_env):
    offers = flights(origin_iata=["AER"], dest_iata=["KZN"])  # hubs: SVO (RU) and IST (TR)
    assert offers and {o["segments"][0]["dest_iata"] for o in offers} == {"SVO"}
    assert all(o["international"] is False for o in offers)


def test_no_route_no_offers(mock_env):
    assert flights(origin_iata=["KZN"], dest_iata=["AYT"]) == []  # no KZN-IST route in the fixture


def test_flex_days_and_past_dates(mock_env):
    days = {o["date"] for o in flights(flex_days=1)}
    assert days == {"2026-11-13", "2026-11-14", "2026-11-15"}
    assert flights(date="2026-10-20") == []
    near = {o["date"] for o in flights(date="2026-10-25", flex_days=2)}
    assert min(near) == "2026-10-25"


def _same_offer(offers, ref):
    return next(o for o in offers if o["flight_no"] == ref["flight_no"] and o["baggage"] == ref["baggage"]
                and o["fare"] == ref["fare"])


def test_lead_time_raises_last_minute_prices(mock_env):
    early = flights(today="2026-09-10")
    late = flights(today="2026-11-12")
    ref = early[0]
    assert _same_offer(late, ref)["price_rub"] > ref["price_rub"]


def test_season_factor_summer_beach_costs_more(mock_env):
    # same lead time (40 days), destination profile `beach`: July is peak, November is low
    nov = flights(date="2026-11-14", today="2026-10-05")
    jul = flights(date="2027-07-14", today="2027-06-04")
    cheapest = lambda offers: min(o["price_rub"] for o in offers if o["baggage"]["checked_kg"] is None)
    assert cheapest(jul) > cheapest(nov) * 1.2


def test_party_pricing_children_and_infants(mock_env):
    base = flights(adults=1)[0]
    kid = _same_offer(flights(adults=1, children=1), base)
    inf = _same_offer(flights(adults=1, infants=1), base)
    assert abs(kid["price_rub"] - base["price_rub"] * 1.75) <= 10
    assert abs(inf["price_rub"] - base["price_rub"] * 1.10) <= 10


# ---------------------------------------------------------------- trains

def trains(**kw):
    payload = {"origin_city": "Москва", "dest_city": "Казань", "date": "2026-11-14", "passengers": 2, "today": TODAY}
    payload.update(kw)
    out = s.search_trains(payload)
    c.SearchTrainsOut.model_validate(out)
    return out


def test_trains_domestic_short_route(mock_env):
    out = trains()
    assert out["offers"] and out["note"] is None
    assert {o["car_class"] for o in out["offers"]} <= {"seat", "platzkart", "coupe", "sv"}
    assert all(o["from_station"] == "Москва Казанская" for o in out["offers"])
    assert all(o["to_station"] == "Казань-Пассажирская" for o in out["offers"])
    by_class = {}
    for o in out["offers"]:
        by_class.setdefault((o["train_no"], o["car_class"]), o["price_rub"])
    for (no, cls), price in by_class.items():
        if cls == "coupe" and (no, "platzkart") in by_class:
            assert price > by_class[(no, "platzkart")]
    assert trains() == out


def test_trains_only_domestic_and_short(mock_env):
    far = trains(dest_city="Сочи")
    assert far["offers"] == [] and "1000" in far["note"]
    abroad = trains(dest_city="Анталья")
    assert abroad["offers"] == [] and "России" in abroad["note"]
    unknown = trains(dest_city="Атлантида")
    assert unknown["offers"] == [] and unknown["note"]


# ---------------------------------------------------------------- hotels

def test_hotels_shape_sort_and_limit(mock_env):
    out = hotels()
    offers = out["offers"]
    assert out["total_found"] == len(offers) == 4
    for o in offers:
        assert o["check_in"].startswith("2026-11-14T") and o["check_in"].endswith("+03:00")
        assert o["check_out"].startswith("2026-11-21T")
        assert o["osm_id"] and isinstance(o["lat"], float)
    assert hotels(limit=2)["offers"] == offers[:2]
    assert hotels() == out


def test_hotel_filters(mock_env):
    assert all(o["meals"] == "breakfast" for o in hotels(filters={"meals": "breakfast"})["offers"])
    fc = hotels(filters={"free_cancellation": True})["offers"]
    assert fc and all(o["cancellation"]["free_until"] for o in fc)
    assert all(o["stars"] >= 4 for o in hotels(filters={"stars_min": 4})["offers"])
    pets = hotels(filters={"pets": True})["offers"]
    assert pets and all(o["pets_allowed"] for o in pets)
    assert [o["kind"] for o in hotels(filters={"kinds": ["apartment"]})["offers"]] == ["apartment"]
    cap = sorted(o["price_total_rub"] for o in hotels()["offers"])[1]
    assert all(o["price_total_rub"] <= cap for o in hotels(filters={"max_price_total": cap})["offers"])


def test_hotels_children_policy_and_capacity(mock_env):
    names = {o["hotel_id"] for o in hotels(children_ages=[5])["offers"]}
    assert "h_sochi_3" not in names  # adults only
    big = hotels(adults=4, children_ages=[8, 3])["offers"]
    assert big
    for o in big:
        assert o["room"]["capacity_adults"] >= 4


def test_hotel_price_scales_with_nights_party_and_meals(mock_env):
    one = {o["hotel_id"]: o for o in hotels(check_out="2026-11-15")["offers"]}
    week = {o["hotel_id"]: o for o in hotels()["offers"]}
    assert week["h_sochi_1"]["price_total_rub"] > 5 * one["h_sochi_1"]["price_total_rub"]
    fam = {o["hotel_id"]: o for o in hotels(children_ages=[7])["offers"]}
    bfast = {o["hotel_id"]: o for o in hotels(filters={"meals": "breakfast"})["offers"]}
    assert bfast["h_sochi_1"]["price_total_rub"] > week["h_sochi_1"]["price_total_rub"]
    solo = {o["hotel_id"]: o for o in hotels(adults=1)["offers"]}
    assert solo["h_sochi_1"]["price_total_rub"] < week["h_sochi_1"]["price_total_rub"]
    assert fam["h_sochi_1"]["price_total_rub"] >= week["h_sochi_1"]["price_total_rub"]


def test_hotel_availability_noise_is_seeded(mock_env):
    starts = [date(2026, 11, 1) + timedelta(days=k) for k in range(60)]
    found = [bool(hotels(destination_id="kazan", check_in=d.isoformat(), check_out=(d + timedelta(days=3)).isoformat())
                  ["offers"]) for d in starts]
    assert 0 < found.count(False) < len(found) // 3  # some sold out, most available


def test_hotels_past_or_unknown(mock_env):
    assert hotels(check_in="2026-10-01", check_out="2026-10-03")["offers"] == []
    assert hotels(destination_id="atlantis")["offers"] == []


def test_get_hotel_details_and_real_photos_only(mock_env):
    det = s.get_hotel({"hotel_id": "h_kzn_1"})
    c.HotelDetails.model_validate(det)
    assert det["photos"][0]["source"] == "wikimedia"
    assert det["policies"]["early_checkin"] == {"available": True, "price_rub": 1800}
    assert s.get_hotel({"hotel_id": "h_sochi_1"})["photos"] == []
    with pytest.raises(ServiceError):
        s.get_hotel({"hotel_id": "nope"})


# ---------------------------------------------------------------- transfers

def transfers(**kw):
    payload = {"from": {"iata": "AER"}, "to": {"hotel_id": "h_sochi_1"}, "at": "2026-11-14T12:00:00+03:00", "pax": 2,
               "today": TODAY}
    payload.update(kw)
    out = s.search_transfers(payload)
    c.SearchTransfersOut.model_validate(out)
    return out["offers"]


def test_transfers_kinds_and_texts(mock_env):
    offers = transfers()
    kinds = {o["kind"]: o for o in offers}
    assert set(kinds) == {"taxi", "shuttle", "public", "private"}
    assert kinds["private"]["duration_min"] < kinds["public"]["duration_min"]
    assert kinds["public"]["price_rub"] < kinds["taxi"]["price_rub"] < kinds["private"]["price_rub"]
    assert all("аэропорт" in o["meeting_point"] for o in offers)
    assert transfers() == offers


def test_transfers_night_reverse_station_and_kinds_filter(mock_env):
    night = {o["kind"] for o in transfers(at="2026-11-14T02:30:00+03:00")}
    assert night == {"taxi", "private"}
    back = transfers(reverse=True)
    assert back and all("отел" in o["meeting_point"] for o in back)
    station = transfers(**{"from": {"station": "Сочи"}})
    assert station and "shuttle" not in {o["kind"] for o in station}
    assert {o["kind"] for o in transfers(kinds=["taxi"])} == {"taxi"}
    big = {o["kind"]: o["price_rub"] for o in transfers(pax=5)}
    small = {o["kind"]: o["price_rub"] for o in transfers(pax=2)}
    assert abs(big["taxi"] - 2 * small["taxi"]) <= 10  # two cars (prices rounded to 10 RUB)
    with pytest.raises(ServiceError):
        transfers(**{"from": {"iata": "XXX"}})


# ---------------------------------------------------------------- quotes

def test_quote_destinations(mock_env):
    payload = {"destination_ids": ["sochi", "antalya", "glush", "atlantis"], "origin_city": "Москва",
               "month": 11, "party": {"adults": 2, "children_ages": [7]}, "today": TODAY}
    out = s.quote_destinations(payload)
    assert s.quote_destinations(payload) == out
    c.QuoteDestinationsOut.model_validate(out)
    q = {x["destination_id"]: x for x in out["quotes"]}
    assert q["sochi"]["available"] and q["sochi"]["nights"] == 7
    assert q["sochi"]["total_rub"] == q["sochi"]["flight_price_rub"] + q["sochi"]["stay_price_rub"]
    assert date.fromisoformat(q["sochi"]["check_in"]).weekday() == 5 and q["sochi"]["check_in"].startswith("2026-11")
    assert q["sochi"]["flight_offer_id"].startswith("fl1.") and q["sochi"]["hotel_offer_id"].startswith("ho1.")
    assert q["antalya"]["available"]  # 1 stop via IST
    assert not q["glush"]["available"] and q["glush"]["total_rub"] is None and q["glush"]["notes"]
    assert not q["atlantis"]["available"] and q["atlantis"]["notes"]
    # flight price = cheapest outbound + cheapest return for the party
    out_f = flights(origin_iata=["SVO", "DME", "VKO", "ZIA"], date=q["sochi"]["check_in"], children=1)
    back_f = flights(origin_iata=["AER"], dest_iata=["SVO", "DME", "VKO", "ZIA"], date=q["sochi"]["check_out"],
                     children=1)
    assert q["sochi"]["flight_price_rub"] == min(o["price_rub"] for o in out_f) + min(o["price_rub"] for o in back_f)


def test_quote_with_dates_and_unknown_origin(mock_env):
    out = s.quote_destinations({"destination_ids": ["kazan"], "origin_city": "Москва",
                                "dates": {"check_in": "2026-11-20", "check_out": "2026-11-23"},
                                "party": {"adults": 1}, "today": TODAY})
    k = out["quotes"][0]
    assert k["available"] and k["nights"] == 3 and k["check_in"] == "2026-11-20"
    out = s.quote_destinations({"destination_ids": ["kazan"], "origin_city": "Атлантида", "month": 11,
                                "party": {"adults": 1}, "today": TODAY})
    k = out["quotes"][0]
    assert not k["available"] and k["flight_price_rub"] is None and k["stay_price_rub"] is not None
    assert "Атлантида" in k["notes"][0]


# ---------------------------------------------------------------- orchestrator additions (contract v1.1)

def test_quote_has_return_flight_id_and_notes_field(mock_env):
    out = s.call("quote_destinations", {"destination_ids": [d for d in ("antalya", "sochi") if d],
                                              "origin_city": "Москва", "month": 11,
                                              "party": {"adults": 2, "children_ages": []},
                                              "today": TODAY})
    for q in out["quotes"]:
        assert "notes" in q and "return_flight_offer_id" in q
        if q["flight_offer_id"]:
            assert q["return_flight_offer_id"] and q["return_flight_offer_id"] != q["flight_offer_id"]


def test_fare_name_marks_tiers(mock_env):
    out = s.call("search_flights", {"origin_iata": ["SVO", "DME", "VKO"], "dest_iata": ["AYT"],
                                          "date": "2026-11-14", "adults": 1, "children": 0, "infants": 0,
                                          "flex_days": 0, "today": TODAY})
    names = {o["fare"]["name"] for o in out["offers"]}
    assert names and names <= {"basic", "standard", "flex"}
    for o in out["offers"]:
        if o["fare"]["name"] == "flex":
            assert o["fare"]["refundable"]


def test_hotel_id_and_all_rooms_give_priced_alternatives(mock_env):
    base = {"destination_id": "antalya", "check_in": "2026-11-14", "check_out": "2026-11-21", "adults": 2,
            "children_ages": [], "limit": 40, "today": TODAY}
    first = s.call("search_hotels", base)["offers"][0]
    one = s.call("search_hotels", dict(base, hotel_id=first["hotel_id"], all_rooms=True))
    assert one["offers"] and {o["hotel_id"] for o in one["offers"]} == {first["hotel_id"]}
    prices = [o["price_total_rub"] for o in one["offers"]]
    assert prices[0] == min(prices)
    assert s.call("search_hotels", dict(base, hotel_id=first["hotel_id"], all_rooms=True)) == one
