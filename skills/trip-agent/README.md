# trip-agent skill package

Data only: the engine (`agent/`) loads it with `agent.skill_loader.load_skill("skills/trip-agent")`
at startup and never rewrites it while running. Contract: `AGENT_PLAN.md`, section 4 and "Card inputs".

- `turns.json` — `cards`: per card the prompt file, output schema, `thinking` (`off|low|high`),
  `max_tokens`, `voice` (append `voice.md`). `routes`: matched top-down; the first route whose
  `when` holds picks the `steps` (names from `agent/steps_spec.py:STEP_NAMES`) and the `present`
  card (`null` = the engine answers deterministically, no model call).
- `when` keys (all given keys must hold; `{}` matches anything):
  `action` — UI action types (route matches only action turns);
  `intent` — understand intents (route matches only text turns);
  `stage` — session stages; `has` / `missing` — brief paths with / without a value
  (`destination`, `dates.start`, ...), evaluated after the turn's patch is applied.
- `understand.md`, `present/*.md` — card instructions (English, for DeepSeek V4.1 Flash: short
  imperative rules, decision tables, compact few-shot). `voice.md` — tone guide (Russian do/don't pairs).
  The system prompt of a card = prompt + `voice.md` (when `voice: true`) + the output schema.
- `schema/` — draft-07 JSON Schemas of card outputs. The patch path enum in
  `understand.schema.json` equals `agent.state.ALLOWED_PATCH_PATHS` (tested).
- `guide.md` + `schema/guide.schema.json` — the `guide` card of guide mode (flag `guide`, `agent/guide.py`):
  not in `turns.json`; thinking / temperature / max_tokens and the loop's limits come from `rules.json` `guide`.
- `questions.json` — askable fields: Russian question text, `hint` (when to ask), choices with
  `chip` labels (text of the chip when the engine assumes the value), recommendation rule.
  `place_confirm` — the photo confirmation question (`{place}` template) for
  `rules.image.confirm_min_conf ≤ confidence < rules.image.place_min_conf`.
- `segments.json` — segment defaults: party counts, prefs, district weights, `jev_criteria`
  (segment routing), `jev_district` (district inference prior); `district` — chip labels and Jev
  choice texts of the four OSM district labels.
- `upsell.json` — add-on rules (kinds = what the mock sells: transfer, early_checkin, room_upgrade,
  meal_upgrade, flexible_fare). `rules_pending` holds the room_upgrade / meal_upgrade / flexible_fare
  rules until `agent/skill_loader.py` accepts those kinds; then move them into `rules`.
  When several rules of one kind hit one variant, the first one in file order wins.
- `rules.json` — limits (questions, upsell, jokes), sensitivity and Jev thresholds, `image`
  thresholds, `guard` (emoji, exclamations, message length per card, banned phrases, cards where a
  «?» needs engine questions), connection buffers (copied from trip-planner), retry limits.
- `examples/<card>.<n>.input.json` / `.output.json` — card inputs exactly per "Card inputs" and
  reference outputs (`understand.<n>.image.txt` describes the photo of image cases). They are the
  few-shot sources and seed eval cases; all outputs pass the number/id/voice checks.
- `lessons.md` — decisions and lessons from the improvement loop (rule + why).

Any loading problem raises `SkillLoadError` naming the file. Content checks:
`.venv/bin/python -m pytest tests/skill -q`; after editing anything here run
`.venv/bin/python -m pytest tests -q`.
