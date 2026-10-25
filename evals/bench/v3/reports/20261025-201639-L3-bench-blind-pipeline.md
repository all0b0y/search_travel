# Eval report 20261025-201639-L3-bench-blind-pipeline

- level: 3; set: bench; model: flash; flags: stage1; dry run: False
- started: 2026-10-25T20:16:39+03:00; finished: 2026-10-25T20:25:12+03:00
- run dir: `evals/bench/v3/runs/20261025-201639-L3-bench-blind-pipeline`

## Thresholds (decision 16)

- PASS — первая рекомендация в первом ответе ≥ 80 %: 0.95 (need >= 0.8)
- PASS — касаний до готовой корзины ≤ 4 (среднее): 3.889 (need <= 4.0)
- FAIL — скрытые требования персон ≥ 90 %: 0.89 (need >= 0.9)
- FAIL — ошибки стыковок = 0: 3 (need <= 0)
- run is NOT green

## Metrics

- scenarios: 20
- completion_rate: 0.85
- first_rec_rate: 0.95
- reached_cart_rate: 0.9
- touches_to_cart_mean: 3.889
- touches_to_cart_p50: 3
- touches_to_cart_max: 10
- hidden_must_rate: 0.89
- scenarios_all_must_rate: 0.7
- connection_errors_total: 3
- calls_per_turn_mean: 1.091
- calls_per_turn_max: 3
- latency_p50_ms: 3,122
- latency_p95_ms: 11,872
- cost_usd_total: 0.09
- persona_cost_usd_total: 5.466
- jev_calls: 0
- jev_fallback_rate: нет данных
- fallback_card_rate: 0
- response_schema_error_turns: 0
- degraded_turns: 2
- error_response_turns: 4
- end_reasons: persona_stop_failed: 4, paid: 14, persona_stop_done: 2
- profiles_injected: 20
- served_models: deepseek/deepseek-v4.1-flash-1017: 144
- served_providers: DeepSeek: 144
- persona_models: claude-sonnet-5-5: 118

## Scenarios

| scenario | category | end | paid | first rec | touches | must | judge |
|---|---|---|---|---|---|---|---|
| bench_01_nursery | family | persona_stop_failed | False | True | 5 | 4/5 | — |
| bench_02_decembersea | couple | paid | True | True | 5 | 5/5 | — |
| bench_03_pskovdeal | budget | paid | True | True | 3 | 4/5 | — |
| bench_04_snowmates | friends | paid | True | True | 4 | 4/5 | — |
| bench_05_workvisit | business | paid | True | True | 3 | 5/5 | — |
| bench_06_basenjitrain | pets | paid | True | True | 4 | 5/5 | — |
| bench_07_catweekend | pets | paid | True | True | 3 | 5/5 | — |
| bench_08_stepfreerest | seniors | persona_stop_failed | False | True | нет данных | 3/5 | — |
| bench_09_junelake | solo | paid | True | True | 8 | 5/5 | — |
| bench_10_firstmountains | other | persona_stop_failed | False | True | нет данных | 0/5 | — |
| bench_11_springtaste | couple | paid | True | True | 4 | 5/5 | — |
| bench_12_islandrethink | budget | paid | True | False | 10 | 4/5 | — |
| bench_13_movedorder | aftercare | persona_stop_done | True | True | 2 | 5/5 | — |
| bench_14_shorternizhny | budget | paid | True | True | 3 | 5/5 | — |
| bench_15_earlyminsk | aftercare | persona_stop_done | True | True | 2 | 5/5 | — |
| bench_16_tashkentpickup | aftercare | persona_stop_failed | True | True | 2 | 5/5 | — |
| bench_17_tbilisiconnection | couple | paid | True | True | 4 | 5/5 | — |
| bench_18_railfamily | family | paid | True | True | 3 | 5/5 | — |
| bench_19_arcticbaggage | solo | paid | True | True | 2 | 5/5 | — |
| bench_20_bukharareschedule | couple | paid | True | True | 3 | 5/5 | — |

### Failed must predicates

- bench_01_nursery: order_paid  — order status: pending_payment
- bench_03_pskovdeal: brief_has_constraint {'token': 'no_hostel'} — constraint no_hostel missing
- bench_04_snowmates: brief_has_constraint {'token': 'no_dorm'} — constraint no_dorm missing
- bench_08_stepfreerest: order_paid  — order status: no order
- bench_08_stepfreerest: meals_at_least {'level': 'breakfast'} — no data: meals of the chosen hotel
- bench_10_firstmountains: order_paid  — order status: no order
- bench_10_firstmountains: destination_in {'names': ['Домбай', 'Архыз']} — no data: destination in the brief
- bench_10_firstmountains: direct_flights_only  — no data: no flight steps in the chosen variant
- bench_10_firstmountains: nights_between {'min': 4, 'max': 6} — 7 nights not in 4..6
- bench_10_firstmountains: dates_within {'start': '2027-02-08', 'end': '2027-02-28'} — no data: stay dates
- bench_12_islandrethink: destination_in {'names': ['Владимир']} — destination 'Нижний Новгород' not in ['Владимир']
