"""Deterministic synthetic events refer only to named OSM POIs in the catalog."""
import shutil
import sqlite3
from datetime import datetime

import pytest
from pydantic import ValidationError

from ostrovok_mcp import db, service


@pytest.fixture
def venues(mock_env, monkeypatch):
    path = mock_env["tmp"] / "events-opendata.sqlite"
    shutil.copyfile(mock_env["opendata"], path)
    rows = [("n1", "museum", "Музей", "overpass"),
            ("n2", "theatre", "Театр", "overpass+wikidata"),
            ("n3", "stadium", "Стадион", "overpass"),
            ("n4", "park", "Парк", "overpass"),
            ("n5", "museum", "Выдуманный музей", "synthetic")]
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO pois(destination_id, osm_id, kind, name, lat, lon, dist_center_m, rank, "
                         "source, fetched_at) VALUES ('kazan', ?, ?, ?, 55.8, 49.1, 100, 1, ?, '2026-10-20')", rows)
    monkeypatch.setenv("OPENDATA_DB", str(path))
    db.reset_caches()
    return {row[0]: row for row in rows if row[3] != "synthetic"}


def search(**changes):
    return service.call("search_events", {"destination_id": "kazan", "date_from": "2026-12-01",
                                          "date_to": "2026-12-07", "limit": 100, **changes})


def test_stable_ids_times_prices_and_real_venue_links(venues):
    first = search()
    assert first == search() and first["events"] and first["synthetic"] is True
    events = first["events"]
    assert len({e["event_id"] for e in events}) == len(events)
    assert {e["kind"] for e in events} == {"excursion", "concert", "show", "sport", "festival"}
    for event in events:
        assert event["venue_poi_id"] in venues
        assert event["venue_name"] == venues[event["venue_poi_id"]][2]
        assert event["source"] == "synthetic" and "синтетика" in event["title_ru"]
        start, end = map(datetime.fromisoformat, (event["starts_at"], event["ends_at"]))
        assert start.tzinfo and end.tzinfo and end > start
        assert (end - start).total_seconds() == event["duration_min"] * 60
        assert "2026-12-01" <= event["starts_at"][:10] <= "2026-12-07"
        assert event["price_from_rub"] > 0


def test_filters_do_not_regenerate_events_and_apply_before_limit(venues):
    all_events = search()["events"]
    excursions = [e for e in all_events if e["kind"] == "excursion"]
    assert search(kinds=["excursion"])["events"] == excursions
    tag = excursions[-1]["niche_tags"][0]
    expected = [e for e in excursions if tag in e["niche_tags"]]
    assert expected
    assert search(kinds=["excursion"], tags=[tag], limit=2)["events"] == expected[:2]
    assert search(tags=["not_a_theme"])["events"] == []
    assert search(limit=2)["events"] == all_events[:2]


def test_date_window_is_inclusive_and_events_survive_window_changes(venues):
    all_events = search()["events"]
    expected = [e for e in all_events if e["starts_at"].startswith("2026-12-03")]
    assert expected
    assert search(date_from="2026-12-03", date_to="2026-12-03")["events"] == expected


def test_unknown_destination_and_missing_venues_are_empty(venues):
    assert search(destination_id="unknown")["events"] == []
    assert search(destination_id="sochi")["events"] == []


@pytest.mark.parametrize("changes", [
    {"date_from": "2026-12-10"}, {"date_to": "2028-01-01"}, {"kinds": ["invented"]},
    {"limit": 0}, {"limit": 101},
])
def test_invalid_search_is_rejected(venues, changes):
    with pytest.raises(ValidationError):
        search(**changes)
