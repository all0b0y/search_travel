# Benchmark v2

- Written 2026-10-25 by GPT-6.1 (`gpt-6.1-sol`, reasoning high) through Codex in an empty folder holding only `BRIEF.md` and `routes.json`; it read nothing else and did not browse (58 812 tokens).
- `routes.json`: for each home city and catalog place, flights (direct / connecting / none) and trains, computed from the mock after long-distance trains were added. The brief asks for transport requirements consistent with it and for at least two scenarios whose first transport wish does not exist.
- Content: 20 persona scenarios (`source/scenarios/`), 40 understanding cases (`source/cases.json`: 26 first messages, 14 changes to shown variants).
- Edits before the first run: none. `convert.py` found no problem of form and no transport check that contradicts `routes.json`.
