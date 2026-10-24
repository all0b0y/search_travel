"""Insights: k-anonymity (n >= 20) over catalog + live events; the log never takes personal data."""
import sqlite3

import pytest
from pydantic import ValidationError

from ostrovok_mcp import contract as c
from ostrovok_mcp import service as s
from ostrovok_mcp.db import ServiceError

NOW = "2026-10-25T13:05:00+03:00"


def popular(segment="family", origin="Москва", month=11):
    out = s.insights_popular_destinations({"origin_city": origin, "month": month, "segment": segment})
    c.InsightsPopularDestinationsOut.model_validate(out)
    return out["items"]


def test_popular_only_groups_with_k20(mock_env):
    items = popular()
    assert [i["destination_id"] for i in items] == ["antalya"]  # sochi has 15 < 20
    assert items[0]["n"] == 25 and items[0]["share"] == pytest.approx(25 / 40, abs=1e-4)
    assert popular(origin="москва") == items  # case-insensitive city match
    assert popular("couple") == []           # whole group of 10 < 20
    assert popular(month=7) == []


def test_segment_preferences_threshold(mock_env):
    fam = s.insights_segment_preferences({"segment": "family", "destination_kind": "resort"})
    c.InsightsSegmentPreferencesOut.model_validate(fam)
    assert fam["available"] and fam["n"] == 40
    assert 0 <= fam["shares"]["all_inclusive"] <= 1 and fam["median_nights"] in (7.0, 8.0, 9.0)
    p = fam["budget_rub_percentiles"]
    assert p["p25"] <= p["p50"] <= p["p75"]
    assert sum(fam["district_preference"].values()) == pytest.approx(1.0, abs=1e-3)
    couple = s.insights_segment_preferences({"segment": "couple"})
    assert couple["available"] is False and couple["n"] == 0 and couple["shares"] is None
    assert s.insights_segment_preferences({"segment": "family", "destination_kind": "city"})["available"] is False


def test_live_events_join_the_aggregates(mock_env):
    for i in range(20):
        ev = {"kind": "variant_chosen", "segment": "couple", "origin_city": "Москва", "destination_id": "sochi",
              "month": 11, "data": {"variant": "fastest", "meals": "breakfast", "free_cancellation": True,
                                    "direct_flight": True, "district": "lively", "nights": 5, "budget_rub": 90000},
              "at": NOW}
        assert s.insights_log_event({"event": ev}) == {"ok": True}
    items = popular("couple")
    assert items == [{"destination_id": "sochi", "share": 1.0, "n": 30}]
    prefs = s.insights_segment_preferences({"segment": "couple", "destination_kind": "resort"})
    assert prefs["available"] and prefs["n"] == 30


@pytest.mark.parametrize("event", [
    {"kind": "paid", "data": {"guest": {"email": "x@y.ru"}}, "at": NOW},
    {"kind": "paid", "data": {"who": {"Name": "Иван"}}, "at": NOW},
    {"kind": "turn", "data": {"text": "звоните +7 916 123-45-67"}, "at": NOW},
    {"kind": "turn", "data": {"list": [{"passport": "1234"}]}, "at": NOW},
    {"kind": "turn", "origin_city": "ivan@mail.ru", "at": NOW},
])
def test_log_event_rejects_personal_data(mock_env, event):
    with pytest.raises(ServiceError) as err:
        s.insights_log_event({"event": event})
    assert err.value.code == "personal_data"
    s.cart_create({"session_id": "touch", "now": NOW})  # make sure the state DB and its tables exist
    with sqlite3.connect(mock_env["tmp"] / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM insights_events_live").fetchone()[0] == 0


def test_contract_level_guard(mock_env):
    with pytest.raises(ValidationError):
        s.insights_log_event({"event": {"kind": "paid", "data": {"email": "a@b"}, "at": NOW}})
    with pytest.raises(ValidationError):
        s.insights_log_event({"event": {"kind": "paid", "data": {}, "at": "2026-10-25T13:05:00"}})
