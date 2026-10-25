"""Stage `pois`: sights and named districts from a fake Overpass + Wikidata (no network)."""
from __future__ import annotations

import json
import sqlite3

from data_build import common, pois
from data_build.common import open_db

from .helpers import FakeHttp

ISTANBUL = ("istanbul", "Стамбул", "Istanbul", "Турция", "TR", 41.004114, 28.976909, "city")

OVERPASS = {"elements": [
    # the same sight as a node and as a way: one row, the way wins, names merged
    {"type": "node", "id": 11, "lat": 41.0085, "lon": 28.9801,
     "tags": {"tourism": "attraction", "wikidata": "Q12506", "name": "Ayasofya", "alt_name": "Aya Sofya"}},
    {"type": "way", "id": 12, "center": {"lat": 41.0086, "lon": 28.9802},
     "tags": {"amenity": "place_of_worship", "wikidata": "Q12506", "name": "Ayasofya-i Kebir Câmi-i Şerîfi",
              "name:ru": "Айя-София", "name:en": "Hagia Sophia"}},
    # no Russian name in OSM: the Wikidata ru label and aliases fill it
    {"type": "way", "id": 13, "center": {"lat": 41.0054, "lon": 28.9768},
     "tags": {"amenity": "place_of_worship", "wikidata": "Q19233", "name": "Sultan Ahmet Camii",
              "name:en": "Blue Mosque"}},
    {"type": "node", "id": 14, "lat": 41.0101, "lon": 28.9700,
     "tags": {"historic": "memorial", "wikidata": "Q999", "name": "Small plaque"}},
    # a sight whose only name is unreadable for a Russian user and without Wikidata labels: dropped
    {"type": "node", "id": 15, "lat": 41.02, "lon": 28.98, "tags": {"historic": "monument", "wikidata": "Q777",
                                                                     "name": "ضريح"}},
    # OSM tagged the monument with the person's item (a human in Wikidata): OSM names only, no item
    {"type": "way", "id": 17, "center": {"lat": 41.0167, "lon": 28.9497},
     "tags": {"historic": "monument", "wikidata": "Q34474", "name": "Fatih Anıtı"}},
    # a huge object reaching into the box with its centre far away: dropped
    {"type": "relation", "id": 18, "center": {"lat": 40.2, "lon": 27.9},
     "tags": {"natural": "bay", "wikidata": "Q4918", "name": "Marmara Denizi", "name:ru": "Мраморное море"}},
    # not a Wikidata item: ignored for sights
    {"type": "node", "id": 16, "lat": 41.03, "lon": 28.99, "tags": {"tourism": "attraction", "name": "Nameless"}},
    # districts: named ones kept, an unreadable one dropped, node + relation of the same name collapse
    {"type": "node", "id": 21, "lat": 41.0058, "lon": 28.9768,
     "tags": {"place": "quarter", "name": "Sultanahmet", "name:ru": "Султанахмет"}},
    {"type": "relation", "id": 22, "center": {"lat": 41.0059, "lon": 28.9770},
     "tags": {"place": "quarter", "name": "Sultanahmet", "name:ru": "Султанахмет", "wikidata": "Q1046593"}},
    {"type": "node", "id": 23, "lat": 41.0256, "lon": 28.9741, "tags": {"place": "neighbourhood", "name": "Galata"}},
    {"type": "node", "id": 24, "lat": 41.04, "lon": 28.95, "tags": {"place": "suburb", "name": "ضاحية"}},
]}


def _entity(qid, ru, en, aliases_ru=(), sitelinks=0):
    return {"id": qid, "labels": {**({"ru": {"value": ru}} if ru else {}), **({"en": {"value": en}} if en else {})},
            "aliases": {"ru": [{"value": a} for a in aliases_ru]},
            "sitelinks": {f"wiki{i}": {"title": "x"} for i in range(sitelinks)}}


ENTITIES = {
    "Q12506": _entity("Q12506", "Собор Святой Софии", "Hagia Sophia", ("Айя-София",), 150),
    "Q19233": _entity("Q19233", "Мечеть Султанахмет", "Sultan Ahmed Mosque", ("Голубая мечеть",), 90),
    "Q999": _entity("Q999", None, "Small plaque", (), 1),
    "Q777": {"id": "Q777", "missing": ""},
    "Q1046593": _entity("Q1046593", "Султанахмет", "Sultanahmet", (), 40),
    "Q34474": _entity("Q34474", "Мехмед II", "Mehmed II", (), 122),
}
HUMANS = {"Q34474"}


def _wikidata(url, params):
    ids = params["ids"].split("|")
    return 200, {"entities": {q: ENTITIES.get(q, {"id": q, "missing": ""}) for q in ids}}


def _sparql(url, data):
    asked = {t[3:] for t in data["query"].split() if t.startswith("wd:Q")}
    return 200, {"results": {"bindings": [{"item": {"value": f"http://www.wikidata.org/entity/{q}"}}
                                          for q in sorted(asked & HUMANS)]}}


