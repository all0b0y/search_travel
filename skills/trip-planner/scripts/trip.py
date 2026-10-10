#!/usr/bin/env python3
"""trip-planner helper.

  trip.py validate <response.json>          check a reply against the response schema
  trip.py finalize <draft.json> [--out DIR] [--final]
                                            compute rubles, totals, risks, reminders;
                                            write itinerary.json and one .ics per variant;
                                            --final also requires the judge's judge.json
  trip.py event <itinerary.json> <event.json> --out <draft.json>
                                            apply an on-the-way event to the chosen variant

validate and finalize print {"ok": bool, "problems": [...]} and exit 1 when problems remain;
event prints {"ok": bool, "changes": [...]}, ok false while a step still needs a replacement.
"""
import argparse
import html
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

def parties(variant, brief):
    """Each door-to-door chain of a variant: one for a single origin, one per party for a group."""
    if "parties" not in variant:
        return [{"id": None, "origin": brief.get("origin"), "steps": variant.get("steps", [])}]
    origins = {p["id"]: p["origin"] for p in brief.get("parties", [])}
    return [{"id": p["id"], "origin": origins.get(p["id"]), "steps": p["steps"]} for p in variant["parties"]]


def all_steps(variant):
    for chain in variant.get("parties", [variant]):
        yield from chain.get("steps", [])


def fill_prices(variant, rates, rules):
    found = []
    for step in walk(all_steps(variant)):
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


def norm(place):
    return place.strip().casefold()


def check_chain(steps, origin, vid):
    found = []
    if not steps:
        return [problem("chain", "no steps", vid)]
    if origin is None:
        return [problem("chain", "party has no origin in brief.parties", vid, steps[0]["id"])]
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


def check_timing(steps, vid, buffers, rules):
    found = []
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


def stay_index(steps):
    return next((i for i, s in enumerate(steps) if s["type"] == "stay"), None)


def check_dates(steps, vid, brief):
    found, stay = [], stay_index(steps)
    if stay is None:
        return found
    legs = (("depart", steps[:stay]), ("return", steps[stay + 1:]))
    for key, part in legs:
        first = next((s for s in part if s["type"] in TRANSPORT), None)
        if key in brief and first and not in_window(ts(first["start"]).date(), brief[key]):
            found.append(problem("brief", f"{key} on {ts(first['start']).date()} is outside {brief[key]}", vid, first["id"]))
    return found


def check_meeting(chains, vid, rules):
    """A group meets at one stay, every party arriving within the meeting window."""
    found, arrivals, stays = [], {}, set()
    for chain in chains:
        i = stay_index(chain["steps"])
        if i:
            stays.add(norm(chain["steps"][i]["to"]))
            arrivals[chain["id"]] = ts(chain["steps"][i - 1]["end"])
    if len(stays) > 1:
        found.append(problem("meeting", f"parties stay at different places: {sorted(stays)}", vid))
    if arrivals:
        spread = minutes(min(arrivals.values()), max(arrivals.values()))
        if spread > rules["meeting_window_min"]:
            found.append(problem("meeting", f"arrivals spread over {spread:.0f} min, allowed {rules['meeting_window_min']}", vid))
    return found


def travel_hours(steps):
    return sum(minutes(ts(s["start"]), ts(s["end"])) for s in steps if s["type"] != "stay") / 60


def totals(variant, brief):
    chains = parties(variant, brief)
    rub = sum(s["price"].get("rub", 0) for s in all_steps(variant))
    hours = [travel_hours(c["steps"]) for c in chains]
    result = {"rub": rub, "hours": round(max(hours), 1)}
    if "value_of_hour_rub" in brief:
        result["effective_rub"] = round(rub + sum(hours) * brief["value_of_hour_rub"])
    return result


