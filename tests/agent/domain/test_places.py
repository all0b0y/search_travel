"""Named places (agent.places + OpenData.find_poi over the real `pois` table), the mock's near filter and the
ranking's `near` component. Only istanbul / moscow / saint-petersburg are used (the pois build adds the rest
later)."""
import time
from pathlib import Path

import pytest

from agent.places import PoiIndex, is_generic, key, near_phrases, norm
from agent.ranking import near_score, ranking_rules

ROOT = Path(__file__).resolve().parents[3]
OPENDATA = ROOT / "data" / "opendata.sqlite"
CATALOG = ROOT / "data" / "ostrovok_catalog.sqlite"
real = pytest.mark.skipif(not (OPENDATA.exists() and CATALOG.exists()), reason="real data DBs not built")

CUES = ["рядом с", "у", "недалеко от", "поближе к", "ближе к", "в районе", "в шаговой доступности от", "возле"]


@pytest.fixture(scope="module")
def od():
    from agent.opendata import OpenData
    return OpenData(path=str(OPENDATA), overlay=None)


# ---------------------------------------------------------------- pure helpers

def test_keys_bridge_cyrillic_and_latin():
    assert key("Айя-София") == key("Ayasofya") == "aiasofia"
    assert key("Султанахмет") == key("Sultan Ahmet")
    assert key("Галата") == key("Galata")
    assert norm("Исаа́киевский Собо́р") == "исаакиевский собор"
    assert norm("Ёлки-Палки!") == "елки палки"


def test_generic_words():
    for w in ("собор", "собора", "мечети", "центра", "парк", "площадь", "музея", "аэропорта"):
        assert is_generic(norm(w)), w
    for w in ("софии", "эрмитажа", "кремля", "галата", "султанахмет"):
        assert not is_generic(norm(w)), w


def test_near_phrases():
    assert near_phrases("Отель тот же, 2,5 км от центра. Есть что-то рядом с Айя-Софией?", CUES) == ["Айя-Софией"]
    assert near_phrases("в районе Султанахмет на 4 ночи", CUES) == ["Султанахмет"]
    assert near_phrases("рядом с самой Красной площадью, пожалуйста", CUES) == ["Красной площадью"]
    assert near_phrases("рядом с Голубой мечетью и Айя-Софией", CUES) == ["Голубой мечетью"]
    assert near_phrases("хочу в Стамбул на неделю", CUES) == []


def test_index_on_toy_rows():
    rows = [{"destination_id": "x", "osm_id": "w1", "kind": "worship", "name": "Собор Святой Софии",
             "names": '["Собор Святой Софии", "Hagia Sophia", "Ayasofya"]', "lat": 1.0, "lon": 2.0,
             "dist_center_m": 500, "sitelinks": 150, "rank": 1},
            {"destination_id": "x", "osm_id": "w2", "kind": "worship", "name": "Собор Святого Георгия",
             "names": '["Собор Святого Георгия"]', "lat": 1.1, "lon": 2.1, "dist_center_m": 900, "sitelinks": 20,
             "rank": 2},
            {"destination_id": "x", "osm_id": "w3", "kind": "district", "name": "Sultan Ahmet",
             "names": '["Sultan Ahmet", "Sultanahmet"]', "lat": 1.2, "lon": 2.2, "dist_center_m": 0,
             "sitelinks": None, "rank": 3}]
    idx = PoiIndex(rows)
    assert idx.find("Айя-Софией")["osm_id"] == "w1"
    assert idx.find("собор") is None and idx.find("Собора") is None     # a generic word never picks a church
    assert idx.find("Святого Георгия")["osm_id"] == "w2"
    hit = idx.find("Султанахмет")
    assert hit["osm_id"] == "w3" and hit["name"] == "Султанахмет"       # a Latin-only name shown as the user wrote it
    assert idx.find("Таксим") is None
    assert PoiIndex([]).find("Айя-София") is None


# ---------------------------------------------------------------- the real pois

