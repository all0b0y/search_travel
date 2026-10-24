# Ostrovok mock (`ostrovok_mcp/`)

MCP server over stdio: `python -m ostrovok_mcp` (run from the repo root or with the repo on
`PYTHONPATH`). Tools = `contract.TOOL_NAMES` (19); in-process use: `ostrovok_mcp.service.call(tool, payload)`.

| Env var | Default | Role |
|---|---|---|
| `OSTROVOK_CATALOG_DB` | `data/ostrovok_catalog.sqlite` | hotels, rooms, policies, synthetic insights (read-only) |
| `OSTROVOK_CATALOG_EXTRA_DB` | `runtime/catalog_extra.sqlite` | overlay (lazy cities), unioned when present |
| `OPENDATA_DB` | `data/opendata.sqlite` | destinations, airports, routes (read-only) |
| `OPENDATA_EXTRA_DB` | `runtime/opendata_extra.sqlite` | overlay, unioned when present |
| `OSTROVOK_STATE_DB` | `runtime/ostrovok_state.sqlite` | carts, orders, live insights (created from `data/schema/ostrovok_state.sql`) |
| `OSTROVOK_OFFER_KEY` | fixed mock key | HMAC key of offer-id checksums |

Defaults are relative to the repository root, not the working directory.

Modules and where each formula is documented (module docstrings):
- `geo.py` — time-zone priority (destination IANA tz → RU airport table → country table → RU
  longitude bands → lon/15), known origin cities, Moscow terminal by direction.
- `pricing.py` — season curves (beach / tropical / ski / city + RU holidays), lead-time tables.
- `flights.py` — routing (direct, or 1 stop via hub; domestic trips connect only at home),
  schedules, fare formula, tiers basic / standard / flex.
- `trains.py` — RU only, ≤ 1000 km, fast/regular trains, class tariffs.
- `hotels.py` — room fit, nightly price, meals, availability noise, score.
- `transfers.py` — kinds, speeds, per-country tariffs, meeting points.
- `quotes.py` — date choice for `month`, flight + mid-range stay estimate.
- `orders.py` — cart rules (same-slot replacement), compatibility issues, traveller checks,
  booking numbers, early check-in, date changes, transfers after purchase.
- `insights.py` — k ≥ 20 aggregation, personal-data guard.
- `offer_ids.py` — `<fl|rw|ho|tf>1.<base64url JSON params>.<hmac12>`.

Determinism: every random draw is `sha256(entity|field|date|…)` (`rng.py`); `today` / `now` come
from the payload; the wall clock is never read. Everything priced or scheduled is synthetic
(`synthetic: true`); names, coordinates, airports and routes are real data from the DBs.
