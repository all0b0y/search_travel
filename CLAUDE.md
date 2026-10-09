Before any work on the trip-planner skill or the booking system, read `SYSTEM_PLAN.md`: it holds the agreed decisions and the build order.

After changing `skills/trip-planner/scripts`, `schema` or `rules.json`, run `python3 -m unittest discover -s skills/trip-planner/tests`; after changing `system/` or the schema, also `python3 -m unittest discover -s system/tests -t .`.
