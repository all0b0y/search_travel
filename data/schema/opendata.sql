-- data/opendata.sqlite: open data read by the engine's open-data tools (AGENT_PLAN.md section 8).
-- Built offline by data_build/build_all.py. Every fact row carries `source` and `fetched_at`
-- (ISO 8601 UTC timestamp of the download / computation). JSON columns hold JSON text.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS destinations (
    id              TEXT PRIMARY KEY,                -- slug, e.g. 'antalya'
    name_ru         TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    country         TEXT NOT NULL,                   -- Russian country name
    country_code    TEXT NOT NULL,                   -- ISO 3166-1 alpha-2
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    kind            TEXT NOT NULL,                   -- city | resort | nature | village ...
    tags            TEXT NOT NULL DEFAULT '[]',      -- JSON array of agent.state.DESTINATION_TRAITS
    airports        TEXT NOT NULL DEFAULT '[]',      -- JSON array of IATA codes serving the place
    stations        TEXT NOT NULL DEFAULT '[]',      -- JSON array of railway station names
    aliases         TEXT NOT NULL DEFAULT '[]',      -- JSON array of colloquial names: 'Питер', 'СПб', 'Екб'
    timezone        TEXT,                            -- IANA, e.g. 'Europe/Istanbul'
    description_ru  TEXT,
    popularity      REAL NOT NULL DEFAULT 0,         -- 0..1
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_destinations_country ON destinations(country_code);
CREATE INDEX IF NOT EXISTS idx_destinations_kind ON destinations(kind);

CREATE TABLE IF NOT EXISTS climate (
    destination_id  TEXT NOT NULL REFERENCES destinations(id),
    month           INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    air_max_c       REAL,
    air_min_c       REAL,
    sea_c           REAL,                            -- NULL when no sea / no marine data
    rain_days       REAL,
    sunny_days      REAL,
    source          TEXT NOT NULL,                   -- 'open-meteo-archive', 'open-meteo-marine'
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (destination_id, month)
);

CREATE TABLE IF NOT EXISTS airports (
    ident           TEXT PRIMARY KEY,                -- OurAirports ident
    iata            TEXT,
    name            TEXT NOT NULL,
    city            TEXT,
    country_code    TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    type            TEXT NOT NULL,                   -- large_airport | medium_airport ...
    source          TEXT NOT NULL,                   -- 'ourairports'
    fetched_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_airports_iata ON airports(iata) WHERE iata IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_airports_country ON airports(country_code);

CREATE TABLE IF NOT EXISTS routes (
    origin_iata     TEXT NOT NULL,
    dest_iata       TEXT NOT NULL,
    stops           INTEGER NOT NULL DEFAULT 0,
    carriers        TEXT NOT NULL DEFAULT '[]',      -- JSON array of airline codes
    source          TEXT NOT NULL,                   -- 'openflights'
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (origin_iata, dest_iata, stops)
);
CREATE INDEX IF NOT EXISTS idx_routes_dest ON routes(dest_iata);

CREATE TABLE IF NOT EXISTS districts (
    osm_id          TEXT PRIMARY KEY,                -- hotel OSM id, e.g. 'node/123'
    destination_id  TEXT REFERENCES destinations(id),
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    poi_counts      TEXT NOT NULL DEFAULT '{}',      -- JSON {category: count within 500 m}
    nearest_m       TEXT NOT NULL DEFAULT '{}',      -- JSON {category: distance in metres}
    lively          REAL NOT NULL CHECK (lively BETWEEN 0 AND 1),
    quiet           REAL NOT NULL CHECK (quiet BETWEEN 0 AND 1),
    family          REAL NOT NULL CHECK (family BETWEEN 0 AND 1),
    walkable        REAL NOT NULL CHECK (walkable BETWEEN 0 AND 1),
    source          TEXT NOT NULL,                   -- 'overpass' | 'photon'
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_districts_destination ON districts(destination_id);

-- Notable sights and named districts around a destination (data_build/pois.py): the points a user
-- names («рядом с Айя-Софией», «в районе Султанахмет») or shows on a photo.
CREATE TABLE IF NOT EXISTS pois (
    destination_id  TEXT NOT NULL REFERENCES destinations(id),
    osm_id          TEXT NOT NULL,                   -- 'n123' | 'w123' | 'r123'
    kind            TEXT NOT NULL,                   -- museum | worship | park | monument | ... | district
    name            TEXT NOT NULL,                   -- display name for a Russian user
    name_ru         TEXT,
    name_en         TEXT,
    names           TEXT NOT NULL DEFAULT '[]',      -- JSON array: every OSM name and Wikidata label/alias (ru, en, local)
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    dist_center_m   INTEGER NOT NULL,                -- from the destination centre (destinations.lat/lon)
    wikidata        TEXT,                            -- 'Q12506'
    sitelinks       INTEGER,                         -- Wikidata sitelinks: notability
    rank            INTEGER NOT NULL,                -- 1 = most notable of its group (sights / districts)
    source          TEXT NOT NULL,                   -- 'overpass+wikidata' | 'overpass'
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (destination_id, osm_id)
);
CREATE INDEX IF NOT EXISTS idx_pois_destination ON pois(destination_id, kind, rank);
