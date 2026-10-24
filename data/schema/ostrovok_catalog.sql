-- data/ostrovok_catalog.sqlite: the mock's committed read-only inventory (AGENT_PLAN.md section 8).
-- Real: hotel names, coordinates, kinds (OSM). Synthetic (deterministic): stars, rating, reviews,
-- rooms, prices, policies, insights events. Every fact row carries `source` and `fetched_at`
-- (for synthetic rows: the generation timestamp). JSON columns hold JSON text.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hotels (
    hotel_id            TEXT PRIMARY KEY,
    osm_id              TEXT NOT NULL UNIQUE,            -- 'node/123' | 'way/456'
    destination_id      TEXT NOT NULL,                   -- opendata destinations.id
    name                TEXT NOT NULL,                   -- real (OSM)
    kind                TEXT NOT NULL,                   -- real OSM tourism tag: hotel, apartment, guest_house ...
    lat                 REAL NOT NULL,
    lon                 REAL NOT NULL,
    address             TEXT,
    stars               INTEGER CHECK (stars BETWEEN 0 AND 5),   -- OSM 'stars' if tagged, else synthetic
    rating              REAL CHECK (rating BETWEEN 0 AND 10),    -- synthetic
    reviews             INTEGER NOT NULL DEFAULT 0,              -- synthetic
    distance_beach_m    INTEGER,
    distance_center_m   INTEGER,
    photos              TEXT NOT NULL DEFAULT '[]',      -- JSON [{url, source, license}] real links only
    synthetic_fields    TEXT NOT NULL DEFAULT '[]',      -- JSON array of column names that are synthetic
    source              TEXT NOT NULL,                   -- 'osm' | 'photon'
    fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hotels_destination ON hotels(destination_id);
CREATE INDEX IF NOT EXISTS idx_hotels_kind ON hotels(destination_id, kind);

CREATE TABLE IF NOT EXISTS rooms (
    room_id             TEXT PRIMARY KEY,
    hotel_id            TEXT NOT NULL REFERENCES hotels(hotel_id),
    name                TEXT NOT NULL,                   -- Russian
    capacity_adults     INTEGER NOT NULL CHECK (capacity_adults >= 1),
    capacity_children   INTEGER NOT NULL DEFAULT 0,
    bed                 TEXT,
    base_price_rub      INTEGER NOT NULL CHECK (base_price_rub >= 0),  -- per night, before seasonal factors
    meals_options       TEXT NOT NULL DEFAULT '["none"]',               -- JSON array of meals codes
    source              TEXT NOT NULL DEFAULT 'synthetic',
    fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rooms_hotel ON rooms(hotel_id);

CREATE TABLE IF NOT EXISTS policies (
    hotel_id                TEXT PRIMARY KEY REFERENCES hotels(hotel_id),
    check_in_from           TEXT NOT NULL,               -- 'HH:MM'
    check_out_until         TEXT NOT NULL,               -- 'HH:MM'
    pets_allowed            INTEGER NOT NULL CHECK (pets_allowed IN (0, 1)),
    children_allowed        INTEGER NOT NULL DEFAULT 1 CHECK (children_allowed IN (0, 1)),
    min_child_age           INTEGER,
    infant_cot              TEXT NOT NULL DEFAULT 'on_request' CHECK (infant_cot IN ('free', 'paid', 'on_request', 'none')),
    extra_bed_price_rub     INTEGER,
    early_checkin_available INTEGER NOT NULL DEFAULT 0 CHECK (early_checkin_available IN (0, 1)),
    early_checkin_price_rub INTEGER,
    free_cancellation_days  INTEGER,                     -- free until N days before check-in; NULL = non-refundable
    penalty_pct             INTEGER NOT NULL DEFAULT 100 CHECK (penalty_pct BETWEEN 0 AND 100),
    source                  TEXT NOT NULL DEFAULT 'synthetic',
    fetched_at              TEXT NOT NULL
);

-- Synthetic log from a hidden preference model; the mock aggregates it with k >= 20.
CREATE TABLE IF NOT EXISTS insights_events (
    id                  INTEGER PRIMARY KEY,
    at                  TEXT NOT NULL,                   -- ISO 8601 with offset
    kind                TEXT NOT NULL,                   -- search | booking
    origin_city         TEXT,
    destination_id      TEXT,
    destination_kind    TEXT,
    month               INTEGER CHECK (month BETWEEN 1 AND 12),
    segment             TEXT CHECK (segment IN ('solo', 'couple', 'family', 'friends', 'other')),
    nights              INTEGER,
    budget_rub          INTEGER,
    meals               TEXT,
    free_cancellation   INTEGER CHECK (free_cancellation IN (0, 1)),
    direct_flight       INTEGER CHECK (direct_flight IN (0, 1)),
    district            TEXT CHECK (district IN ('lively', 'quiet', 'family', 'walkable')),
    data                TEXT NOT NULL DEFAULT '{}',
    source              TEXT NOT NULL DEFAULT 'synthetic' CHECK (source = 'synthetic'),
    fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insights_popular ON insights_events(origin_city, month, segment);
CREATE INDEX IF NOT EXISTS idx_insights_segment ON insights_events(segment, destination_kind);
