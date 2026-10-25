"""Response assembly (AGENT_PLAN.md section 6, agent/contracts/response.schema.json).

Prices, times and totals come from code data (variants, mock carts and orders); only the texts
`why` / `sacrifices` / `risks` / `not_for` / upsell lines come from the present card's output, matched
by id. When the card failed (degraded turn) the code-written Note texts are used instead.
"""
import re

from . import payloads as P
from .steps import (_arrival, _hhmm_after, _hotel_checkin, _order_has_transfers, pay_label, pending_order,
                    recommended_hint, tool_safe)

MAX_ITEMS = 6
SEVERITY = {"block": "blocking", "warn": "warning", "blocking": "blocking", "warning": "warning", "info": "info"}
EVENT_TEXT = {"created": "Заказ создан", "paid": "Оплачено картой (демо)", "early_checkin_requested": "Запрошен ранний заезд",
              "dates_changed": "Даты изменены", "transfer_added": "Добавлен трансфер", "changed": "Заказ изменён",
              "cancelled": "Заказ отменён"}
STEP_TITLE = {"flight": "Перелёт", "train": "Поезд", "transfer": "Трансфер", "taxi": "Такси", "stay": "Проживание"}


# ---------------------------------------------------------------- pieces

def _int(n):
    return int(round(n)) if isinstance(n, (int, float)) else None


def issue_out(i, item_ids=None):
    return {"code": str(i.get("code") or "issue"), "message": str(i.get("message") or i.get("code") or "Проблема"),
            "severity": SEVERITY.get(i.get("severity"), "warning"),
            "item_ids": list(i.get("item_ids") or item_ids or [])}


def question_out(q):
    return {"id": q.id, "text": q.text, "field": q.field,
            "choices": [{"label": c.label, "value": c.value, "recommended": bool(c.recommended)} for c in q.choices][:6]}


def chip_out(c):
    return {"field": c.field, "label": c.label, "value": c.value,
            "options": [{"label": o.label, "value": o.value} for o in c.options]}


def step_out(step):
    item = P.step_item(step)
    title = step.title or (f"{STEP_TITLE.get(step.type, step.type)} {step.from_} → {step.to}"
                           if step.type in ("flight", "train") else STEP_TITLE.get(step.type, step.type))
    out = {"id": step.id, "type": step.type, "from": step.from_, "to": step.to, "start": step.start,
           "end": step.end, "title": title, "carrier": step.carrier, "price_rub": item["price_rub"],
           "offer_id": step.offer_id, "status": step.status}
    risk = step.risk or {}
    if isinstance(risk.get("slack_min"), (int, float)) and isinstance(risk.get("risky"), bool):
        out["risk"] = {"slack_min": risk["slack_min"], "risky": risk["risky"]}
    return out


def code_why(variant):
    title = variant.title or P.VARIANT_TITLES.get(variant.id, variant.id)
    t = variant.totals
    out_h, back_h = getattr(t, "out_hours", None), getattr(t, "back_hours", None)
    if out_h is not None and back_h is not None:
        road = f"туда {P.fmt_hours(out_h)}, обратно {P.fmt_hours(back_h)}"
    elif out_h is not None or back_h is not None:
        road = f"в пути {P.fmt_hours(out_h if out_h is not None else back_h)} в одну сторону"
    else:
        road = f"{P.fmt_hours(t.door_to_door_hours)} в дороге"
    return f"«{title}»: {P.fmt_rub(t.rub)} за всё, {road}."


def _texts(out_item, key, notes):
    if out_item is not None and key in out_item:
        return [t for t in out_item[key] if t][:MAX_ITEMS]
    return [n.text for n in notes if n.text][:MAX_ITEMS]


