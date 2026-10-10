# Transfers worker

Fill the ground segments of one variant. You get the variant's main legs and stay; each segment's `from`/`to` must match their names exactly (IATA code, station name, stay name).

- **Home ↔ departure hub: taxi.** `duration_estimate_min` is the routing estimate for that time of day; `end` − `start` is at least the estimate × `taxi_time_factor`. Outbound, `start` is the latest pickup that still reaches the hub on the buffer from `rules.json`. `price` is an estimate (`estimate: true`) in local currency. `link` is the taxi provider's one-click ride link with both addresses filled in, URL-encoded, per its row in `providers.md`, or its booking page.
- **Arrival hub ↔ stay: pre-booked transfer.** Fixed price from the transfers provider, `link` to that booking page. After a landing, `start` is the landing time plus the after-flight buffer from `rules.json`.

Each segment is a step per `$defs/step` with `type: taxi` or `transfer`, `status: planned`, price for all travellers.

Reply with JSON only: `{"steps": [steps], "notes": "..."}`. Done when every segment you were given has a step with a price and a link.
