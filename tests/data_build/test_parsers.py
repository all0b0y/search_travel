"""Parsers on small recorded fixtures (OurAirports, OpenFlights, Overpass, Photon, Open-Meteo, Nominatim)."""
from data_build.airports import parse_airports_csv, parse_routes_dat
from data_build.climate import monthly_air_normals, monthly_sea_normals
from data_build.destinations import pick_nominatim
from data_build.overpass import build_query, parse_overpass, parse_photon, pick_name, readable

from .helpers import fixture_json, fixture_text


def test_airports_keep_large_medium_with_iata():
    rows = parse_airports_csv(fixture_text("ourairports_sample.csv"))
    by = {r["iata"]: r for r in rows}
    assert set(by) == {"SVO", "DME", "AYT", "SUI", "AAQ", "RGK"}   # the small airport is dropped
    assert by["SVO"]["type"] == "large_airport" and by["SVO"]["scheduled"] is True
    assert by["AAQ"]["scheduled"] is False                          # Anapa: closed, no scheduled service
    assert by["SUI"]["contested"] is True and by["SVO"]["contested"] is False
    assert by["AYT"]["country_code"] == "TR" and by["AYT"]["city"] == "Antalya"
    assert abs(by["SVO"]["lat"] - 55.97) < 0.05


def test_airports_duplicate_iata_prefers_large():
    hdr = fixture_text("ourairports_sample.csv").splitlines()[0]
    row = '1,"AAAA","medium_airport","X",1,2,0,"EU","RU","RU-MOS","M","yes","AAAA","ZZZ",,,,,'
    row2 = '2,"BBBB","large_airport","Y",3,4,0,"EU","RU","RU-MOS","M","no","BBBB","ZZZ",,,,,'
    rows = parse_airports_csv("\n".join([hdr, row, row2]))
    assert len(rows) == 1 and rows[0]["ident"] == "BBBB"


def test_routes_aggregate_carriers_and_filter_unknown():
    text = fixture_text("routes_sample.dat")
    rows = parse_routes_dat(text, ["SVO", "AYT", "DME"])
    key = {(r["origin_iata"], r["dest_iata"], r["stops"]): r["carriers"] for r in rows}
    assert key[("AYT", "SVO", 0)] == ["8Q", "SU"]
    assert ("LHR", "SVO", 0) not in key          # LHR not in the known airports
    assert all(r["carriers"] == sorted(r["carriers"]) for r in rows)
    assert len(parse_routes_dat(text)) >= len(rows)


def test_overpass_query_is_one_batched_query():
    q = build_query((36.8, 30.6, 36.9, 30.8))
    assert "[timeout:180]" in q and "[bbox:36.8,30.6,36.9,30.8]" in q
    for tag in ("tourism", "amenity", "shop", "leisure", "natural", "railway", "public_transport", "highway"):
        assert tag in q
    assert "chalet" in q and "guest_house" in q and "out geom" in q


def test_parse_overpass_fixture():
    hotels, feats = parse_overpass(fixture_json("overpass_antalya_trim.json"))
    kinds = {h.kind for h in hotels}
    assert {"hotel", "guest_house", "apartment", "hostel"} <= kinds
    assert all(readable(h.name) for h in hotels)
    assert all(h.osm_id.split("/")[0] in ("node", "way", "relation") for h in hotels)
    assert "node/1" not in {h.osm_id for h in hotels} and "node/2" not in {h.osm_id for h in hotels}
    for cat in ("restaurant", "cafe", "bar", "pub", "nightclub", "pharmacy", "supermarket", "playground", "park", "stop"):
        assert feats.points.get(cat), cat
    assert feats.lines.get("major_road") and feats.lines.get("pedestrian")
    assert feats.has_beach()
    assert hotels == sorted(hotels, key=lambda h: h.osm_id)


def test_pick_name_prefers_readable():
    assert pick_name({"name": "北京饭店", "name:en": "Beijing Hotel"}) == "Beijing Hotel"
    assert pick_name({"name": "Отель Волна"}) == "Отель Волна"
    assert pick_name({"name": "โรงแรม"}) is None


def test_parse_photon_filters_by_bbox():
    fc = {"tourism:hotel": {"features": [
        {"geometry": {"coordinates": [30.70, 36.88]},
         "properties": {"osm_type": "N", "osm_id": 5, "name": "Inside Hotel", "osm_value": "hotel", "street": "A"}},
        {"geometry": {"coordinates": [31.50, 36.88]},
         "properties": {"osm_type": "W", "osm_id": 6, "name": "Far Hotel", "osm_value": "hotel"}}]},
        "poi:bar": {"features": [{"geometry": {"coordinates": [30.71, 36.881]}, "properties": {"osm_value": "bar"}}]}}
    hotels, feats = parse_photon(fc, (36.8, 30.6, 36.95, 30.8))
    assert [h.osm_id for h in hotels] == ["node/5"]
    assert hotels[0].tags["addr:street"] == "A"
    assert len(feats.points["bar"]) == 1


def test_open_meteo_monthly_normals_by_hand():
    daily = fixture_json("openmeteo_archive_trim.json")["daily"]
    out = monthly_air_normals(daily)
    jan = [i for i, t in enumerate(daily["time"]) if t[5:7] == "01"]
    exp_max = round(sum(daily["temperature_2m_max"][i] for i in jan) / len(jan), 1)
    assert out[1]["air_max_c"] == exp_max
    rain = sum(1 for i in jan if daily["precipitation_sum"][i] >= 1.0)
    assert out[1]["rain_days"] == round(rain / 2, 1)        # 2 years in the fixture
    sunny = sum(1 for i in jan if daily["sunshine_duration"][i] >= 6 * 3600)
    assert out[1]["sunny_days"] == round(sunny / 2, 1)
    assert out[7]["air_max_c"] > out[1]["air_max_c"]
    assert out[3] == {"air_max_c": None, "air_min_c": None, "rain_days": None, "sunny_days": None}


def test_sea_normals_ignore_nulls():
    daily = {"time": ["2023-01-01", "2023-01-02", "2024-01-01", "2023-07-01"],
             "sea_surface_temperature_mean": [17.0, None, 18.0, 27.4]}
    sea = monthly_sea_normals(daily)
    assert sea[1] == 17.5 and sea[7] == 27.4 and sea[2] is None


def test_nominatim_pick_prefers_exact_name_over_importance():
    res = fixture_json("nominatim_alanya.json")
    assert res[0]["name"] == "Antalya"                      # Nominatim ranks the province city first
    hit = pick_nominatim(res, "TR", "Alanya, Antalya")
    assert abs(float(hit["lon"]) - 32.0) < 0.1
    assert pick_nominatim(res, "RU", "Alanya") is None


def test_rate_limit_wait():
    from data_build.climate import rate_limit_wait_s
    t = 1_700_000_000 - (1_700_000_000 % 3600) + 600          # 10 min past an hour
    assert rate_limit_wait_s("Hourly API request limit exceeded", t) == 3000 + 60
    assert rate_limit_wait_s("Daily API request limit exceeded", t) == 86400 - (t % 86400) + 60
