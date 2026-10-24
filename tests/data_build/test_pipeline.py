"""End-to-end pipeline on fixtures (fake HTTP, temporary cache and DBs) and schema compatibility."""
import json
import shutil
import sqlite3

import pytest

from data_build import common
from data_build.build_all import STAGES, parse_stages
from data_build.common import CATALOG_DB, OPENDATA_DB, migrate, schema_sql

from .helpers import FIX, FakeHttp, fixture_json, fixture_text


def _columns(con):
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        out[t] = [(r[1], r[2], r[3], r[5]) for r in con.execute(f"PRAGMA table_info({t})")]
    return out


def _schema_columns(name):
    ref = sqlite3.connect(":memory:")
    ref.executescript(schema_sql(name))
    return _columns(ref)


@pytest.mark.parametrize("path,name", [(OPENDATA_DB, "opendata"), (CATALOG_DB, "ostrovok_catalog")])
def test_committed_dbs_match_schema(path, name):
    assert path.exists(), f"{path} must be built (python -m data_build.build_all)"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    have, want = _columns(con), _schema_columns(name)
    for table, cols in want.items():
        assert table in have, table
        # same names, types, NOT NULL and PK flags (migrated columns are appended at the end)
        assert sorted(have[table]) == sorted(cols), table
    con.close()


def test_migrate_adds_new_columns(tmp_path):
    p = tmp_path / "x.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE destinations (id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO destinations VALUES ('a')")
    added = migrate(con, "CREATE TABLE IF NOT EXISTS destinations (id TEXT PRIMARY KEY,"
                         " aliases TEXT NOT NULL DEFAULT '[]');")
    assert added == ["destinations.aliases"]
    assert con.execute("SELECT aliases FROM destinations").fetchone()[0] == "[]"


def test_parse_stages():
    assert parse_stages("all") == list(STAGES)
    assert parse_stages("synthetic,hotels") == ["hotels", "synthetic"]
    with pytest.raises(SystemExit):
        parse_stages("hotel")


