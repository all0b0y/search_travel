# План: агент трека «О!дин запрос»

Решения приняты в интервью 2026-10-24. Решение, которое нужно поменять, сначала согласуй с владельцем репозитория. `SYSTEM_PLAN.md` и `skills/trip-planner/` — прежний проект «от двери до двери»; они остаются как есть, как источник идей, и этим планом не меняются.

## Цель

Скилл, с которым дешёвая модель **DeepSeek V4.1 Flash** стабильно и предсказуемо проводит пользователя от одного сообщения до имитированной покупки поездки (трек Островка «О!дин запрос»: вдохновение → выбор направления → подбор → сравнение → сборка → покупка → после покупки).

Главная метрика: **минимум касаний пользователя без потери качества подбора**. Дешёвые вызовы модели — следствие, не цель.

## Решения

| # | Решение |
|---|---|
| 1 | Факты — только через типизированные инструменты. Модель не ходит в веб. |
| 2 | MCP «Островок-мок»: каталог, наличие, корзина, заказ, действия после покупки и `insights_*` (обезличенные паттерны; агрегаты только по группам k ≥ 20). Открытые данные — отдельные инструменты агента. |
| 3 | Свой тонкий движок на Python: машина фаз в коде, модель работает внутри фаз. Готовые обвязки (bb, DeepSeek Harness, Harness²) не используем. |
| 4 | Скилл — пакет данных: `understand.md`, `present/*`, `voice.md`, `turns.json`, `upsell.json`, `segments.json`, `questions.json`, `rules.json`, `schema/`, `lessons.md`. Скиллы персоны и судьи — отдельно, в `evals/skills/`. |
| 5 | Модель без памяти между вызовами; состояние держит код. `understand` возвращает правку брифа, пересборку ведёт код. Инструменты вызывает код. Не больше 2 вызовов модели на ход; для странных запросов — карточка `fallback`. |
| 6 | «Показать и спросить»: первый ответ почти всегда с результатом; допущения — чипы без указания источника (источник хранится внутри: `user / profile / pattern / default / inferred`). Вопрос — только если ответ меняет выдачу (проверка чувствительности), не больше 2 за ответ, кнопками с рекомендованным ответом. |
| 7 | Входы A–F (ниже). Блокирующих полей почти нет. Город вылета и данные о человеке — из профиля; состав — одно нажатие (`один · вдвоём · с детьми · компанией`); возраст гостей приходит на оформлении, после чего хук проверяет корзину до оплаты. |
| 8 | Сначала пользователь. Выгода Островка — цельная поездка и уместные допы с причиной из брифа (не больше 2 в ответе); выгода отеля — честное «кому не подойдёт». Веса маржи в ранжировании нет. |
| 9 | Профиль района отеля по OSM: «движ», «спокойно», «семейно», «пешком». Сегмент задаёт веса по умолчанию (чип). |
| 10 | Тон лёгкий, шутливый, дружелюбный. Не больше одной шутки в ответе; деньги, риски и условия отмены — прямо, без шуток. |
| 11 | Хуки: проверочные (код, блокируют показ, не больше одного перезапуска этапа), опережающие (фоновый поиск под вероятный следующий шаг), аналитические (события в журнал `insights`). Скилл во время работы не переписывается. |
| 12 | Jev (`typesafe/jev-1.13`): маршрутизация входа и коротких ответов, «спросить или допустить» (порог из замеров), смысловой хук на выходе `present`. Недоступен или не уверен — правила или Flash. |
| 13 | Данные: ~150 направлений; настоящие отели и районы из OSM, климат Open-Meteo, аэропорты OurAirports, маршруты OpenFlights; цены, наличие и расписание — синтетика, детерминированная. Всё собрано офлайн в SQLite в репозитории. Город вне каталога — одна попытка через Overpass, иначе честное «нет в демо» и похожие направления. |
| 14 | Фото: «хочу сюда» (узнать место, проверить геокодером) и «туда, где так же» (признаки картинки → `find_destinations`). |
| 15 | Тесты в три уровня: unit без модели; ~40 фазовых кейсов × 5 прогонов; 25 сквозных сценариев + 10 отложенных. Персона — Sonnet 5.5 через Claude Code (подписка, не API); судья — Opus (субагенты со скиллом судьи, спорное проверяет оркестратор). |
| 16 | Пороги: фазовые кейсы 5/5; первая рекомендация в первом ответе ≥ 80 %; касаний до готовой корзины ≤ 4; скрытые требования персон ≥ 90 %; ошибки стыковок 0; отложенные сценарии хуже основных не больше чем на 10 п. п. Финиш — три зелёных прогона подряд. |
| 17 | Инструкции карточек — на английском, всё, что читает пользователь, — на русском (`voice.md` и примеры — на русском). Режим размышления — на карточку, подбирается замерами. |
| 18 | trip-planner не трогаем; его проверки копируются в `agent/checks.py` вместе с тестами. README переписываем в конце. |
| 19 | Интерфейс делает отдельный человек. Отсюда — договор ответа с фикстурами и отладочный макет. |
| 20 | Строим всё сразу параллельными субагентами (Opus 5.5 high), включаем флагами ступенями, каждая ступень — прогон evals. |
| 21 | Целевая модель — `deepseek/deepseek-v4.1-flash-1017` через OpenRouter, провайдер закреплён (DeepSeek, `allow_fallbacks: false`); фактические модель и провайдер пишутся в каждом прогоне. Запасной провайдер — только явно и с записью в лог. |
| 22 | Финальная проверка на сильной модели — `gpt-6-luna` effort `high` через Codex (подписка) в изолированном профиле `.codex-profile/`. |

