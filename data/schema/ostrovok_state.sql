-- runtime/ostrovok_state.sqlite: the mock's runtime writes (gitignored; never the committed catalog).
-- Carts, orders and live anonymous insights events. Times are ISO 8601 with offset taken from
-- the caller's session clock (`now`), never from the wall clock.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS carts (
    cart_id         TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'checked_out')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- one open cart per session
CREATE UNIQUE INDEX IF NOT EXISTS idx_carts_open_session ON carts(session_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS cart_items (
    item_id         TEXT PRIMARY KEY,
    cart_id         TEXT NOT NULL REFERENCES carts(cart_id) ON DELETE CASCADE,
    offer_id        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('flight', 'train', 'hotel', 'transfer')),
    title           TEXT NOT NULL,
    price_rub       INTEGER NOT NULL CHECK (price_rub >= 0),
    start_at        TEXT,
    end_at          TEXT,
    details         TEXT NOT NULL DEFAULT '{}',      -- JSON snapshot of the offer
    added_at        TEXT NOT NULL,
    UNIQUE (cart_id, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_cart_items_cart ON cart_items(cart_id);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    cart_id         TEXT NOT NULL REFERENCES carts(cart_id),
    session_id      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending_payment', 'paid', 'changed', 'cancelled')),
    total_rub       INTEGER NOT NULL CHECK (total_rub >= 0),
    contact         TEXT NOT NULL DEFAULT '{}',      -- JSON {email?, phone?}
    issues          TEXT NOT NULL DEFAULT '[]',      -- JSON array of contract Issue
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id);

CREATE TABLE IF NOT EXISTS order_items (
    item_id         TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    offer_id        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('flight', 'train', 'hotel', 'transfer')),
    title           TEXT NOT NULL,
    price_rub       INTEGER NOT NULL CHECK (price_rub >= 0),
    booking_number  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'on_request', 'changed', 'cancelled')),
    start_at        TEXT,
    end_at          TEXT,
    details         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS order_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    data            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, at);

-- personal data lives only here (runtime, gitignored), never in insights tables
CREATE TABLE IF NOT EXISTS travellers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    name            TEXT NOT NULL,
    birth_date      TEXT NOT NULL,                   -- YYYY-MM-DD
    UNIQUE (order_id, position)
);

-- anonymous events from insights_log_event (no personal data, no session/order ids)
CREATE TABLE IF NOT EXISTS insights_events_live (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    stage           TEXT,
    segment         TEXT CHECK (segment IN ('solo', 'couple', 'family', 'friends', 'other')),
    origin_city     TEXT,
    destination_id  TEXT,
    month           INTEGER CHECK (month BETWEEN 1 AND 12),
    data            TEXT NOT NULL DEFAULT '{}',
    source          TEXT NOT NULL DEFAULT 'live' CHECK (source = 'live'),
    fetched_at      TEXT NOT NULL                    -- when the event was recorded
);
CREATE INDEX IF NOT EXISTS idx_insights_live_kind ON insights_events_live(kind, at);