def test_pipeline_on_fixtures(tmp_path, monkeypatch):
    """airports -> routes -> destinations (2) -> climate -> hotels/districts/synthetic -> insights."""
    from data_build import airports, climate, destinations, hotels, insights
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    od, cat = tmp_path / "opendata.sqlite", tmp_path / "catalog.sqlite"
    nomi = fixture_json("nominatim_alanya.json")
    daily = fixture_json("openmeteo_archive_trim.json")
    marine = {"daily": {"time": ["2024-07-01", "2024-07-02", "2025-01-01"],
                        "sea_surface_temperature_mean": [28.0, 28.4, 17.0]}}
    overpass_calls = []

    def overpass(url, data):
        overpass_calls.append(url)
        if len(overpass_calls) == 1:
            return 502, "Bad Gateway"                    # first mirror fails -> next mirror
        return 200, fixture_json("overpass_antalya_trim.json")

    http = FakeHttp({
        "ourairports": (200, fixture_text("ourairports_sample.csv")),
        "openflights": (200, fixture_text("routes_sample.dat")),
        "nominatim": (200, nomi),
        "archive-api": (200, daily),
        "marine-api": (200, marine),
        "overpass": overpass,
    })
    airports.run_airports(od, http)
    airports.run_routes(od, http)
    destinations.run_destinations(od, http, only=["antalya", "alanya"], sleep=lambda s: None)
    climate.run_climate(od, http, sleep=lambda s: None)
    s = hotels.run_hotel_pipeline(["hotels", "districts", "synthetic"], only=["antalya"], http=http,
                                  opendata_path=od, catalog_path=cat, sleep=lambda s: None)
    assert s["done"] == ["antalya"] and len(overpass_calls) == 2
    assert (tmp_path / "cache" / "overpass" / "antalya.json").exists()
    insights.run_insights(od, cat, total=9000)

    o, c = sqlite3.connect(od), sqlite3.connect(cat)
    assert _columns(o) == _schema_columns("opendata") or set(_columns(o)) == set(_schema_columns("opendata"))
    d = dict(zip(("id", "airports", "timezone", "aliases", "popularity", "source"), o.execute(
        "SELECT id, airports, timezone, aliases, popularity, source FROM destinations WHERE id='antalya'").fetchone()))
    assert json.loads(d["airports"]) == ["AYT"] and d["timezone"] == "Europe/Istanbul"
    assert json.loads(d["aliases"]) == ["Анталия"] and d["popularity"] == 0.95 and d["source"] == "curated+nominatim"
    clim = o.execute("SELECT month, air_max_c, sea_c, source FROM climate WHERE destination_id='antalya' "
                     "ORDER BY month").fetchall()
    assert len(clim) == 12 and clim[6][2] == 28.2 and clim[0][2] == 17.0 and clim[2][1] is None
    assert clim[6][3] == "open-meteo-archive+open-meteo-marine"
    n_h = c.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]
    assert 0 < n_h <= 60
    assert o.execute("SELECT COUNT(*) FROM districts WHERE source='overpass'").fetchone()[0] == n_h
    assert c.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == n_h
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'hotel_extras:%'").fetchone()[0] == n_h
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key='synthetic.season_factors:antalya'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM insights_events").fetchone()[0] == 9000
    first = c.execute("SELECT hotel_id, rating, stars FROM hotels ORDER BY hotel_id").fetchall()

    # resume: nothing refetched, nothing changed; rerun from cache gives identical synthetic values
    http.calls.clear()
    s2 = hotels.run_hotel_pipeline(["hotels", "districts", "synthetic"], only=["antalya"], resume=True, http=http,
                                   opendata_path=od, catalog_path=cat)
    assert s2["skipped"] == ["antalya"] and http.calls == []
    s3 = hotels.run_hotel_pipeline(["hotels", "districts", "synthetic"], only=["antalya"], network=False,
                                   opendata_path=od, catalog_path=cat)
    assert s3["done"] == ["antalya"]
    assert c.execute("SELECT hotel_id, rating, stars FROM hotels ORDER BY hotel_id").fetchall() == first
    s4 = hotels.run_hotel_pipeline(["synthetic"], opendata_path=od, catalog_path=cat, network=False)
    assert s4["done"] == ["antalya"]
    assert c.execute("SELECT hotel_id, rating, stars FROM hotels ORDER BY hotel_id").fetchall() == first


def test_photon_fallback_when_overpass_stays_down(tmp_path, monkeypatch):
    from data_build import hotels
    from data_build.common import open_db
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    od, cat = tmp_path / "opendata.sqlite", tmp_path / "catalog.sqlite"
    con = open_db(od, "opendata")
    con.execute("INSERT INTO destinations(id, name_ru, name_en, country, country_code, lat, lon, kind, tags, source,"
                " fetched_at) VALUES ('antalya','Анталья','Antalya','Турция','TR',36.8866,30.703,'resort',"
                " '[\"sea\",\"beach\"]','curated','t')")
    con.commit()
    feature = {"features": [{"geometry": {"coordinates": [30.704, 36.887]},
                             "properties": {"osm_type": "N", "osm_id": 99, "name": "Photon Hotel",
                                            "osm_value": "hotel"}}]}
    http = FakeHttp({"overpass": (504, "timeout"),
                     "photon": lambda url, p: (200, feature if p["osm_tag"] == "tourism:hotel" else {"features": []})})
    s = hotels.run_hotel_pipeline(["hotels", "districts", "synthetic"], only=["antalya"], http=http,
                                  opendata_path=od, catalog_path=cat, sleep=lambda s: None)
    assert s["photon"] == ["antalya"]
    c = sqlite3.connect(cat)
    assert c.execute("SELECT name, source FROM hotels").fetchall() == [("Photon Hotel", "photon")]
    assert sqlite3.connect(od).execute("SELECT source FROM districts").fetchall() == [("photon",)]
    assert sum(1 for m, u, _ in http.calls if m == "POST") == 6   # all Overpass attempts used first