### Входы и этапы

| Вход | Пример | Стартовый этап |
|---|---|---|
| A. Не знаю куда | «к тёплому морю в ноябре с ребёнком, недорого» | вдохновение → направления |
| A′. Фото | пейзаж / «хочу сюда» | вдохновение (место узнано → B) |
| B. Знаю куда, не знаю когда/как | «хочу в Стамбул на майские» | подбор |
| C. Знаю всё | «Сочи, 12–19 ноября, двое взрослых» | подбор → сразу 3 варианта |
| D. Только часть | «отель в Казани на 3 ночи» | подбор только этой части; добавить остальное — одной кнопкой |
| E. Пересборка | «а если на 2 дня позже», «с собакой» | этап, которого касается изменение |
| F. После покупки | «можно заселиться пораньше?» | после покупки |

Общий путь: вдохновение → подбор (перелёты/ЖД, отели, трансферы → 3 варианта: дешевле / быстрее / комфортнее) со встроенным сравнением и объяснением → сборка с проверкой стыковок → корзина → покупка → после покупки.

### Ступени включения (решение 20)

1. База: движок + Flash + проверочные хуки.
2. + проверка чувствительности и чипы.
3. + Jev.
4. + опережающие хуки.
5. + допы и паттерны `insights`.

Ступень остаётся, если улучшает свою метрику и не ухудшает остальные. Дальше — цикл улучшения скилла до порогов (решение 16), уроки — в `skills/trip-agent/lessons.md`.

---

# Technical contract

Everything below is binding for every builder. A change to a shared contract (state model, card schemas, response contract, tool contract, DB schema) goes through the orchestrator, never silently.

## 1. Layout and ownership

```
skills/trip-agent/      skill package (data only, loaded by the engine)
agent/                  engine: state, turn pipeline, steps, ranking, hooks, backends, HTTP API
  contracts/            response.schema.json (UI contract), fixtures/ (example responses)
ostrovok_mcp/           MCP server "Ostrovok mock": catalog queries, generators, cart, orders, insights
data_build/             offline builders of data/*.sqlite (network allowed here only)
data/                   committed read-only databases + schema SQL
web_debug/              debug mockup page (served by agent/server.py at /debug); not the product UI
evals/                  cases/, scenarios/, holdout/, skills/{persona,judge}/, runner, metrics, reports/
tests/                  unit tests (no network, no model calls): tests/agent, tests/ostrovok_mcp, tests/data_build
runtime/                gitignored: sessions, profiles, carts/orders, caches
```

Python 3.11, venv in `.venv` (`uv venv --python 3.11 .venv && uv pip install -r requirements.txt`). Dependencies: `openai` (OpenRouter client), `mcp` (server and client), `pydantic` v2, `jsonschema`, `httpx`, `pytest`. Nothing else without the orchestrator. HTTP server: stdlib `http.server` (as in `system/server.py`), no web framework.

## 2. Models

All model calls go through `agent/llm.py` (`Backend` protocol) or `agent/jev.py`. The only key is `OPENROUTER_API_KEY` (DeepSeek and Jev, plus the optional portability check), loaded from `.env` by `agent/env.py`; never printed, never logged, never written to files other than `.env`. Sonnet and GPT run on the user's subscriptions through their CLIs, not through an API key.

| Use | Model | Access |
|---|---|---|
| Agent (understand / present) | `deepseek/deepseek-v4.1-flash-1017` | OpenRouter chat completions, `provider: {"order": ["DeepSeek"], "allow_fallbacks": false}`, `response_format: {"type": "json_object"}`, thinking via `reasoning` (`{"enabled": false}` for off, `{"effort": "low"|"high"}` otherwise) |
| Jev decisions | `typesafe/jev-1.13` | `POST https://openrouter.ai/api/alpha/decisions` `{model, state, questions}` → `answers` (noul / choice+probabilities+confidence / score) |
| Eval persona | Sonnet 5.5 (`--model claude-sonnet-5-5`) | Claude Code CLI on the Max subscription: `claude -p --model claude-sonnet-5-5 --system-prompt-file <persona prompt> --tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources "" --no-session-persistence --output-format json --json-schema <reply schema>`, run with a working directory outside the repository (so no project `CLAUDE.md` is picked up). Never `--bare` (it disables subscription auth). |
| Portability check | `qwen/qwen3.5-flash-02-23` | OpenRouter chat completions |
| Strong-model check | `gpt-6-luna`, effort `high` | `codex app-server` (stdio JSON-RPC) under `CODEX_HOME=.codex-profile/codex`, `HOME=.codex-profile/home`; ephemeral threads, `environments: []`, `web_search: "disabled"`, `outputSchema` per card |

