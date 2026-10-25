# Sealed benchmark

A measure of how the agent copes with requests nobody tuned it for. It is not a test set.

- **Written by an outside model.** Each version is written by a different model family (GPT through Codex) from `BRIEF.md` alone: the public description of what the agent can do, the catalog of places and the check vocabulary. The author never sees the agent's code, prompts, examples or our own cases.
- **Never used for tuning.** Its messages, places and phrasings never go into prompts, examples, cue lists, `evals/cases`, `evals/scenarios`, `evals/holdout` or `tests/`, and pytest never loads it. Whoever changes the agent does not open this folder.
- **Measured on frozen code.** A run records the commit it measured. The first run of a version is the honest number and goes to `RESULTS.md` as is, before any fix.
- **Living.** After we look at a version's failures, it is no longer blind. Its later runs are marked «seen», and the next honest number needs a fresh version written the same way (`v2`, …). A failure found here is fixed for its whole class and covered by our own new cases written in different words, never by copying the benchmark's text.

## Layout

- `vN/BRIEF.md`: exactly what the author model got.
- `vN/source/`: its files as written: `cases.json` and `scenarios/*.json`.
- `vN/cases/`, `vN/scenarios/`: the runner's formats, made by `python -m evals.bench.convert evals/bench/vN`. Fixes to the form only, logged below in the version's README.
- `vN/reports/`: run reports (committed). `vN/runs/`: raw transcripts (not committed).
- `RESULTS.md`: one line per run.

## Running

```bash
.venv/bin/python -m evals.run --level 2 --set bench --bench evals/bench/v1 --model flash --parallel 6
.venv/bin/python -m evals.run --level 3 --set bench --bench evals/bench/v1 --flags stage1 --model flash --parallel 3
```
