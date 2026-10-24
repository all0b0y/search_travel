"""District scoring formulas (data_build/districts.py docstring) and hotel selection."""
import random

import pytest

from data_build.districts import RADIUS_M, Features, district_for, near, sat, scores
from data_build.hotels import MAX_HOTELS, select_hotels
from data_build.overpass import HotelCandidate

C = {k: 0 for k in ("bar", "pub", "nightclub", "restaurant", "cafe", "fast_food", "pharmacy", "supermarket",
                    "convenience", "playground", "park", "pedestrian", "stop", "station", "beach")}


def test_helpers():
    assert sat(0, 5) == 0 and sat(5, 5) == 1 and sat(10, 5) == 1 and sat(2.5, 5) == 0.5
    assert near(None, 50, 300) == 0 and near(10, 50, 300) == 1 and near(300, 50, 300) == 0
    assert near(175, 50, 300) == pytest.approx(0.5)


def test_empty_area_is_quiet_not_lively():
    s = scores(dict(C), {"centre": 8000}, dest_has_beach=False)
    assert s["lively"] == 0 and s["quiet"] == 1 and s["walkable"] == 0 and s["family"] == 0


def test_party_street_is_lively_not_quiet_not_family():
    c = dict(C, bar=10, pub=6, nightclub=3, restaurant=40, cafe=20, playground=3, park=3, supermarket=5)
    s = scores(c, {"centre": 300, "major_road": 40}, dest_has_beach=False)
    assert s["lively"] == 1.0
    # quiet = 1 - (0.55 + 0.15 + 0.2) = 0.1
    assert s["quiet"] == pytest.approx(0.1)
    # family = 0.3 + 0.25 + 0.2 + 0.25 * sat(park, 3) - 0.45 = 0.55
    assert s["family"] == pytest.approx(0.55)


def test_family_formula_with_beach():
    c = dict(C, playground=3, park=3, supermarket=3, pharmacy=2)
    s = scores(c, {"beach": 200, "centre": 2000}, dest_has_beach=True)
    assert s["family"] == pytest.approx(1.0)
    s2 = scores(c, {"beach": 2000, "centre": 2000}, dest_has_beach=True)
    assert s2["family"] == pytest.approx(0.75)


def test_walkable_formula():
    c = dict(C, restaurant=20, cafe=10, supermarket=10, stop=8, pedestrian=4)
    s = scores(c, {"centre": 1000}, dest_has_beach=False)
    assert s["walkable"] == pytest.approx(1.0)
    s2 = scores(c, {"centre": 3000}, dest_has_beach=False)
    assert s2["walkable"] == pytest.approx(0.9)


def test_scores_always_in_range():
    rnd = random.Random(1)
    for _ in range(300):
        c = {k: rnd.randint(0, 80) for k in C}
        nearest = {k: rnd.randint(0, 6000) for k in ("beach", "major_road", "railway", "centre")}
        for v in scores(c, nearest, rnd.random() < 0.5).values():
            assert 0 <= v <= 1


def test_district_for_counts_within_radius_and_nearest():
    f = Features()
    lat, lon = 43.6, 39.7
    f.add_point("bar", lat + 0.001, lon)            # ~111 m
    f.add_point("bar", lat + 0.004, lon)            # ~445 m
    f.add_point("bar", lat + 0.01, lon)             # ~1.1 km, outside
    f.add_point("station", lat, lon + 0.02)         # ~1.6 km
    f.add_line("major_road", [(lat - 0.01, lon + 0.0005), (lat + 0.01, lon + 0.0005)])   # ~40 m east
    f.add_line("beach", [(lat + 0.006, lon - 0.01), (lat + 0.006, lon + 0.01)])         # ~667 m north
    d = district_for(lat, lon, f, (lat, lon + 0.01))
    assert d["poi_counts"]["bar"] == 2
    assert 1500 < d["nearest_m"]["station"] < 1700
    assert 30 < d["nearest_m"]["major_road"] < 50
    assert 640 < d["nearest_m"]["beach"] < 700
    assert 780 < d["nearest_m"]["centre"] < 830
    assert "railway" not in d["nearest_m"]
    assert set(d) >= {"lively", "quiet", "family", "walkable", "poi_counts", "nearest_m"}
    assert RADIUS_M == 500


def _cands(n, kinds=("hotel", "hostel", "apartment", "guest_house")):
    rnd = random.Random(7)
    return [HotelCandidate(f"node/{1000 + i}", f"H{i}", kinds[i % len(kinds)], 36.8 + rnd.random() * 0.1,
                           30.6 + rnd.random() * 0.1) for i in range(n)]


def test_select_hotels_deterministic_and_order_independent():
    bbox = (36.8, 30.6, 36.9, 30.7)
    cands = _cands(300)
    a = select_hotels(cands, bbox)
    shuffled = list(cands)
    random.Random(3).shuffle(shuffled)
    b = select_hotels(shuffled, bbox)
    assert [c.osm_id for c in a] == [c.osm_id for c in b]
    assert len(a) == MAX_HOTELS
    assert a == sorted(a, key=lambda c: c.osm_id)


def test_select_hotels_diverse():
    bbox = (36.8, 30.6, 36.9, 30.7)
    sel = select_hotels(_cands(400), bbox)
    kinds = {c.kind for c in sel}
    assert kinds == {"hotel", "hostel", "apartment", "guest_house"}
    n_hotel = sum(c.kind == "hotel" for c in sel)
    n_hostel = sum(c.kind == "hostel" for c in sel)
    assert n_hotel > n_hostel                       # weights 3 vs 1 per round
    cells = {(int((c.lat - 36.8) / 0.025), int((c.lon - 30.6) / 0.025)) for c in sel}
    assert len(cells) >= 12                          # spread over the 4x4 grid


def test_select_hotels_small_input_keeps_all():
    sel = select_hotels(_cands(10), (36.8, 30.6, 36.9, 30.7))
    assert len(sel) == 10