`Backend.complete(req: LLMRequest) -> LLMResult`:
- `LLMRequest`: `card`, `system` (prompt text), `user` (JSON payload as text), `images` (list of data URLs), `schema` (JSON schema of the card's output), `thinking` (`off|low|high`), `max_tokens`.
- `LLMResult`: `data` (parsed and schema-valid dict), `raw_text`, `model` (served model id from the response), `provider` (served provider), `usage` (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd` if reported), `latency_ms`, `retries`.
- Invalid JSON or schema violation → exactly one retry with the validation error appended; still invalid → `LLMError` (the engine then degrades honestly, never invents).
- A fake backend (`FakeBackend`, scripted responses keyed by card) is the only backend used in unit tests.

`agent/jev.py`: `decide(state: dict, questions: dict, timeout_s: float = 1.5) -> dict | None`. Any error, timeout or missing key returns `None` (fail-open); callers fall back to rules or Flash. Every call is recorded in the turn trace with latency and cost.

Codex and Claude Code: builders use fakes only (a fake app-server, a fake `claude` script). Only the orchestrator runs the real `codex` and `claude` binaries — `codex` only under the project's isolated profile, `claude` only with the persona flags above and never with a different `HOME`.

Measured 2026-10-25 (live, OpenRouter, provider DeepSeek; the account allows providers that may train on inputs — without that the DeepSeek provider is filtered out with 404 «Paid model training violation»):
- Text `understand`-like call (100 input tokens): thinking off 0.7–1.3 s, 0 reasoning tokens; `low` 1.3 s; `high` 1.8 s.
- Rich present cards on level-2 cases (13 cases × 5 runs, 2026-10-25): `present.variants/compare/update` with thinking `low` → 11–12/13 cases fully passing, per-case p50 ≈ 6.3 s (400–1100 reasoning tokens); with `off` → 12/13, p50 ≈ 2.9 s, cost ÷2. Decision: all present cards run with thinking off; the code guards catch what thinking was meant to prevent.
- Image + text (≈ 515 input tokens): thinking off 1.6–1.8 s, ≈ $0.00013; `low` 2.6–5.4 s with up to 750 reasoning tokens. A famous landmark (Hagia Sophia) → confidence 0.99; a landscape (Big Sur coast) → right place at 0.55 off / 0.85 low, traits correct. Free-text trait lists come back unless the schema enforces enums.
- Jev decision with 2 questions: 347 ms, $0.00002; answers matched expectations (family + «недорого» → district `quiet` 0.98; «budget stated precisely» 0.04).

## 3. State

`agent/state.py`, pydantic v2. One `Session` per conversation, persisted as JSON in `runtime/sessions.sqlite`.

```
Field[T] = { value: T | null, source: "user"|"profile"|"pattern"|"default"|"inferred", confidence: float | null }

Brief:
  origin:        Field[{ city: str, iata: [str] }]
  destination:   Field[{ id: str|null, name: str, country: str|null }]
  traits:        Field[[DestinationTrait]]          # wishes for "where": sea, mountains, warm, city, nature, ...
  dates:         Field[{ start: date|null, end: date|null, month: int|null, window: {from: date, to: date}|null,
                         nights: int|null, flexible_days: int }]
  party:         Field[{ segment: "solo"|"couple"|"family"|"friends"|"other", adults: int, children: int,
                         children_ages: [int]|null, pets: [str] }]
  budget_rub:    Field[{ max: int|null }]
  scope:         Field[["flights"|"trains"|"stay"|"transfers"]]
  prefs:         Field[{ meals: "any"|"none"|"breakfast"|"half_board"|"full_board"|"all_inclusive",
                         free_cancellation: bool|null, stars_min: int|null, direct_only: bool|null,
                         max_travel_hours: float|null, district: "any"|"lively"|"quiet"|"family"|"walkable",
                         beach_max_m: int|null, transport: "any"|"plane"|"train",
                         arrive_before: "HH:MM"|null,   # outbound arrival, local clock at the destination
                         no_night_flights: bool|null,   # no flight leaving/landing in ranking.night_window
                         domestic: bool|null }]         # inspiration: destinations in the origin's country
                 # ranking: user-set arrive_before / no_night_flights / free_cancellation (refundable
                 # fares) filter tickets, relaxed per direction with risk relaxed_pref / non_refundable_only;
                 # a scope without "stay" builds tickets-only variants (no hotel, no transfers)
  constraints:   Field[[str]]   # hard constraints, enum-like tokens: "pet:dog", "accessible", "no_night_flights", ...
  mood:          Field[[str]]   # "relax", "party", "culture", "active", "romantic", "kids_fun"

Session:
  id, created_at, now (injectable clock; evals pin it), profile_id
  brief: Brief
  stage: "inspiration"|"selection"|"cart"|"checkout"|"ordered"|"aftercare"
  shown: { destinations: [ShownItem], variants: [ShownItem], hotels: [ShownItem] }   # short labels "1","2","3" for references
  results: cached search results by key (search params hash) with fetched_at
  variants: [Variant]           # built by code
  chosen_variant_id, cart_id, order_id
  pending_questions: [Question]
  history: last 6 user messages verbatim + last 3 agent messages (text only)
  trace: per-turn records (calls, hooks, timings), capped
```

`Variant` (built by code): `id` (`cheapest|fastest|comfort` or `custom-N`), `steps` (flight/train/transfer/stay steps compatible with `agent/checks.py`), `totals` (`rub`, `door_to_door_hours`), `facts` (computed numbers the card may cite), `sacrifices` (computed diffs vs the other variants), `risks` (computed: connection slack, night arrival, non-refundable, long transfer...), `not_for` (computed from district scores and hotel policies), `fit` (0..1 with per-criterion breakdown), `upsell_candidates` (rule hits from `upsell.json`, each with its reason).

The model never produces prices, times or totals. It only selects ids and writes text; every number it writes must exist in its input (checked by a hook).

## 4. Skill package format

```
skills/trip-agent/
  README.md            how the engine loads the package (short)
  understand.md        instructions of the understand card (English)
  present/destinations.md, variants.md, compare.md, update.md, info.md, aftercare.md, fallback.md
  voice.md             tone guide with Russian do/don't examples; appended to every present card
  turns.json           cards (prompt file, schema, thinking, max_tokens) + routes (see below)
  questions.json       askable fields: question text (Russian), choices, recommended choice rule
  segments.json        segment -> default prefs/district weights + Jev criteria
  upsell.json          add-on rules: condition on brief/variant -> add-on kind + reason template
  rules.json           thresholds: sensitivity, Jev thresholds, buffers, limits (max questions, max jokes, max upsells)
  schema/understand.schema.json, schema/present.<card>.schema.json
  lessons.md           lessons from the improvement loop (rule + why)
```

`turns.json`:
```json
{
  "cards": {
    "understand":            {"prompt": "understand.md", "schema": "schema/understand.schema.json", "thinking": "off", "max_tokens": 900},
    "present.destinations":  {"prompt": "present/destinations.md", "schema": "schema/present.destinations.schema.json", "thinking": "off", "max_tokens": 1400, "voice": true},
    "present.variants":      {"prompt": "present/variants.md", "schema": "schema/present.variants.schema.json", "thinking": "off", "max_tokens": 2000, "voice": true}
  },
  "routes": [
    {"when": {"intent": ["new_trip", "refine", "answer"], "has": ["destination"]}, "steps": ["apply_patch", "search", "build_variants", "check", "sensitivity", "upsell"], "present": "present.variants"},
    {"when": {"intent": ["new_trip", "refine", "answer"], "missing": ["destination"]}, "steps": ["apply_patch", "find_destinations", "quote_destinations", "sensitivity"], "present": "present.destinations"}
  ]
}
```
Routes are matched top-down; `steps` are names registered in the engine's step registry (`agent/steps.py`); unknown step names fail at load time. The engine owns transitions; the package owns prompts, schemas, texts and thresholds.

### Card I/O

`understand` input: `{brief (values only), stage, shown (labels → titles), pending_questions, history, message, has_image}` + image if any. Output (`schema/understand.schema.json`):
```json
{
  "intent": "new_trip|refine|answer|choose|compare|info|cart|checkout|post_purchase|smalltalk|out_of_scope",
  "patch": {"set": {"<allowed path>": "<value>"}, "unset": ["<allowed path>"]},
  "refs": [{"kind": "destination|variant|hotel|question", "ref": "1"}],
  "image": null,
  "info_question": null,
  "post_purchase": null
}
```
Allowed patch paths are an enum in the schema (`destination.name`, `dates.start`, `dates.month`, `dates.nights`, `dates.shift_days`, `party.segment`, `party.adults`, `party.children`, `party.children_ages`, `party.pets`, `budget_rub.max`, `prefs.*`, `constraints.add`, `constraints.remove`, `traits.add`, `mood.add`, `origin.city`, `scope`). Patched fields get `source: "user"`. `image`: `{"place": {"name", "country", "confidence"} | null, "traits": {"landscape": [...], "water": "sea|lake|river|none", "climate": "tropical|warm|temperate|cold|snow", "setting": "city|resort|nature|village", "vibe": [...]}}`.

`present.*` input: the brief, the computed items (destinations or variants with all numbers), the questions the engine decided to ask (with their fixed texts), allowed upsell candidates, and `voice.md`. Output: `{"message": "<Russian text>", "items": [{"id", "why", "sacrifices", "risks", "not_for"}], "recommended_id", "upsell": [{"candidate_id", "line"}]}` (exact per-card schemas live in `schema/`). Texts reference items by id; numbers are copied from input.

### Card inputs (binding for the engine, the skill prompts and the eval cases)

Every card gets one JSON object as the user message. Common keys: `today` (ISO date), `stage`, `brief` (values only, `Brief.values()`), `assumed` (brief paths whose source is not `user`, e.g. `["origin.city", "party.segment"]`), `assumptions` (the chips shown with this answer: `[{"field", "label"}]`).

- `understand`: common keys + `shown` (`[{"label": "1", "kind": "destination|variant|hotel", "id", "title"}]`), `pending_questions` (`[{"id", "field", "text", "choices": [label]}]`), `history` (`[{"role": "user|agent", "text"}]`, ≤ 6 user + 3 agent), `message` (the new user text), `has_image` (bool; the image itself goes as an image part).
- `present.destinations`: common keys + `items` (`[{"id", "label", "name", "country", "price_from_rub"|null, "nights", "party_label", "weather": {"air_max_c", "sea_c"|null, "rain_days"}|null, "travel_time_h"|null, "match": [trait], "facts": {…}}]`), `questions` (`[{"id", "text"}]` the engine will render with buttons), `image` (the understand `image` object or null), `insight` (`{"popular_ids": [...]}` or null — never cited as a source).
- `present.variants` / `present.update`: common keys + `items` (`[{"id", "label", "title", "totals": {"rub", "door_to_door_hours"}, "steps": [{"type", "from", "to", "start", "end", "carrier"|null, "price_rub"}], "hotel": {"name", "stars", "rating", "district": {"labels", "counts"}}, "facts": {…}, "sacrifices": [Note], "risks": [Note], "not_for": [Note], "fit": {"score", "breakdown"}}]`), `recommended_hint` (the code's pick by fit), `questions`, `upsell_candidates` (`[{"candidate_id", "kind", "label", "price_rub", "reason"}]`); `present.update` also gets `changes` (`[{"field", "before", "after"}]` and what was rebuilt).
- `present.compare`: common keys + `items` (2–3 variants as above) + `dimensions` (the brief's priorities in order).
- `present.info`: common keys + `question` (the user's question) + `facts` (the looked-up records; empty when nothing was found).
- `present.aftercare`: common keys + `order` (order summary) + `result` (the mock's answer to the action).
- `present.fallback`: common keys + `message` + `reason` (`smalltalk|out_of_scope|unknown`).

Every number a card writes must appear in its input; every id it writes must be one of the input ids.

### Foundation decisions (2026-10-25)

1. Provenance is per sub-field: `Field.sub_sources` / `sub_confidence`, read through `Brief.source_of(path)` / `confidence_of(path)`; engine assumptions go through `agent.state.assume(brief, path, value, source, confidence)`, never through `apply_patch`.
2. After every patch the engine normalises `party` from `segments.json` defaults (e.g. `couple` → 2 adults) with `assume(..., "default")` for the sub-fields the user did not give.
3. Photo entry: when a request has an image, the engine runs `image_match` before route matching. `image.place.confidence ≥ rules.image.place_min_conf` (0.8) and the place resolves in the catalog or the geocoder → `assume("destination.name", …, "inferred", conf)` → route B. Between `rules.image.confirm_min_conf` (0.5) and 0.8 → destinations route with the recognised place first plus a confirmation question. Below → traits only.
4. `choose_variant` puts the variant straight into the cart (one tap); `add_to_cart` is for extras.
5. Offer ids are deterministic and encode their parameters, so the mock rebuilds an offer from its id alone (a cache is only an optimisation).
6. `dates.shift_days` with no concrete dates: before `apply_patch` the engine resolves the shift against the dates of the variants currently shown and sends concrete `dates.start/end`; with nothing shown it asks.
7. Children's age buckets send one representative age; exact ages arrive at checkout and the `after_travellers` hook re-checks the cart.
8. No placeholder offers: upsell kinds are only what the mock sells — `transfer`, `early_checkin`, `room_upgrade` (incl. high floor), `meal_upgrade`, `flexible_fare`. The insurance rule is dropped.
9. Early check-in is not a cart item (the mock sells it as an order action): when the user accepts the early check-in add-on before paying, the engine remembers it and calls `order_request_early_checkin` right after `pay`, then reports the result in the same answer.

## 5. Turn pipeline

1. Input: free text (+ image) or a UI action (button, chip, card choice). Actions map to brief patches or engine commands deterministically, without `understand`.
2. Free text: Jev routing first (when enabled): short replies to pending questions or references to shown items are resolved without Flash when confidence ≥ the threshold in `rules.json`; otherwise `understand` (Flash).
3. Validation hook on the patch; the engine applies it, records provenance, computes affected fields, and picks the route.
4. Steps run (code calls MCP and open-data tools). Prefetch hooks may already hold results.
5. Validation hooks on results (`agent/checks.py`: chain, buffers, budget, dates; segment fit). Failure → one rerun of the affected step with the issue; still failing → present the best available with the issue stated honestly.
6. Sensitivity check (stage 2+) and Jev ask-or-assume (stage 3+) decide questions (≤ 2) and chips.
7. `present.<card>` (Flash) writes the text. Semantic hook (Jev, stage 3+) + deterministic checks (numbers ⊂ input numbers, ids exist, ≤ 1 joke heuristic, upsell only from candidates) → one rerun on failure.
8. Response assembled by code (section 6); analytics hook logs events; prefetch hooks start background work for the likely next step.

Budget: ≤ 2 Flash calls per turn (plus at most one rerun per card on a failed hook); recorded per turn in `trace`.

### Module interfaces (fixed before the parallel build)

- **Engine facade** `agent/engine.py`:
  ```python
  class Engine:
      def __init__(self, skill=None, backend=None, jev=None, ostrovok=None, opendata=None,
                   flags=None, store=None, profiles=None, clock=None): ...   # None -> real defaults
      def new_session(self, profile_id: str | None = None, debug: bool = False, now: datetime | None = None) -> str
      def handle(self, session_id: str, request: dict) -> dict   # request.schema.json in, response.schema.json out
      def get_session(self, session_id: str) -> Session
  ```
  `handle` never raises for user-caused problems: it answers with an `error` response; programming errors propagate in tests.
- **Ostrovok client** `agent/mcp_client.py`: `class OstrovokClient(Protocol): def call(self, tool: str, payload: dict) -> dict`, with `StdioOstrovokClient` (spawns `python -m ostrovok_mcp`, MCP over stdio) and `InProcessOstrovokClient` (calls `ostrovok_mcp.service.call(tool, payload)` directly; used by evals for speed). Both validate with `ostrovok_mcp.contract.TOOLS`.
- **Mock service** `ostrovok_mcp/service.py`: `call(tool: str, payload: dict) -> dict` plus one plain function per tool; `ostrovok_mcp/server.py` only wraps them as MCP tools. Paths come from env `OSTROVOK_CATALOG_DB` (default `data/ostrovok_catalog.sqlite`), `OPENDATA_DB` (default `data/opendata.sqlite`, attached read-only for airports/routes/destinations), `OSTROVOK_STATE_DB` (default `runtime/ostrovok_state.sqlite`) and the overlay `runtime/catalog_extra.sqlite`.
- **Open data** `agent/opendata.py`: `class OpenData(path="data/opendata.sqlite", overlay="runtime/opendata_extra.sqlite", http=None)` with the methods listed in section 8. Readers always union the committed database and the overlay.
- **Lazy city** `data_build/lazy.py`: `build_city(query: str, *, timeout_s: float = 60) -> str | None` — one attempt (Nominatim + one batched Overpass query) that writes the destination, its climate, hotels and districts into the overlays `runtime/opendata_extra.sqlite` and `runtime/catalog_extra.sqlite` and returns the new destination id, or None. Only the engine's `find_destinations`/`geocode` path calls it, at most once per unknown name per session. The engine owns the attempt (`TRIP_AGENT_LAZY_CITY=0` or `Engine(lazy_city=False)` turns it off); `OpenData` is never built with its own `lazy_builder` in the product.
- **Ranking and checks** (`agent/ranking.py`, `agent/checks.py`, `agent/upsell.py`, `agent/guards.py`):
  ```python
  build_variants(brief, results: dict, districts: dict, rules: dict, today) -> list[Variant]   # cheapest / fastest / comfort (+ custom)
  fit(brief, variant, rules) -> Fit
  sensitivity(brief, variants, rerun: Callable[[Brief], list[Variant]], skill) -> list[Question]  # ≤ rules.max_questions
  check_variant(variant, brief, rules) -> list[Issue]          # chain, buffers, budget, dates, party fit
  upsell_candidates(brief, variant, offers: dict, upsell_rules) -> list[UpsellCandidate]
  check_card_output(card, card_input, output, rules) -> list[Issue]   # numbers ⊂ input numbers, ids ⊂ input ids, ≤ 1 joke heuristic, upsell ⊂ candidates
  ```
  `Issue = {code, severity: "block"|"warn", message, data}`.
  As built (2026-10-25): `build_variants(..., segments=None)` and `fit(..., segments=None)` take `segments.json` data (else `rules["segments"]`); `results["hotel_upgrades"][hotel_id]` holds `search_hotels(hotel_id=…, all_rooms=True)` / higher-`meals` offers for room and meal upgrades; `recommended_hint(variants)`, `check_variant_set(variants)`, `offers_for_variant(variant, results)`, `OpenData.districts_for(ids)` / `origin_airports(city)`, `render_variant(session_id, variant, brief, rules, out_root=, now=)` exist. Ranking, sensitivity and guard thresholds live in `rules.json` (`ranking`, `sensitivity`, `guard`), code defaults only as fallback.
  As built after the first level-3 run (2026-10-25): a scope without `stay` builds ticket-only variants (out + back pairs, no hotel, no transfers); trains are also searched for the evening before check-in (overnight trains); user-set `prefs.arrive_before` / `prefs.no_night_flights` / `free_cancellation` (refundable fares) are relaxable hard filters (`relaxed_pref` / `non_refundable_only` risks), the `no_night_flights` constraint token stays a never-relaxed filter and understand no longer emits it. Destinations path: wishes («горы», «море», «юг»…) are never places (`opendata.wish_traits`); `prefs.transport = train` keeps only train-reachable places (the mock's rule: Russia, a station at both ends, 30–1000 km) priced with real round-trip trains; `prefs.domestic` keeps the origin's country; with a budget, fitting places go first, places above 1.3× budget are dropped when ≥ 3 fit, otherwise the cheapest come first and `facts.within_budget` lets the card say so. Seasons («летом» 7, «весной» 4, «зимой» 12 or 1, «осенью» the next autumn month) are set by code and shown as a chip. After an order whose tickets cannot be exchanged, a request for other dates starts a new selection on those dates (the paid order stays).

## 6. Response contract (engine → UI)

`agent/contracts/response.schema.json` (JSON Schema draft-07), with fixtures in `agent/contracts/fixtures/` for every stage. HTTP API (`agent/server.py`):

- `POST /api/session` `{profile_id?}` → `{session_id}`
- `POST /api/turn` `{session_id, text?, image?: <data URL>, action?: {type, payload}}` → `Response`
- `GET /api/session/<id>` → session state (debug)
- `GET /files/<path>` → `.ics` and pages from `runtime/files/`
- `GET /debug` → `web_debug/index.html`

`Response`:
```json
{
  "session_id": "s_…", "turn": 3, "stage": "selection",
  "message": "Russian text",
  "chips": [{"field": "prefs.meals", "label": "с завтраком", "value": "breakfast", "options": [{"label": "…", "value": "…"}]}],
  "questions": [{"id": "q_party", "text": "Кто едет?", "field": "party.segment", "choices": [{"label": "вдвоём", "value": "couple", "recommended": true}]}],
  "cards": {"destinations": [], "variants": [], "hotels": []},
  "comparison": null, "cart": null, "order": null,
  "actions": [{"type": "choose_variant", "label": "Беру этот", "payload": {"variant_id": "cheapest"}}],
  "files": {"ics": null, "page": null},
  "debug": {"stage_path": [], "calls": [], "hooks": [], "timings_ms": {}, "jev": [], "flags": {}}
}
```
Action types: `answer_question {question_id, value}`, `set_chip {field, value}`, `choose_destination {destination_id}`, `choose_variant {variant_id}`, `swap_hotel {variant_id, hotel_offer_id}`, `compare {variant_ids}`, `add_to_cart {variant_id, extras: [offer_id]}`, `checkout {travellers: [{name, birth_date}], contact}`, `pay {order_id}`, `order_action {order_id, kind: early_checkin|change_dates|add_transfer, params}`. `debug` is present only when the session is created with `debug: true`. The `checkout` action in a response is a form template (empty `travellers`, `contact: null`): the UI fills it in before sending, and the request schema rejects it unfilled. Server-level problems answer `{"error": {code, message, details?}}` with an HTTP error status; problems inside a turn answer a normal Response with `error` set and HTTP 200.

## 7. Ostrovok mock: MCP tool contract

`python -m ostrovok_mcp` — MCP server over stdio (`mcp` SDK, FastMCP). Input and output models in `ostrovok_mcp/contract.py` (pydantic), shared with the engine's client `agent/mcp_client.py`. All prices in RUB, all times ISO 8601 with the local UTC offset. Generators are deterministic: `seed = sha256(entity_id | field | date)`; every tool that depends on "today" takes `today` from the caller (the engine passes the session clock).

| Tool | Input | Output |
|---|---|---|
| `quote_destinations` | `destination_ids[]`, `origin_city`, `month` or `dates`, `party` | per destination: cheapest flight+stay estimate for the party, nights used, currency notes |
| `search_hotels` | `destination_id`, `check_in`, `check_out`, `adults`, `children_ages[]`, `filters {meals?, free_cancellation?, stars_min?, max_price_total?, pets?, kinds?}`, `limit` (≤ 40) | offers: `offer_id`, `hotel_id`, `osm_id`, `name`, `kind`, `stars`, `rating`, `reviews`, `lat`, `lon`, `room`, `meals`, `cancellation {free_until?, penalty_pct}`, `price_total_rub`, `pets_allowed`, `kids_policy`, `early_checkin {available, price_rub}`, `distance_beach_m?`, `distance_center_m` |
| `get_hotel` | `hotel_id` | details, policies, room types, photos (Wikimedia/OSM links where real) |
| `search_flights` | `origin_iata[]`, `dest_iata[]`, `date`, `adults`, `children`, `infants`, `flex_days` (0–3) | offers: `offer_id`, `carrier`, `flight_no`, `segments[]`, `dep`, `arr`, `duration_min`, `stops`, `baggage`, `fare {refundable, change_fee_rub}`, `price_rub`, `date` |
| `search_trains` | `origin_city`, `dest_city`, `date`, `passengers` | offers (domestic RU routes ≤ ~1000 km only; empty otherwise) |
| `search_transfers` | `from {iata|station|lat,lon}`, `to {hotel_id}`, `at`, `pax`, `kinds?` | offers: `offer_id`, `kind` (taxi/shuttle/public/private), `duration_min`, `price_rub`, `meeting_point` |
| `cart_create` / `cart_add` / `cart_remove` / `cart_get` | `session_id`, `offer_id`, `kind` | cart with items, totals, compatibility issues found by the mock |
| `checkout` | `cart_id`, `travellers [{name, birth_date}]`, `contact` | `order_id`, `status: pending_payment`, `issues[]` (age policies: infant fare, child beds, hotel age limits) |
| `pay` | `order_id`, `method: "mock_card"` | `status: paid`, booking numbers per item |
| `order_get` | `order_id` | order with items and events |
| `order_request_early_checkin` | `order_id`, `time` | `granted | on_request | refused`, price |
| `order_change_dates` | `order_id`, `new_dates`, `confirm: bool` | quote of the difference; applies when `confirm` |
| `order_add_transfer` | `order_id`, `offer_id` | updated order |
| `insights_popular_destinations` | `origin_city`, `month`, `segment` | `[{destination_id, share, n}]`, only groups with n ≥ 20 |
| `insights_segment_preferences` | `segment`, `destination_kind?` | shares: breakfast, all-inclusive, free cancellation, direct flights; median nights; budget percentiles; district preference |
| `insights_log_event` | event (no personal data) | `ok` |

Runtime writes (carts, orders, live insights events) go to `runtime/ostrovok_state.sqlite`, never to the committed catalog.

## 8. Data

Two committed read-only databases, built by `data_build/build_all.py` (resumable, cached raw downloads in `data_build/cache/`, gitignored; mirrors and retries for Overpass; Nominatim and Photon as fallbacks for geocoding; polite User-Agent and rate limits):

- `data/opendata.sqlite` — open data, read by the engine's open-data tools: `destinations` (id slug, names ru/en, country, lat/lon, kind, tags, airports, stations, short description ru, popularity), `climate` (destination × month: air max/min, sea temperature, rain days, sunny days; source Open-Meteo archive/marine, fetched_at), `airports` (OurAirports), `routes` (OpenFlights: origin, destination, carriers, stops), `districts` (per hotel osm id: POI counts in 500 m and nearest distances; scores lively/quiet/family/walkable 0..1; source, fetched_at).
- `data/ostrovok_catalog.sqlite` — the mock's inventory: `hotels` (real OSM names/coordinates/kinds + deterministic synthetic fields), `rooms`, `policies`, `insights_events` (synthetic log from a hidden preference model, flagged `source = synthetic`).
- Schemas in `data/schema/opendata.sql` and `data/schema/ostrovok_catalog.sql`. Every fact row carries `source` and `fetched_at`.
- Honesty: real = places, hotels' names/coordinates, districts, climate, airports, routes; synthetic = prices, availability, schedules, ratings, policies, insights. The README and the demo say so.
- Measured 2026-10-24 from the dev machine: Open-Meteo archive/marine, OurAirports CSV, OpenFlights `routes.dat`, CBR XML, Nominatim and Photon answer fast. Overpass is slow and flaky: a first round gave `overpass-api.de` 502 and 30 s timeouts on the kumi/private.coffee/mail.ru mirrors; a retry minutes later succeeded on `overpass-api.de`, `lz4.overpass-api.de` and `z.overpass-api.de`, but each tiny query took 32–36 s. So: one batched query per city (all hotel and POI tags for the city's bounding box at once, never one query per hotel), long timeouts, sequential requests with backoff across those mirrors, every successful response cached, resumable builds; fall back to Photon (`photon.komoot.io/api/?q=…&lat=…&lon=…&osm_tag=tourism:hotel` and amenity tags) with distance filtering only when Overpass stays down, and a district built from the fallback records `source = "photon"`.

Open-data tools (`agent/opendata.py`, called by code): `destination(id)`, `find_destinations(traits, month, origin_city, budget?, max_travel_hours?, segment?, limit)`, `climate(destination_id, month)`, `district_profile(osm_id)`, `airports_near(lat, lon)`, `geocode(name)` (catalog first, then Nominatim), `travel_time(origin_city, destination_id)` (routes graph + great-circle duration), `cbr_rates()` (live with cache, fallback to the cached rate with its date).

## 9. Hooks and flags

`agent/hooks.py`: `register(event, fn, kind)`, events `after_understand`, `after_search`, `after_variants`, `before_present`, `after_present`, `after_travellers`, `turn_end`; kinds `validate` (blocking, returns issues), `prefetch` (background, results land in `session.results`), `analytics` (fire-and-forget). Hook verdicts go to `trace` and `debug`.

`agent/flags.py`: `sensitivity`, `jev_routing`, `jev_ask`, `jev_guard`, `prefetch`, `upsell`, `insights`; presets `stage1`…`stage5` per the activation order above; set via `TRIP_AGENT_FLAGS=stage3` or a comma list.

## 10. Evals

- Level 2 case `evals/cases/<id>.json`: `{id, category, card, input, image?, expect: [assertion], runs: 5}`. `input` is the exact card payload, produced by `python -m evals.build_inputs` from the engine with a pinned clock and the committed data (no model). Assertion types: `schema`, `path_equals`, `path_in`, `path_absent`, `max_items`, `regex_absent`, `numbers_subset`, `ids_subset`, `predicate` (named function in `evals/assertions.py`).
- Level 3 scenario `evals/scenarios/<id>.json` (and `evals/holdout/`): `{id, category, persona: {start_message, image?, profile, hidden: {facts, must: [predicate], nice: [str]}, behaviour}, max_turns}`. `must` predicates are checked by code on the final cart/order/state.
- Runner: `python -m evals.run --level 2|3 --set main|holdout --runs N --flags stageK --model flash|qwen|codex --out evals/runs/<ts>`. Raw transcripts → `evals/runs/` (gitignored); summary → `evals/reports/<ts>.json` and `.md` (committed): pass k/N per case, first recommendation in first reply, touches to a ready cart (every text, button, chip and card choice is a touch), hidden-requirement satisfaction, connection errors, calls per turn, latency p50/p95, cost, served model and provider, Jev fallback rate, fallback-card rate.
- Persona: `evals/persona.py` drives Sonnet 5.5 through the Claude Code CLI (section 2) with `evals/skills/persona/SKILL.md` as the system prompt; it sees the response rendered like the UI (message, questions with choices, chips, cards, actions) and answers `{"type": "text", "text"}` or `{"type": "action", "action": {...}}`. Unit tests use a fake `claude` executable.
- Judge: the runner writes one packet per scenario to `evals/runs/<ts>/judge/<id>.md`; Opus subagents apply `evals/skills/judge/SKILL.md` and write `<id>.verdict.json`; `python -m evals.report --merge-judge evals/runs/<ts>` folds verdicts into the report.

## 11. Tests and rules for builders

- Unit tests: `.venv/bin/python -m pytest tests -q`. No network, no model calls, no real `codex` in tests; fakes only.
- Old projects stay green: `python3 -m unittest discover -s skills/trip-planner/tests` and `python3 -m unittest discover -s system/tests -t .`.
- Builders run only the tests of their own area; the orchestrator runs every suite after each batch.
- Never skip or delete a test to get green; never invent data — degrade to an honest "no data" state and report it.
- No commits, no pushes; leave changes in the working tree.

## 12. Build order

1. **Foundation** (alone): venv and requirements; `agent/state.py`, `agent/env.py`, `agent/llm.py` (Backend protocol, OpenRouter backend, FakeBackend), `agent/jev.py`, `agent/flags.py`, `agent/skill_loader.py` (+ `turns.json` validation), `agent/contracts/response.schema.json` + fixtures, `ostrovok_mcp/contract.py`, `data/schema/*.sql`, the skill package skeleton with the card schemas, `tests/` scaffolding.
2. **Parallel**, each against the foundation: data (`data_build/`), the mock (`ostrovok_mcp/`), the engine (`agent/` steps, ranking, sensitivity, checks, hooks, prefetch, opendata, mcp client, sessions, profiles), the server + debug page + Codex backend, skill content v0 (`skills/trip-agent/`), evals (`evals/`).
3. Integration and stage 1 baseline run; then stages 2–5 and the improvement loop.