def variant_cards(ctx, variants, output):
    by_id = {i["id"]: i for i in (output or {}).get("items") or []}
    labels = {i.id: i.label for i in ctx.session.shown.variants}
    rec = (output or {}).get("recommended_id")
    ids = [v.id for v in variants]
    if rec not in ids:
        rec = recommended_hint(variants)
    chosen = {}
    cand_index = {c.id: (v.id, c) for v in variants for c in v.upsell_candidates}
    for u in (output or {}).get("upsell") or []:
        hit = cand_index.get(u.get("candidate_id"))
        if hit and sum(len(x) for x in chosen.values()) < int(ctx.rules.get("max_upsell", 2)):
            chosen.setdefault(hit[0], []).append((hit[1], u.get("line")))
    cards = []
    for i, v in enumerate(variants):
        o = by_id.get(v.id)
        hotel = None
        if v.hotel is not None:
            scores = {k: float(v.hotel.district.scores.get(k, 0.0)) for k in ("lively", "quiet", "family", "walkable")}
            hotel = {"hotel_id": v.hotel.hotel_id, "name": v.hotel.name, "stars": v.hotel.stars,
                     "rating": v.hotel.rating, "district": {"labels": list(dict.fromkeys(v.hotel.district.labels)),
                                                            "scores": scores}}
        cards.append({
            "id": v.id, "label": labels.get(v.id, str(i + 1)),
            "title": v.title or P.VARIANT_TITLES.get(v.id, v.id), "recommended": v.id == rec,
            "totals": {"rub": int(v.totals.rub), "door_to_door_hours": float(v.totals.door_to_door_hours)},
            "steps": [step_out(s) for s in v.steps], "hotel": hotel,
            "why": (o or {}).get("why") or code_why(v),
            "sacrifices": _texts(o, "sacrifices", v.sacrifices), "risks": _texts(o, "risks", v.risks),
            "not_for": _texts(o, "not_for", v.not_for),
            "upsell": [{"offer_id": c.offer_id, "label": c.label, "price_rub": c.price_rub,
                        "reason": line or c.reason} for c, line in chosen.get(v.id, [])][:2]})
    return cards


def destination_cards(ctx, output):
    by_id = {i["id"]: i for i in (output or {}).get("items") or []}
    cards = []
    for item in ctx.destination_items[:MAX_ITEMS]:
        o = by_id.get(item["id"]) or {}
        why = o.get("why") or item["facts"].get("description") or (
            "Подходит по: " + ", ".join(item["match"]) if item["match"] else "Подходит под ваш запрос.")
        cards.append({"id": item["id"], "label": item["label"], "name": item["name"],
                      "country": item["country"] or "—", "image_url": item.get("image_url"),
                      "price_from_rub": _int(item["price_from_rub"]), "nights": int(item["nights"]),
                      "party_label": item["party_label"], "weather": item["weather"],
                      "travel_time_h": item["travel_time_h"], "why": why, "watch_out": o.get("watch_out")})
    return cards


def cart_out(cart):
    if not cart:
        return None
    return {"cart_id": cart["cart_id"], "status": cart.get("status", "open"),
            "items": [{"item_id": i["item_id"], "offer_id": i["offer_id"], "kind": i["kind"], "title": i["title"],
                       "price_rub": int(i["price_rub"]), "start": i.get("start"), "end": i.get("end")}
                      for i in cart.get("items") or []],
            "total_rub": int(cart.get("total_rub") or 0), "issues": [issue_out(i) for i in cart.get("issues") or []]}


def _event_text(e):
    data = e.get("data") or {}
    text = EVENT_TEXT.get(e.get("kind"), e.get("kind") or "событие")
    if e.get("kind") == "early_checkin_requested" and data.get("time"):
        text += f" на {data['time']}"
        if data.get("status"):
            text += f" ({data['status']})"
    return text


def order_out(order, extra_issues=()):
    if not order:
        return None
    issues = [issue_out(i) for i in order.get("issues") or []] + [issue_out(i) for i in extra_issues]
    return {"order_id": order["order_id"], "status": order["status"],
            "items": [{"item_id": i["item_id"], "kind": i["kind"], "title": i["title"],
                       "price_rub": int(i["price_rub"]), "booking_number": i.get("booking_number"),
                       "status": i.get("status", "pending"), "start": i.get("start"), "end": i.get("end")}
                      for i in order.get("items") or []],
            "total_rub": int(order.get("total_rub") or 0), "issues": issues,
            "events": [{"at": e["at"], "kind": e["kind"], "text": _event_text(e)} for e in order.get("events") or []]}


# ---------------------------------------------------------------- actions

