Read `AGENTS.md` first: team workflow (issues, labels, branches, PRs, review), repository map and product invariants for every agent working here.

Before any work on the agent (`skills/trip-agent/`, `agent/`, `ostrovok_mcp/`, `data_build/`, `evals/`, `web_debug/`), read `AGENT_PLAN.md` (agreed decisions and the technical contract), `docs/ARCHITECTURE.md` and the workstream of your issue in `docs/IMPLEMENTATION_PLAN.md`. After changing any of them, run `.venv/bin/python -m pytest tests -q`.

Never open `evals/bench/**` when you change the agent (sealed benchmark, see `AGENTS.md` §7).
