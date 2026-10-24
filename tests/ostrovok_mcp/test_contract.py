import json

import pytest
from pydantic import ValidationError

from ostrovok_mcp import contract as c

TABLE = ("quote_destinations", "search_hotels", "get_hotel", "search_flights", "search_trains", "search_transfers",
         "cart_create", "cart_add", "cart_remove", "cart_get", "checkout", "pay", "order_get",
         "order_request_early_checkin", "order_change_dates", "order_add_transfer",
         "insights_popular_destinations", "insights_segment_preferences", "insights_log_event")
NOW = "2026-10-25T13:05:00+03:00"
TODAY = "2026-10-25"

KIDS = {"children_allowed": True, "min_child_age": None, "infant_cot": "free", "extra_bed_price_rub": 1500}
ROOM = {"room_id": "r1", "name": "Стандарт, 2 взрослых", "capacity_adults": 2, "capacity_children": 1, "bed": "double"}
HOTEL_OFFER = {
    "offer_id": "ho_1", "hotel_id": "h1", "osm_id": "node/1", "name": "Пример Отель", "kind": "hotel", "stars": 4,
    "rating": 8.6, "reviews": 312, "lat": 43.4, "lon": 39.9, "room": ROOM, "meals": "breakfast",
    "cancellation": {"free_until": "2026-11-09T23:59:00+03:00", "penalty_pct": 30}, "price_total_rub": 58800,
    "pets_allowed": False, "kids_policy": KIDS, "early_checkin": {"available": True, "price_rub": 4500},
    "check_in": "2026-11-12T14:00:00+03:00", "check_out": "2026-11-19T12:00:00+03:00",
    "distance_beach_m": 400, "distance_center_m": 2100,
}
SEG = {"origin_iata": "SVO", "dest_iata": "AER", "dep": "2026-11-12T06:40:00+03:00",
       "arr": "2026-11-12T09:05:00+03:00", "carrier": "Пример Авиа", "flight_no": "EX101"}
FLIGHT = {"offer_id": "fl_1", "carrier": "Пример Авиа", "flight_no": "EX101", "origin_iata": "SVO", "dest_iata": "AER",
          "segments": [SEG], "dep": SEG["dep"], "arr": SEG["arr"], "duration_min": 145, "stops": 0,
          "international": False, "baggage": {"checked_kg": 23, "cabin_kg": 10},
          "fare": {"refundable": False, "change_fee_rub": 3000}, "price_rub": 11200, "date": "2026-11-12"}
CART = {"cart_id": "c1", "session_id": "s_1", "items": [{"item_id": "ci1", "offer_id": "fl_1", "kind": "flight",
        "title": "Перелёт", "price_rub": 11200, "start": SEG["dep"], "end": SEG["arr"]}], "total_rub": 11200}
ORDER = {"order_id": "o1", "cart_id": "c1", "session_id": "s_1", "status": "paid",
         "items": [{"item_id": "oi1", "offer_id": "fl_1", "kind": "flight", "title": "Перелёт", "price_rub": 11200,
                    "booking_number": "PNR1", "status": "confirmed"}],
         "travellers": [{"name": "Иван", "birth_date": "1990-01-01"}], "contact": {"phone": "+70000000000"},
         "total_rub": 11200, "events": [{"at": NOW, "kind": "paid"}]}

