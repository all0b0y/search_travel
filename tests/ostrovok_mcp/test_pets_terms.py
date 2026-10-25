"""Pet carriage terms on train and flight offers (RZD rules since 01.01.2025 for trains, one generic mock rule
for flights; fees synthetic) and the `pets` search input that prices the animals into the tickets."""
from pathlib import Path

import pytest

from ostrovok_mcp import contract as c
from ostrovok_mcp import service as s

from .conftest import NOW, TODAY

ROOT = Path(__file__).resolve().parents[2]
REAL = [ROOT / "data" / "ostrovok_catalog.sqlite", ROOT / "data" / "opendata.sqlite"]


def trains(**kw):
    payload = {"origin_city": "Москва", "dest_city": "Казань", "date": "2026-11-14", "passengers": 2, "today": TODAY}
    payload.update(kw)
    out = s.search_trains(payload)
    c.SearchTrainsOut.model_validate(out)
    return out["offers"]


def flights(**kw):
    payload = {"origin_iata": ["SVO"], "dest_iata": ["AER"], "date": "2026-11-14", "adults": 1, "today": TODAY}
    payload.update(kw)
    out = s.search_flights(payload)
    c.SearchFlightsOut.model_validate(out)
    return out["offers"]


def by_slot(offers):
    return {(o["train_no"], o["car_class"]): o for o in offers}


def test_long_distance_terms_by_class(mock_env):
    offers = trains()
    assert offers and all(o["train_kind"] == "long_distance" for o in offers if o["car_class"] != "seat")
    for o in offers:
        t = o["pets"]
        assert t["small_in_carrier"] is True and t["carrier_max_cm_sum"] == 180
        assert t["max_animals"] == 2 * 2 and t["guide_dogs_free"] and t["synthetic_fee"]
        assert t["fee_rub"] > 0 and t["count"] == 0 and t["fee_total_rub"] == 0
        if o["car_class"] in ("coupe", "sv"):
            assert t["large_dogs"] is True and "отдельном купе" in t["large_dogs_how"]
            assert "наморднике" in t["large_dogs_how"] and "поводке" in t["large_dogs_how"]
            assert t["large_dog_fee_rub"] is not None
        else:
            assert t["large_dogs"] is False and t["large_dog_fee_rub"] is None


def test_compartment_buyout_counts_free_places(mock_env):
    one, two = by_slot(trains(passengers=1)), by_slot(trains(passengers=2))
    sv = [k for k in one if k[1] == "sv"]
    assert sv
    assert two[sv[0]]["pets"]["large_dog_fee_rub"] == 0          # two passengers fill an SV compartment
    assert one[sv[0]]["pets"]["large_dog_fee_rub"] > 0


def test_pets_are_priced_into_train_offers_and_kept_in_the_id(mock_env):
    plain, dog = by_slot(trains()), by_slot(trains(pets=1))
    assert plain.keys() == dog.keys()
    for k, o in dog.items():
        t = o["pets"]
        assert t["count"] == 1 and t["fee_total_rub"] == t["fee_rub"]
        assert o["price_rub"] == plain[k]["price_rub"] + t["fee_rub"]
        assert o["offer_id"] != plain[k]["offer_id"]
    # backwards compatible: without pets the offer ids and prices stay as they were
    assert trains() == trains(pets=0)
    # more animals than two per ticket: nothing priced
    many = trains(passengers=1, pets=3)
    assert all(o["pets"]["count"] == 0 and o["pets"]["fee_total_rub"] == 0 for o in many)


def test_cart_rebuilds_the_pet_fee_from_the_offer_id(mock_env):
    offer = trains(pets=1)[0]
    cart = s.cart_add({"session_id": "s_pets", "offer_id": offer["offer_id"], "kind": "train", "now": NOW})
    assert cart["total_rub"] == offer["price_rub"]
    assert cart["items"][0]["details"]["offer"]["pets"]["count"] == 1


def test_flight_terms_cabin_and_hold(mock_env):
    plain, cat = flights(), flights(pets=1)
    assert plain and len(plain) == len(cat)
    for a, b in zip(plain, cat):
        t = b["pets"]
        assert t["small_in_carrier"] and t["carrier_max_kg"] == 8 and t["large_dogs"] is True
        assert "багажном отсеке" in t["large_dogs_how"] and t["large_dog_fee_rub"] > 0
        assert t["max_animals"] == 1 and t["count"] == 1
        assert b["price_rub"] == a["price_rub"] + t["fee_rub"]
        assert a["pets"]["count"] == 0
    intl = flights(dest_iata=["IST"])
    assert intl and min(o["pets"]["fee_rub"] for o in intl) > max(o["pets"]["fee_rub"] for o in plain)


@pytest.mark.skipif(not all(p.exists() for p in REAL), reason="real data DBs not built")
def test_sapsan_seats_take_small_pets_only(tmp_path, monkeypatch):
    from ostrovok_mcp import db
    monkeypatch.setenv("OSTROVOK_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("OSTROVOK_CATALOG_EXTRA_DB", str(tmp_path / "no_catalog_extra.sqlite"))
    monkeypatch.setenv("OPENDATA_EXTRA_DB", str(tmp_path / "no_opendata_extra.sqlite"))
    db.reset_caches()
    try:
        offers = trains(dest_city="Санкт-Петербург", date="2026-10-31", passengers=1, pets=1)
    finally:
        db.reset_caches()
    sapsan = [o for o in offers if o["train_kind"] == "high_speed"]
    assert sapsan and all("Сапсан" in o["carrier"] and o["car_class"] == "seat" for o in sapsan)
    assert any(o["train_kind"] == "long_distance" for o in offers)
    for o in sapsan:
        t = o["pets"]
        assert t["small_in_carrier"] and t["carrier_max_cm_sum"] == 180
        assert t["places"] == "места для пассажиров с животными"
        assert t["large_dogs"] is False and t["count"] == 1 and t["max_animals"] == 2
