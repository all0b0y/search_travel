"""Eval runner (AGENT_PLAN.md section 10).

    python -m evals.run --level 2 [--runs N] [--model flash|qwen|codex] [--parallel P] [--only id1,id2]
    python -m evals.run --level 3 --set main|holdout [--flags stageK] [--persona claude|buttons] ...
    python -m evals.run --level 2|3 --dry-run        # FakeBackend / FakeEngine / ButtonPersona, offline

Raw records go to the run dir (default evals/runs/<timestamp>, gitignored): level2.jsonl or
level3.jsonl (one line per turn) + scenarios.jsonl, judge/<packet>.md, summary.json. The summary
report goes to evals/reports/<timestamp>-L<level>-<set>.json/.md (dry runs: <run dir>/reports/).
The OpenRouter key is loaded with agent.env.load_env() and never printed.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import jsonschema

from .assertions import evaluate_all
from .card_inputs import CARDS, validate_card_input
from .common import (CASES_DIR, HOLDOUT_DIR, REPORTS_DIR, RESPONSE_SCHEMA, ROOT, RUNS_DIR, SCENARIOS_DIR, append_jsonl,
                     image_data_url, mean, percentile, read_json, rub, timestamp, write_json)
from .predicates import build_final_state, check_all
from .render_ui import render, render_user_turn
from .report import write_report

DEFAULT_NOW = "2026-10-25T12:00:00+03:00"
FLASH_MODEL = "deepseek/deepseek-v4.1-flash-1017"
FLASH_PROVIDER = {"order": ["DeepSeek"], "allow_fallbacks": False}
QWEN_MODEL = "qwen/qwen3.5-flash-02-23"
MODELS = ("flash", "qwen", "codex")

_lock = threading.Lock()


# ---------------------------------------------------------------- loading

def _only(ids):
    return {x.strip() for x in ids.split(",") if x.strip()} if ids else None


def load_cases(only=None, cases_dir=CASES_DIR):
    cases = [read_json(p) for p in sorted(Path(cases_dir).glob("*.json"))]
    want = _only(only)
    if want:
        cases = [c for c in cases if c["id"] in want]
        missing = want - {c["id"] for c in cases}
        if missing:
            raise SystemExit(f"unknown case ids: {sorted(missing)}")
    return cases


def load_scenarios(which="main", only=None):
    folder = {"main": SCENARIOS_DIR, "holdout": HOLDOUT_DIR}[which]
    scenarios = [read_json(p) for p in sorted(folder.glob("*.json"))]
    want = _only(only)
    if want:
        scenarios = [s for s in scenarios if s["id"] in want]
        missing = want - {s["id"] for s in scenarios}
        if missing:
            raise SystemExit(f"unknown scenario ids in set {which}: {sorted(missing)}")
    return scenarios


# ---------------------------------------------------------------- backends / skill

def make_backend(model):
    """Real backend for live runs. Loads .env (key never printed)."""
    from agent.env import load_env
    load_env(str(ROOT / ".env"))
    if model == "flash":
        from agent.llm import OpenRouterBackend
        return OpenRouterBackend(model=FLASH_MODEL, provider=FLASH_PROVIDER)
    if model == "qwen":
        from agent.llm import OpenRouterBackend
        # Portability check: no provider pin is specified for Qwen in the plan; recorded in the report.
        return OpenRouterBackend(model=QWEN_MODEL, provider={})
    if model == "codex":
        from agent.llm_codex import CodexBackend  # sibling module (SERVER); imported lazily
        return CodexBackend(effort="high")  # decision 22: gpt-6-luna at effort high
    raise ValueError(f"unknown model: {model}")


class _CardShim:
    def __init__(self, name, schema, thinking="off", max_tokens=1000):
        self.name, self.schema, self.thinking, self.max_tokens = name, schema, thinking, max_tokens


class SkillShim:
    """Dry-run stand-in when the skill package does not load (sibling mid-edit): card schemas only."""

    def __init__(self):
        base = ROOT / "skills" / "trip-agent" / "schema"
        self.cards = {}
        for card in CARDS:
            path = base / f"{card}.schema.json"
            if path.is_file():
                self.cards[card] = _CardShim(card, read_json(path))

    def card(self, name):
        return self.cards[name]

    def system_prompt(self, name):
        return f"(dry-run shim prompt for {name})"


def load_skill_for_run(dry_run):
    from agent.skill_loader import SkillLoadError, load_skill
    try:
        return load_skill(), None
    except SkillLoadError as exc:
        if not dry_run:
            raise
        return SkillShim(), f"skill package did not load ({exc}); dry run used schema shim"


# ---------------------------------------------------------------- level 2

THINK_ORDER = ("off", "low", "high")


def call_settings(case, card, policy=None):
    """(thinking, temperature) for one level-2 call: the card's own settings, then the experiment policy
    (--temperature card=value, --think-image level, --think-long chars:level)."""
    thinking, temperature = card.thinking, getattr(card, "temperature", None)
    policy = policy or {}
    for pattern, value in (policy.get("temperature") or {}).items():
        if case["card"] == pattern or case["card"].startswith(pattern + "."):
            temperature = value
    raised = []
    if case.get("image") and policy.get("think_image"):
        raised.append(policy["think_image"])
    long_rule = policy.get("think_long")
    if long_rule and case["card"] == "understand" and len((case["input"] or {}).get("message") or "") >= long_rule[0]:
        raised.append(long_rule[1])
    for level in raised:
        if THINK_ORDER.index(level) > THINK_ORDER.index(thinking):
            thinking = level
    return thinking, temperature


def parse_policy(args):
    policy = {}
    if getattr(args, "temperature", None):
        policy["temperature"] = {k.strip(): float(v) for k, v in (p.split("=") for p in args.temperature.split(","))}
    if getattr(args, "think_image", None):
        policy["think_image"] = args.think_image
    if getattr(args, "think_long", None):
        chars, level = args.think_long.split(":")
        policy["think_long"] = (int(chars), level)
    return policy


def run_case_once(case, run_index, backend, skill, policy=None):
    from agent.llm import LLMError, LLMRequest
    rec = {"case_id": case["id"], "category": case["category"], "card": case["card"], "run_index": run_index,
           "ok": False, "results": [], "output": None, "error": None, "latency_ms": None, "usage": None,
           "model": None, "provider": None, "retries": None}
    input_errors = validate_card_input(case["card"], case["input"])
    if input_errors:
        rec["error"] = "invalid case input: " + "; ".join(input_errors[:3])
        return rec
    card = skill.card(case["card"])
    images = [image_data_url(case["image"])] if case.get("image") else []
    thinking, temperature = call_settings(case, card, policy)
    rec.update(thinking=thinking, temperature=temperature)
    req = LLMRequest(card=case["card"], system=skill.system_prompt(case["card"]),
                     user=json.dumps(case["input"], ensure_ascii=False), schema=card.schema, images=images,
                     thinking=thinking, max_tokens=card.max_tokens, temperature=temperature)
    started = time.monotonic()
    try:
        res = backend.complete(req)
    except LLMError as exc:
        rec.update(error=f"LLMError: {exc}", latency_ms=exc.latency_ms or int((time.monotonic() - started) * 1000),
                   usage=exc.usage, retries=exc.retries)
        return rec
    from agent.engine import card_guard_issues, repair_output_ids, rerun_system
    from agent.hooks import blocking
    if case["card"].startswith("present.") and isinstance(res.data, dict):
        repair_output_ids(res.data, case["input"])   # like the engine: id typos are repaired before the guard
    from agent.respond import polish_output   # the user sees the card output after the code-side polish
    rerun = None
    if case["card"].startswith("present."):
        # like the engine: the guards check the card; a blocked reply gets exactly one rerun with the reasons
        issues = blocking(card_guard_issues(case["card"], case["input"], res.data, skill.rules))
        if issues:
            rerun = {"issues": [f"{i['code']}: {i['message']}" for i in issues], "first_output": res.data}
            req2 = LLMRequest(card=case["card"], system=rerun_system(req.system, issues), user=req.user,
                              schema=card.schema, images=images, thinking=thinking, max_tokens=card.max_tokens,
                              temperature=temperature)
            try:
                res = backend.complete(req2)
            except LLMError as exc:
                rec.update(error=f"LLMError on rerun: {exc}", rerun=rerun,
                           latency_ms=int((time.monotonic() - started) * 1000))
                return rec
            if isinstance(res.data, dict):
                repair_output_ids(res.data, case["input"])
    data = polish_output(res.data)
    if case["card"] == "understand" and isinstance(data, dict):
        # like the engine: deterministic code-side normalisation of the reply before it reaches the brief
        from agent.engine import normalize_understand
        rec["normalized"] = normalize_understand(data, case["input"])
    ok, results = evaluate_all(case["expect"], data, case["input"], card.schema)
    rec["rerun"] = rerun
    rec.update(ok=ok, results=results, output=data, latency_ms=res.latency_ms, usage=res.usage, model=res.model,
               provider=res.provider, retries=res.retries, validation_errors=res.validation_errors)
    return rec


def summarize_level2(cases, records, runs_for):
    by_case = {c["id"]: [] for c in cases}
    for r in records:
        by_case[r["case_id"]].append(r)
    case_rows = []
    for c in cases:
        recs = sorted(by_case[c["id"]], key=lambda r: r["run_index"])
        failures = []
        for r in recs:
            if r["error"]:
                failures.append(f"run {r['run_index']}: {r['error'][:200]}")
            for a in r["results"]:
                if not a["ok"]:
                    failures.append(f"run {r['run_index']}: {a['type']} {a.get('label') or ''}: {a['detail'][:160]}")
        case_rows.append({
            "id": c["id"], "category": c["category"], "card": c["card"], "runs": len(recs),
            "passed": sum(1 for r in recs if r["ok"]), "full_pass": bool(recs) and all(r["ok"] for r in recs),
            "latency_p50_ms": percentile([r["latency_ms"] for r in recs], 50),
            "failures": failures[:10], "unsure": c.get("unsure"),
        })
    lat = [r["latency_ms"] for r in records]
    costs = [(r.get("usage") or {}).get("cost_usd") for r in records]
    cats = {}
    for row in case_rows:
        k = cats.setdefault(row["category"], [0, 0])
        k[0] += int(row["full_pass"])
        k[1] += 1
    metrics = {
        "cases_total": len(case_rows),
        "cases_full_pass": sum(1 for r in case_rows if r["full_pass"]),
        "cases_full_pass_rate": (sum(1 for r in case_rows if r["full_pass"]) / len(case_rows)) if case_rows else None,
        "runs_total": len(records),
        "runs_passed": sum(1 for r in records if r["ok"]),
        "runs_pass_rate": (sum(1 for r in records if r["ok"]) / len(records)) if records else None,
        "llm_errors": sum(1 for r in records if (r["error"] or "").startswith("LLMError")),
        "input_errors": sum(1 for r in records if (r["error"] or "").startswith("invalid case input")),
        "retries_total": sum(r.get("retries") or 0 for r in records),
        "guard_reruns": sum(1 for r in records if r.get("rerun")),   # first reply blocked by the engine's guards
        "guard_rerun_reasons": sorted({x.split(":")[0] for r in records if r.get("rerun") for x in r["rerun"]["issues"]}),
        "latency_p50_ms": percentile(lat, 50),
        "latency_p95_ms": percentile(lat, 95),
        "input_tokens": sum((r.get("usage") or {}).get("input_tokens") or 0 for r in records),
        "output_tokens": sum((r.get("usage") or {}).get("output_tokens") or 0 for r in records),
        "cost_usd_total": sum(c for c in costs if c is not None) if any(c is not None for c in costs) else None,
        "served_models": dict(Counter(r["model"] for r in records if r.get("model"))),
        "served_providers": dict(Counter(r["provider"] for r in records if r.get("provider"))),
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(cats.items())},
    }
    return case_rows, metrics


def run_level2(cases, backend, skill, run_dir, runs=None, parallel=1, log=print, policy=None):
    jobs = [(c, i) for c in cases for i in range(runs or c.get("runs", 5))]
    out = Path(run_dir) / "level2.jsonl"
    records = []

    def work(job):
        case, i = job
        rec = run_case_once(case, i, backend, skill, policy)
        with _lock:
            append_jsonl(out, rec)
            records.append(rec)
        return rec

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for rec in pool.map(work, jobs):
            log(f"  {rec['case_id']} run {rec['run_index']}: {'ok' if rec['ok'] else 'FAIL'}"
                + (f" ({rec['error'][:80]})" if rec["error"] else ""))
    return summarize_level2(cases, records, runs)


# ---------------------------------------------------------------- level 3

def required_cart_kinds(brief):
    scope = (brief or {}).get("scope") or []
    kinds = set()
    if not scope or "stay" in scope:
        kinds.add("hotel")
    if not scope:
        kinds.add("transport")
    if "flights" in scope or "trains" in scope:
        kinds.add("transport")
    return kinds


def cart_ready(response, kinds):
    holder = response.get("cart") or response.get("order")
    if not holder or not holder.get("items"):
        return False
    got = {i.get("kind") for i in holder["items"]}
    if "hotel" in kinds and "hotel" not in got:
        return False
    if "transport" in kinds and not got & {"flight", "train"}:
        return False
    return True


def _jev_fallback(entry):
    """Jev did not decide: failed/unavailable (ok false, answers null) or not confident / not accepted
    (accepted false, a fallback marker) -> rules or Flash took over."""
    if entry.get("fallback") or entry.get("ok") is False or entry.get("accepted") is False:
        return True
    return "answers" in entry and entry["answers"] is None


def _call_cost(call):
    """agent.engine records `cost`; fakes record usage.cost_usd."""
    if call.get("cost") is not None:
        return call["cost"]
    return (call.get("usage") or call.get("tokens") or {}).get("cost_usd")


def _model_calls(debug):
    return [c for c in (debug or {}).get("calls") or [] if isinstance(c, dict) and c.get("card")]


def connection_errors(session, responses, rules=None):
    """Blocking check issues in the final variants (agent.checks.check_variant when it exists,
    else blocking issues reported by validation hooks in debug) + blocking cart/order issues."""
    count, source = 0, "debug"
    variants = []
    if session is not None and hasattr(session, "variants"):
        chosen = session.variant(session.chosen_variant_id) if session.chosen_variant_id else None
        variants = [chosen] if chosen else list(session.variants)
    try:
        from agent.checks import check_variant  # sibling module (ENGINE-B)
    except ImportError:
        check_variant = None
    if check_variant is not None and variants:
        source = "checks"
        try:
            for v in variants:
                for issue in check_variant(v, session.brief, rules or {}):
                    sev = issue.get("severity") if isinstance(issue, dict) else getattr(issue, "severity", None)
                    count += int(sev in ("block", "blocking"))
        except Exception as exc:  # report, do not crash the run
            source = f"checks_error: {type(exc).__name__}: {exc}"
    else:
        last = responses[-1] if responses else {}
        for hook in ((last.get("debug") or {}).get("hooks") or []):
            for issue in hook.get("issues") or []:
                count += int(issue.get("severity") in ("block", "blocking"))
    final = {}
    for r in responses:
        final["cart"] = r.get("cart") or final.get("cart")
        final["order"] = r.get("order") or final.get("order")
    for holder in final.values():
        for issue in (holder or {}).get("issues") or []:
            count += int(issue.get("severity") == "blocking")
    return count, source


# Real airport codes of the persona home cities (facts, not preferences).
HOME_IATA = {"Москва": ["SVO", "DME", "VKO"], "Санкт-Петербург": ["LED"], "Екатеринбург": ["SVX"], "Казань": ["KZN"],
             "Новосибирск": ["OVB"], "Самара": ["KUF"]}


def inject_profile(engine, profile_id, profile):
    """Put the scenario profile where the engine reads profiles: agent.profiles.ProfileStore.put(Profile)
    for the real engine, put(profile_id, dict) for dict-like fake stores. Returns False when impossible."""
    store = getattr(engine, "profiles", None)
    put = getattr(store, "put", None)
    if not callable(put):
        return False
    if isinstance(store, dict):
        put(profile_id, profile)
        return True
    from agent.profiles import Profile  # sibling (ENGINE-A)
    put(Profile(id=profile_id, home_city=profile["home_city"], home_iata=HOME_IATA.get(profile["home_city"], []),
                age=profile.get("age"), travellers=list(profile.get("travellers") or [])))
    return True


def _log_request(request, image_rel):
    r = dict(request)
    if "image" in r:
        r["image"] = f"<image {image_rel}>"
    return r


def _schema_errors(resp, validator):
    return [e.message for e in validator.iter_errors(resp)][:3]


def run_scenario(scenario, run_index, engine, persona, run_dir, set_name, lookup=None, rules=None, max_turns=None,
                 packet=None, log=print):
    p = scenario["persona"]
    now = datetime.fromisoformat(scenario.get("now") or DEFAULT_NOW)
    profile_id = f"eval_{scenario['id']}"
    injected = inject_profile(engine, profile_id, p["profile"])
    sid = engine.new_session(profile_id=profile_id, debug=True, now=now)
    request = {"session_id": sid, "text": p["start_message"]}
    if p.get("image"):
        request["image"] = image_data_url(p["image"])
    limit = max_turns or scenario["max_turns"]
    validator = jsonschema.Draft7Validator(read_json(RESPONSE_SCHEMA))
    responses, transcript, turns = [], [], []
    touches, touches_to_cart = 0, None
    end_reason, persona_models, persona_ms, persona_cost = "max_turns", Counter(), [], 0.0
    schema_errors = 0
    stop_on_paid = scenario.get("stop_on_paid", True)
    last_label = None
    for turn in range(1, limit + 1):
        touches += 1
        transcript.append({"role": "user", "text": render_user_turn(request, last_label)})
        started = time.monotonic()
        try:
            response = engine.handle(sid, request)
        except Exception as exc:
            end_reason = "engine_error"
            rec = {"scenario_id": scenario["id"], "packet": packet, "run_index": run_index, "turn": turn,
                   "request": _log_request(request, p.get("image")), "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-2000:]}
            with _lock:
                append_jsonl(Path(run_dir) / "level3.jsonl", rec)
            turns.append(rec)
            break
        latency = int((time.monotonic() - started) * 1000)
        responses.append(response)
        errs = _schema_errors(response, validator)
        schema_errors += int(bool(errs))
        screen, _ = render(response, with_refs=True)
        transcript.append({"role": "agent", "text": screen})
        try:
            session = engine.get_session(sid)
            brief = session.brief.values()
        except Exception:
            session, brief = None, {}
        kinds = required_cart_kinds(brief)
        if touches_to_cart is None and cart_ready(response, kinds):
            touches_to_cart = touches
        rec = {"scenario_id": scenario["id"], "packet": packet, "run_index": run_index, "turn": turn,
               "request": _log_request(request, p.get("image")), "response": response, "latency_ms": latency,
               "schema_errors": errs}
        paid = (response.get("order") or {}).get("status") == "paid"
        if paid and stop_on_paid:
            end_reason = "paid"
            with _lock:
                append_jsonl(Path(run_dir) / "level3.jsonl", rec)
            turns.append(rec)
            break
        if turn == limit:
            with _lock:
                append_jsonl(Path(run_dir) / "level3.jsonl", rec)
            turns.append(rec)
            break
        reply = None
        for attempt in range(2):
            try:
                reply = persona.reply(scenario, transcript, response)
                break
            except Exception as exc:  # PersonaError or a crash: one retry
                rec.setdefault("persona_errors", []).append(f"{type(exc).__name__}: {exc}"[:500])
        if reply is None:
            end_reason = "persona_error"
        else:
            meta = reply.get("meta") or {}
            if meta.get("model"):
                persona_models[meta["model"]] += 1
            if meta.get("duration_ms") is not None:
                persona_ms.append(meta["duration_ms"])
            persona_cost += meta.get("cost_usd") or 0.0
            rec["persona"] = {k: v for k, v in reply.items() if k != "meta"}
            rec["persona_meta"] = {k: v for k, v in meta.items() if k != "raw"}
        with _lock:
            append_jsonl(Path(run_dir) / "level3.jsonl", rec)
        turns.append(rec)
        if reply is None:
            break
        if reply["type"] == "stop":
            end_reason = f"persona_stop_{reply.get('outcome', 'done')}"
            transcript.append({"role": "user", "text": f"(пользователь ушёл: {reply.get('reason', '')})"})
            break
        if reply["type"] == "text":
            request, last_label = {"session_id": sid, "text": reply["text"]}, None
        else:
            request, last_label = {"session_id": sid, "action": reply["action"]}, reply.get("label")
    try:
        session = engine.get_session(sid)
    except Exception:
        session = None
    state = build_final_state(session, responses, lookup=lookup)
    must = check_all(p["hidden"]["must"], state)
    conn, conn_source = connection_errors(session, responses, rules)
    calls = [len(_model_calls(r.get("debug"))) if r.get("debug") else None for r in responses]
    jev = [e for r in responses for e in ((r.get("debug") or {}).get("jev") or []) if isinstance(e, dict)]
    model_calls = [c for r in responses for c in _model_calls(r.get("debug"))]
    costs = [_call_cost(c) for c in model_calls]
    fallback_turns = sum(1 for r in responses if any(c.get("card") == "present.fallback" for c in _model_calls(r.get("debug")))
                         or "present.fallback" in ((r.get("debug") or {}).get("stage_path") or []))
    first = responses[0] if responses else {}
    cards = first.get("cards") or {}
    summary = {
        "scenario_id": scenario["id"], "packet": packet or scenario["id"], "category": scenario["category"],
        "set": set_name, "run_index": run_index, "turns": len(responses), "end_reason": end_reason,
        "paid": (state.order or {}).get("status") == "paid",
        "first_rec": bool(cards.get("destinations") or cards.get("variants")),
        "touches_to_cart": touches_to_cart, "touches_total": touches,
        "must": must, "must_ok": sum(1 for m in must if m["ok"]), "must_total": len(must),
        "connection_errors": conn, "connection_errors_source": conn_source,
        "calls_per_turn": calls, "latencies_ms": [t.get("latency_ms") for t in turns if t.get("latency_ms") is not None],
        "cost_usd": sum(c for c in costs if c is not None) if any(c is not None for c in costs) else None,
        "jev_calls": len(jev), "jev_fallbacks": sum(1 for e in jev if _jev_fallback(e)),
        "fallback_turns": fallback_turns, "schema_error_turns": schema_errors,
        "degraded_turns": sum(1 for r in responses if (r.get("debug") or {}).get("degraded")),
        "error_turns": sum(1 for r in responses if r.get("error")),
        "served_models": dict(Counter(c.get("model") for c in model_calls if c.get("model"))),
        "served_providers": dict(Counter(c.get("provider") for c in model_calls if c.get("provider"))),
        "persona_models": dict(persona_models), "persona_ms_p50": percentile(persona_ms, 50),
        "persona_cost_usd": round(persona_cost, 6), "profile_injected": injected,
        "final": {"total_rub": (state.order or state.cart or {}).get("total_rub"),
                  "chosen_variant": (state.chosen or {}).get("id"), "stage": state.stage},
    }
    with _lock:
        append_jsonl(Path(run_dir) / "scenarios.jsonl", summary)
    write_judge_packet(run_dir, scenario, summary, transcript, state)
    log(f"  {summary['packet']}: {end_reason}, turns {summary['turns']}, must {summary['must_ok']}/{summary['must_total']}")
    return summary


def write_judge_packet(run_dir, scenario, summary, transcript, state):
    p = scenario["persona"]
    lines = [f"# Judge packet: {summary['packet']}", "",
             "Apply `evals/skills/judge/SKILL.md`. Write the verdict JSON to "
             f"`{Path(run_dir) / 'judge' / (summary['packet'] + '.verdict.json')}`.", "",
             f"- scenario: `{scenario['id']}` (category {scenario['category']}, set {summary['set']}, run {summary['run_index']})",
             f"- start message: «{p['start_message']}»" + (f" + photo `{p['image']}`" if p.get("image") else ""),
             f"- persona behaviour: {p['behaviour']}", "",
             "## Hidden facts (the agent did not see these unless the traveller said them)", "",
             "```json", json.dumps(p["hidden"]["facts"], ensure_ascii=False, indent=2), "```", "",
             "Nice to have: " + ("; ".join(p["hidden"].get("nice") or []) or "—"), "",
             "## Must predicates (checked by code)", ""]
    for m in summary["must"]:
        lines.append(f"- {'OK  ' if m['ok'] else 'FAIL'} {m['name']} {json.dumps(m['args'], ensure_ascii=False) if m['args'] else ''} — {m['detail']}")
    lines += ["", "## Key numbers", "",
              f"- end: {summary['end_reason']}; paid: {summary['paid']}; turns: {summary['turns']}",
              f"- first reply had a recommendation: {summary['first_rec']}",
              f"- touches to a ready cart: {summary['touches_to_cart'] if summary['touches_to_cart'] is not None else 'never'}",
              f"- final total: {rub(summary['final']['total_rub'])}; chosen variant: {summary['final']['chosen_variant']}",
              f"- connection errors: {summary['connection_errors']} ({summary['connection_errors_source']})",
              f"- model calls per turn: {summary['calls_per_turn']}; fallback-card turns: {summary['fallback_turns']}",
              "", "## Transcript (as the traveller saw it)", ""]
    for i, t in enumerate(transcript):
        text = t["text"]
        if t["role"] == "agent":
            text = re.sub(r"\[(?:Q\d+\.\d+|C\d+\.\d+|D\d+|V\d+|A\d+)\] ", "", text)
        lines += [text, ""]
    path = Path(run_dir) / "judge" / f"{summary['packet']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def summarize_level3(rows):
    n = len(rows)
    must_total = sum(r["must_total"] for r in rows)
    lat = [x for r in rows for x in r["latencies_ms"]]
    calls = [c for r in rows for c in r["calls_per_turn"] if c is not None]
    touches = [r["touches_to_cart"] for r in rows if r["touches_to_cart"] is not None]
    jev_calls = sum(r["jev_calls"] for r in rows)
    turns = sum(r["turns"] for r in rows)
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    agg = lambda key: dict(sum((Counter(r[key]) for r in rows), Counter()))  # noqa: E731
    return {
        "scenarios": n,
        "completion_rate": sum(1 for r in rows if r["paid"]) / n if n else None,
        "first_rec_rate": sum(1 for r in rows if r["first_rec"]) / n if n else None,
        "reached_cart_rate": len(touches) / n if n else None,
        "touches_to_cart_mean": mean(touches),
        "touches_to_cart_p50": percentile(touches, 50),
        "touches_to_cart_max": max(touches) if touches else None,
        "hidden_must_rate": sum(r["must_ok"] for r in rows) / must_total if must_total else None,
        "scenarios_all_must_rate": sum(1 for r in rows if r["must_ok"] == r["must_total"]) / n if n else None,
        "connection_errors_total": sum(r["connection_errors"] for r in rows),
        "calls_per_turn_mean": mean(calls),
        "calls_per_turn_max": max(calls) if calls else None,
        "latency_p50_ms": percentile(lat, 50),
        "latency_p95_ms": percentile(lat, 95),
        "cost_usd_total": sum(costs) if costs else None,
        "persona_cost_usd_total": round(sum(r["persona_cost_usd"] for r in rows), 6),
        "jev_calls": jev_calls,
        "jev_fallback_rate": sum(r["jev_fallbacks"] for r in rows) / jev_calls if jev_calls else None,
        "fallback_card_rate": sum(r["fallback_turns"] for r in rows) / turns if turns else None,
        "response_schema_error_turns": sum(r["schema_error_turns"] for r in rows),
        "degraded_turns": sum(r["degraded_turns"] for r in rows),
        "error_response_turns": sum(r["error_turns"] for r in rows),
        "end_reasons": dict(Counter(r["end_reason"] for r in rows)),
        "profiles_injected": sum(1 for r in rows if r["profile_injected"]),
        "served_models": agg("served_models"),
        "served_providers": agg("served_providers"),
        "persona_models": agg("persona_models"),
    }


def make_live_engine(backend, flags, run_dir):
    os.environ.setdefault("OSTROVOK_STATE_DB", str(Path(run_dir) / "ostrovok_state.sqlite"))
    from agent.engine import Engine  # sibling (ENGINE-A)
    from agent.mcp_client import InProcessOstrovokClient
    from agent.profiles import ProfileStore
    from agent.sessions import SessionStore
    client = InProcessOstrovokClient()
    # eval sessions and personas never touch runtime/sessions or runtime/profiles
    profiles = ProfileStore(Path(run_dir) / "profiles.sqlite")
    store = SessionStore(Path(run_dir) / "sessions.sqlite")
    return Engine(backend=backend, ostrovok=client, flags=flags, profiles=profiles, store=store), client.call


def run_level3(scenarios, engine_factory, persona_factory, run_dir, set_name, runs=1, parallel=1, rules=None,
               max_turns=None, log=print):
    jobs = [(s, i) for s in scenarios for i in range(runs)]

    def work(job):
        scenario, i = job
        engine, lookup = engine_factory()
        persona = persona_factory(scenario)
        packet = scenario["id"] if runs == 1 else f"{scenario['id']}__r{i}"
        return run_scenario(scenario, i, engine, persona, run_dir, set_name, lookup=lookup, rules=rules,
                            max_turns=max_turns, packet=packet, log=log)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        rows = list(pool.map(work, jobs))
    return rows, summarize_level3(rows)


# ---------------------------------------------------------------- CLI

def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m evals.run")
    ap.add_argument("--level", type=int, choices=(2, 3), required=True)
    ap.add_argument("--set", dest="set_name", choices=("main", "holdout"), default="main")
    ap.add_argument("--runs", type=int, default=None, help="level 2: runs per case (default: case.runs); level 3: runs per scenario (default 1)")
    ap.add_argument("--flags", default="stage1", help="flag spec for the engine (agent.flags.parse_flags)")
    ap.add_argument("--model", choices=MODELS, default="flash")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--only", default=None, help="comma-separated case/scenario ids")
    ap.add_argument("--out", default=None, help="run dir (default evals/runs/<timestamp>)")
    ap.add_argument("--reports-dir", default=None, help="default evals/reports (dry run: <run dir>/reports)")
    ap.add_argument("--persona", choices=("claude", "buttons"), default="claude")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="offline: FakeBackend / FakeEngine / ButtonPersona")
    ap.add_argument("--temperature", default=None, help="level 2 experiment: card=value,... (understand=0.3,present=0.6)")
    ap.add_argument("--think-image", default=None, choices=("low", "high"), help="level 2: thinking for calls with an image")
    ap.add_argument("--think-long", default=None, help="level 2: CHARS:LEVEL thinking for long understand messages")
    ap.add_argument("--tag", default=None, help="suffix of the run dir and report name (experiment label)")
    return ap.parse_args(argv)


def main(argv=None, log=print):
    args = parse_args(argv)
    from agent.flags import parse_flags
    flags = parse_flags(args.flags)
    ts = timestamp()
    set_label = args.set_name if args.level == 3 else "cases"
    tag = f"-{args.tag}" if args.tag else ""
    run_dir = Path(args.out) if args.out else RUNS_DIR / f"{ts}-L{args.level}-{set_label}{tag}{'-dryrun' if args.dry_run else ''}"
    run_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = Path(args.reports_dir) if args.reports_dir else (run_dir / "reports" if args.dry_run else REPORTS_DIR)
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    notes = []
    model_label = "fake" if args.dry_run else args.model
    report = {"run_id": run_dir.name, "level": args.level, "set": args.set_name if args.level == 3 else "cases",
              "model": model_label, "flags": args.flags, "flags_enabled": list(flags.enabled()),
              "dry_run": args.dry_run, "persona": ("buttons" if args.dry_run else args.persona) if args.level == 3 else None,
              "started_at": started, "run_dir": str(run_dir), "argv": sys.argv[1:] if argv is None else list(argv),
              "policy": {k: list(v) if isinstance(v, tuple) else v for k, v in parse_policy(args).items()},
              "tag": args.tag}
    if args.model == "qwen" and not args.dry_run:
        notes.append("qwen: no provider pin (plan does not fix one); served provider recorded per call")
    if args.level == 2:
        cases = load_cases(args.only)
        skill, note = load_skill_for_run(args.dry_run)
        if note:
            notes.append(note)
        if args.dry_run:
            from .testing.fakes import dry_run_backend
            backend = dry_run_backend(CARDS)
        else:
            backend = make_backend(args.model)
        log(f"level 2: {len(cases)} cases -> {run_dir}")
        rows, metrics = run_level2(cases, backend, skill, run_dir, runs=args.runs, parallel=args.parallel, log=log,
                                   policy=parse_policy(args))
        report.update(cases=rows, metrics=metrics)
    else:
        scenarios = load_scenarios(args.set_name, args.only)
        rules = None
        try:
            from agent.skill_loader import load_skill
            rules = load_skill().rules
        except Exception as exc:
            notes.append(f"skill rules not loaded for checks: {exc}")
        if args.dry_run:
            from .persona import ButtonPersona
            from .testing.fakes import FakeEngine
            engine_factory = lambda: (FakeEngine(), None)  # noqa: E731
            persona_factory = lambda s: ButtonPersona()  # noqa: E731
        else:
            backend = make_backend(args.model)
            engine_factory = lambda: make_live_engine(backend, flags, run_dir)  # noqa: E731
            from .persona import make_persona
            persona_factory = lambda s: make_persona(args.persona, claude_bin=args.claude_bin)  # noqa: E731
        log(f"level 3 ({args.set_name}): {len(scenarios)} scenarios -> {run_dir}")
        rows, metrics = run_level3(scenarios, engine_factory, persona_factory, run_dir, args.set_name,
                                   runs=args.runs or 1, parallel=args.parallel, rules=rules,
                                   max_turns=args.max_turns, log=log)
        report.update(scenarios=rows, metrics=metrics)
        if metrics["profiles_injected"] < metrics["scenarios"]:
            notes.append("scenario profiles were not injected into the engine for some scenarios "
                         "(engine has no profiles.put/save/upsert/set); home city came from engine defaults")
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    report["notes"] = notes
    stem = f"{ts}-L{args.level}-{report['set']}{tag}" + ("-dryrun" if args.dry_run else "")
    jpath, mpath = write_report(report, reports_dir, stem)
    write_json(run_dir / "summary.json", report)
    log(f"report: {jpath}\nreport: {mpath}")
    for line in report["thresholds"]:
        log(f"{'PASS' if line['pass'] else 'FAIL'} {line['label']}: {line['value']}")
    return report


if __name__ == "__main__":
    main()
