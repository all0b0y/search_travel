"""Calendar (.ics) and a standalone route page per variant (ported from trip-planner trip.py).

    files = render_variant(session_id, variant, brief, rules, now=session.clock())
    # -> {"ics": "/files/<session>/trip-cheapest.ics", "page": "/files/<session>/trip-cheapest.html"}

Files go to runtime/files/<session>/ (served by agent/server.py at GET /files/<path>); the returned
URLs are relative to the server root and fit Response `files`. The page is one HTML file with inline
CSS (light and dark via prefers-color-scheme), no scripts and no external assets. `now` pins DTSTAMP
and the page footer so outputs are deterministic in tests and evals.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .checks import fmt_hours, fmt_rub, ts

REPO = Path(__file__).resolve().parent.parent
TITLES = {"taxi": "Такси", "flight": "Рейс", "train": "Поезд", "transfer": "Трансфер", "stay": "Отель"}
ICONS = {"taxi": "🚕", "flight": "✈️", "train": "🚆", "transfer": "🚐", "stay": "🏨"}
LABELS = {"cheapest": "Дешевле", "fastest": "Быстрее", "comfort": "Комфортнее"}
# Used only when rules.json has no "reminders" (same values as trip-planner's rules.json).
DEFAULT_REMINDERS = {
    "taxi": {"lead_min": 10, "text": "Пора вызывать такси: {from} → {to}, выезд в {time}"},
    "transfer": {"lead_min": 30, "text": "Трансфер {from} → {to} в {time}"},
    "flight": {"lead_min": 60, "text": "Рейс {carrier} {from} → {to}, вылет в {time}"},
    "train": {"lead_min": 20, "text": "Поезд {carrier} {from} → {to}, отправление в {time}"},
    "stay": {"lead_min": 60, "text": "Заселение в {to} с {time}"},
}
PAGE_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e3e1da;--accent:#0b6e4f;--warn:#a1520f}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1c;--ink:#ecebe6;--muted:#a3a29c;--line:#34332f;--accent:#5cc49b;--warn:#f0a35e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 48px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:24px 0 8px}.sub{color:var(--muted);margin:0 0 20px}
.totals{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;flex:1 1 140px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted);font-size:13px}
ol{list-style:none;margin:0;padding:0;border-left:2px solid var(--line);margin-left:10px}
li{position:relative;margin:0 0 16px;padding-left:22px}li:before{content:"";position:absolute;left:-7px;top:22px;width:12px;height:12px;border-radius:50%;background:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px}.when{color:var(--muted);font-size:14px}
.what{font-weight:600;margin:2px 0}.row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-top:6px}
.id{color:var(--muted);font-size:12px}.note{color:var(--warn);font-size:14px;margin-top:6px}ul.notes{margin:0;padding-left:20px}
.foot{color:var(--muted);font-size:13px;margin-top:24px}
"""


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))


def _variant(variant):
    from .state import Variant
    return variant if isinstance(variant, Variant) else Variant.model_validate(variant)


def _reminders(rules):
    return {**DEFAULT_REMINDERS, **((rules or {}).get("reminders") or {})}


# ---------------------------------------------------------------- ics (ported)

