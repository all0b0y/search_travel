"""Eval reports: write, merge judge verdicts, compare runs.

    python -m evals.report --merge-judge evals/runs/<ts>
    python -m evals.report --compare evals/reports/<A>.json evals/reports/<B>.json

Thresholds are decision 16 of AGENT_PLAN.md. "Three green runs in a row" is the orchestrator's
call over successive reports; this module only marks each run green or not.
"""
import argparse
import json
import sys
from pathlib import Path

import jsonschema

from .common import mean, read_json, write_json, load_schema

VERDICT_SCORES = ("understanding", "questions_economy", "recommendation_quality", "explanation_honesty", "tone",
                  "upsell_relevance", "hidden_requirements")

# (metric, op, threshold, label)
THRESHOLDS = {
    2: [("cases_full_pass_rate", ">=", 1.0, "фазовые кейсы 5/5 (каждый кейс прошёл все прогоны)")],
    3: [("first_rec_rate", ">=", 0.8, "первая рекомендация в первом ответе ≥ 80 %"),
        ("touches_to_cart_mean", "<=", 4.0, "касаний до готовой корзины ≤ 4 (среднее)"),
        ("hidden_must_rate", ">=", 0.9, "скрытые требования персон ≥ 90 %"),
        ("connection_errors_total", "<=", 0, "ошибки стыковок = 0")],
}
HOLDOUT_GAP_PP = 10.0
HOLDOUT_METRICS = ("completion_rate", "first_rec_rate", "hidden_must_rate")


def _cmp(value, op, threshold):
    if value is None:
        return False
    return value >= threshold if op == ">=" else value <= threshold


def threshold_lines(report):
    lines = []
    for metric, op, thr, label in THRESHOLDS.get(report.get("level"), []):
        value = report.get("metrics", {}).get(metric)
        lines.append({"metric": metric, "label": label, "value": value, "op": op, "threshold": thr,
                      "pass": _cmp(value, op, thr)})
    return lines


def is_green(report):
    lines = threshold_lines(report)
    ok = all(line["pass"] for line in lines)
    if report.get("level") == 3 and report.get("judge"):
        ok = ok and report["judge"].get("pass_rate") == 1.0
    return ok


def _fmt(v):
    if v is None:
        return "нет данных"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    if isinstance(v, dict):
        return ", ".join(f"{k}: {n}" for k, n in v.items()) or "—"
    return str(v)


def to_markdown(report):
    lvl = report.get("level")
    m = report.get("metrics", {})
    out = [f"# Eval report {report.get('run_id')}", "",
           f"- level: {lvl}; set: {report.get('set')}; model: {report.get('model')}; flags: {report.get('flags')}"
           f"; dry run: {report.get('dry_run')}",
           f"- started: {report.get('started_at')}; finished: {report.get('finished_at')}",
           f"- run dir: `{report.get('run_dir')}`", ""]
    out += ["## Thresholds (decision 16)", ""]
    for line in threshold_lines(report):
        mark = "PASS" if line["pass"] else "FAIL"
        out.append(f"- {mark} — {line['label']}: {_fmt(line['value'])} (need {line['op']} {line['threshold']})")
    out += [f"- run is {'GREEN' if is_green(report) else 'NOT green'}", "", "## Metrics", ""]
    for k, v in m.items():
        out.append(f"- {k}: {_fmt(v)}")
    if lvl == 2:
        out += ["", "## Cases", "", "| case | category | card | pass | p50 ms | first failure |", "|---|---|---|---|---|---|"]
        for c in report.get("cases", []):
            fail = (c.get("failures") or [""])[0]
            out.append(f"| {c['id']} | {c['category']} | {c['card']} | {c['passed']}/{c['runs']} | "
                       f"{_fmt(c.get('latency_p50_ms'))} | {str(fail)[:120].replace('|', '/')} |")
    if lvl == 3:
        judge = report.get("judge") or {}
        out += ["", "## Scenarios", "",
                "| scenario | category | end | paid | first rec | touches | must | judge |", "|---|---|---|---|---|---|---|---|"]
        for s in report.get("scenarios", []):
            v = (judge.get("verdicts") or {}).get(s["packet"])
            jv = "—" if not v else ("pass" if v["pass"] else "FAIL")
            out.append(f"| {s['packet']} | {s['category']} | {s['end_reason']} | {s['paid']} | {s['first_rec']} | "
                       f"{_fmt(s['touches_to_cart'])} | {s['must_ok']}/{s['must_total']} | {jv} |")
        failed = [(s["packet"], r) for s in report.get("scenarios", []) for r in s.get("must", []) if not r["ok"]]
        if failed:
            out += ["", "### Failed must predicates", ""]
            out += [f"- {sid}: {r['name']} {r.get('args') or ''} — {r['detail']}" for sid, r in failed]
        if judge:
            out += ["", "## Judge", "", f"- verdicts: {judge.get('count')}; pass rate: {_fmt(judge.get('pass_rate'))}",
                    f"- mean scores: {_fmt(judge.get('mean_scores'))}"]
            for sid, v in sorted((judge.get("verdicts") or {}).items()):
                for f in v.get("failures", [])[:3]:
                    out.append(f"- {sid} turn {f['turn']} [{f['kind']}]: «{f['evidence'][:140]}» → {f['fix_hint'][:140]}")
    return "\n".join(out) + "\n"


