# data_build — offline open data + mock catalog

Builds `data/opendata.sqlite` and `data/ostrovok_catalog.sqlite` (schemas: `data/schema/*.sql`).
Network is used here only. Raw downloads are cached in `data_build/cache/` (gitignored).

```bash
.venv/bin/python -m data_build.build_all                       # everything
.venv/bin/python -m data_build.build_all --stage hotels,districts,synthetic --resume   # finish the Overpass sweep
.venv/bin/python -m data_build.build_all --stage climate --resume --wait-rate-limit    # finish climate (Open-Meteo quota)
.venv/bin/python -m data_build.build_all --stage hotels,districts,synthetic --no-network  # rebuild from cache only
.venv/bin/python -m data_build.build_all --stage insights      # regenerate the synthetic event log
.venv/bin/python -m data_build.report                          # coverage -> data_build/REPORT.md
```
Options: `--only id1,id2`, `--limit N` (destinations processed), `--resume` (skip destinations marked
done), `--refresh` (re-download airports/routes). Interrupting is safe: each destination commits atomically
and is marked done (`hotels_done:<id>` in catalog meta, `climate_done:<id>` in opendata meta) only after
its rows are written; cached responses are never refetched.

## Stages and sources

| stage | source | notes |
|---|---|---|
| airports | OurAirports `airports.csv` | large/medium with IATA; `source='ourairports'` |
| routes | OpenFlights `routes.dat` | **dated 2014-06**; aggregated by (origin, dest, stops) with carriers; distance not stored (compute from airports) |
| destinations | curated list `destinations.py` + Nominatim coords | 159 places; tags from `agent.state.DESTINATION_TRAITS`; popularity = curated 1..100 stored /100; aliases curated (`ALIASES`); timezone from `timezones.py` |
| climate | Open-Meteo archive 2015–2024 (daily) + marine SST 2023–2025 | rain day ≥ 1 mm, sunny day ≥ 6 h sunshine; SST only for coastal places, nearest marine grid point with data |
| hotels | one batched Overpass query per destination (8 km box; 6 km for small places, 10 km for spread-out resorts) | up to 60 named objects, deterministic diverse selection (`hotels.select_hotels`); Photon fallback when Overpass stays down |
| districts | same Overpass response | counts in 500 m, nearest distances, scores — formulas in `districts.py` docstring |
| synthetic | `synthetic.py`, `prices.py` | sha256(osm_id \| field); stars kept from OSM when tagged |
| insights | `insights.py` | 24 000 events, every origin × segment × month group ≥ 24 |

Airports of a destination (`destinations.nearest_airports`): scheduled airports within 75 km, plus the
nearest scheduled large airport within 150 km when none of them is large; domestic first; if nothing,
the nearest scheduled domestic airport within 150 km; airports in contested regions (GE-AB, UA-43, UA-40)
are never used. Operating status follows OurAirports' `scheduled_service` (one curated override: RGK).

## Catalog meta keys (read by the mock)

- `hotel_extras:<hotel_id>` → `{pets_fee_rub (per night; 0 = free; null = pets not allowed), child_free_under_age, meal_multipliers, source:"synthetic"}` — fields that have no schema column.
- `synthetic.season_factors:<destination_id>` → `{profile, factors: [12 floats Jan..Dec], source}`; nightly price = `rooms.base_price_rub × factors[month-1] × meal_multipliers[meal]`.
- `synthetic.meal_multipliers` → `{none, breakfast, half_board, full_board, all_inclusive}`.
- `hotels_done:<destination_id>` → build info (candidates, source overpass|photon, skipped duplicates).
- insights: `origin_city` is the Russian city name (`Москва`, `Санкт-Петербург`, `Казань`, `Екатеринбург`, `Новосибирск`, `Краснодар`, `Нижний Новгород`).

## Measured network facts (2026-10-25)

- Overpass: ~30–50 s per destination query; frequent 502 on `overpass-api.de`, the rotation to `lz4`/`z` mirrors recovers.
- Open-Meteo free API: a 10-year daily archive request with 4 variables is weighted ~100 calls; the hourly
  quota (5 000) stops the climate stage after ~48 destinations, the daily quota (10 000) after ~96.
  `--wait-rate-limit` sleeps to the next window.
- Nominatim: 1 req/s with the project User-Agent; its ranking needs help (`pick_nominatim`: exact name
  match first, provinces and natural features last).

## Lazy city

`data_build.lazy.build_city(query, *, timeout_s=60, http=None, ...) -> str | None` — one Nominatim
search + one Overpass attempt (+ one Open-Meteo archive call in parallel); writes only
`runtime/opendata_extra.sqlite` and `runtime/catalog_extra.sqlite`; returns an existing id when the
name or a place within 10 km is already known; returns None and writes nothing on failure.
