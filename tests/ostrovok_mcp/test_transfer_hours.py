"""Transfer service hours are local to the place, whatever offset `at` carries."""
import sqlite3

import pytest

from ostrovok_mcp import service


@pytest.fixture()
def minsk_hotel():
    con = sqlite3.connect("data/ostrovok_catalog.sqlite")
    row = con.execute("SELECT hotel_id FROM hotels WHERE destination_id = 'minsk' ORDER BY hotel_id LIMIT 1").fetchone()
    con.close()
    assert row, "the catalog has Minsk hotels"
    return row[0]


def _kinds(hotel_id, at):
    out = service.call("search_transfers", {"from": {"iata": "MSQ"}, "to": {"hotel_id": hotel_id}, "at": at, "pax": 1,
                                            "reverse": False, "today": "2026-10-25"})
    return {o["kind"] for o in out.get("offers") or []}


def test_no_public_transport_at_night_even_when_at_is_utc(minsk_hotel):
    assert "public" not in _kinds(minsk_hotel, "2026-12-05T20:53:00+00:00")      # 23:53 in Minsk
    assert "public" not in _kinds(minsk_hotel, "2026-12-05T23:53:00+03:00")
    assert "taxi" in _kinds(minsk_hotel, "2026-12-05T20:53:00+00:00")


def test_public_transport_by_day_and_not_when_it_would_end_after_midnight(minsk_hotel):
    assert "public" in _kinds(minsk_hotel, "2026-12-05T12:00:00+03:00")
    assert "public" in _kinds(minsk_hotel, "2026-12-05T09:00:00+00:00")           # 12:00 in Minsk
    assert "public" not in _kinds(minsk_hotel, "2026-12-05T22:50:00+03:00")      # a ~2 h ride ends after midnight
