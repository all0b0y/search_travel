Before any work on the track agent (`skills/trip-agent/`, `agent/`, `ostrovok_mcp/`, `data_build/`, `evals/`, `web_debug/`), read `AGENT_PLAN.md`: it holds the agreed decisions and the technical contract. After changing any of them, run `.venv/bin/python -m pytest tests -q`.

Before any work on the trip-planner skill or the booking system, read `SYSTEM_PLAN.md`: it holds the agreed decisions and the build order.

After changing `skills/trip-planner/scripts`, `schema` or `rules.json`, run `python3 -m unittest discover -s skills/trip-planner/tests`; after changing `system/` or the schema, also `python3 -m unittest discover -s system/tests -t .`.