def reminders(variant, brief, rules):
    out = []
    for chain in parties(variant, brief):
        for step in chain["steps"]:
            conf = rules["reminders"][step["type"]]
            start = ts(step["start"])
            fields = defaultdict(str, step, time=start.strftime("%H:%M"))
            reminder = {
                "at": (start - timedelta(minutes=conf["lead_min"])).isoformat(),
                "step_id": step["id"],
                "text": " ".join(conf["text"].format_map(fields).split()),
            }
            if chain["id"]:
                reminder["party"] = chain["id"]
            out.append(reminder)
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


# --- on the way ---

def shift(step, delta):
    step["start"] = (ts(step["start"]) + delta).isoformat()
    step["end"] = (ts(step["end"]) + delta).isoformat()


def cascade(steps, i, buffers, rules, changes):
    """Re-time what follows steps[i] after it moved later."""
    for j in range(i + 1, len(steps)):
        a, b = steps[j - 1], steps[j]
        need = required_gap(a, b, buffers)
        late = need - minutes(ts(a["end"]), ts(b["start"]))
        if late <= 0:
            return
        if b["type"] == "stay":
            arrival = ts(a["end"]).time().strftime("%H:%M")
            late_from, late_to = rules["late_checkin"]
            if arrival >= late_from or arrival < late_to:
                changes.append({"step": b["id"], "change": "late_checkin", "detail": f"прибытие в {arrival}"})
            return
        if b["type"] in ("taxi", "transfer"):
            shift(b, timedelta(minutes=late))
            b["status"] = "changed"
            changes.append({"step": b["id"], "change": "shifted", "detail": f"на {late:.0f} мин позже, теперь в {ts(b['start']).strftime('%H:%M')}"})
            continue
        backup = b.get("plan_b", {}).get("step")
        if backup and minutes(ts(a["end"]), ts(backup["start"])) >= need:
            steps[j] = dict(backup, status="changed")
            changes.append({"step": b["id"], "change": "plan_b", "detail": f"запасной вариант в {ts(backup['start']).strftime('%H:%M')}"})
            continue
        b["status"] = "changed"
        changes.append({"step": b["id"], "change": "missed", "detail": f"не хватает {late:.0f} мин"})
        return


def apply_event(doc, event):
    rules = load(RULES_PATH)
    buffers = {**rules["buffers_min"], **doc.get("brief", {}).get("buffers_override", {})}
    variant = next(v for v in doc["variants"] if v["id"] == doc["chosen_variant"])
    steps = next(c["steps"] for c in parties(variant, doc["brief"]) if any(s["id"] == event["step_id"] for s in c["steps"]))
    i = next(k for k, s in enumerate(steps) if s["id"] == event["step_id"])
    changes = []
    if event["type"] == "step_done":
        for s in steps[: i + 1]:
            if s["status"] != "done":
                s["status"] = "done"
                changes.append({"step": s["id"], "change": "done"})
    elif event["type"] == "delay":
        shift(steps[i], timedelta(minutes=event["delay_min"]))
        steps[i]["status"] = "changed"
        changes.append({"step": steps[i]["id"], "change": "delayed", "detail": f"на {event['delay_min']} мин, теперь в {ts(steps[i]['start']).strftime('%H:%M')}"})
        cascade(steps, i, buffers, rules, changes)
    else:
        backup = steps[i].get("plan_b", {}).get("step")
        if backup:
            steps[i] = dict(backup, status="changed")
            changes.append({"step": event["step_id"], "change": "plan_b", "detail": f"запасной вариант в {ts(backup['start']).strftime('%H:%M')}"})
            cascade(steps, i, buffers, rules, changes)
        else:
            steps[i]["status"] = "changed"
            changes.append({"step": event["step_id"], "change": "cancelled"})
    doc["changes"] = changes
    return {"ok": not any(c["change"] in ("missed", "cancelled") for c in changes), "changes": changes}


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


def calendar(trip_id, variant, steps, rules):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    notes = {r["step_id"]: r["text"] for r in variant["reminders"]}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//trip-planner//RU", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for step in steps:
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


