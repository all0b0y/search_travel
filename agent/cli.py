"""Manual turns against the Engine, in-process (for the orchestrator's runs).

    python -m agent.cli [--profile demo] [--debug] [--flags stage3] [--image photo.jpg] "к морю в ноябре"
    python -m agent.cli [--profile demo] [--debug]          # interactive REPL

Options: --backend codex (strong-model check via CodexBackend, effort high), --fixtures (no engine: walk
the contract fixtures), --now 2026-10-24T12:00+03:00 (pin the clock), --json (print the raw Response).
REPL: type text to send it; a number triggers that numbered choice/action; `/image <path>` attaches a photo
to the next message; `/json` toggles raw output; `/action {...}` sends a raw action; `/new`; `/quit`.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
from datetime import datetime
from pathlib import Path

DISTRICT_RU = {"lively": "движ", "quiet": "спокойно", "family": "семейно", "walkable": "пешком"}
STEP_RU = {"taxi": "такси", "flight": "перелёт", "train": "поезд", "transfer": "трансфер", "stay": "отель"}
STAGE_RU = {"inspiration": "вдохновение", "selection": "подбор", "cart": "корзина", "checkout": "оформление",
            "ordered": "оплачено", "aftercare": "после покупки"}


def rub(n):
    return "—" if n is None else f"{int(round(n)):,}".replace(",", " ") + " ₽"


def hhmm(iso):
    if not iso or len(iso) < 16:
        return iso or "—"
    return f"{iso[8:10]}.{iso[5:7]} {iso[11:16]}"


def image_data_url(path):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        raise SystemExit(f"unsupported image type {mime}: use png, jpeg, webp or gif")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


# ---------------------------------------------------------------- rendering

def render(resp):
    """Response -> (text, numbered) where numbered[i-1] is the action dict number i triggers."""
    lines, numbered = [], []

    def number(action):
        numbered.append(action)
        return f"{len(numbered)})"

    lines.append(f"── ход {resp.get('turn')} · {STAGE_RU.get(resp.get('stage'), resp.get('stage'))}")
    lines.append(resp.get("message") or "")
    if resp.get("error"):
        err = resp["error"]
        lines.append(f"! ошибка {err.get('code')}" + (" (можно повторить)" if err.get("retryable") else ""))
    for q in resp.get("questions") or []:
        lines.append(f"? {q['text']}")
        for c in q["choices"]:
            action = {"type": "answer_question", "payload": {"question_id": q["id"], "value": c["value"]}}
            lines.append(f"   {number(action)} {c['label']}" + ("  ★ рекомендуем" if c.get("recommended") else ""))
    chips = resp.get("chips") or []
    if chips:
        lines.append("Допущения:")
        for chip in chips:
            others = []
            for opt in chip.get("options") or []:
                if opt["value"] == chip["value"]:
                    continue
                action = {"type": "set_chip", "payload": {"field": chip["field"], "value": opt["value"]}}
                others.append(f"{number(action)} {opt['label']}")
            lines.append(f"  [{chip['label']}]" + (" → " + ", ".join(others) if others else ""))
    cards = resp.get("cards") or {}
    for d in cards.get("destinations") or []:
        w = d.get("weather")
        weather = (f"воздух {w['air_max_c']}°, море {'—' if w['sea_c'] is None else str(w['sea_c']) + '°'}, "
                   f"дождь {w['rain_days']} дн.") if w else "нет данных о погоде"
        price = "цены нет" if d.get("price_from_rub") is None else "от " + rub(d["price_from_rub"])
        travel = "—" if d.get("travel_time_h") is None else f"{d['travel_time_h']} ч"
        lines.append(f"• {d['label']}. {d['name']}, {d['country']} — {price} · {d['nights']} ноч. · {d['party_label']}"
                     f" · в пути {travel} · {weather}")
        lines.append(f"    {d['why']}")
        if d.get("watch_out"):
            lines.append(f"    ⚠ {d['watch_out']}")
    for v in cards.get("variants") or []:
        mark = " ★" if v.get("recommended") else ""
        lines.append(f"• {v['label']}. «{v['title']}»{mark} — {rub(v['totals']['rub'])}, "
                     f"дверь-дверь {v['totals']['door_to_door_hours']} ч  [{v['id']}]")
        steps = " → ".join(f"{STEP_RU.get(s['type'], s['type'])} {hhmm(s['start'])}" for s in v.get("steps") or [])
        if steps:
            lines.append(f"    {steps}")
        hotel = v.get("hotel")
        if hotel:
            labels = ", ".join(DISTRICT_RU.get(x, x) for x in hotel["district"]["labels"]) or "—"
            stars = f"{hotel['stars']}★ " if hotel.get("stars") else ""
            lines.append(f"    отель: {stars}{hotel['name']} · рейтинг {hotel.get('rating') or '—'} · район: {labels}")
        lines.append(f"    {v['why']}")
        for title, key in (("жертвуем", "sacrifices"), ("риски", "risks"), ("не для", "not_for")):
            if v.get(key):
                lines.append(f"    {title}: " + "; ".join(v[key]))
        for u in v.get("upsell") or []:
            lines.append(f"    + {u['label']} {rub(u['price_rub']) if u.get('price_rub') is not None else ''}"
                         f" — {u['reason']}")
    for ho in cards.get("hotels") or []:
        lines.append(f"• {ho['label']}. {ho['name']} — {rub(ho['price_total_rub'])} · {ho['meals']} [{ho['offer_id']}]")
    comp = resp.get("comparison")
    if comp:
        ids = comp["variant_ids"]
        lines.append("Сравнение: " + " | ".join(i + (" ★" if i == comp.get("recommended_id") else "") for i in ids))
        for row in comp["rows"]:
            cells = " | ".join(row["values"].get(i, "—") + (" ✓" if i == row.get("best_id") else "") for i in ids)
            lines.append(f"  {row['label']}: {cells}")
    for title, key in (("Корзина", "cart"), ("Заказ", "order")):
        box = resp.get(key)
        if not box:
            continue
        ident = box.get("cart_id") or box.get("order_id")
        lines.append(f"{title} {ident} · {box['status']} · итого {rub(box['total_rub'])}")
        for it in box["items"]:
            extra = f" · {it['booking_number']}" if it.get("booking_number") else ""
            lines.append(f"  - {it['title']} · {hhmm(it.get('start'))} · {rub(it['price_rub'])}{extra}")
        for issue in box.get("issues") or []:
            lines.append(f"  ! [{issue['severity']}] {issue['message']}")
    actions = resp.get("actions") or []
    if actions:
        lines.append("Действия:")
        for a in actions:
            lines.append(f"  {number({'type': a['type'], 'payload': a.get('payload') or {}})} {a['label']}")
    files = resp.get("files") or {}
    if files.get("ics") or files.get("page"):
        lines.append("Файлы: " + "  ".join(x for x in (files.get("ics"), files.get("page")) if x))
    summary = debug_summary(resp.get("debug"))
    if summary:
        lines.append(summary)
    return "\n".join(lines), numbered


def debug_summary(dbg):
    if not dbg:
        return ""
    calls = dbg.get("calls") or []
    ms = sum(int(c.get("latency_ms") or 0) for c in calls)
    cost = 0.0
    for c in calls:  # engine records carry `cost` and `tokens`; accept `usage.cost_usd` too
        value = c.get("cost")
        if value is None:
            value = (c.get("tokens") or c.get("usage") or {}).get("cost_usd", c.get("cost_usd"))
        cost += float(value or 0)
    total = (dbg.get("timings_ms") or {}).get("total")
    path = "→".join(dbg.get("stage_path") or [])
    hooks_failed = sum(1 for k in dbg.get("hooks") or []
                       if str(k.get("verdict", "")).lower() in ("block", "error", "fail"))
    failed_calls = sum(1 for c in calls if c.get("ok") is False)
    text = "[debug" + (" · ПРИМЕР, не прогон" if dbg.get("example") else "") + f"] calls {len(calls)} · model {ms} ms · ${cost:.5f}"
    if total is not None:
        text += f" · total {total} ms"
    if failed_calls:
        text += f" · calls failed {failed_calls}"
    if hooks_failed:
        text += f" · hooks failed {hooks_failed}"
    return text + (f" · {path}" if path else "")


# ---------------------------------------------------------------- engine

def make_engine(args):
    if args.fixtures:
        from .server import FixtureEngine
        return FixtureEngine()
    from .server import build_engine
    return build_engine(args.flags, args.backend)


def parse_now(value):
    if not value:
        return None
    now = datetime.fromisoformat(value)
    if now.tzinfo is None:
        raise SystemExit("--now needs a UTC offset, e.g. 2026-10-24T12:00+03:00")
    return now


def new_session(engine, args):
    kwargs = {"profile_id": args.profile, "debug": args.debug}
    now = parse_now(args.now)
    if now is not None:
        kwargs["now"] = now
    return engine.new_session(**kwargs)


def ask_checkout(payload, read):
    """Fill an empty checkout payload interactively (travellers + contact)."""
    if payload.get("travellers") and payload.get("contact"):
        return payload
    raw = read("Путешественники (Имя Фамилия ГГГГ-ММ-ДД; …): ").strip()
    travellers = []
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        name, _, birth = part.rpartition(" ")
        travellers.append({"name": name.strip(), "birth_date": birth.strip()})
    contact_raw = read("Контакт (email или телефон): ").strip()
    contact = {"email": contact_raw} if "@" in contact_raw else {"phone": contact_raw}
    return {"travellers": travellers, "contact": contact}


def turn(engine, session_id, request, as_json, write):
    from .server import RequestValidator
    errors = RequestValidator().errors(dict(request, session_id=session_id))
    if errors:  # same contract check as POST /api/turn
        write("! запрос не по договору: " + errors[0][0] + "  (" + "; ".join(t for _, t in errors[:3]) + ")")
        return None, []
    response = engine.handle(session_id, request)
    if as_json:
        write(json.dumps(response, ensure_ascii=False, indent=2))
        _, numbered = render(response)
    else:
        text, numbered = render(response)
        write(text)
    return response, numbered


def repl(engine, args, read=input, write=print):
    session_id = new_session(engine, args)
    write(f"сессия {session_id}. /quit — выход, /new — новая сессия, число — выбрать пункт.")
    numbered, pending_image, as_json = [], None, args.json
    while True:
        try:
            line = read("> ").strip()
        except EOFError:
            return
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            return
        if line == "/new":
            session_id, numbered = new_session(engine, args), []
            write(f"сессия {session_id}")
            continue
        if line == "/json":
            as_json = not as_json
            write(f"json {'вкл' if as_json else 'выкл'}")
            continue
        if line.startswith("/image "):
            pending_image = image_data_url(line[7:].strip())
            write("картинка прикреплена к следующему сообщению")
            continue
        if line.startswith("/action "):
            try:
                request = {"action": json.loads(line[8:])}
            except json.JSONDecodeError as exc:
                write(f"не JSON: {exc}")
                continue
        elif line.isdigit():
            index = int(line)
            if not 1 <= index <= len(numbered):
                write("нет такого пункта")
                continue
            action = json.loads(json.dumps(numbered[index - 1]))
            if action["type"] == "checkout":
                action["payload"] = ask_checkout(action["payload"], read)
            request = {"action": action}
        else:
            request = {"text": line}
            if pending_image:
                request["image"], pending_image = pending_image, None
        response, new_numbered = turn(engine, session_id, request, as_json, write)
        if response is not None:
            numbered = new_numbered


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("message", nargs="?", help="one turn; omit for the interactive REPL")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--flags", default=None, help="flag spec, e.g. stage3 (default: $TRIP_AGENT_FLAGS)")
    parser.add_argument("--image", default=None, help="attach a photo (png/jpeg/webp/gif)")
    parser.add_argument("--backend", choices=("openrouter", "codex"), default=None)
    parser.add_argument("--fixtures", action="store_true", help="no engine: walk the contract fixtures")
    parser.add_argument("--now", default=None, help="pin the session clock (ISO with offset)")
    parser.add_argument("--json", action="store_true", help="print the raw Response JSON")
    args = parser.parse_args(argv)
    engine = make_engine(args)
    try:
        if args.message is None and args.image is None:
            repl(engine, args)
            return 0
        request = {}
        if args.message:
            request["text"] = args.message
        if args.image:
            request["image"] = image_data_url(args.image)
        turn(engine, new_session(engine, args), request, args.json, print)
        return 0
    finally:
        backend = getattr(engine, "backend", None)
        if hasattr(backend, "close"):
            backend.close()


if __name__ == "__main__":
    sys.exit(main())
