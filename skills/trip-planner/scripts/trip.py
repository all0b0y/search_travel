#!/usr/bin/env python3
"""trip-planner helper.

  trip.py validate <response.json>          check a reply against the response schema
  trip.py finalize <draft.json> [--out DIR] compute rubles, totals, risks, reminders;
                                            write itinerary.json and one .ics per variant

Both print {"ok": bool, "problems": [...]} and exit 1 when problems remain.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SKILL_DIR / "schema" / "response.schema.json"
RULES_PATH = SKILL_DIR / "rules.json"
TRANSPORT = {"flight", "train"}
TITLES = {"taxi": "Такси", "flight": "Рейс", "train": "Поезд", "transfer": "Трансфер", "stay": "Отель"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def problem(check, detail, variant=None, step=None):
    return {"check": check, "variant": variant, "step": step, "detail": detail}


# --- schema: the subset of JSON Schema the response schema uses ---

def _is_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"object": dict, "array": list, "string": str, "boolean": bool}[name])


def schema_errors(value, schema, root, path="$"):
    if "$ref" in schema:
        return schema_errors(value, root["$defs"][schema["$ref"].split("/")[-1]], root, path)
    if "type" in schema and not _is_type(value, schema["type"]):
        return [f"{path}: expected {schema['type']}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        errors += [f"{path}: missing {key}" for key in schema.get("required", []) if key not in value]
        props, extra = schema.get("properties", {}), schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                errors += schema_errors(item, props[key], root, f"{path}.{key}")
            elif isinstance(extra, dict):
                errors += schema_errors(item, extra, root, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if len(value) > schema.get("maxItems", len(value)):
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        for i, item in enumerate(value):
            if "items" in schema:
                errors += schema_errors(item, schema["items"], root, f"{path}[{i}]")
    if _is_type(value, "number") and value < schema.get("minimum", value):
        errors.append(f"{path}: below minimum {schema['minimum']}")
    return errors


def validate_doc(doc):
    schema = load(SCHEMA_PATH)
    return [problem("schema", e) for e in schema_errors(doc, schema, schema)]


# --- time helpers ---

def ts(text):
    return datetime.fromisoformat(text)


def minutes(a, b):
    return (b - a).total_seconds() / 60


def in_window(day, window):
    return date.fromisoformat(window["from"]) <= day <= date.fromisoformat(window["to"])


# --- finalize checks ---

def fill_prices(variant, rates, rules):
    found = []
    for step in walk(variant["steps"]):
        price = step["price"]
        if price["currency"] == "RUB":
            price["rub"] = round(price["amount"])
        elif price["currency"] in rates:
            price["rub"] = round(price["amount"] * rates[price["currency"]])
        else:
            found.append(problem("price", f"no rate for {price['currency']}", variant["id"], step["id"]))
        if not step["link"]:
            found.append(problem("price", "empty link", variant["id"], step["id"]))
        step["link"] = with_affiliate(step["link"], rules["affiliate"])
    return found


def walk(steps):
    for step in steps:
        yield step
        if "plan_b" in step:
            yield step["plan_b"]["step"]


def with_affiliate(link, affiliate):
    parts = urlsplit(link)
    for domain, conf in affiliate.items():
        if conf["value"] and (parts.netloc == domain or parts.netloc.endswith("." + domain)):
            query = dict(parse_qsl(parts.query))
            query[conf["param"]] = conf["value"]
            return urlunsplit(parts._replace(query=urlencode(query)))
    return link


def check_chain(variant, origin):
    steps, vid, found = variant["steps"], variant["id"], []
    norm = lambda s: s.strip().casefold()
    if norm(steps[0]["from"]) != norm(origin):
        found.append(problem("chain", f"starts at {steps[0]['from']!r}, brief origin is {origin!r}", vid, steps[0]["id"]))
    if norm(steps[-1]["to"]) != norm(origin):
        found.append(problem("chain", f"ends at {steps[-1]['to']!r}, brief origin is {origin!r}", vid, steps[-1]["id"]))
    for a, b in zip(steps, steps[1:]):
        if norm(a["to"]) != norm(b["from"]):
            found.append(problem("chain", f"{a['id']} ends at {a['to']!r}, next starts at {b['from']!r}", vid, b["id"]))
    if not any(s["type"] == "stay" for s in steps):
        found.append(problem("chain", "no stay", vid))
    return found


def required_gap(a, b, buffers):
    need = 0
    if a["type"] == "flight":
        need += buffers["after_flight_with_baggage" if a.get("checked_baggage") else "after_flight"]
    if b["type"] == "flight":
        need += buffers["before_flight_international" if b.get("international") else "before_flight_domestic"]
    if b["type"] == "train":
        need += buffers["before_train"]
    return need


def check_timing(variant, buffers, rules):
    steps, vid, found = variant["steps"], variant["id"], []
    for step in steps:
        length = minutes(ts(step["start"]), ts(step["end"]))
        if length <= 0:
            found.append(problem("timing", "ends before it starts", vid, step["id"]))
        estimate = step.get("duration_estimate_min")
        if step["type"] in ("taxi", "transfer") and estimate and length < estimate * rules["taxi_time_factor"]:
            found.append(problem("timing", f"{length:.0f} min planned, estimate {estimate} needs {estimate * rules['taxi_time_factor']:.0f}", vid, step["id"]))
    slack = []  # slack[i]: spare minutes between steps[i] and steps[i+1]
    for a, b in zip(steps, steps[1:]):
        gap, need = minutes(ts(a["end"]), ts(b["start"])), required_gap(a, b, buffers)
        slack.append(gap - need)
        if gap < 0:
            found.append(problem("timing", f"overlaps {a['id']} by {-gap:.0f} min", vid, b["id"]))
        elif gap < need:
            found.append(problem("timing", f"needs {need} min after {a['id']}, has {gap:.0f}", vid, b["id"]))
    # A flight or train reached from another flight or train (through taxis and transfers) is missable.
    for i, step in enumerate(steps):
        if step["type"] not in TRANSPORT:
            continue
        spare, j = 0, i - 1
        while j >= 0 and steps[j]["type"] in ("taxi", "transfer"):
            spare += slack[j]
            j -= 1
        if j < 0 or steps[j]["type"] not in TRANSPORT:
            continue
        spare += slack[j]
        risky = spare < rules["risk_slack_min"]
        step["risk"] = {"slack_min": round(spare), "risky": risky}
        if risky and "plan_b" not in step:
            found.append(problem("plan_b", f"risky connection ({spare:.0f} min spare) has no plan_b", vid, step["id"]))
    return found


def check_dates(variant, brief):
    steps, vid, found = variant["steps"], variant["id"], []
    stay = next((i for i, s in enumerate(steps) if s["type"] == "stay"), None)
    if stay is None:
        return found
    legs = (("depart", steps[:stay]), ("return", steps[stay + 1:]))
    for key, part in legs:
        first = next((s for s in part if s["type"] in TRANSPORT), None)
        if key in brief and first and not in_window(ts(first["start"]).date(), brief[key]):
            found.append(problem("brief", f"{key} on {ts(first['start']).date()} is outside {brief[key]}", vid, first["id"]))
    return found


def totals(variant, brief):
    steps = variant["steps"]
    rub = sum(s["price"].get("rub", 0) for s in steps)
    hours = round(sum(minutes(ts(s["start"]), ts(s["end"])) for s in steps if s["type"] != "stay") / 60, 1)
    result = {"rub": rub, "hours": hours}
    if "value_of_hour_rub" in brief:
        result["effective_rub"] = round(rub + hours * brief["value_of_hour_rub"])
    return result


def reminders(variant, rules):
    out = []
    for step in variant["steps"]:
        conf = rules["reminders"][step["type"]]
        start = ts(step["start"])
        fields = defaultdict(str, step, time=start.strftime("%H:%M"))
        out.append({
            "at": (start - timedelta(minutes=conf["lead_min"])).isoformat(),
            "step_id": step["id"],
            "text": " ".join(conf["text"].format_map(fields).split()),
        })
    return out


def check_variants(variants):
    found, labels = [], [v["label"] for v in variants]
    for label in set(labels):
        if labels.count(label) > 1:
            found.append(problem("variants", f"label {label} used twice"))
    for label, key in (("cheapest", "rub"), ("fastest", "hours")):
        best = next((v for v in variants if v["label"] == label), None)
        if best and any(v["totals"][key] < best["totals"][key] for v in variants):
            found.append(problem("variants", f"{label} variant is not the lowest by {key}", best["id"]))
    return found


# --- calendar ---

def ics_time(text):
    return ts(text).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics_text(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    parts, cur, limit = [], b"", 75
    for ch in line:
        enc = ch.encode("utf-8")
        if len(cur) + len(enc) > limit:
            parts.append(cur.decode("utf-8"))
            cur, limit = b"", 74
        cur += enc
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def calendar(trip_id, variant, rules):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    notes = {r["step_id"]: r["text"] for r in variant["reminders"]}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//trip-planner//RU", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for step in variant["steps"]:
        price = step["price"]
        cost = f"{'≈' if price.get('estimate') else ''}{price.get('rub', '?')} ₽ ({price['amount']} {price['currency']})"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{trip_id}-{variant['id']}-{step['id']}@trip-planner",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{ics_time(step['start'])}",
            f"DTEND:{ics_time(step['end'])}",
            f"SUMMARY:{ics_text(TITLES[step['type']] + ': ' + step['from'] + ' → ' + step['to'])}",
            f"LOCATION:{ics_text(step['from'])}",
            f"DESCRIPTION:{ics_text(cost + chr(10) + step['link'])}",
            f"URL:{step['link']}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{rules['reminders'][step['type']]['lead_min']}M",
            f"DESCRIPTION:{ics_text(notes[step['id']])}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


# --- commands ---

def finalize(draft_path, out_dir=None):
    doc, rules = load(draft_path), load(RULES_PATH)
    out_dir = Path(out_dir) if out_dir else Path(draft_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    doc["type"] = "itinerary"
    found = validate_doc(doc)
    found += [problem("schema", f"$: an itinerary needs {key}") for key in ("brief", "rates", "variants") if key not in doc]
    if found:  # the checks below rely on the schema's required fields
        return {"ok": False, "problems": found}
    brief = doc.get("brief", {})
    buffers = {**rules["buffers_min"], **brief.get("buffers_override", {})}
    rates = doc.get("rates", {}).get("rub_per_unit", {})
    for variant in doc.get("variants", []):
        found += fill_prices(variant, rates, rules)
        found += check_chain(variant, brief["origin"])
        found += check_timing(variant, buffers, rules)
        found += check_dates(variant, brief)
        variant["totals"] = totals(variant, brief)
        variant["reminders"] = reminders(variant, rules)
        if variant["totals"]["rub"] > brief["budget_rub"]:
            found.append(problem("brief", f"{variant['totals']['rub']} ₽ is over the {brief['budget_rub']} ₽ budget", variant["id"]))
    found += check_variants(doc.get("variants", []))
    trip_id = doc.get("trip_id", out_dir.name)
    calendars = {}
    for variant in doc.get("variants", []):
        path = out_dir / f"trip-{variant['id']}.ics"
        path.write_text(calendar(trip_id, variant, rules), encoding="utf-8", newline="")
        calendars[variant["id"]] = str(path)
    itinerary = out_dir / "itinerary.json"
    doc["files"] = {"itinerary": str(itinerary), "calendars": calendars}
    found += validate_doc(doc)
    itinerary.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": not found, "problems": found}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate").add_argument("file")
    fin = sub.add_parser("finalize")
    fin.add_argument("file")
    fin.add_argument("--out")
    args = parser.parse_args(argv)
    if args.command == "validate":
        found = validate_doc(load(args.file))
        report = {"ok": not found, "problems": found}
    else:
        report = finalize(args.file, args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
