"""Time zones, distances and city resolution used by the generators."""
from datetime import date, datetime

from ostrovok_mcp import geo, pricing, rng


def offset(tz, day=date(2026, 11, 14)):
    return geo.local_dt(day, 12 * 60, tz).isoformat()[-6:]


def test_zone_priority_and_fallbacks():
    assert offset(geo.zone("Europe/Istanbul")) == "+03:00"
    assert offset(geo.zone(None, "RU", 60.6, "SVX")) == "+05:00"       # RU airport table
    assert offset(geo.zone(None, "RU", 20.5)) == "+02:00"               # RU longitude band (Kaliningrad)
    assert offset(geo.zone(None, "RU", 82.9)) == "+07:00"               # Novosibirsk band
    assert offset(geo.zone(None, "TH", 100.5)) == "+07:00"              # country table
    assert offset(geo.zone(None, "XX", 76.0)) == "+05:00"               # lon / 15 fallback
    assert offset(geo.zone("Not/AZone", "AE", 55.3)) == "+04:00"        # bad name -> next candidate
    berlin = geo.zone(None, "DE", 13.4)
    assert offset(berlin, date(2026, 7, 1)) == "+02:00" and offset(berlin, date(2026, 1, 15)) == "+01:00"


def test_distance_and_bearing():
    km = geo.haversine_km(55.7558, 37.6173, 55.7963, 49.1088)  # Moscow - Kazan
    assert 700 < km < 740
    assert 80 < geo.bearing_deg(55.7558, 37.6173, 55.7963, 49.1088) < 100


def test_resolve_city_catalog_first_then_known(mock_env):
    kazan = geo.resolve_city("казань")
    assert kazan.destination_id == "kazan" and kazan.stations == ["Казань-Пассажирская"]
    moscow = geo.resolve_city("Moscow")
    assert moscow.name_ru == "Москва" and "SVO" in moscow.iata and moscow.destination_id is None
    assert geo.resolve_city("Питер").name_ru == "Санкт-Петербург"
    assert geo.resolve_city("Istanbul").iata == ["IST"]  # airports.city fallback
    assert geo.resolve_city("Атлантида") is None
    assert geo.station_for(moscow, kazan) == "Москва Казанская"


def test_seeded_draws_are_pure():
    assert rng.unit("a", 1, "2026-11-14") == rng.unit("a", 1, "2026-11-14")
    assert rng.unit("a", 1, "2026-11-14") != rng.unit("a", 1, "2026-11-15")
    assert all(1 <= rng.rint(1, 4, "x", i) <= 4 for i in range(200))


def test_price_factors():
    assert pricing.lead_factor(0) > pricing.lead_factor(10) > pricing.lead_factor(40)
    assert pricing.season_factor(date(2026, 7, 10), "beach") > pricing.season_factor(date(2026, 11, 10), "beach")
    assert pricing.season_factor(date(2026, 12, 31), "city") > pricing.season_factor(date(2026, 12, 10), "city")
    assert pricing.profile("resort", ["sea"], "AE") == "tropical"
    assert pricing.profile("city", ["ski", "mountains"], "RU") == "ski"
    assert isinstance(datetime.fromisoformat(geo.iso(geo.local_dt(date(2026, 1, 1), 1500, geo.zone("Asia/Dubai")))),
                      datetime)
