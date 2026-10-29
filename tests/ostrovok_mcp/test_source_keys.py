"""Source signatures live for one tool call; new sources remain visible to the next call."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from ostrovok_mcp import db, service

REQUEST = {"destination_id": "sochi", "check_in": "2026-11-14", "check_out": "2026-11-16",
           "adults": 2, "today": "2026-10-25"}
EMPTY = {"offers": [], "total_found": 0, "synthetic": True}


def test_signature_checked_once_per_call_and_refreshed_next_time(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"a")
    original = Path.stat
    calls, keys = [], []

    def stat(path, *args, **kwargs):
        if path == source:
            calls.append(path)
        return original(path, *args, **kwargs)

    def search(request):
        keys.append([db._cache_key([source]) for _ in range(3)])
        return EMPTY

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setitem(service.IMPL, "search_hotels", search)
    service.search_hotels(REQUEST)
    assert len(calls) == 1
    source.write_bytes(b"a longer source")
    service.search_hotels(REQUEST)
    assert len(calls) == 2 and keys[0][0] != keys[1][0]
    assert all(len(set(values)) == 1 for values in keys)


def test_failed_tool_does_not_leave_a_source_signature_cached(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"a")
    seen = []

    def broken(request):
        seen.append(db._cache_key([source]))
        raise db.ServiceError("test_failure", "expected")

    monkeypatch.setitem(service.IMPL, "search_hotels", broken)
    with pytest.raises(db.ServiceError, match="test_failure"):
        service.search_hotels(REQUEST)
    source.write_bytes(b"a longer source")
    assert db._cache_key([source]) != seen[0]


def test_parallel_tool_calls_have_separate_source_signatures(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"a")
    first_read, second_read = Event(), Event()
    seen = {}

    def search(request):
        if request.destination_id == "first":
            seen["before"] = db._cache_key([source])
            first_read.set()
            assert second_read.wait(3)
            seen["after"] = db._cache_key([source])
        else:
            assert first_read.wait(3)
            source.write_bytes(b"a longer source")
            seen["other"] = db._cache_key([source])
            second_read.set()
        return EMPTY

    monkeypatch.setitem(service.IMPL, "search_hotels", search)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.search_hotels, {**REQUEST, "destination_id": "first"})
        second = pool.submit(service.search_hotels, {**REQUEST, "destination_id": "second"})
        first.result()
        second.result()
    assert seen["before"] == seen["after"] and seen["before"] != seen["other"]


def test_overlay_created_between_calls_is_visible(mock_env, monkeypatch):
    seen = []

    def search(request):
        seen.append(db.catalog().query("hotels", "hotel_id = ?", ("new-overlay-hotel",)))
        return EMPTY

    monkeypatch.setitem(service.IMPL, "search_hotels", search)
    service.search_hotels(REQUEST)
    with sqlite3.connect(mock_env["tmp"] / "no_catalog_extra.sqlite") as conn:
        conn.execute("CREATE TABLE hotels (hotel_id TEXT, name TEXT)")
        conn.execute("INSERT INTO hotels VALUES (?, ?)", ("new-overlay-hotel", "Новый отель"))
    service.search_hotels(REQUEST)
    assert seen[0] == [] and seen[1][0]["name"] == "Новый отель"
