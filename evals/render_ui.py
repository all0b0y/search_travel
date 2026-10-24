"""Render an engine Response like the UI shows it, with clickable references for the persona.

    text, refs = render(response, with_refs=True)
    refs["Q1.2"] -> {"type": "answer_question", "payload": {...}, "label": "вдвоём"}

Clickable elements get a reference in square brackets:
  Qn.m  choice m of question n          -> answer_question {question_id, value}
  Cn.m  option m of chip n              -> set_chip {field, value}
  Dn    destination card n              -> choose_destination {destination_id}
  Vn    variant card n                  -> choose_variant {variant_id}
  An    action button n (as sent by the engine: choose_variant, compare, checkout, pay, ...)
"""
import re

from .common import rub

STAGE_RU = {"inspiration": "вдохновение", "selection": "подбор", "cart": "корзина", "checkout": "оформление",
            "ordered": "оплачено", "aftercare": "после покупки"}
DISTRICT_RU = {"lively": "движ", "quiet": "спокойно", "family": "семейно", "walkable": "пешком"}
KIND_RU = {"flight": "перелёт", "train": "поезд", "hotel": "отель", "transfer": "трансфер", "taxi": "такси",
           "stay": "проживание"}


def _num(x):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:g}".replace(".", ",")
    return str(x)


def _hm(iso):
    """'2026-11-12T06:40+03:00' -> '12.11 06:40'."""
    if not iso or len(iso) < 16:
        return iso or "—"
    return f"{iso[8:10]}.{iso[5:7]} {iso[11:16]}"


def _action_from_ui(action):
    return {"type": action["type"], "payload": action.get("payload") or {}, "label": action.get("label") or action["type"]}


def build_refs(response):
    refs = {}
    for qi, q in enumerate(response.get("questions") or [], 1):
        for ci, c in enumerate(q.get("choices") or [], 1):
            refs[f"Q{qi}.{ci}"] = {"type": "answer_question", "payload": {"question_id": q["id"], "value": c.get("value")},
                                   "label": c.get("label")}
    for hi, chip in enumerate(response.get("chips") or [], 1):
        for oi, o in enumerate(chip.get("options") or [], 1):
            refs[f"C{hi}.{oi}"] = {"type": "set_chip", "payload": {"field": chip["field"], "value": o.get("value")},
                                   "label": o.get("label")}
    cards = response.get("cards") or {}
    for di, d in enumerate(cards.get("destinations") or [], 1):
        refs[f"D{di}"] = {"type": "choose_destination", "payload": {"destination_id": d["id"]}, "label": d.get("name")}
    for vi, v in enumerate(cards.get("variants") or [], 1):
        refs[f"V{vi}"] = {"type": "choose_variant", "payload": {"variant_id": v["id"]}, "label": v.get("title")}
    for ai, a in enumerate(response.get("actions") or [], 1):
        refs[f"A{ai}"] = _action_from_ui(a)
    return refs


def _lines_destinations(dests):
    out = ["Направления:"]
    for i, d in enumerate(dests, 1):
        price = f"от {rub(d['price_from_rub'])}" if d.get("price_from_rub") is not None else "цены нет"
        line = f"  [D{i}] {d['name']}, {d.get('country') or '—'} — {price} за {d.get('nights')} ноч., {d.get('party_label', '')}"
        w = d.get("weather")
        if w:
            sea = f", море {_num(w.get('sea_c'))}°" if w.get("sea_c") is not None else ""
            line += f"; воздух {_num(w.get('air_max_c'))}°{sea}, дождливых дней {_num(w.get('rain_days'))}"
        if d.get("travel_time_h") is not None:
            line += f"; в пути {_num(d['travel_time_h'])} ч"
        out.append(line)
        if d.get("why"):
            out.append(f"       Почему: {d['why']}")
        if d.get("watch_out"):
            out.append(f"       Учтите: {d['watch_out']}")
    return out


def _lines_variants(variants):
    out = ["Варианты:"]
    for i, v in enumerate(variants, 1):
        rec = " (рекомендуем)" if v.get("recommended") else ""
        t = v.get("totals") or {}
        out.append(f"  [V{i}] «{v.get('title')}»{rec} — {rub(t.get('rub'))}, в дороге {_num(t.get('door_to_door_hours'))} ч")
        h = v.get("hotel")
        if h:
            labels = ", ".join(str(DISTRICT_RU.get(x, x)) for x in (h.get("district") or {}).get("labels") or [])
            stars = f"{h['stars']}★" if h.get("stars") is not None else "без звёзд"
            out.append(f"       Отель: {h['name']}, {stars}, рейтинг {_num(h.get('rating'))}; район: {labels or '—'}")
        for s in v.get("steps") or []:
            price = f", {rub(s['price_rub'])}" if s.get("price_rub") is not None else ""
            out.append(f"       · {s.get('title') or KIND_RU.get(s['type'], s['type'])}: {_hm(s.get('start'))} → {_hm(s.get('end'))}{price}")
        if v.get("why"):
            out.append(f"       Почему: {v['why']}")
        for key, title in (("sacrifices", "Чем жертвуете"), ("risks", "Риски"), ("not_for", "Кому не подойдёт")):
            if v.get(key):
                out.append(f"       {title}: " + "; ".join(v[key]))
        for u in v.get("upsell") or []:
            price = f" ({rub(u['price_rub'])})" if u.get("price_rub") is not None else ""
            out.append(f"       Доп: {u['label']}{price} — {u['reason']}")
    return out