def _http():
    return FakeHttp({"overpass": (200, OVERPASS), "query.wikidata.org": _sparql, "www.wikidata.org": _wikidata})


def _source_db(tmp_path):
    db = tmp_path / "opendata.sqlite"
    con = open_db(db, "opendata")
    con.execute("INSERT INTO destinations (id, name_ru, name_en, country, country_code, lat, lon, kind, source, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', '2026-10-25T00:00:00Z')", ISTANBUL)
    con.commit()
    con.close()
    return db


def _rows(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM pois ORDER BY kind = 'district', rank")]
    con.close()
    return rows


def test_pois_stage_ranks_sights_by_wikidata_and_keeps_readable_districts(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    src = _source_db(tmp_path)
    stage = tmp_path / "stage.sqlite"
    http = _http()
    stats = pois.run_pois(source_db=src, out_db=stage, http=http, sleep=lambda s: None)
    assert stats["done"] == 1 and stats["failed"] == []
    rows = _rows(stage)
    sights = [r for r in rows if r["kind"] != "district"]
    assert [r["wikidata"] for r in sights] == ["Q12506", "Q19233", "Q999", None]
    sofia, mosque, plaque, fatih = sights
    assert fatih["name"] == "Fatih Anıtı" and fatih["sitelinks"] is None and "Мехмед II" not in fatih["names"]
    assert sofia["osm_id"] == "w12" and sofia["name"] == "Айя-София" and sofia["kind"] == "worship"
    assert {"Айя-София", "Hagia Sophia", "Собор Святой Софии", "Ayasofya", "Aya Sofya"} <= set(json.loads(sofia["names"]))
    assert 200 < sofia["dist_center_m"] < 800 and sofia["sitelinks"] == 150 and sofia["rank"] == 1
    assert mosque["name"] == "Мечеть Султанахмет" and "Голубая мечеть" in json.loads(mosque["names"])
    assert plaque["name"] == "Small plaque" and plaque["kind"] == "monument"
    districts = [r for r in rows if r["kind"] == "district"]
    assert [(r["name"], r["osm_id"]) for r in districts] == [("Султанахмет", "r22"), ("Galata", "n23")]
    # the staging DB carries the destination row (foreign key) and the done mark
    con = sqlite3.connect(str(stage))
    assert con.execute("SELECT value FROM meta WHERE key='pois_done:istanbul'").fetchone()
    con.close()
    # resume skips; no network reads the cache only
    assert pois.run_pois(source_db=src, out_db=stage, http=http, resume=True, sleep=lambda s: None)["skipped"] == 1
    calls = len(http.calls)
    again = pois.run_pois(source_db=src, out_db=tmp_path / "again.sqlite", network=False, sleep=lambda s: None)
    assert again["done"] == 1 and len(http.calls) == calls
    assert [r["wikidata"] for r in _rows(tmp_path / "again.sqlite") if r["kind"] != "district"][:2] == \
        ["Q12506", "Q19233"]


def test_merge_replaces_the_staged_destinations_only(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    src = _source_db(tmp_path)
    con = sqlite3.connect(str(src))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("INSERT INTO destinations (id, name_ru, name_en, country, country_code, lat, lon, kind, source, "
                "fetched_at) VALUES ('kazan', 'Казань', 'Kazan', 'Россия', 'RU', 55.79, 49.12, 'city', 't', 't')")
    con.execute(f"INSERT INTO pois ({pois.COLUMNS}) VALUES ('istanbul', 'n1', 'monument', 'Старое', NULL, NULL, '[]', "
                "41, 29, 10, NULL, NULL, 1, 'overpass', 't'), ('kazan', 'n2', 'castle', 'Кремль', 'Кремль', NULL, "
                "'[]', 55.8, 49.1, 500, 'Q1', 5, 1, 'overpass+wikidata', 't')")
    con.commit()
    con.close()
    stage = tmp_path / "stage.sqlite"
    pois.run_pois(source_db=src, out_db=stage, only={"istanbul"},
                  http=_http(), sleep=lambda s: None)
    pois.merge(stage, into_db=src)
    rows = _rows(src)
    assert {r["destination_id"] for r in rows} == {"istanbul", "kazan"}
    assert "Старое" not in {r["name"] for r in rows} and "Кремль" in {r["name"] for r in rows}
    assert sum(1 for r in rows if r["destination_id"] == "istanbul") == 6


def test_query_asks_for_wikidata_sights_and_named_districts():
    q = pois.build_query((40.9, 28.8, 41.1, 29.1))
    assert "[bbox:40.9,28.8,41.1,29.1]" in q and '["wikidata"]' in q
    assert "tomb" not in q and "memorial" not in q and "castle" in q
    assert 'nwr["place"~"^(suburb|quarter|neighbourhood)$"]["name"]' in q
