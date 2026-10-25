# Benchmark v1

- Written 2026-10-25 by GPT-6.1 (`gpt-6.1-sol`, reasoning high) through Codex in an empty folder holding only `BRIEF.md`; it read nothing else and did not browse (54 252 tokens).
- Content: 14 persona scenarios (`source/scenarios/`), 30 understanding cases (`source/cases.json`: 20 first messages, 10 changes to shown variants).
- Edits before the first run: none. `convert.py` found no problem of form.
- After the first run: `convert.py` sets the runner's `stop_on_paid: false` for scenarios that check an after-purchase action (bench_05, bench_10, bench_11; BRIEF v1 did not have the flag). bench_05 and bench_11, cut at payment, were rerun on the same commit; the agent code did not change in between, so the rerun is still blind.
