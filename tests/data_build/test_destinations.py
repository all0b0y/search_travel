"""Curated destinations, aliases and the offline timezone table."""
from collections import Counter
from zoneinfo import ZoneInfo

import pytest

from agent.state import DESTINATION_TRAITS
from data_build.destinations import ALIASES, DESTINATIONS, nearest_airports, validate_destinations
from data_build.timezones import COUNTRY_TZ, DEST_TZ, MULTI_ZONE, tz_for


def test_list_is_valid_and_balanced():
    assert validate_destinations() == []
    assert 140 <= len(DESTINATIONS) <= 170
    for d in DESTINATIONS:
        assert set(d.tags) <= set(DESTINATION_TRAITS)
        assert d.description_ru and d.description_ru.endswith(".")
    ru = [d for d in DESTINATIONS if d.country_code == "RU"]
    sea = [d for d in DESTINATIONS if "sea" in d.tags and "beach" in d.tags]
    cities = [d for d in DESTINATIONS if d.kind == "city"]
    assert len(ru) >= 40 and len(sea) >= 50 and len(cities) >= 50
    ids = {d.id for d in DESTINATIONS}
    for must in ("antalya", "sochi", "istanbul", "dubai", "kazan", "saint-petersburg", "tbilisi", "hurghada",
                 "phuket", "kaliningrad", "moscow", "nha-trang", "batumi", "yerevan", "altai", "paris", "rome",
                 "barcelona", "prague", "bali"):
        assert must in ids


def test_no_contested_territories():
    names = " ".join(d.name_en.lower() + " " + d.name_ru.lower() for d in DESTINATIONS)
    for bad in ("crimea", "yalta", "sevastopol", "sukhumi", "abkhaz", "gagra", "tskhinvali", "kyrenia"):
        assert bad not in names


def test_aliases():
    ids = {d.id for d in DESTINATIONS}
    assert set(ALIASES) <= ids
    assert {"Питер", "СПб", "Петербург"} <= set(ALIASES["saint-petersburg"])
    assert "Екб" in ALIASES["yekaterinburg"] and "Мск" in ALIASES["moscow"]
    flat = [a.casefold() for v in ALIASES.values() for a in v]
    assert len(flat) == len(set(flat))


@pytest.mark.parametrize("d", DESTINATIONS, ids=lambda d: d.id)
def test_every_destination_has_a_valid_timezone(d):
    tz, method = tz_for(d.id, d.country_code)
    assert tz is not None and method in ("destination", "country")
    ZoneInfo(tz)
    if d.country_code in MULTI_ZONE:
        assert d.id in DEST_TZ


def test_timezone_table_values():
    for z in list(COUNTRY_TZ.values()) + list(DEST_TZ.values()):
        ZoneInfo(z)
    assert tz_for("kaliningrad", "RU")[0] == "Europe/Kaliningrad"
    assert tz_for("yekaterinburg", "RU")[0] == "Asia/Yekaterinburg"
    assert tz_for("istanbul", "TR") == ("Europe/Istanbul", "country")
    assert tz_for("new-ru-town", "RU", lon=61.0) == ("Asia/Yekaterinburg", "longitude")
    assert tz_for("new-ru-town", "RU", lon=37.6) == ("Europe/Moscow", "longitude")
    assert tz_for("somewhere", "ZZ") == (None, "unknown")
    assert Counter(DEST_TZ.values())["Europe/Moscow"] >= 30


