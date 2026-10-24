"""OpenData on small fixture DBs built from data/schema/opendata.sql (tmp dirs, no network)."""
import json

import pytest

from agent.opendata import OpenData, name_score

from .factory import make_opendata
from .test_checks import CBR_XML

DESTS = [
    {"id": "hurghada", "name_ru": "Хургада", "name_en": "Hurghada", "country": "Египет", "country_code": "EG",
     "lat": 27.257, "lon": 33.812, "tags": ["sea", "beach", "resort"], "airports": ["HRG"], "popularity": 0.8},
    {"id": "dubai", "name_ru": "Дубай", "name_en": "Dubai", "country": "ОАЭ", "country_code": "AE", "kind": "city",
     "lat": 25.204, "lon": 55.270, "tags": ["sea", "beach", "city", "shopping"], "airports": ["DXB"], "popularity": 0.9},
    {"id": "phuket", "name_ru": "Пхукет", "name_en": "Phuket", "country": "Таиланд", "country_code": "TH",
     "lat": 7.880, "lon": 98.392, "tags": ["sea", "beach", "island", "tropical"], "airports": ["HKT"], "popularity": 0.7},
    {"id": "sochi", "name_ru": "Сочи", "name_en": "Sochi", "country": "Россия", "country_code": "RU",
     "lat": 43.585, "lon": 39.723, "tags": ["sea", "mountains", "beach"], "airports": ["AER"], "popularity": 0.9},
    {"id": "tallinn", "name_ru": "Таллин", "name_en": "Tallinn", "country": "Эстония", "country_code": "EE",
     "kind": "city", "lat": 59.437, "lon": 24.754, "tags": ["sea", "city", "history"], "airports": ["TLL"], "popularity": 0.4},
    {"id": "kazan", "name_ru": "Казань", "name_en": "Kazan", "country": "Россия", "country_code": "RU", "kind": "city",
     "lat": 55.796, "lon": 49.108, "tags": ["city", "history", "food"], "airports": ["KZN"], "popularity": 0.6},
    {"id": "goreme", "name_ru": "Гёреме", "name_en": "Goreme", "country": "Турция", "country_code": "TR",
     "kind": "village", "lat": 38.643, "lon": 34.829, "tags": ["nature", "history"], "airports": [], "popularity": 0.5},
]
CLIMATE_NOV = [
    {"destination_id": "hurghada", "month": 11, "air_max_c": 27, "sea_c": 25, "rain_days": 0},
    {"destination_id": "dubai", "month": 11, "air_max_c": 30, "sea_c": 27, "rain_days": 1},
    {"destination_id": "phuket", "month": 11, "air_max_c": 31, "sea_c": 28.5, "rain_days": 12},
    {"destination_id": "sochi", "month": 11, "air_max_c": 14, "sea_c": 15, "rain_days": 10},
    {"destination_id": "tallinn", "month": 11, "air_max_c": 5, "sea_c": 7, "rain_days": 15},
    {"destination_id": "kazan", "month": 11, "air_max_c": 2, "rain_days": 12},
    {"destination_id": "goreme", "month": 11, "air_max_c": 13, "rain_days": 5},
]
AIRPORTS = [
    {"iata": "SVO", "name": "Sheremetyevo", "city": "Moscow", "country_code": "RU", "lat": 55.972, "lon": 37.415},
    {"iata": "DME", "name": "Domodedovo", "city": "Moscow", "country_code": "RU", "lat": 55.409, "lon": 37.906},
    {"iata": "HRG", "name": "Hurghada", "city": "Hurghada", "country_code": "EG", "lat": 27.178, "lon": 33.799},
    {"iata": "DXB", "name": "Dubai", "city": "Dubai", "country_code": "AE", "lat": 25.253, "lon": 55.364},
    {"iata": "HKT", "name": "Phuket", "city": "Phuket", "country_code": "TH", "lat": 8.113, "lon": 98.317},
    {"iata": "AER", "name": "Sochi", "city": "Sochi", "country_code": "RU", "lat": 43.450, "lon": 39.957},
    {"iata": "TLL", "name": "Tallinn", "city": "Tallinn", "country_code": "EE", "lat": 59.413, "lon": 24.833},
    {"iata": "KZN", "name": "Kazan", "city": "Kazan", "country_code": "RU", "lat": 55.606, "lon": 49.278},
    {"iata": "IST", "name": "Istanbul", "city": "Istanbul", "country_code": "TR", "lat": 41.262, "lon": 28.742},
    {"iata": "NAV", "name": "Nevsehir", "city": "Nevsehir", "country_code": "TR", "lat": 38.772, "lon": 34.534,
     "type": "medium_airport"},
    {"iata": "XXS", "name": "Small strip", "city": "Nevsehir", "country_code": "TR", "lat": 38.70, "lon": 34.80,
     "type": "small_airport"},
]
ROUTES = [{"origin_iata": "SVO", "dest_iata": d} for d in ("HRG", "DXB", "AER", "KZN", "IST")] + [
    {"origin_iata": "IST", "dest_iata": "TLL"}, {"origin_iata": "DXB", "dest_iata": "HKT"},
    {"origin_iata": "IST", "dest_iata": "NAV"}]
