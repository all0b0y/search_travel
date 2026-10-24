"""Deterministic synthetic generation and the price table."""
import statistics

import pytest

from data_build.destinations import DESTINATIONS
from data_build.prices import (CITY_PRICE_LEVEL_RUB, KIND_FACTOR, MEAL_MULTIPLIERS, PRICE_LEVEL_RUB,
                               SEASON_PROFILES, STARS_FACTOR, base_price_rub, price_level, season_factors,
                               season_profile)
from data_build.synthetic import parse_osm_stars, synth_hotel

DEST = {"id": "antalya", "country_code": "TR"}


def _h(osm_id, kind="hotel", stars=None):
    return {"hotel_id": "h_" + osm_id.replace("/", ""), "osm_id": osm_id, "kind": kind, "stars": stars}


def test_same_input_same_output():
    assert synth_hotel(_h("node/42"), DEST) == synth_hotel(_h("node/42"), DEST)


def test_different_ids_different_values():
    outs = [synth_hotel(_h(f"node/{i}"), DEST) for i in range(200)]
    assert len({o["rating"] for o in outs}) > 20
    assert len({o["reviews"] for o in outs}) > 150
    assert len({o["rooms"][0]["base_price_rub"] for o in outs}) > 50
    assert len({o["stars"] for o in outs}) >= 3


def test_ranges_and_structure():
    for i in range(300):
        kind = ("hotel", "hostel", "apartment", "guest_house", "resort", "motel", "chalet")[i % 7]
        o = synth_hotel(_h(f"way/{i}", kind), {"id": "x", "country_code": "RU"})
        assert 6.5 <= o["rating"] <= 9.6
        assert o["reviews"] >= 1
        assert 1 <= o["stars"] <= 5
        assert 2 <= len(o["rooms"]) <= 4
        for r in o["rooms"]:
            assert r["capacity_adults"] >= 1 and r["base_price_rub"] > 0
            assert set(r["meals_options"]) <= set(MEAL_MULTIPLIERS)
        p = o["policy"]
        if p["free_cancellation_days"] is None:
            assert p["penalty_pct"] == 100
        if not p["pets_allowed"]:
            assert o["extras"]["pets_fee_rub"] is None
        if not p["children_allowed"]:
            assert o["extras"]["child_free_under_age"] is None
        if p["early_checkin_available"]:
            assert p["early_checkin_price_rub"] >= 500
        else:
            assert p["early_checkin_price_rub"] is None
        assert p["infant_cot"] in ("free", "paid", "on_request", "none")
        assert "rating" in o["synthetic_fields"] and "policies" in o["synthetic_fields"]


def test_osm_stars_are_kept_and_marked_real():
    o = synth_hotel(_h("node/7", stars=4), DEST)
    assert o["stars"] == 4 and o["stars_real"] and "stars" not in o["synthetic_fields"]
    o2 = synth_hotel(_h("node/7"), DEST)
    assert "stars" in o2["synthetic_fields"]
    assert parse_osm_stars("4S") == 4 and parse_osm_stars("3.5") == 3 and parse_osm_stars("yes") is None
    assert parse_osm_stars("7") is None and parse_osm_stars(None) is None


def test_all_inclusive_in_turkish_resorts_not_in_hostels():
    resorts = [synth_hotel(_h(f"node/{i}", "resort"), DEST) for i in range(50)]
    assert all("all_inclusive" in o["rooms"][0]["meals_options"] for o in resorts)
    hostels = [synth_hotel(_h(f"node/{i}", "hostel"), DEST) for i in range(50)]
    assert not any("all_inclusive" in o["rooms"][0]["meals_options"] for o in hostels)


def test_price_table_covers_every_destination_country():
    for d in DESTINATIONS:
        assert d.country_code in PRICE_LEVEL_RUB, d.country_code
    for did in CITY_PRICE_LEVEL_RUB:
        assert any(d.id == did for d in DESTINATIONS), did
    assert price_level("moscow", "RU") == CITY_PRICE_LEVEL_RUB["moscow"]
    assert price_level("kostroma", "RU") == 5000
    assert price_level("nowhere", "RU") == PRICE_LEVEL_RUB["RU"]


def test_base_price_formula():
    assert base_price_rub(10000, 3, "hotel", 1.0, 1.0) == 10000
    assert base_price_rub(10000, 5, "hotel", 1.0, 1.0) == int(10000 * STARS_FACTOR[5])
    assert base_price_rub(10000, 3, "hostel", 1.0, 1.0) == int(10000 * KIND_FACTOR["hostel"])
    assert base_price_rub(10000, 4, "hotel", 1.0, 1.0) > base_price_rub(10000, 3, "hotel", 1.0, 1.0)
    assert base_price_rub(10003, 3, "hotel", 1.0, 1.0) % 50 == 0
    assert base_price_rub(10, 1, "hostel", 0.1, 0.5) == 300          # floor


def test_season_profiles():
    for name, f in SEASON_PROFILES.items():
        assert len(f) == 12
        assert 0.85 <= statistics.mean(f) <= 1.2, name
    assert season_profile("antalya", "TR", "resort", ["sea", "beach"]) == "med_beach"
    assert season_profile("sochi", "RU", "resort", ["sea", "beach"]) == "ru_sea"
    assert season_profile("krasnaya-polyana", "RU", "resort", ["ski", "mountains"]) == "ski"
    assert season_profile("dubai", "AE", "city", ["sea", "beach"]) == "gulf"
    assert season_profile("hurghada", "EG", "resort", ["sea", "beach"]) == "red_sea"
    assert season_profile("phuket", "TH", "resort", ["sea", "beach", "tropical"]) == "tropical"
    assert season_profile("saint-petersburg", "RU", "city", ["city"]) == "white_nights"
    assert season_profile("paris", "FR", "city", ["city"]) == "city"
    f = season_factors("antalya", "TR", "resort", ["sea", "beach"])
    assert f[7] > f[0]                                               # August dearer than January
    assert season_factors("dubai", "AE", "city", ["sea"])[6] < season_factors("dubai", "AE", "city", ["sea"])[0]
    assert MEAL_MULTIPLIERS["none"] == 1.0
    assert MEAL_MULTIPLIERS["all_inclusive"] > MEAL_MULTIPLIERS["half_board"] > MEAL_MULTIPLIERS["breakfast"]


@pytest.mark.parametrize("d", DESTINATIONS, ids=lambda d: d.id)
def test_every_destination_has_a_season_profile(d):
    assert season_profile(d.id, d.country_code, d.kind, d.tags) in SEASON_PROFILES