# --- page ---

LABELS = {"cheapest": "Дешевле", "fastest": "Быстрее", "comfort": "Комфортнее"}
ICONS = {"taxi": "🚕", "flight": "✈️", "train": "🚆", "transfer": "🚐", "stay": "🏨"}
ACTIONS = {"taxi": "Вызвать такси", "stay": "Забронировать жильё"}
PAGE_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e3e1da;--accent:#0b6e4f;--warn:#a1520f}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1c;--ink:#ecebe6;--muted:#a3a29c;--line:#34332f;--accent:#5cc49b;--warn:#f0a35e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 48px}h1{font-size:26px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.totals{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;flex:1 1 140px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted);font-size:13px}
ol{list-style:none;margin:0;padding:0;border-left:2px solid var(--line);margin-left:10px}
li{position:relative;margin:0 0 16px;padding-left:22px}li:before{content:"";position:absolute;left:-7px;top:22px;width:12px;height:12px;border-radius:50%;background:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px}.when{color:var(--muted);font-size:14px}
.what{font-weight:600;margin:2px 0}.row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-top:6px}
a.btn{background:var(--accent);color:var(--bg);text-decoration:none;border-radius:8px;padding:6px 12px;font-size:14px}
.note{color:var(--warn);font-size:14px;margin-top:6px}.foot{color:var(--muted);font-size:13px;margin-top:24px}
"""


def rub_text(value):
    return "? ₽" if value is None else f"{round(value):,} ₽".replace(",", " ")


def page(doc, variant, steps, party=None):
    esc = html.escape
    when = lambda s: ts(s).strftime("%d.%m %H:%M")
    totals_ = variant.get("totals", {})
    tiles = [(rub_text(totals_.get("rub")), "от двери до двери"), (f"{totals_.get('hours', 0)} ч".replace(".", ","), "в пути")]
    if "effective_rub" in totals_:
        tiles.append((rub_text(totals_["effective_rub"]), "с учётом цены вашего времени"))
    items = []
    for s in steps:
        price = s["price"]
        cost = f"{'≈ ' if price.get('estimate') else ''}{rub_text(price.get('rub'))} · {price['amount']} {esc(price['currency'])}"
        notes = []
        if s.get("risk", {}).get("risky"):
            notes.append(f"Короткая стыковка: запас {s['risk']['slack_min']} мин")
        if "plan_b" in s:
            b = s["plan_b"]
            notes.append(f"План Б — {esc(b['trigger'])}: {esc(b['step']['from'])} → {esc(b['step']['to'])}, {when(b['step']['start'])}")
        title = esc(s["to"]) if s["type"] == "stay" else f"{esc(s['from'])} → {esc(s['to'])}"
        carrier = f" · {esc(s['carrier'])}" if s.get("carrier") else ""
        items.append(
            f'<li><div class="card"><div class="when">{when(s["start"])} — {when(s["end"])}</div>'
            f'<div class="what">{ICONS[s["type"]]} {TITLES[s["type"]]}: {title}{carrier}</div>'
            f'<div class="row"><span>{cost}</span><a class="btn" href="{esc(s["link"], quote=True)}">{ACTIONS.get(s["type"], "Забронировать")}</a></div>'
            + "".join(f'<div class="note">{n}</div>' for n in notes)
            + "</div></li>"
        )
    brief = doc.get("brief", {})
    heading = esc(brief.get("destination", doc.get("trip_id", "Поездка")))
    sub = f"{LABELS[variant['label']]} · {esc(variant['tradeoff'])}" + (f" · группа {esc(party)}" if party else "")
    return (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{heading} — маршрут</title><style>{PAGE_CSS}</style></head><body><main>"
        f'<h1>{heading}</h1><p class="sub">{sub}</p>'
        f'<div class="totals">{"".join(f"<div class=tile><b>{v}</b><span>{k}</span></div>" for v, k in tiles)}</div>'
        f'<ol>{"".join(items)}</ol>'
        f'<p class="foot">Цены проверены на дату в договоре и могут измениться. Курс: {esc(doc.get("rates", {}).get("source", ""))} на {esc(doc.get("rates", {}).get("date", ""))}.</p>'
        "</main></body></html>\n"
    )


# --- commands ---

def check_judge(doc, out_dir):
    path = out_dir / "judge.json"
    if not path.exists():
        return [problem("judge", f"no verdict: save the judge's reply to {path}")]
    if not load(path).get("pass") and not doc.get("unmet_checks"):
        return [problem("judge", "judge failed: fix the variants, or after 2 rework rounds list the failures in unmet_checks")]
    return []


def finalize(draft_path, out_dir=None, final=False):
    doc, rules = load(draft_path), load(RULES_PATH)
    out_dir = Path(out_dir) if out_dir else Path(draft_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    on_the_way = "chosen_variant" in doc
    doc["type"] = "update" if on_the_way else "itinerary"
    found = validate_doc(doc)
    found += [problem("schema", f"$: an itinerary needs {key}") for key in ("brief", "rates", "variants") if key not in doc]
    if found:  # the checks below rely on the schema's required fields
        return {"ok": False, "problems": found}
    brief = doc.get("brief", {})
    buffers = {**rules["buffers_min"], **brief.get("buffers_override", {})}
    rates = doc.get("rates", {}).get("rub_per_unit", {})
    for variant in doc.get("variants", []):
        vid, chains = variant["id"], parties(variant, brief)
        if "steps" not in variant and "parties" not in variant:
            found.append(problem("schema", "variant needs steps or parties", vid))
            continue
        found += fill_prices(variant, rates, rules)
        for chain in chains:
            found += check_chain(chain["steps"], chain["origin"], vid)
            if chain["steps"]:
                found += check_timing(chain["steps"], vid, buffers, rules)
                found += check_dates(chain["steps"], vid, brief)
        if len(chains) > 1:
            found += check_meeting(chains, vid, rules)
        variant["totals"] = totals(variant, brief)
        variant["reminders"] = reminders(variant, brief, rules)
        if variant["totals"]["rub"] > brief["budget_rub"]:
            found.append(problem("brief", f"{variant['totals']['rub']} ₽ is over the {brief['budget_rub']} ₽ budget", variant["id"]))
    if not on_the_way:  # once the user travels, the variants no longer compete
        found += check_variants(doc.get("variants", []))
        if final:
            found += check_judge(doc, out_dir)
    trip_id = doc.get("trip_id", out_dir.name)
    calendars, pages = {}, {}
    for variant in doc.get("variants", []):
        if "reminders" not in variant:
            continue
        for chain in parties(variant, brief):
            key = f"{variant['id']}-{chain['id']}" if chain["id"] else variant["id"]
            path = out_dir / f"trip-{key}.ics"
            path.write_text(calendar(trip_id, variant, chain["steps"], rules), encoding="utf-8", newline="")
            calendars[key] = str(path)
            path = out_dir / f"trip-{key}.html"
            path.write_text(page(doc, variant, chain["steps"], chain["id"]), encoding="utf-8")
            pages[key] = str(path)
    itinerary = out_dir / "itinerary.json"
    doc["files"] = {"itinerary": str(itinerary), "calendars": calendars, "pages": pages}
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
    fin.add_argument("--final", action="store_true", help="the reply is about to be sent: require the judge's verdict")
    ev = sub.add_parser("event")
    ev.add_argument("itinerary")
    ev.add_argument("event")
    ev.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        found = validate_doc(load(args.file))
        report = {"ok": not found, "problems": found}
    elif args.command == "event":
        doc = load(args.itinerary)
        report = apply_event(doc, load(args.event))
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        report = finalize(args.file, args.out, args.final)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
