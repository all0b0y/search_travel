"""Tiny fixture databases for the Ostrovok mock tests (built from data/schema/*.sql)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "data" / "schema"
FETCHED = "2026-10-20T00:00:00+00:00"
TODAY = "2026-10-25"
NOW = "2026-10-25T13:05:00+03:00"

DESTINATIONS = [
    # id, ru, en, country, cc, lat, lon, kind, tags, airports, stations, tz
    ("sochi", "Сочи", "Sochi", "Россия", "RU", 43.5855, 39.7231, "resort", ["sea", "beach", "mountains"], ["AER"],
     ["Сочи"], "Europe/Moscow"),
    ("antalya", "Анталья", "Antalya", "Турция", "TR", 36.8969, 30.7133, "resort", ["sea", "beach", "warm"], ["AYT"],
     [], "Europe/Istanbul"),
    ("kazan", "Казань", "Kazan", "Россия", "RU", 55.7963, 49.1088, "city", ["city", "history", "food"], ["KZN"],
     ["Казань-Пассажирская"], "Europe/Moscow"),
    ("glush", "Глушь", "Glush", "Россия", "RU", 61.0, 45.0, "nature", ["forest"], [], [], None),
]
AIRPORTS = [
    ("UUEE", "SVO", "Шереметьево", "Moscow", "RU", 55.9726, 37.4146, "large_airport"),
    ("UUWW", "VKO", "Внуково", "Moscow", "RU", 55.5915, 37.2615, "large_airport"),
    ("UUDD", "DME", "Домодедово", "Moscow", "RU", 55.4088, 37.9063, "large_airport"),
    ("URSS", "AER", "Сочи", "Sochi", "RU", 43.4499, 39.9566, "large_airport"),
    ("LTAI", "AYT", "Анталья", "Antalya", "TR", 36.8987, 30.8005, "large_airport"),
    ("LTFM", "IST", "Стамбул", "Istanbul", "TR", 41.2753, 28.7519, "large_airport"),
    ("UWKD", "KZN", "Казань", "Kazan", "RU", 55.6062, 49.2787, "large_airport"),
]
ROUTES = [
    ("SVO", "AER", ["SU"]), ("AER", "SVO", ["SU"]), ("VKO", "AER", ["DP", "UT"]), ("AER", "VKO", ["DP"]),
    ("SVO", "IST", ["SU", "TK"]), ("IST", "SVO", ["TK", "SU"]), ("IST", "AYT", ["TK"]), ("AYT", "IST", ["TK"]),
    ("SVO", "KZN", ["SU"]), ("KZN", "SVO", ["SU"]), ("AER", "IST", ["TK"]), ("IST", "KZN", ["TK"]),
]
HOTELS = [
    # hotel_id, osm, dest, name, kind, lat, lon, stars, rating, reviews, beach, center, photos
    ("h_sochi_1", "node/101", "sochi", "Отель Морской", "hotel", 43.5800, 39.7200, 4, 8.7, 540, 300, 900, []),
    ("h_sochi_2", "node/102", "sochi", "Гостевой дом Лаванда", "guest_house", 43.5900, 39.7300, 3, 7.9, 120, 800, 1500,
     []),
    ("h_sochi_3", "way/103", "sochi", "Spa Adults Only", "hotel", 43.5700, 39.7400, 5, 9.2, 300, 150, 2500, []),
    ("h_sochi_4", "node/104", "sochi", "Апартаменты у моря", "apartment", 43.5750, 39.7150, None, 8.1, 60, 200, None,
     []),
    ("h_ayt_1", "node/201", "antalya", "Lara Beach Resort", "resort", 36.8500, 30.8500, 5, 8.9, 2100, 50, 12000, []),
    ("h_ayt_2", "node/202", "antalya", "Kaleici Boutique", "hotel", 36.8850, 30.7050, 3, 8.3, 410, 1200, 400, []),
    ("h_kzn_1", "node/301", "kazan", "Отель Кремлёвский", "hotel", 55.7980, 49.1060, 4, 8.8, 900, None, 300,
     [{"url": "https://upload.wikimedia.org/wikipedia/commons/x/xx/Example.jpg", "source": "wikimedia",
       "license": "CC BY-SA 4.0"}]),
]
ROOMS = [
    # room_id, hotel, name, cap_a, cap_c, bed, base, meals
    ("r_s1_std", "h_sochi_1", "Стандарт", 2, 1, "double", 6000, ["none", "breakfast", "half_board"]),
    ("r_s1_fam", "h_sochi_1", "Семейный", 2, 2, "double", 9000, ["breakfast", "all_inclusive"]),
    ("r_s2_std", "h_sochi_2", "Двухместный", 2, 0, "twin", 3500, ["none"]),
    ("r_s3_dlx", "h_sochi_3", "Делюкс", 2, 0, "double", 15000, ["breakfast"]),
    ("r_s4_apt", "h_sochi_4", "Апартаменты с двумя спальнями", 4, 2, None, 8000, ["none"]),
    ("r_a1_fam", "h_ayt_1", "Family Room", 2, 2, "double", 12000, ["all_inclusive"]),
    ("r_a2_std", "h_ayt_2", "Standard", 2, 1, "double", 4000, ["none", "breakfast"]),
    ("r_k1_std", "h_kzn_1", "Стандарт", 2, 1, "double", 5500, ["none", "breakfast"]),
]
POLICIES = [
    # hotel, in, out, pets, kids_ok, min_age, cot, extra_bed, early_ok, early_price, free_days, penalty
    ("h_sochi_1", "14:00", "12:00", 0, 1, None, "free", 1500, 1, 2500, 3, 30),
    ("h_sochi_2", "15:00", "11:00", 1, 1, None, "none", None, 0, None, None, 100),
    ("h_sochi_3", "14:00", "12:00", 0, 0, None, "none", None, 1, 5000, 1, 50),
    ("h_sochi_4", "15:00", "11:00", 1, 1, None, "paid", 1000, 0, None, 7, 20),
    ("h_ayt_1", "14:00", "12:00", 0, 1, None, "free", 2000, 1, 3000, 5, 25),
    ("h_ayt_2", "14:00", "12:00", 0, 1, 6, "on_request", None, 0, None, None, 100),
    ("h_kzn_1", "14:00", "12:00", 0, 1, None, "on_request", 1200, 1, 1800, 2, 50),
]


def _insights_rows():
    rows = []
    def add(n, origin, dest, kind, month, segment, **kw):
        for i in range(n):
            rows.append({"at": "2026-01-01T10:00:00+03:00", "kind": "booking", "origin_city": origin,
                         "destination_id": dest, "destination_kind": kind, "month": month, "segment": segment,
                         "nights": 7 + (i % 3), "budget_rub": 120000 + 5000 * i,
                         "meals": kw.get("meals", "breakfast") if i % 4 else "all_inclusive",
                         "free_cancellation": 1 if i % 3 else 0, "direct_flight": 1 if i % 2 else 0,
                         "district": kw.get("district", "family") if i % 5 else "quiet"})
    add(25, "Москва", "antalya", "resort", 11, "family")
    add(15, "Москва", "sochi", "resort", 11, "family")
    add(10, "Москва", "sochi", "resort", 11, "couple", district="lively")
    return rows


def build_fixture_dbs(base: Path) -> dict[str, Path]:
    base.mkdir(parents=True, exist_ok=True)
    od, cat = base / "opendata.sqlite", base / "ostrovok_catalog.sqlite"
    for p in (od, cat):
        if p.exists():
            p.unlink()
    with sqlite3.connect(od) as c:
        c.executescript((SCHEMA / "opendata.sql").read_text(encoding="utf-8"))
        for d in DESTINATIONS:
            c.execute("INSERT INTO destinations (id, name_ru, name_en, country, country_code, lat, lon, kind, tags, "
                      "airports, stations, timezone, description_ru, popularity, source, fetched_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*d[:8], json.dumps(d[8]), json.dumps(d[9]), json.dumps(d[10], ensure_ascii=False), d[11],
                       None, 0.5, "test", FETCHED))
        for a in AIRPORTS:
            c.execute("INSERT INTO airports VALUES (?,?,?,?,?,?,?,?,?,?)", (*a, "ourairports", FETCHED))
        for o, d, carriers in ROUTES:
            c.execute("INSERT INTO routes VALUES (?,?,?,?,?,?)", (o, d, 0, json.dumps(carriers), "openflights", FETCHED))
    with sqlite3.connect(cat) as c:
        c.executescript((SCHEMA / "ostrovok_catalog.sql").read_text(encoding="utf-8"))
        for h in HOTELS:
            c.execute("INSERT INTO hotels (hotel_id, osm_id, destination_id, name, kind, lat, lon, address, stars, "
                      "rating, reviews, distance_beach_m, distance_center_m, photos, synthetic_fields, source, "
                      "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*h[:7], None, *h[7:12], json.dumps(h[12]), json.dumps(["rating", "reviews"]), "osm", FETCHED))
        for r in ROOMS:
            c.execute("INSERT INTO rooms VALUES (?,?,?,?,?,?,?,?,?,?)", (*r[:7], json.dumps(r[7]), "synthetic", FETCHED))
        for p in POLICIES:
            c.execute("INSERT INTO policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*p, "synthetic", FETCHED))
        for i, e in enumerate(_insights_rows(), start=1):
            c.execute("INSERT INTO insights_events (id, at, kind, origin_city, destination_id, destination_kind, month, "
                      "segment, nights, budget_rub, meals, free_cancellation, direct_flight, district, data, source, "
                      "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (i, e["at"], e["kind"], e["origin_city"], e["destination_id"], e["destination_kind"], e["month"],
                       e["segment"], e["nights"], e["budget_rub"], e["meals"], e["free_cancellation"],
                       e["direct_flight"], e["district"], "{}", "synthetic", FETCHED))
    return {"opendata": od, "catalog": cat}


@pytest.fixture(scope="session")
def fixture_dbs(tmp_path_factory):
    return build_fixture_dbs(tmp_path_factory.mktemp("ostrovok_fixture"))


@pytest.fixture
def mock_env(fixture_dbs, tmp_path, monkeypatch):
    """Point the mock at the fixture DBs and a fresh state DB; overlays at non-existent paths."""
    from ostrovok_mcp import db
    monkeypatch.setenv("OSTROVOK_CATALOG_DB", str(fixture_dbs["catalog"]))
    monkeypatch.setenv("OPENDATA_DB", str(fixture_dbs["opendata"]))
    monkeypatch.setenv("OSTROVOK_CATALOG_EXTRA_DB", str(tmp_path / "no_catalog_extra.sqlite"))
    monkeypatch.setenv("OPENDATA_EXTRA_DB", str(tmp_path / "no_opendata_extra.sqlite"))
    monkeypatch.setenv("OSTROVOK_STATE_DB", str(tmp_path / "state.sqlite"))
    db.reset_caches()
    yield {"tmp": tmp_path, **fixture_dbs}
    db.reset_caches()
