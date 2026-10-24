import sqlite3

import pytest

SCHEMAS = ("opendata.sql", "ostrovok_catalog.sql", "ostrovok_state.sql")
FACT_TABLES = {
    "opendata.sql": ("destinations", "climate", "airports", "routes", "districts"),
    "ostrovok_catalog.sql": ("hotels", "rooms", "policies", "insights_events"),
    "ostrovok_state.sql": ("insights_events_live",),
}
EXPECTED_TABLES = {
    "opendata.sql": {"meta", "destinations", "climate", "airports", "routes", "districts"},
    "ostrovok_catalog.sql": {"meta", "hotels", "rooms", "policies", "insights_events"},
    "ostrovok_state.sql": {"carts", "cart_items", "orders", "order_items", "order_events", "travellers",
                           "insights_events_live"},
}


def apply(root, name):
    db = sqlite3.connect(":memory:")
    db.executescript((root / "data/schema" / name).read_text(encoding="utf-8"))
    return db


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_applies_cleanly_and_is_idempotent(root, name):
    db = apply(root, name)
    assert tables(db) == EXPECTED_TABLES[name]
    db.executescript((root / "data/schema" / name).read_text(encoding="utf-8"))  # IF NOT EXISTS everywhere
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("name", SCHEMAS)
def test_fact_tables_have_source_and_fetched_at(root, name):
    db = apply(root, name)
    for table in FACT_TABLES[name]:
        assert {"source", "fetched_at"} <= columns(db, table), table


def test_opendata_roundtrip_and_checks(root):
    db = apply(root, "opendata.sql")
    db.execute("INSERT INTO destinations (id, name_ru, name_en, country, country_code, lat, lon, kind, source, fetched_at)"
               " VALUES ('sochi', 'Сочи', 'Sochi', 'Россия', 'RU', 43.6, 39.7, 'resort', 'test', '2026-10-24T00:00:00Z')")
    db.execute("INSERT INTO climate VALUES ('sochi', 11, 15.2, 8.1, 16.0, 11, 9, 'open-meteo-archive', '2026-10-24T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO climate VALUES ('sochi', 13, 1, 1, 1, 1, 1, 'x', 'y')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO climate VALUES ('nowhere', 1, 1, 1, 1, 1, 1, 'x', 'y')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO districts (osm_id, lat, lon, lively, quiet, family, walkable, source, fetched_at)"
                   " VALUES ('node/1', 0, 0, 1.5, 0, 0, 0, 'overpass', 'x')")


def test_catalog_insights_must_be_synthetic(root):
    db = apply(root, "ostrovok_catalog.sql")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO insights_events (at, kind, source, fetched_at) VALUES ('t', 'search', 'real', 'x')")
    db.execute("INSERT INTO insights_events (at, kind, segment, fetched_at) VALUES ('t', 'search', 'family', 'x')")


def test_state_cart_order_flow(root):
    db = apply(root, "ostrovok_state.sql")
    t = "2026-10-25T13:05:00+03:00"
    db.execute("INSERT INTO carts VALUES ('c1', 's_1', 'open', ?, ?)", (t, t))
    with pytest.raises(sqlite3.IntegrityError):  # one open cart per session
        db.execute("INSERT INTO carts VALUES ('c2', 's_1', 'open', ?, ?)", (t, t))
    db.execute("INSERT INTO cart_items (item_id, cart_id, offer_id, kind, title, price_rub, added_at)"
               " VALUES ('ci1', 'c1', 'fl_1', 'flight', 'Перелёт', 11200, ?)", (t,))
    db.execute("INSERT INTO orders (order_id, cart_id, session_id, status, total_rub, created_at, updated_at)"
               " VALUES ('o1', 'c1', 's_1', 'pending_payment', 11200, ?, ?)", (t, t))
    db.execute("INSERT INTO travellers (order_id, position, name, birth_date) VALUES ('o1', 1, 'Иван', '1990-01-01')")
    db.execute("INSERT INTO order_events (order_id, at, kind) VALUES ('o1', ?, 'created')", (t,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO order_items (item_id, order_id, offer_id, kind, title, price_rub)"
                   " VALUES ('oi1', 'o1', 'x', 'rocket', 'x', 1)")
    assert "session_id" not in columns(db, "insights_events_live")
