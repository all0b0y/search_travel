"""lazy.build_city with a fake HTTP client: writes overlays only, never the committed databases."""
import hashlib
import json
import sqlite3

import pytest

from data_build import lazy
from data_build.common import open_db

from .helpers import FakeHttp, fixture_json


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def committed(tmp_path):
    """A tiny committed opendata DB with one destination and two airports."""
    p = tmp_path / "opendata.sqlite"
    con = open_db(p, "opendata")
    con.execute("INSERT INTO destinations(id, name_ru, name_en, country, country_code, lat, lon, kind, source,"
                " fetched_at) VALUES ('sochi','Сочи','Sochi','Россия','RU',43.59,39.72,'resort','curated','t')")
    con.executemany("INSERT INTO airports(ident, iata, name, city, country_code, lat, lon, type, source, fetched_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [("LTAI", "AYT", "Antalya", "Antalya", "TR", 36.90, 30.80, "large_airport", "ourairports", "t"),
                     ("UUEE", "SVO", "Sheremetyevo", "Moscow", "RU", 55.97, 37.41, "large_airport", "ourairports", "t")])
    con.commit()
    con.close()
    return p


def _nominatim(lat="36.5437", lon="32.0", name="Аланья"):
    return [{"lat": lat, "lon": lon, "category": "boundary", "type": "administrative", "addresstype": "town",
             "name": name, "namedetails": {"name": "Alanya", "name:ru": "Аланья", "name:en": "Alanya"},
             "address": {"country": "Турция", "country_code": "tr"}}]


def _archive():
    daily = fixture_json("openmeteo_archive_trim.json")
    daily["timezone"] = "Europe/Istanbul"
    return daily


def _overpass_shifted(dlat, dlon):
    data = fixture_json("overpass_antalya_trim.json")
    for el in data["elements"]:
        if "lat" in el:
            el["lat"] += dlat
            el["lon"] += dlon
        if "center" in el:
            el["center"] = {"lat": el["center"]["lat"] + dlat, "lon": el["center"]["lon"] + dlon}
        for g in el.get("geometry") or []:
            g["lat"] += dlat
            g["lon"] += dlon
    return data


def test_build_city_writes_overlays_only(tmp_path, committed):
    before = _sha(committed)
    http = FakeHttp({
        "nominatim": (200, _nominatim()),
        "archive-api": (200, _archive()),
        "overpass": (200, _overpass_shifted(36.5437 - 36.88, 32.0 - 30.70)),
    })
    od, cat = tmp_path / "rt" / "opendata_extra.sqlite", tmp_path / "rt" / "catalog_extra.sqlite"
    did = lazy.build_city("Аланья", http=http, opendata_overlay=od, catalog_overlay=cat, committed_opendata=committed)
    assert did == "alanya"
    assert _sha(committed) == before
    o, c = sqlite3.connect(od), sqlite3.connect(cat)
    row = o.execute("SELECT name_ru, name_en, country_code, timezone, source, kind FROM destinations").fetchone()
    assert row == ("Аланья", "Alanya", "TR", "Europe/Istanbul", "nominatim+lazy", "city")
    assert o.execute("SELECT COUNT(*) FROM climate WHERE destination_id='alanya'").fetchone()[0] == 12
    assert o.execute("SELECT COUNT(*) FROM climate WHERE sea_c IS NOT NULL").fetchone()[0] == 0
    n_h = c.execute("SELECT COUNT(*) FROM hotels WHERE destination_id='alanya'").fetchone()[0]
    assert n_h > 0
    assert o.execute("SELECT COUNT(*) FROM districts WHERE destination_id='alanya'").fetchone()[0] == n_h
    assert c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] >= 2 * n_h
    assert c.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == n_h
    assert c.execute("SELECT COUNT(*) FROM hotels WHERE rating IS NULL").fetchone()[0] == 0
    methods = [m for m, _u, _p in http.calls]
    assert methods.count("POST") == 1                    # exactly one Overpass attempt
    assert sum("nominatim" in u for _m, u, _p in http.calls) == 1
    # a second call finds the overlay destination and makes no request
    http2 = FakeHttp({})
    assert lazy.build_city("Alanya", http=http2, opendata_overlay=od, catalog_overlay=cat,
                           committed_opendata=committed) == "alanya"
    assert http2.calls == []


def test_known_city_returns_existing_id_without_network(tmp_path, committed):
    http = FakeHttp({})
    assert lazy.build_city("сочи", http=http, opendata_overlay=tmp_path / "a.sqlite",
                           catalog_overlay=tmp_path / "b.sqlite", committed_opendata=committed) == "sochi"
    assert http.calls == [] and not (tmp_path / "a.sqlite").exists()


def test_nearby_place_maps_to_existing_destination(tmp_path, committed):
    http = FakeHttp({"nominatim": (200, _nominatim(lat="43.60", lon="39.73", name="Хоста"))})
    assert lazy.build_city("Хоста", http=http, opendata_overlay=tmp_path / "a.sqlite",
                           catalog_overlay=tmp_path / "b.sqlite", committed_opendata=committed) == "sochi"


def test_overpass_failure_writes_nothing(tmp_path, committed):
    http = FakeHttp({"nominatim": (200, _nominatim()), "archive-api": (200, _archive()),
                     "overpass": (504, "Gateway Timeout")})
    od, cat = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    assert lazy.build_city("Аланья", http=http, opendata_overlay=od, catalog_overlay=cat,
                           committed_opendata=committed) is None
    assert not od.exists() and not cat.exists()


def test_no_geocoder_hit_returns_none(tmp_path, committed):
    http = FakeHttp({"nominatim": (200, [])})
    assert lazy.build_city("Нигдеград", http=http, opendata_overlay=tmp_path / "a.sqlite",
                           catalog_overlay=tmp_path / "b.sqlite", committed_opendata=committed) is None


def test_network_error_returns_none(tmp_path, committed):
    http = FakeHttp({"nominatim": (200, RuntimeError("boom"))})
    assert lazy.build_city("Аланья", http=http, opendata_overlay=tmp_path / "a.sqlite",
                           catalog_overlay=tmp_path / "b.sqlite", committed_opendata=committed) is None


def test_slugify():
    assert lazy.slugify("Нижний Новгород") == "nizhniy-novgorod"
    assert lazy.slugify("Ölüdeniz") == "oludeniz"
    assert json.dumps(lazy.slugify("!!")) == '"place"'
