# Eval report 20261025-173123-L3-bench-blind-aftercare

- level: 3; set: bench; model: flash; flags: stage1; dry run: False
- started: 2026-10-25T17:31:23+03:00; finished: 2026-10-25T17:32:08+03:00
- run dir: `evals/bench/v1/runs/20261025-173123-L3-bench-blind-aftercare`

## Thresholds (decision 16)

- PASS — первая рекомендация в первом ответе ≥ 80 %: 1 (need >= 0.8)
- PASS — касаний до готовой корзины ≤ 4 (среднее): 2 (need <= 4.0)
- FAIL — скрытые требования персон ≥ 90 %: 0.8 (need >= 0.9)
- PASS — ошибки стыковок = 0: 0 (need <= 0)
- run is NOT green

## Metrics

- scenarios: 2
- completion_rate: 1
- first_rec_rate: 1
- reached_cart_rate: 1
- touches_to_cart_mean: 2
- touches_to_cart_p50: 2
- touches_to_cart_max: 2
- hidden_must_rate: 0.8
- scenarios_all_must_rate: 0.5
- connection_errors_total: 0
- calls_per_turn_mean: 0.909
- calls_per_turn_max: 2
- latency_p50_ms: 30
- latency_p95_ms: 6,471
- cost_usd_total: 0.003
- persona_cost_usd_total: 0.331
- jev_calls: 0
- jev_fallback_rate: нет данных
- fallback_card_rate: 0
- response_schema_error_turns: 0
- degraded_turns: 0
- error_response_turns: 0
- end_reasons: persona_stop_done: 2
- profiles_injected: 2
- served_models: deepseek/deepseek-v4.1-flash-1017: 10
- served_providers: DeepSeek: 10
- persona_models: claude-sonnet-5-5: 11

## Scenarios

| scenario | category | end | paid | first rec | touches | must | judge |
|---|---|---|---|---|---|---|---|
| bench_05_work_early_checkin | business | persona_stop_done | True | True | 2 | 5/5 | — |
| bench_11_bukhara_date_change | aftercare | persona_stop_done | True | True | 2 | 3/5 | — |

### Failed must predicates

- bench_11_bukhara_date_change: stay_starts_on {'day': '2027-04-14'} — stay starts 2027-04-07 != 2027-04-14
- bench_11_bukhara_date_change: dates_within {'start': '2027-04-14', 'end': '2027-04-20'} — stay 2027-04-07..2027-04-12 not within 2027-04-14..2027-04-20