def ics_time(text):
    return ts(text).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics_text(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


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


def reminder_text(step, rules):
    conf = _reminders(rules)[step["type"]]
    fields = defaultdict(str, step, time=ts(step["start"]).strftime("%H:%M"))
    return " ".join(conf["text"].format_map(fields).split())


def calendar(trip_id, variant, rules=None, now=None):
    v = _variant(variant)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reminders = _reminders(rules)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//trip-agent//RU", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for s in v.steps:
        step = s.to_check_dict()
        cost = fmt_rub(s.price.rub if s.price.rub is not None else s.price.amount)
        desc = [s.title or TITLES[s.type], cost]
        if s.carrier:
            desc.append(s.carrier)
        if s.risk and s.risk.get("risky"):
            desc.append(f"Короткая стыковка: запас {s.risk['slack_min']} мин")
        if s.offer_id:
            desc.append(f"Предложение {s.offer_id}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_safe(trip_id)}-{v.id}-{s.id}@trip-agent",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{ics_time(s.start)}",
            f"DTEND:{ics_time(s.end)}",
            f"SUMMARY:{ics_text(TITLES[s.type] + ': ' + s.from_ + ' → ' + s.to)}",
            f"LOCATION:{ics_text(s.from_)}",
            f"DESCRIPTION:{ics_text(chr(10).join(desc))}",
        ]
        if s.link.startswith(("http://", "https://")):
            lines.append(f"URL:{s.link}")
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{reminders[s.type]['lead_min']}M",
            f"DESCRIPTION:{ics_text(reminder_text(step, rules))}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


# ---------------------------------------------------------------- page (ported)

def page(variant, title=None, subtitle=None, now=None, rates=None):
    v = _variant(variant)
    esc = html.escape
    when = lambda t: ts(t).strftime("%d.%m %H:%M")
    heading = esc(title or v.title or "Поездка")
    tiles = [(fmt_rub(v.totals.rub), "итого")]
    if v.totals.door_to_door_hours:
        tiles.append((f"{fmt_hours(v.totals.door_to_door_hours)} ч", "в пути туда и обратно"))
    nights = v.facts.get("nights")
    if nights:
        tiles.append((str(nights), "ночей в отеле"))
    items = []
    for s in v.steps:
        notes = []
        if s.risk and s.risk.get("risky"):
            notes.append(f"Короткая стыковка: запас {s.risk['slack_min']} мин")
        what = esc(s.to) if s.type == "stay" else f"{esc(s.from_)} → {esc(s.to)}"
        carrier = f" · {esc(s.carrier)}" if s.carrier else ""
        label = esc(s.title) if s.title else TITLES[s.type]
        oid = f'<span class="id">{esc(s.offer_id)}</span>' if s.offer_id else ""
        items.append(
            f'<li><div class="card"><div class="when">{when(s.start)} — {when(s.end)}</div>'
            f'<div class="what">{ICONS[s.type]} {label}: {what}{carrier}</div>'
            f'<div class="row"><span>{esc(fmt_rub(s.price.rub if s.price.rub is not None else s.price.amount))}</span>{oid}</div>'
            + "".join(f'<div class="note">{esc(n)}</div>' for n in notes)
            + "</div></li>")
    sections = []
    for name, notes in (("Чем жертвуете", v.sacrifices), ("Риски", v.risks), ("Кому не подойдёт", v.not_for)):
        if notes:
            sections.append(f"<h2>{name}</h2><ul class=\"notes\">" + "".join(f"<li>{esc(n.text)}</li>" for n in notes) + "</ul>")
    sub = esc(subtitle or LABELS.get(v.id, v.title))
    rate_note = ""
    if rates and rates.get("date"):
        rate_note = f" Курс ЦБ на {esc(rates['date'])}."
    stamp = (now or datetime.now(timezone.utc)).strftime("%d.%m.%Y %H:%M")
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{heading} — маршрут</title><style>{PAGE_CSS}</style></head><body><main>"
        f'<h1>{heading}</h1><p class="sub">{sub}</p>'
        f'<div class="totals">{"".join(f"<div class=tile><b>{esc(val)}</b><span>{esc(k)}</span></div>" for val, k in tiles)}</div>'
        f'<ol>{"".join(items)}</ol>' + "".join(sections) +
        f'<p class="foot">Демо: цены, наличие и расписание — синтетические данные мока Островка; места и отели — из открытых данных.{rate_note} Сформировано {esc(stamp)}.</p>'
        "</main></body></html>\n")


# ---------------------------------------------------------------- files

def render_variant(session_id, variant, brief=None, rules=None, out_root="runtime/files", now=None, rates=None,
                   url_prefix="/files"):
    """Write trip-<variant>.ics and trip-<variant>.html under <out_root>/<session>/ -> {"ics", "page"} URLs."""
    v = _variant(variant)
    root = Path(out_root)
    if not root.is_absolute():
        root = REPO / root
    folder = root / _safe(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    dest = None
    if brief is not None and brief.destination.value is not None:
        dest = brief.destination.value.name
    title = f"{dest} — {LABELS.get(v.id, v.title)}" if dest else (v.title or "Поездка")
    ics_name, page_name = f"trip-{_safe(v.id)}.ics", f"trip-{_safe(v.id)}.html"
    (folder / ics_name).write_text(calendar(session_id, v, rules, now), encoding="utf-8", newline="")
    (folder / page_name).write_text(page(v, title=title, now=now, rates=rates), encoding="utf-8")
    base = f"{url_prefix.rstrip('/')}/{_safe(session_id)}"
    return {"ics": f"{base}/{ics_name}", "page": f"{base}/{page_name}"}


def render_all(session_id, variants, brief=None, rules=None, out_root="runtime/files", now=None, rates=None):
    """render_variant for each variant -> {variant_id: {"ics", "page"}}."""
    return {(_variant(v)).id: render_variant(session_id, v, brief, rules, out_root, now, rates) for v in variants}


__all__ = ["calendar", "page", "render_variant", "render_all", "reminder_text", "ics_fold", "ics_text", "ics_time"]