SAMPLES = {
    "quote_destinations": (
        {"destination_ids": ["antalya"], "origin_city": "Москва", "month": 11, "party": {"adults": 2, "children_ages": [4]},
         "today": TODAY},
        {"quotes": [{"destination_id": "antalya", "available": True, "nights": 7, "check_in": "2026-11-14",
                     "check_out": "2026-11-21", "flight_price_rub": 46800, "stay_price_rub": 71600,
                     "total_rub": 118400, "currency_notes": []}]}),
    "search_hotels": (
        {"destination_id": "sochi", "check_in": "2026-11-12", "check_out": "2026-11-19", "adults": 2,
         "filters": {"meals": "breakfast", "free_cancellation": True}, "limit": 40, "today": TODAY},
        {"offers": [HOTEL_OFFER], "total_found": 1}),
    "get_hotel": (
        {"hotel_id": "h1"},
        {"hotel_id": "h1", "osm_id": "node/1", "destination_id": "sochi", "name": "Пример Отель", "kind": "hotel",
         "lat": 43.4, "lon": 39.9, "policies": {"check_in_from": "14:00", "check_out_until": "12:00",
         "pets_allowed": False, "kids_policy": KIDS, "early_checkin": {"available": False}}, "rooms": [ROOM]}),
    "search_flights": (
        {"origin_iata": ["SVO", "VKO"], "dest_iata": ["AER"], "date": "2026-11-12", "adults": 2, "flex_days": 1,
         "today": TODAY},
        {"offers": [FLIGHT]}),
    "search_trains": (
        {"origin_city": "Москва", "dest_city": "Казань", "date": "2026-11-12", "passengers": 2, "today": TODAY},
        {"offers": [], "note": "Поезда только по России до ~1000 км"}),
    "search_transfers": (
        {"from": {"iata": "AER"}, "to": {"hotel_id": "h1"}, "at": "2026-11-12T09:50:00+03:00", "pax": 2,
         "today": TODAY},
        {"offers": [{"offer_id": "tr_1", "kind": "taxi", "duration_min": 35, "price_rub": 1500,
                     "meeting_point": "Выход из зоны прилёта"}]}),
    "cart_create": ({"session_id": "s_1", "now": NOW}, {"cart_id": "c1", "session_id": "s_1"}),
    "cart_add": ({"session_id": "s_1", "offer_id": "fl_1", "kind": "flight", "now": NOW}, CART),
    "cart_remove": ({"session_id": "s_1", "offer_id": "fl_1", "kind": "flight"}, {"cart_id": "c1", "session_id": "s_1"}),
    "cart_get": ({"session_id": "s_1"}, CART),
    "checkout": (
        {"cart_id": "c1", "travellers": [{"name": "Иван", "birth_date": "1990-01-01"}],
         "contact": {"email": "ivan@example.com"}, "now": NOW},
        {"order_id": "o1", "total_rub": 11200, "issues": [{"code": "infant_fare", "message": "Нужен билет младенца",
                                                           "severity": "info", "item_ids": ["oi1"]}]}),
    "pay": ({"order_id": "o1", "method": "mock_card", "now": NOW},
            {"order_id": "o1", "bookings": [{"item_id": "oi1", "booking_number": "PNR1"}]}),
    "order_get": ({"order_id": "o1"}, ORDER),
    "order_request_early_checkin": ({"order_id": "o1", "time": "10:00", "now": NOW},
                                    {"order_id": "o1", "status": "on_request", "price_rub": 4500}),
    "order_change_dates": (
        {"order_id": "o1", "new_dates": {"check_in": "2026-11-14", "check_out": "2026-11-21"}, "confirm": False,
         "now": NOW},
        {"order_id": "o1", "available": True, "quote": {"old_total_rub": 58800, "new_total_rub": 61200,
                                                         "difference_rub": 2400}}),
    "order_add_transfer": ({"order_id": "o1", "offer_id": "tr_1", "now": NOW}, ORDER),
    "insights_popular_destinations": (
        {"origin_city": "Москва", "month": 11, "segment": "family"},
        {"items": [{"destination_id": "antalya", "share": 0.31, "n": 412}]}),
    "insights_segment_preferences": (
        {"segment": "family", "destination_kind": "resort"},
        {"segment": "family", "available": True, "n": 950,
         "shares": {"breakfast": 0.4, "all_inclusive": 0.45, "free_cancellation": 0.6, "direct_flights": 0.8},
         "median_nights": 7, "budget_rub_percentiles": {"p25": 90000, "p50": 140000, "p75": 210000},
         "district_preference": {"lively": 0.1, "quiet": 0.3, "family": 0.5, "walkable": 0.1}}),
    "insights_log_event": ({"event": {"kind": "variant_chosen", "segment": "couple", "destination_id": "sochi",
                                      "month": 11, "data": {"variant": "fastest"}, "at": NOW}}, {"ok": True}),
}


def test_tool_names_exact():
    assert c.TOOL_NAMES == TABLE
    assert set(SAMPLES) == set(TABLE)


@pytest.mark.parametrize("tool", TABLE)
def test_models_instantiate_and_round_trip(tool):
    model_in, model_out = c.TOOLS[tool]
    raw_in, raw_out = SAMPLES[tool]
    for model, raw in ((model_in, raw_in), (model_out, raw_out)):
        obj = model.model_validate(raw)
        dumped = obj.model_dump(mode="json")
        json.dumps(dumped, ensure_ascii=False)
        assert model.model_validate(dumped) == obj
        assert model.model_validate_json(obj.model_dump_json()) == obj


def test_synthetic_markers_default_true():
    assert c.SearchHotelsOut.model_validate(SAMPLES["search_hotels"][1]).offers[0].synthetic is True
    assert c.SearchFlightsOut.model_validate(SAMPLES["search_flights"][1]).synthetic is True
    assert c.InsightsPopularDestinationsOut.model_validate(SAMPLES["insights_popular_destinations"][1]).synthetic


def test_transfer_from_alias():
    obj = c.SearchTransfersIn.model_validate(SAMPLES["search_transfers"][0])
    assert obj.model_dump(mode="json")["from"]["iata"] == "AER"


@pytest.mark.parametrize("model, raw", [
    (c.SearchFlightsIn, dict(SAMPLES["search_flights"][0], flex_days=5)),
    (c.SearchHotelsIn, dict(SAMPLES["search_hotels"][0], limit=41)),
    (c.SearchHotelsIn, dict(SAMPLES["search_hotels"][0], check_out="2026-11-12")),
    (c.QuoteDestinationsIn, dict(SAMPLES["quote_destinations"][0], dates={"check_in": "2026-11-01",
                                                                          "check_out": "2026-11-08"})),
    (c.QuoteDestinationsIn, {k: v for k, v in SAMPLES["quote_destinations"][0].items() if k != "month"}),
    (c.CartCreateIn, {"session_id": "s", "now": "2026-10-25T13:05:00"}),
    (c.SearchTransfersIn, dict(SAMPLES["search_transfers"][0], **{"from": {"iata": "AER", "lat": 1.0, "lon": 2.0}})),
    (c.PayIn, {"order_id": "o1", "method": "real_card", "now": NOW}),
    (c.CheckoutIn, dict(SAMPLES["checkout"][0], contact={})),
    (c.InsightsPopularDestinationsOut, {"items": [{"destination_id": "x", "share": 0.5, "n": 19}]}),
    (c.InsightsLogEventIn, {"event": {"kind": "paid", "data": {"email": "a@b"}, "at": NOW}}),
    (c.OrderRequestEarlyCheckinIn, {"order_id": "o1", "time": "25:00", "now": NOW}),
    (c.HotelOffer, dict(HOTEL_OFFER, price_total_rub=-1)),
])
def test_invalid_inputs_rejected(model, raw):
    with pytest.raises(ValidationError):
        model.model_validate(raw)