DISTRICTS = [{"osm_id": "node/1", "destination_id": "sochi", "lat": 43.58, "lon": 39.72,
              "poi_counts": {"playground": 2, "bar": 9}, "nearest_m": {"beach": 250},
              "lively": 0.75, "quiet": 0.2, "family": 0.62, "walkable": 0.5}]


@pytest.fixture
def od(tmp_path):
    db = make_opendata(tmp_path / "opendata.sqlite", DESTS, CLIMATE_NOV, AIRPORTS, ROUTES, DISTRICTS)
    return OpenData(path=db, overlay=tmp_path / "extra.sqlite", cache_dir=tmp_path)


class TestFindDestinations:
    def test_warm_sea_in_november_from_moscow(self, od):
        res = od.find_destinations(["sea", "warm"], 11, "Москва")
        ids = [r["id"] for r in res]
        assert set(ids[:3]) == {"hurghada", "dubai", "phuket"}
        assert ids.index("sochi") > 2 and ids.index("tallinn") > ids.index("sochi")
        top = res[0]
        assert top["weather"]["sea_c"] >= 24 and top["travel_time_h"] is not None
        assert set(top["breakdown"]) == {"trait", "climate", "travel", "popularity"}

    def test_derived_climate_traits_match(self, od):
        hurghada = next(r for r in od.find_destinations(["sea", "warm"], 11, "Москва") if r["id"] == "hurghada")
        assert hurghada["match"] == ["sea", "warm"]

    def test_one_stop_and_max_travel_hours(self, od):
        phuket = next(r for r in od.find_destinations(["sea"], 11, "Москва", limit=10) if r["id"] == "phuket")
        assert phuket["stops"] == 1 and phuket["via"] == "DXB"
        ids = [r["id"] for r in od.find_destinations(["sea", "warm"], 11, "Москва", max_travel_hours=7, limit=10)]
        assert "phuket" not in ids and "hurghada" in ids

    def test_cold_wishes(self, od):
        res = od.find_destinations(["snow", "city"], 11, "Москва", limit=10)
        assert res[0]["id"] == "kazan"

    def test_city_break_comfort_climate(self, od):
        res = od.find_destinations(["city", "history"], 11, None, limit=3)
        assert {r["id"] for r in res} <= {"tallinn", "kazan", "goreme", "dubai"}
        assert all(r["travel_time_h"] is None for r in res)

    def test_limit_and_family_segment(self, od):
        assert len(od.find_destinations(["sea"], 11, "Москва", limit=2)) == 2
        fam = od.find_destinations(["sea", "warm"], 11, "Москва", segment="family", limit=10)
        solo = od.find_destinations(["sea", "warm"], 11, "Москва", limit=10)
        rank = lambda rs, i: [r["id"] for r in rs].index(i)
        assert rank(fam, "phuket") >= rank(solo, "phuket")


class TestLookups:
    def test_climate(self, od):
        assert od.climate("dubai", 11)["sea_c"] == 27
        assert od.climate("dubai", 7) is None

    def test_district_profile(self, od):
        p = od.district_profile("node/1")
        assert p["labels"] == ["lively", "family"] and p["counts"]["playground"] == 2 and p["nearest_m"]["beach"] == 250
        assert od.district_profile("node/404") is None
        assert set(od.districts_for(["node/1", "node/404"])) == {"node/1"}

    def test_airports_near(self, od):
        near = od.airports_near(38.643, 34.829, radius_km=100)
        assert [a["iata"] for a in near] == ["NAV"]  # small strips are skipped
        assert od.airports_near(0, 0) == []

    def test_travel_time(self, od):
        t = od.travel_time("Москва", "hurghada")
        assert t["stops"] == 0 and t["from_iata"] == "SVO" and 4 < t["hours"] < 6
        g = od.travel_time("Москва", "goreme")  # no airport listed: nearest NAV, via IST
        assert g["to_iata"] == "NAV" and g["via"] == "IST"
        assert od.travel_time("Москва", "nowhere") is None
        assert od.travel_time("Урюпинск", "dubai") is None

    def test_origin_by_iata(self, od):
        assert od.origin_airports("SVO") == ["SVO"]