def build_actions(ctx, cards, order):
    s = ctx.session
    actions = []
    for d in cards["destinations"]:
        actions.append({"type": "choose_destination", "label": f"Смотреть {d['name']}",
                        "payload": {"destination_id": d["id"]}})
    if s.stage == "selection" and s.variants and not ctx.error:
        shown = [v for v in (s.variant(i.id) for i in s.shown.variants) if v is not None] or list(s.variants)
        for v in shown:
            actions.append({"type": "choose_variant", "label": f"Беру «{v.title or P.VARIANT_TITLES.get(v.id, v.id)}»",
                            "payload": {"variant_id": v.id}})
        if len(shown) >= 2 and ctx.comparison is None:
            actions.append({"type": "compare", "label": "Сравнить",
                            "payload": {"variant_ids": list(dict.fromkeys(v.id for v in shown))[:4]}})
    if s.stage == "cart" and s.cart_id:
        actions.append({"type": "checkout", "label": "Оформить", "payload": {"travellers": [], "contact": None}})
    if order and order["status"] == "pending_payment":
        if not any(i["severity"] == "blocking" for i in order["issues"]):
            actions.append({"type": "pay", "label": pay_label(order["total_rub"]),
                            "payload": {"order_id": order["order_id"]}})
        elif s.stage == "checkout":
            # a blocker is never a dead end: the other variants are one tap away (a new cart, a new order)
            for v in [v for v in s.variants if v.id != s.chosen_variant_id][:3]:
                actions.append({"type": "choose_variant",
                                "label": f"Выбрать «{v.title or P.VARIANT_TITLES.get(v.id, v.id)}»",
                                "payload": {"variant_id": v.id}})
    if s.stage in ("ordered", "aftercare") and s.order_id:
        arr, checkin = _arrival(ctx), _hotel_checkin(ctx)
        if arr is not None and (checkin is None or arr < checkin):   # early check-in only if they arrive early
            actions.append({"type": "order_action", "label": "Ранний заезд",
                            "payload": {"order_id": s.order_id, "kind": "early_checkin",
                                        "params": {"time": _hhmm_after(arr)}}})
        if not _order_has_transfers(ctx):
            actions.append({"type": "order_action", "label": "Добавить трансфер",
                            "payload": {"order_id": s.order_id, "kind": "add_transfer", "params": {}}})
    return actions


# ---------------------------------------------------------------- message

DEGRADED = {
    "present.destinations": "Вот направления под ваш запрос — цены и погода посчитаны по данным каталога.",
    "present.variants": "Вот варианты поездки — цифры посчитаны по данным каталога.",
    "present.update": "Пересобрал варианты с учётом изменений — цифры посчитаны по данным каталога.",
    "present.compare": "Сравнение вариантов — в таблице ниже.",
    "present.info": "Точного ответа в данных нет — не буду выдумывать.",
    "present.aftercare": "Запрос по заказу отправлен — статус ниже.",
    "present.fallback": "Я помогаю с поездками: куда поехать, подобрать билеты и отель, оформить. С чего начнём?",
}


def build_message(ctx):
    if ctx.error and ctx.message:
        body = ctx.message
    elif ctx.message:
        body = ctx.message
    elif ctx.card_output and ctx.card_output.get("message"):
        body = ctx.card_output["message"]
    else:
        body = DEGRADED.get(ctx.present or "", "Готово.")
        if ctx.present == "present.info" and ctx.info_facts:
            body = "Вот что есть в данных по вашему вопросу — без пересказа моделью."
    return " ".join(ctx.notes + [calm_risks(body)]).strip()


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF]\uFE0F?")
RISK_RE = re.compile(r"штраф|невозврат|риск|пересадк|ночью|ночной|поздн|нюанс|без обмена|не вернут|не успе|отмен", re.I)
FREE_CANCEL_RE = re.compile(r"бесплатн\w*\s+отмен\w*|отмен\w*\s+бесплатн\w*", re.I)


POLISH_KEYS = {"message", "why", "watch_out", "text", "reason", "summary", "sacrifices", "risks", "not_for"}


def polish_output(output):
    """Code-side text polish of a card output (what the user sees): calm_risks on every user-visible string."""
    def walk(x, key=None):
        if isinstance(x, str):
            return calm_risks(x) if key in POLISH_KEYS else x
        if isinstance(x, list):
            return [walk(v, key) for v in x]
        if isinstance(x, dict):
            return {k: walk(v, k) for k, v in x.items()}
        return x
    return walk(output) if isinstance(output, dict) else output


