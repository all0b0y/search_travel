# Lessons

Decisions of skill content v0 (rule — why). The owner may change any of them; update the prompts and examples together.

1. Address the user as «вы», warm and casual, even when the user writes «ты» — one predictable register is easier for a cheap model than mirroring; switch to «ты» globally in `voice.md` if the owner prefers.
2. `understand` patches only what the user said; segment defaults, dates for a bare month, nights and districts are the engine's assumptions — keeps provenance honest (`source: user` means the user said it).
3. «недорого» sets no budget number — any number would be invented; the engine/Jev reads the word from `history`.
4. «с ребёнком» → `party.children 1` but no adults; «с женой» → `couple` only — the engine fills adults from `segments.json` (Foundation decision 2).
5. «с мамой / с подругой» → segment `other` + 2 adults — `couple` implies a romantic pair (district weights, room types).
6. Pets go to both `party.pets` and `constraints.add pet:<kind>` — the hotel filter reads constraints, the party is shown to the user.
7. Closed constraint vocabulary: pet:dog, pet:cat, accessible, no_night_flights, crib, kitchen, no_hostel — free tokens would never match a filter.
8. Fixed holiday windows: «на майские» = May 1–4, «на новогодние» = Dec 31 – Jan 8, «на выходные» = next Sat–Sun — a table is more predictable than letting the model guess.
9. «первая линия» = `beach_max_m 150` — a fixed mapping, not a guess per message.
10. Photo «туда, где так же» → `image.place` null even when the place is recognised — otherwise the engine (confidence ≥ 0.8) would route to that exact place (decision 3) instead of similar ones.
11. `image.place.name` is the city/resort, not the landmark — the catalog and geocoder resolve cities.
12. Prompt injection → `out_of_scope` with an empty patch, even when the message also has a trip request — one simple rule beats partial compliance for a cheap model.
13. present cards never compute: price changes are «X ₽ вместо Y ₽»; differences only when the input has them (notes, `difference_rub`) — the guard checks that every number is in the input.
14. Stars are written «4 звезды», never ★ — ★ falls into the emoji range of the emoji guard.
15. `present.variants` recommends `recommended_hint`, except when the hint is over `budget_rub.max` and another item fits — the only override, deterministic.
16. `present.destinations` recommends the first item — the engine ranks; the model must not re-rank.
17. `watch_out` comes from a fixed condition table (sea ≤ 22°, ≥ 5 rain days above 0°, frost ≤ −10°, ≥ 5 h flight, no price, most expensive) — free-form warnings drift into invented facts.
18. No «?» in variants/destinations/update/compare messages when the engine sent no questions — the model must not invent questions; questions are rendered as buttons by the engine.
19. Upsell lines name the variant («Если возьмёте «Дешевле»: …») when the candidate is not for the recommended variant — candidates carry no variant field in the card input.
20. The update card reads «dates.shift_days» from `changes` to say «Сдвинул на N дня» — the engine resolves shifts to concrete dates (decision 6), so N must be passed explicitly.
