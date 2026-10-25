# Benchmark v3

- Written 2026-10-25 by GPT-6.1 (`gpt-6.1-sol`, reasoning high) through Codex in an empty folder holding only `BRIEF.md` and `routes.json` (the same table as v2); it read nothing else and did not browse (53 892 tokens).
- Brief: the v2 brief plus scenarios aimed at a travel guide: 4+ where the person does not know where or when and relies on advice (real priorities hidden until asked), 3+ where they push for a cheaper trip once or twice (accepted and refused trade-offs hidden), 2+ where the first result does not suit them for a reason they reveal only after seeing it.
- Content: 20 persona scenarios (3 continue after payment), 40 understanding cases (26 first messages, 14 changes).
- Purpose: the A/B measurement of decision 26 — the pipeline (main) and guide mode (branch `guide`) are each run once on it, on frozen code; nobody reads its failures before both runs are done.
- Edits before the first run: none. `convert.py` found no problem of form and no transport check that contradicts `routes.json`.