def calm_risks(text: str) -> str:
    """Risks are said plainly: drop emoji from any sentence that states a risk (voice rule 5).

    Cheap models sometimes end a risk sentence with a palm tree; code removes it instead of a rerun.
    «Бесплатная отмена» is good news, not a risk.
    """
    if not text or not EMOJI_RE.search(text):
        return text
    # a sentence ends at . ! ? … or at an emoji followed by a new capitalised sentence («… отмена 🌴 У «Дешевле» …»)
    parts = re.split(r"(?<=[.!?…])\s+|(?<=[\U0001F300-\U0001FAFF\U00002600-\U000027BF\uFE0F])\s+(?=[А-ЯЁA-Z«])", text)
    out = []
    for part in parts:
        if RISK_RE.search(FREE_CANCEL_RE.sub("", part)) and EMOJI_RE.search(part):
            part = re.sub(r"\s{2,}", " ", EMOJI_RE.sub("", part)).strip()
            part = re.sub(r"\s+([.!?…,])", r"\1", part)
        out.append(part)
    return " ".join(out)


# ---------------------------------------------------------------- response

def debug_out(ctx):
    t = ctx.trace
    return {"stage_path": list(t["stage_path"]), "calls": list(t["calls"]), "hooks": list(t["hooks"]),
            "timings_ms": dict(t["timings_ms"]), "jev": list(t["jev"]),
            "flags": {k: v for k, v in ctx.flags.as_dict().items()},
            "route": t.get("route"), "steps": list(t["steps"]), "tools": list(t["tools"]),
            "budget": dict(t["budget"]), "notes": list(t["notes"]), "errors": list(t["errors"]),
            "degraded": ctx.degraded}


def build_response(ctx):
    s = ctx.session
    ctx.card_output = polish_output(ctx.card_output)
    output = ctx.card_output
    cards = {"destinations": [], "variants": [], "hotels": []}
    comparison = None
    if not ctx.error:
        if ctx.present == "present.destinations" and ctx.destination_items:
            cards["destinations"] = destination_cards(ctx, output)
        if ctx.present in ("present.variants", "present.update") and ctx.variants:
            cards["variants"] = variant_cards(ctx, ctx.variants, output)
        if ctx.comparison is not None:
            comparison = dict(ctx.comparison)
            rec = (output or {}).get("recommended_id")
            if rec in comparison["variant_ids"]:
                comparison["recommended_id"] = rec
    order_src, order_extra = ctx.order, ctx.order_issues
    if order_src is None:   # an info question / small talk / error at checkout still shows the order and its button
        order_src, order_extra = pending_order(ctx)
    cart = ctx.cart
    if cart is None and s.stage == "cart" and s.cart_id and not ctx.order:
        cart = tool_safe(ctx, "cart_get", {"session_id": s.id}, cache=False)
        cart = cart if cart and cart.get("status", "open") == "open" else None
    order = order_out(order_src, order_extra)
    response = {
        "session_id": s.id, "turn": s.turn, "stage": s.stage, "message": build_message(ctx),
        "chips": [chip_out(c) for c in ctx.chips] if not ctx.error else [],
        "questions": [question_out(q) for q in ctx.questions][:2] if not ctx.error else [],
        "cards": cards, "comparison": comparison, "cart": cart_out(cart), "order": order,
        "actions": [], "files": dict(ctx.files),
    }
    response["actions"] = build_actions(ctx, cards, order)
    if ctx.error:
        response["error"] = {"code": ctx.error["code"], "retryable": bool(ctx.error.get("retryable"))}
    if s.debug:
        response["debug"] = debug_out(ctx)
    return response


def error_response(session_id, code, message, retryable=False, turn=0, stage="inspiration"):
    """A response for turns that never reached a session (unknown session id, malformed request)."""
    sid = session_id if isinstance(session_id, str) and session_id.startswith("s_") and all(
        ch.isalnum() or ch == "_" for ch in session_id) else "s_unknown"
    return {"session_id": sid, "turn": turn, "stage": stage, "message": message, "chips": [], "questions": [],
            "cards": {"destinations": [], "variants": [], "hotels": []}, "comparison": None, "cart": None,
            "order": None, "actions": [], "files": {"ics": None, "page": None},
            "error": {"code": code, "retryable": retryable}}

