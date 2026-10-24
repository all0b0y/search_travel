"""Insights generator: determinism, size, k-anonymity-friendly groups, visible preferences."""
from collections import Counter

import pytest

from data_build.insights import FLOOR_PER_CELL, ORIGINS, SEGMENTS, TOTAL_EVENTS, allocate, generate

DESTS = [
    {"id": "antalya", "kind": "resort", "tags": ["sea", "beach", "warm"], "popularity": 0.95, "country_code": "TR",
     "climate": {m: {"air_max_c": 15 + m if m < 8 else 34 - m, "sea_c": 17 + m if m < 9 else 29 - m} for m in range(1, 13)}},
    {"id": "moscow", "kind": "city", "tags": ["city"], "popularity": 0.9, "country_code": "RU", "climate": {}},
    {"id": "kazan", "kind": "city", "tags": ["city"], "popularity": 0.85, "country_code": "RU", "climate": {}},
    {"id": "krasnaya-polyana", "kind": "resort", "tags": ["ski", "mountains"], "popularity": 0.8,
     "country_code": "RU", "climate": {}},
    {"id": "paris", "kind": "city", "tags": ["city"], "popularity": 0.65, "country_code": "FR", "climate": {}},
    {"id": "altai", "kind": "nature", "tags": ["mountains"], "popularity": 0.6, "country_code": "RU", "climate": {}},
]


@pytest.fixture(scope="module")
def events():
    return generate(DESTS)


def test_deterministic():
    a = generate(DESTS, total=9000)
    b = generate(list(reversed(DESTS)), total=9000)
    assert a == b
    c = generate(DESTS, total=9000, seed=1)
    assert a != c


def test_size_and_source(events):
    assert len(events) == TOTAL_EVENTS >= 20000
    assert all(e["source"] == "synthetic" for e in events)
    assert len({e["id"] for e in events}) == len(events)
    allowed = {"id", "at", "kind", "origin_city", "destination_id", "destination_kind", "month", "segment", "nights",
               "budget_rub", "meals", "free_cancellation", "direct_flight", "district", "data", "source", "fetched_at"}
    assert all(set(e) == allowed for e in events)                   # no personal data fields
    assert set(events[0]["data"]) == {"budget_band", "adults", "children"}


def test_every_origin_segment_month_group_has_at_least_20(events):
    groups = Counter((e["origin_city"], e["segment"], e["month"]) for e in events)
    assert len(groups) == len(ORIGINS) * len(SEGMENTS) * 12
    assert min(groups.values()) >= max(20, FLOOR_PER_CELL)
    alloc = allocate()
    assert sum(alloc.values()) == TOTAL_EVENTS
    assert alloc[("Москва", "couple", 7)] > alloc[("Казань", "solo", 2)]


def test_home_city_is_never_the_destination(events):
    assert not any(e["origin_city"] == "Москва" and e["destination_id"] == "moscow" for e in events)
    assert not any(e["origin_city"] == "Казань" and e["destination_id"] == "kazan" for e in events)


def test_hidden_preferences_show_up(events):
    def share(seg, pred):
        es = [e for e in events if e["segment"] == seg]
        return sum(pred(e) for e in es) / len(es)
    ai = lambda e: e["meals"] == "all_inclusive"  # noqa: E731
    assert share("family", ai) > share("solo", ai)
    assert share("family", lambda e: e["free_cancellation"]) > share("solo", lambda e: e["free_cancellation"])
    assert share("family", lambda e: e["district"] == "family") > 0.4
    assert share("friends", lambda e: e["district"] == "lively") > 0.5
    ski_winter = sum(e["destination_id"] == "krasnaya-polyana" for e in events if e["month"] in (1, 2))
    ski_summer = sum(e["destination_id"] == "krasnaya-polyana" for e in events if e["month"] in (7, 8))
    assert ski_winter > 2 * ski_summer
    sea_summer = sum(e["destination_id"] == "antalya" for e in events if e["month"] == 8)
    sea_winter = sum(e["destination_id"] == "antalya" for e in events if e["month"] == 1)
    assert sea_summer > sea_winter
    assert all(e["nights"] >= 1 and e["budget_rub"] > 0 for e in events)
    assert {e["kind"] for e in events} == {"search", "booking"}