class TestGeocode:
    @pytest.mark.parametrize("query,expect", [
        ("Хургада", "hurghada"), ("хургада", "hurghada"), ("в Хургаде", "hurghada"), ("ДУБАЙ", "dubai"),
        ("Dubai", "dubai"), ("Phuket", "phuket"), ("Пхукетт", "phuket"), ("Гереме", "goreme"), ("Гёреме", "goreme"),
        ("в Казани", "kazan"), ("Пхукета", "phuket"), ("Казань!", "kazan"), ("Таллинн", "tallinn"), ("Сочи", "sochi"),
    ])
    def test_catalog_fuzzy(self, od, query, expect):
        hit = od.geocode(query)
        assert hit["id"] == expect and hit["source"] == "catalog" and hit["in_catalog"]

    def test_unknown_without_network_or_lazy(self, od):
        assert od.geocode("Рейкьявик") is None
        assert od.geocode("") is None

    def test_name_score(self):
        assert name_score("Москву", "Москва") >= 0.9
        assert name_score("Париж", "Пхукет") == 0.0


class FakeResponse:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        assert headers and "User-Agent" in headers
        handler = self.routes.get(url)
        if handler is None:
            raise RuntimeError("offline")
        return handler(params)


NOMINATIM = "https://nominatim.openstreetmap.org/search"
CBR = "https://www.cbr.ru/scripts/XML_daily.asp"


def nominatim(places):
    def handle(params):
        hit = places.get(params["q"])
        return FakeResponse(data=[hit] if hit else [])
    return handle


class TestGeocodeNetwork:
    def test_nominatim_snaps_to_catalog(self, tmp_path):
        db = make_opendata(tmp_path / "o.sqlite", DESTS)
        http = FakeHttp({NOMINATIM: nominatim({"Адлер": {"lat": "43.43", "lon": "39.92", "name": "Адлер",
                                                          "address": {"country": "Россия", "country_code": "ru"}}})})
        od = OpenData(path=db, overlay=None, http=http)
        hit = od.geocode("Адлер")
        assert hit["id"] == "sochi" and hit["source"] == "nominatim+catalog"

    def test_lazy_builder_once_per_name_and_overlay_union(self, tmp_path):
        db = make_opendata(tmp_path / "o.sqlite", DESTS)
        overlay = tmp_path / "extra.sqlite"
        calls = []

        def lazy(query):
            calls.append(query)
            make_opendata(overlay, [{"id": "reykjavik", "name_ru": "Рейкьявик", "name_en": "Reykjavik",
                                     "country": "Исландия", "country_code": "IS", "lat": 64.14, "lon": -21.94,
                                     "kind": "city", "tags": ["city", "nature", "cold"], "source": "overpass"}],
                          [{"destination_id": "reykjavik", "month": 11, "air_max_c": 3}])
            return "reykjavik"
        http = FakeHttp({NOMINATIM: nominatim({"Рейкьявик": {"lat": "64.14", "lon": "-21.94", "name": "Рейкьявик",
                                                              "address": {"country": "Исландия", "country_code": "is"}}})})
        od = OpenData(path=db, overlay=overlay, http=http, lazy_builder=lazy)
        hit = od.geocode("Рейкьявик")
        assert hit["id"] == "reykjavik" and hit["source"] == "lazy"
        assert od.geocode("Рейкьявик")["source"] == "catalog"  # now in the overlay
        assert calls == ["Рейкьявик"]
        assert "reykjavik" in [d["id"] for d in od.destinations()]
        assert od.climate("reykjavik", 11)["air_max_c"] == 3
        assert od.find_destinations(["cold"], 11, None, limit=1)[0]["id"] == "reykjavik"

    def test_lazy_failure_is_tried_once_then_nominatim_place(self, tmp_path):
        db = make_opendata(tmp_path / "o.sqlite", DESTS)
        calls = []
        http = FakeHttp({NOMINATIM: nominatim({"Тромсё": {"lat": "69.65", "lon": "18.96", "name": "Тромсё",
                                                           "address": {"country": "Норвегия", "country_code": "no"}}})})
        od = OpenData(path=db, overlay=None, http=http, lazy_builder=lambda q: calls.append(q) or None)
        first, second = od.geocode("Тромсё"), od.geocode("Тромсё")
        assert first["in_catalog"] is False and first["id"] is None and first["country_code"] == "NO"
        assert second["source"] == "nominatim" and calls == ["Тромсё"]
        od.http = FakeHttp({})
        od.geocode("тромсе")  # same name after ё/е and case normalisation: no second lazy build
        assert calls == ["Тромсё"]

    def test_network_errors_are_no_data(self, tmp_path):
        db = make_opendata(tmp_path / "o.sqlite", DESTS)
        od = OpenData(path=db, overlay=None, http=FakeHttp({}))
        assert od.geocode("Рейкьявик") is None