def _lines_hotels(hotels):
    out = ["Отели:"]
    for h in hotels:
        out.append(f"  {h.get('label')}. {h.get('name')} — {rub(h.get('price_total_rub'))}, питание {h.get('meals')}; {h.get('why', '')}")
    return out


def _lines_comparison(cmp_):
    out = ["Сравнение: " + " / ".join(cmp_.get("variant_ids") or [])]
    for row in cmp_.get("rows") or []:
        vals = "; ".join(f"{k}: {v}" for k, v in (row.get("values") or {}).items())
        best = f" (лучше: {row['best_id']})" if row.get("best_id") else ""
        out.append(f"  {row.get('label')}: {vals}{best}")
    if cmp_.get("recommended_id"):
        out.append(f"  Советуем: {cmp_['recommended_id']}")
    return out


def _lines_items(title, holder, total_key="total_rub"):
    out = [f"{title}: {holder.get('status')}, итого {rub(holder.get(total_key))}"]
    for it in holder.get("items") or []:
        bn = f", бронь {it['booking_number']}" if it.get("booking_number") else ""
        out.append(f"  · {it.get('title')} — {rub(it.get('price_rub'))} ({_hm(it.get('start'))} → {_hm(it.get('end'))}){bn}")
    for iss in holder.get("issues") or []:
        out.append(f"  ! [{iss.get('severity')}] {iss.get('message')}")
    for ev in holder.get("events") or []:
        out.append(f"  — {ev.get('text')}")
    return out


def render(response, with_refs=True):
    """-> (text, refs). Without refs the text has no [..] markers (judge packets may keep them)."""
    lines = [f"[этап: {STAGE_RU.get(response.get('stage'), response.get('stage'))}]",
             f"Агент: {response.get('message', '')}"]
    if response.get("error"):
        lines.append(f"(ошибка: {response['error'].get('code')})")
    questions = response.get("questions") or []
    if questions:
        lines.append("Вопросы:")
        for qi, q in enumerate(questions, 1):
            choices = " · ".join(f"[Q{qi}.{ci}] {c['label']}" + (" (рекомендуем)" if c.get("recommended") else "")
                                 for ci, c in enumerate(q.get("choices") or [], 1))
            lines.append(f"  {qi}. {q['text']}  {choices}")
    chips = response.get("chips") or []
    if chips:
        lines.append("Допущения (можно поменять):")
        for hi, chip in enumerate(chips, 1):
            opts = " · ".join(f"[C{hi}.{oi}] {o['label']}" for oi, o in enumerate(chip.get("options") or [], 1))
            lines.append(f"  {chip['label']}" + (f" — {opts}" if opts else ""))
    cards = response.get("cards") or {}
    if cards.get("destinations"):
        lines += _lines_destinations(cards["destinations"])
    if cards.get("variants"):
        lines += _lines_variants(cards["variants"])
    if cards.get("hotels"):
        lines += _lines_hotels(cards["hotels"])
    if response.get("comparison"):
        lines += _lines_comparison(response["comparison"])
    if response.get("cart"):
        lines += _lines_items("Корзина", response["cart"])
    if response.get("order"):
        lines += _lines_items("Заказ", response["order"])
    actions = response.get("actions") or []
    if actions:
        lines.append("Кнопки: " + " · ".join(f"[A{ai}] {a.get('label')} ({a['type']})" for ai, a in enumerate(actions, 1)))
    files = response.get("files") or {}
    if files.get("ics") or files.get("page"):
        lines.append("Файлы: " + ", ".join(v for v in (files.get("ics"), files.get("page")) if v))
    text = "\n".join(lines)
    if not with_refs:
        text = re.sub(r"\[(?:Q\d+\.\d+|C\d+\.\d+|D\d+|V\d+|A\d+)\] ", "", text)
    return text, build_refs(response)


def render_user_turn(request, refs_label=None):
    """How a user turn looks in a transcript."""
    if request.get("action"):
        a = request["action"]
        label = refs_label or a.get("type")
        return f"Пользователь нажал: «{label}» ({a['type']})"
    parts = []
    if request.get("image"):
        parts.append("[прислал фото]")
    if request.get("text"):
        parts.append(request["text"])
    return "Пользователь: " + " ".join(parts)
