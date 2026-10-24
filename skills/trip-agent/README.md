# trip-agent skill package

Data only: the engine (`agent/`) loads it with `agent.skill_loader.load_skill("skills/trip-agent")`
at startup and never rewrites it while running. Contract: `AGENT_PLAN.md`, section 4.

- `turns.json` — `cards`: per card the prompt file, output schema, `thinking` (`off|low|high`),
  `max_tokens`, `voice` (append `voice.md`). `routes`: matched top-down; the first route whose
  `when` holds picks the `steps` (names from `agent/steps_spec.py:STEP_NAMES`) and the `present`
  card (`null` = the engine answers deterministically, no model call).
- `when` keys (all given keys must hold; `{}` matches anything):
  `action` — UI action types (route matches only action turns);
  `intent` — understand intents (route matches only text turns);
  `stage` — session stages; `has` / `missing` — brief paths with / without a value
  (`destination`, `dates.start`, ...), evaluated after the turn's patch is applied.
- `understand.md`, `present/*.md` — card instructions (English). `voice.md` — tone guide (Russian examples).
  The system prompt of a card = prompt + `voice.md` (when `voice: true`) + the output schema.
- `schema/` — draft-07 JSON Schemas of card outputs. The patch path enum in
  `understand.schema.json` equals `agent.state.ALLOWED_PATCH_PATHS` (tested).
- `questions.json` — askable fields with Russian texts, choices and the recommendation rule.
- `segments.json` — segment defaults: party counts, prefs, district weights, Jev criteria.
- `upsell.json` — add-on rules: conditions on brief/variant, kind, reason template.
- `rules.json` — limits (questions, upsell, jokes), sensitivity and Jev thresholds,
  connection buffers (copied from trip-planner), retry limits.
- `lessons.md` — lessons from the improvement loop (rule + why).

Any loading problem raises `SkillLoadError` naming the file; run
`.venv/bin/python -m pytest tests -q` after editing anything here.