def write_report(report, reports_dir, stem):
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    jpath, mpath = reports_dir / f"{stem}.json", reports_dir / f"{stem}.md"
    report["thresholds"] = threshold_lines(report)
    report["green"] = is_green(report)
    report["report_paths"] = {"json": str(jpath), "md": str(mpath)}
    write_json(jpath, report)
    mpath.write_text(to_markdown(report), encoding="utf-8")
    return jpath, mpath


# ---------------------------------------------------------------- judge merge

def load_verdicts(run_dir):
    schema = load_schema("verdict.schema.json")
    verdicts, errors = {}, {}
    for path in sorted((Path(run_dir) / "judge").glob("*.verdict.json")):
        stem = path.name[: -len(".verdict.json")]
        try:
            data = read_json(path)
            jsonschema.validate(data, schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            errors[stem] = str(exc).splitlines()[0]
            continue
        verdicts[stem] = data
    return verdicts, errors


def merge_judge(run_dir):
    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.json"
    report = read_json(summary_path)
    verdicts, errors = load_verdicts(run_dir)
    packets = [s["packet"] for s in report.get("scenarios", [])]
    missing = [p for p in packets if p not in verdicts]
    scores = {k: mean([v["scores"][k] for v in verdicts.values()]) for k in VERDICT_SCORES} if verdicts else {}
    report["judge"] = {
        "count": len(verdicts), "expected": len(packets), "missing": missing, "invalid": errors,
        "pass_rate": (sum(1 for v in verdicts.values() if v["pass"]) / len(verdicts)) if verdicts else None,
        "mean_scores": {k: round(v, 2) for k, v in scores.items() if v is not None},
        "verdicts": verdicts,
    }
    report["metrics"]["judge_pass_rate"] = report["judge"]["pass_rate"]
    paths = report.get("report_paths") or {}
    if paths.get("json"):
        stem = Path(paths["json"]).stem
        write_report(report, Path(paths["json"]).parent, stem)
    write_json(summary_path, report)
    return report


# ---------------------------------------------------------------- compare

def compare(a, b):
    lines = [f"A: {a.get('run_id')} (level {a.get('level')}, set {a.get('set')}, flags {a.get('flags')}, model {a.get('model')})",
             f"B: {b.get('run_id')} (level {b.get('level')}, set {b.get('set')}, flags {b.get('flags')}, model {b.get('model')})",
             "", f"{'metric':32} {'A':>12} {'B':>12} {'B-A':>12}"]
    ma, mb = a.get("metrics", {}), b.get("metrics", {})
    for k in list(dict.fromkeys(list(ma) + list(mb))):
        va, vb = ma.get(k), mb.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
            delta = f"{vb - va:+.3f}"
        else:
            delta = ""
        if isinstance(va, dict) or isinstance(vb, dict):
            lines.append(f"{k:32} {_fmt(va)} | {_fmt(vb)}")
        else:
            lines.append(f"{k:32} {_fmt(va):>12} {_fmt(vb):>12} {delta:>12}")
    for name, rep in (("A", a), ("B", b)):
        lines.append("")
        lines.append(f"Thresholds {name}:")
        for line in threshold_lines(rep):
            lines.append(f"  {'PASS' if line['pass'] else 'FAIL'} {line['label']}: {_fmt(line['value'])}")
        lines.append(f"  run is {'GREEN' if is_green(rep) else 'NOT green'}")
    sets = {a.get("set"), b.get("set")}
    if a.get("level") == 3 and b.get("level") == 3 and sets == {"main", "holdout"}:
        main, hold = (a, b) if a.get("set") == "main" else (b, a)
        lines += ["", f"Holdout vs main (holdout may be worse by at most {HOLDOUT_GAP_PP:g} p.p.):"]
        for k in HOLDOUT_METRICS:
            vm, vh = main["metrics"].get(k), hold["metrics"].get(k)
            if vm is None or vh is None:
                lines.append(f"  FAIL {k}: нет данных")
                continue
            gap = (vm - vh) * 100
            lines.append(f"  {'PASS' if gap <= HOLDOUT_GAP_PP else 'FAIL'} {k}: main {vm:.3f}, holdout {vh:.3f}, gap {gap:+.1f} p.p.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m evals.report")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--merge-judge", metavar="RUN_DIR")
    g.add_argument("--compare", nargs=2, metavar=("REPORT_A", "REPORT_B"))
    args = ap.parse_args(argv)
    if args.merge_judge:
        rep = merge_judge(args.merge_judge)
        j = rep["judge"]
        print(f"merged {j['count']}/{j['expected']} verdicts; pass rate {_fmt(j['pass_rate'])}; "
              f"missing {j['missing']}; invalid {list(j['invalid'])}")
        for line in threshold_lines(rep):
            print(f"{'PASS' if line['pass'] else 'FAIL'} {line['label']}: {_fmt(line['value'])}")
        print(f"report: {(rep.get('report_paths') or {}).get('md')}")
        return 0
    a, b = (read_json(p) for p in args.compare)
    print(compare(a, b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
