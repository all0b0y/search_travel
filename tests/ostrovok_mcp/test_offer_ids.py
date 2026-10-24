"""Offer ids: deterministic, self-describing, tamper-evident; cart_add rebuilds offers from ids alone."""
import base64
import json

import pytest

from ostrovok_mcp import db, offer_ids
from ostrovok_mcp import service as s
from ostrovok_mcp.db import ServiceError

TODAY = "2026-10-25"
NOW = "2026-10-25T13:05:00+03:00"


def _flight():
    return s.search_flights({"origin_iata": ["SVO"], "dest_iata": ["AER"], "date": "2026-11-14", "adults": 2,
                             "today": TODAY})["offers"][0]


def test_round_trip_and_format():
    params = {"o": "SVO", "d": "AER", "x": [1, 2], "t": "2026-10-25"}
    oid = offer_ids.encode("flight", params)
    assert oid.startswith("fl1.") and oid.count(".") == 2
    assert offer_ids.decode(oid) == ("flight", params)
    assert offer_ids.encode("flight", params) == oid


@pytest.mark.parametrize("mutate", [
    lambda oid: oid[:-1] + ("0" if oid[-1] != "0" else "1"),                         # checksum byte
    lambda oid: oid.split(".")[0] + "." + oid.split(".")[1][:-2] + "AA." + oid.split(".")[2],  # payload
    lambda oid: "ho1." + oid.split(".", 1)[1],                                          # prefix
    lambda oid: oid.replace(".", "", 1),                                                # shape
    lambda oid: "zz1." + oid.split(".", 1)[1],                                          # unknown prefix
])
def test_tampered_ids_rejected(mutate):
    oid = offer_ids.encode("flight", {"o": "SVO", "d": "AER"})
    with pytest.raises(ServiceError) as err:
        offer_ids.decode(mutate(oid))
    assert err.value.code == "invalid_offer_id"


def test_forged_params_with_old_checksum_rejected(mock_env):
    oid = _flight()["offer_id"]
    prefix, payload, check = oid.split(".")
    params = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    params["a"] = 1  # cheaper party, same checksum
    forged = base64.urlsafe_b64encode(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).decode()
    with pytest.raises(ServiceError):
        s.cart_add({"session_id": "s1", "offer_id": f"{prefix}.{forged.rstrip('=')}.{check}", "kind": "flight",
                    "now": NOW})


def test_cart_add_rebuilds_offer_from_id_alone(mock_env):
    offer = _flight()
    db.reset_caches()  # nothing cached between search and add
    cart = s.cart_add({"session_id": "s1", "offer_id": offer["offer_id"], "kind": "flight", "now": NOW})
    item = cart["items"][0]
    assert item["details"]["offer"] == offer
    assert item["price_rub"] == offer["price_rub"] and item["start"] == offer["dep"]


def test_kind_mismatch_and_unavailable_offer(mock_env):
    offer = _flight()
    with pytest.raises(ServiceError) as err:
        s.cart_add({"session_id": "s1", "offer_id": offer["offer_id"], "kind": "hotel", "now": NOW})
    assert err.value.code == "offer_kind_mismatch"
    _, params = offer_ids.decode(offer["offer_id"])
    from ostrovok_mcp import flights
    from datetime import date
    inactive = next(i for i in range(flights.SLOTS) if i not in flights.active_slots("SVO", "AER", date(2026, 11, 14)))
    ghost = offer_ids.encode("flight", dict(params, s=inactive))
    with pytest.raises(ServiceError) as err:
        s.cart_add({"session_id": "s1", "offer_id": ghost, "kind": "flight", "now": NOW})
    assert err.value.code == "offer_unavailable"
    incomplete = offer_ids.encode("hotel", {"h": "h_sochi_1"})
    with pytest.raises(ServiceError) as err:
        s.cart_add({"session_id": "s1", "offer_id": incomplete, "kind": "hotel", "now": NOW})
    assert err.value.code == "invalid_offer_id"


def test_every_offer_kind_round_trips(mock_env):
    hotel = s.search_hotels({"destination_id": "sochi", "check_in": "2026-11-14", "check_out": "2026-11-21",
                             "adults": 2, "children_ages": [4], "today": TODAY})["offers"][0]
    train = s.search_trains({"origin_city": "Москва", "dest_city": "Казань", "date": "2026-11-14", "passengers": 2,
                             "today": TODAY})["offers"][0]
    transfer = s.search_transfers({"from": {"iata": "AER"}, "to": {"hotel_id": "h_sochi_1"},
                                   "at": "2026-11-14T12:00:00+03:00", "pax": 3, "today": TODAY})["offers"][0]
    for kind, offer, price_key in (("hotel", hotel, "price_total_rub"), ("train", train, "price_rub"),
                                   ("transfer", transfer, "price_rub")):
        cart = s.cart_add({"session_id": f"rt-{kind}", "offer_id": offer["offer_id"], "kind": kind, "now": NOW})
        assert cart["items"][0]["price_rub"] == offer[price_key]
        assert cart["items"][0]["details"]["offer"]["offer_id"] == offer["offer_id"]