@real
@pytest.mark.parametrize("dest, text, name", [
    ("istanbul", "Айя-София", "Собор Святой Софии"),
    ("istanbul", "Айя-Софией", "Собор Святой Софии"),
    ("istanbul", "Hagia Sophia", "Собор Святой Софии"),
    ("istanbul", "Голубой мечети", "Голубая мечеть"),
    ("istanbul", "Султанахмет", "Площадь Султанахмет"),     # ties -> more sitelinks (the square over the district)
    ("istanbul", "Галатской башни", "Галатская башня"),
    ("istanbul", "Топкапы", "дворец Топкапы"),
    ("moscow", "Красной площадью", "Красная площадь"),
    ("moscow", "Кремлём", "Московский Кремль"),
    ("moscow", "Большого театра", "Большой театр"),
    ("saint-petersburg", "Эрмитажа", "Государственный Эрмитаж"),
    ("saint-petersburg", "Исаакиевского собора", "Исаакиевский собор"),
    ("saint-petersburg", "Зимнего дворца", "Зимний дворец"),
])
def test_find_poi_real(od, dest, text, name):
    hit = od.find_poi(dest, text)
    assert hit is not None and hit["name"] == name, (text, hit)
    assert hit["lat"] and hit["lon"] and hit["dist_center_m"] is not None


@real
@pytest.mark.parametrize("dest, text", [
    ("istanbul", "собор"), ("istanbul", "мечети"), ("istanbul", "центра"), ("istanbul", "старого города"),
    ("istanbul", "Диснейленд"), ("moscow", "Большим"), ("saint-petersburg", "собора"), ("istanbul", "нас"),
])
def test_find_poi_never_guesses(od, dest, text):
    assert od.find_poi(dest, text) is None, text


@real
def test_find_poi_fast_after_first_build(od):
    t0 = time.perf_counter()
    od.find_poi("istanbul", "Голубая мечеть")         # builds (or reuses) the destination's index
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    for text in ("Айя-Софией", "Галатской башни", "Султанахмет", "Таксим", "Диснейленд", "собор") * 20:
        od.find_poi("istanbul", text)
    per_call = (time.perf_counter() - t0) / 120
    assert first < 0.5 and per_call < 0.005, (first, per_call)


@real
def test_find_poi_in_text(od):
    poi, phrase = od.find_poi_in_text("istanbul", "Есть что-то рядом с Айя-Софией?", CUES)
    assert poi["name"] == "Собор Святой Софии" and phrase == "Айя-Софией"
    poi, phrase = od.find_poi_in_text("istanbul", "у нас двое детей", CUES)
    assert poi is None and phrase == "нас двое детей"
    assert od.find_poi("no-such-place", "Айя-София") is None


# ---------------------------------------------------------------- the mock and the ranking

@real
def test_mock_near_filter_before_limit(monkeypatch, tmp_path):
    from ostrovok_mcp import db, service
    monkeypatch.setenv("OSTROVOK_STATE_DB", str(tmp_path / "state.sqlite"))
    db.reset_caches()
    from agent.opendata import OpenData
    hagia = OpenData(path=str(OPENDATA), overlay=None).find_poi("istanbul", "Айя-София")
    base = {"destination_id": "istanbul", "check_in": "2026-12-01", "check_out": "2026-12-05", "adults": 2,
            "children_ages": [], "limit": 5, "today": "2026-10-25"}
    point = {"lat": hagia["lat"], "lon": hagia["lon"]}
    near = service.call("search_hotels", {**base, "filters": {"near": point, "max_distance_near_m": 600}})
    assert near["offers"] and all(o["distance_near_m"] <= 600 for o in near["offers"])
    top = service.call("search_hotels", {**base, "filters": {}})
    assert all(o.get("distance_near_m") is None for o in top["offers"])     # only when asked
    tagged = service.call("search_hotels", {**base, "filters": {"near": point}})
    assert all(isinstance(o["distance_near_m"], int) for o in tagged["offers"])
    assert {o["hotel_id"] for o in near["offers"]} != {o["hotel_id"] for o in top["offers"]}
    db.reset_caches()


def test_near_score_ramp():
    rr = ranking_rules({})
    assert near_score(400, rr) == 1.0 and near_score(3000, rr) == 0.0 and near_score(None, rr) is None
    assert 0.4 < near_score(1750, rr) < 0.6