class TestCbr:
    def test_live_then_cache_then_stale(self, tmp_path):
        from datetime import date
        http = FakeHttp({CBR: lambda p: FakeResponse(content=CBR_XML)})
        od = OpenData(path=tmp_path / "none.sqlite", overlay=None, http=http, cache_dir=tmp_path,
                      today=date(2026, 10, 24))
        r = od.cbr_rates()
        assert r["rub_per_unit"]["EUR"] == 99.1 and r["date"] == "2026-10-24" and r["stale"] is False
        assert json.loads((tmp_path / "cbr_rates.json").read_text())["fetched_on"] == "2026-10-24"
        od.http = FakeHttp({})
        assert od.cbr_rates()["stale"] is False and od.http.calls == []  # same day: cache, no network
        od._today = date(2026, 10, 25)
        stale = od.cbr_rates()
        assert stale["stale"] is True and stale["date"] == "2026-10-24" and stale["rub_per_unit"]["USD"] == 92.5012

    def test_no_cache_no_network(self, tmp_path):
        od = OpenData(path=tmp_path / "none.sqlite", overlay=None, cache_dir=tmp_path)
        assert od.cbr_rates() == {"source": "CBR", "date": None, "rub_per_unit": {}, "stale": True, "available": False}

    def test_bad_payload_falls_back(self, tmp_path):
        http = FakeHttp({CBR: lambda p: FakeResponse(content=b"<html>503</html>")})
        od = OpenData(path=tmp_path / "none.sqlite", overlay=None, http=http, cache_dir=tmp_path)
        assert od.cbr_rates()["available"] is False


def test_missing_database_is_empty(tmp_path):
    od = OpenData(path=tmp_path / "missing.sqlite", overlay=tmp_path / "missing2.sqlite")
    assert od.available is False
    assert od.find_destinations(["sea"], 11, "Москва") == []
    assert od.geocode("Сочи") is None and od.climate("sochi", 11) is None and od.travel_time("Москва", "sochi") is None


def test_aliases_and_two_level_endings(tmp_path):
    db = make_opendata(tmp_path / "o.sqlite", DESTS + [
        {"id": "spb", "name_ru": "Санкт-Петербург", "name_en": "Saint Petersburg", "country": "Россия",
         "lat": 59.93, "lon": 30.33},
        {"id": "antalya", "name_ru": "Анталья", "name_en": "Antalya", "country": "Турция", "lat": 36.9, "lon": 30.7}])
    od = OpenData(path=db, overlay=None)
    assert od.geocode("Питер")["id"] == "spb" and od.geocode("СПб")["id"] == "spb"
    assert od.geocode("Анталию")["id"] == "antalya" and od.geocode("в Анталии")["id"] == "antalya"


def test_month_climate_replaces_static_tags(tmp_path):
    dests = [{"id": "a", "name_ru": "А", "name_en": "A", "country": "X", "lat": 0, "lon": 0, "tags": ["sea", "warm"]},
             {"id": "b", "name_ru": "Б", "name_en": "B", "country": "X", "lat": 1, "lon": 1, "tags": ["sea", "hot"]},
             {"id": "c", "name_ru": "В", "name_en": "C", "country": "X", "lat": 2, "lon": 2, "tags": ["sea"]}]
    clim = [{"destination_id": "a", "month": 11, "air_max_c": 12, "sea_c": 14},
            {"destination_id": "c", "month": 11, "air_max_c": 29, "sea_c": 27}]
    od = OpenData(path=make_opendata(tmp_path / "o.sqlite", dests, clim), overlay=None)
    res = {r["id"]: r for r in od.find_destinations(["sea", "warm"], 11, None)}
    assert res["a"]["match"] == ["sea"]                 # static "warm" dropped: 12 °C in November
    assert res["b"]["match"] == ["sea", "warm"] and res["b"]["climate_known"] is False   # hot implies warm
    assert res["c"]["match"] == ["sea", "warm"] and res["c"]["climate_known"] is True
    assert [r["id"] for r in od.find_destinations(["sea", "warm"], 11, None)] == ["c", "b", "a"]