def test_nearest_airports_domestic_first_and_excludes():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE airports (iata TEXT, lat REAL, lon REAL, country_code TEXT, type TEXT)")
    con.executemany("INSERT INTO airports VALUES (?,?,?,?,?)", [
        ("SVL", 61.94, 28.94, "FI", "medium_airport"), ("PES", 61.88, 32.0, "RU", "medium_airport"), ("XXX", 61.70, 30.70, "RU", "medium_airport"), ("SUI", 61.7, 30.8, "GE", "large_airport")])
    assert nearest_airports(con, 61.70, 30.69, {"SVL", "PES", "SUI"}, country_code="RU") == ["PES"]
    # a foreign medium airport 95 km away is neither within 75 km nor large -> nothing
    assert nearest_airports(con, 61.70, 30.69, {"SVL"}, country_code="RU") == []
    assert nearest_airports(con, 61.70, 30.69, {"SUI", "SVL"}, country_code="GE", exclude={"SUI"}) == []
    assert nearest_airports(con, 61.70, 30.69, {"SUI", "SVL"}, country_code="GE") == ["SUI"]
    assert nearest_airports(con, 61.80, 29.5, {"SVL"}, country_code="RU") == ["SVL"]       # foreign within 75 km


def _airport_db(rows):
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE airports (iata TEXT, lat REAL, lon REAL, country_code TEXT, type TEXT)")
    con.executemany("INSERT INTO airports VALUES (?,?,?,?,?)", rows)
    return con


# Real OurAirports coordinates (subset) for the orchestrator's airport rule.
_TR_RU_AE = [
    ("AYT", 36.898701, 30.800501, "TR", "large_airport"), ("GZP", 36.299217, 32.300598, "TR", "medium_airport"),
    ("ISE", 37.855499, 30.368401, "TR", "medium_airport"), ("DNZ", 37.785599, 29.701300, "TR", "medium_airport"),
    ("IST", 41.262222, 28.727778, "TR", "large_airport"), ("SAW", 40.898601, 29.309200, "TR", "large_airport"),
    ("TEQ", 41.138248, 27.919092, "TR", "medium_airport"), ("YEI", 40.255199, 29.562599, "TR", "medium_airport"),
    ("KZN", 55.606201, 49.278702, "RU", "large_airport"), ("CSY", 56.090301, 47.347301, "RU", "medium_airport"),
    ("DXB", 25.252800, 55.364399, "AE", "large_airport"), ("DWC", 24.896356, 55.161389, "AE", "large_airport"),
    ("SHJ", 25.328600, 55.517200, "AE", "large_airport"), ("AUH", 24.432972, 54.651138, "AE", "large_airport"),
    ("AER", 43.449902, 39.956600, "RU", "large_airport"), ("NOZ", 53.811401, 86.877197, "RU", "medium_airport"),
]
_SCHED = {r[0] for r in _TR_RU_AE}


@pytest.mark.parametrize("dest,lat,lon,cc,expected", [
    ("antalya", 36.886573, 30.703024, "TR", ["AYT"]),
    ("alanya", 36.543700, 31.999800, "TR", ["GZP", "AYT"]),
    ("istanbul", 41.005400, 28.976800, "TR", ["SAW", "IST"]),   # by distance from Sultanahmet: SAW 31 km, IST 35 km
    ("kazan", 55.796100, 49.106400, "RU", ["KZN"]),
    ("dubai", 25.080000, 55.140000, "AE", ["DWC", "DXB", "SHJ"]),
    ("sochi", 43.585300, 39.723100, "RU", ["AER"]),
    ("sheregesh", 52.926000, 87.977000, "RU", ["NOZ"]),        # fallback: nothing within 75 km, no large in 150
])
def test_airport_rule(dest, lat, lon, cc, expected):
    con = _airport_db(_TR_RU_AE)
    got = nearest_airports(con, lat, lon, _SCHED, country_code=cc)
    assert got == expected
    assert "AUH" not in got and "ISE" not in got and "TEQ" not in got and "CSY" not in got


def test_built_db_airports_follow_the_rule():
    import json
    import sqlite3
    from data_build.common import OPENDATA_DB
    con = sqlite3.connect(f"file:{OPENDATA_DB}?mode=ro", uri=True)
    got = {i: json.loads(a) for i, a in con.execute("SELECT id, airports FROM destinations")}
    assert got["antalya"] == ["AYT"] and got["alanya"] == ["GZP", "AYT"] and got["kazan"] == ["KZN"]
    assert sorted(got["istanbul"]) == ["IST", "SAW"] and got["sochi"] == ["AER"] and "AUH" not in got["dubai"]
