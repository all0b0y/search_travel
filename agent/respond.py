"""Response assembly (AGENT_PLAN.md section 6, agent/contracts/response.schema.json).

Prices, times and totals come from code data (variants, mock carts and orders); only the texts
`why` / `sacrifices` / `risks` / `not_for` / upsell lines come from the present card's output, matched
by id. When the card failed (degraded turn) the code-written Note texts are used instead.
"""
import re
from datetime import date

from . import payloads as P
from .steps import (_arrival, _hhmm_after, _hotel_checkin, _order_has_transfers, pay_label, pending_order,
                    pet_fee_note, recommended_hint, road_answer_ru, tool_safe)

MAX_ITEMS = 6
SEVERITY = {"block": "blocking", "warn": "warning", "blocking": "blocking", "warning": "warning", "info": "info"}
EVENT_TEXT = {"created": "Заказ создан", "paid": "Оплачено картой (демо)", "early_checkin_requested": "Запрошен ранний заезд",
              "dates_changed": "Даты изменены", "transfer_added": "Добавлен трансфер",
              "transfer_changed": "Трансфер перенесён", "changed": "Заказ изменён",
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
    if _long_road(variant):
        road = _long_road(variant)
    elif out_h is not None and back_h is not None:
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


def _pet_item(i, brief):
    """A ticket line with animals priced in: the fee named on the line (title suffix + pet_fee_rub / includes_ru,
    optional keys of cart_item / order_item); the line's price_rub already holds it, totals stay the sum."""
    note = pet_fee_note(i, brief)
    if not note:
        return {"title": i["title"]}
    return {"title": f"{i['title']} ({note['includes_ru']})", **note}


def cart_out(cart, brief=None):
    if not cart:
        return None
    return {"cart_id": cart["cart_id"], "status": cart.get("status", "open"),
            "items": [{"item_id": i["item_id"], "offer_id": i["offer_id"], "kind": i["kind"], "title": i["title"],
                       "price_rub": int(i["price_rub"]), "start": i.get("start"), "end": i.get("end"),
                       **_pet_item(i, brief)}
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


def order_out(order, extra_issues=(), brief=None):
    if not order:
        return None
    issues = [issue_out(i) for i in order.get("issues") or []] + [issue_out(i) for i in extra_issues]
    return {"order_id": order["order_id"], "status": order["status"],
            "items": [{"item_id": i["item_id"], "kind": i["kind"], "title": i["title"],
                       "price_rub": int(i["price_rub"]), "booking_number": i.get("booking_number"),
                       "status": i.get("status", "pending"), "start": i.get("start"), "end": i.get("end"),
                       **_pet_item(i, brief)}
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
    # a no_data answer is never a dead end: the other destinations already shown are one tap away
    for a in getattr(ctx, "extra_actions", None) or []:
        if a not in actions:
            actions.append(dict(a))
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

FALLBACK_TEXT = "Я помогаю с поездками: куда поехать, подобрать билеты и отель, оформить. С чего начнём?"


def _day(iso):
    """«21 ноября» from an ISO date / datetime."""
    from datetime import date as _date_cls
    d = _date_cls.fromisoformat(str(iso)[:10])
    return f"{d.day} {P.MONTHS_GEN[d.month - 1]}"


def _clock(iso):
    s = str(iso or "")
    return s[11:16] if len(s) >= 16 and s[10] == "T" else None


def _title(v):
    return v.title or P.VARIANT_TITLES.get(v.id, v.id)


def _long_road(v):
    """A long-distance train (ranking._long_leg_facts): the time on board as code wrote it («туда в пути 1 сутки
    3 ч, выезд 19 ноября»), never hours the reader would take for a flight."""
    f = v.facts or {}
    if not (f.get("out_long_train") or f.get("back_long_train")):
        return None
    parts = []
    for d, word in (("out", "туда"), ("back", "обратно")):
        if f.get(f"{d}_on_road_ru"):
            part = f"{word} поездом в пути {f[f'{d}_on_road_ru']}"
            if d == "out" and f.get("out_days_before_checkin") and f.get("out_date"):
                part += f", выезд {_day_month(f['out_date'])}"
            if d == "back" and f.get("back_days_after_checkout") and f.get("back_arr_date"):
                part += f", дома {_day_month(f['back_arr_date'])}"
            parts.append(part)
        elif getattr(v.totals, f"{d}_hours", None) is not None:
            parts.append(f"{word} {P.fmt_hours(getattr(v.totals, f'{d}_hours'))}")
    return "; ".join(parts)


def _day_month(iso):
    d = date.fromisoformat(str(iso)[:10])
    return f"{d.day} {P.MONTHS_GEN[d.month - 1]}"


def _road(v):
    t = v.totals
    out_h, back_h = getattr(t, "out_hours", None), getattr(t, "back_hours", None)
    if _long_road(v):
        return ", " + _long_road(v)
    if out_h is not None and back_h is not None:
        return f", туда {P.fmt_hours(out_h)}, обратно {P.fmt_hours(back_h)}"
    if out_h is not None or back_h is not None:
        return f", в пути {P.fmt_hours(out_h if out_h is not None else back_h)} в одну сторону"
    return ""


def _met_clause(ctx, v):
    """A condition the user set that the recommended variant meets, confirmed in one clause (kitchen, step-free,
    cot) — from the variant's facts only."""
    cons = set((ctx.session.brief.values().get("constraints") or []))
    f = v.facts or {}
    parts = []
    if "kitchen" in cons and f.get("kitchen") is True:
        parts.append("в номере есть кухня")
    if "accessible" in cons and f.get("step_free") is True:
        parts.append("отель без ступенек" + (", есть лифт" if f.get("lift") is True else ""))
    if "crib" in cons and f.get("infant_cot") in ("free", "paid"):
        parts.append("детская кроватка — " + ("бесплатно" if f["infant_cot"] == "free" else "за доплату"))
    return (", " + ", ".join(parts)) if parts else ""


def _variants_text(ctx, update=False):
    variants = list(ctx.variants or [])
    if not variants:
        return ("Под новые условия вариантов не нашлось — можно сдвинуть даты, бюджет или район, и я пересоберу."
                if update else "Вариантов под этот запрос не нашлось — можно сдвинуть даты, бюджет или район.")
    hint = (ctx.card_input or {}).get("recommended_hint")
    best = next((v for v in variants if v.id == hint), None) or next(
        (v for v in variants if v.id == recommended_hint(variants)), variants[0])
    before = {}
    for row in (ctx.card_input or {}).get("changes") or []:
        field = str(row.get("field") or "")
        if field.startswith("totals.") and row.get("before") is not None:
            before[field.split(".")[1]] = row["before"]
    was = f" (было {P.fmt_rub(before[best.id])})" if best.id in before else ""
    head = "Пересобрал варианты. " if update else ""
    gap = best.facts.get("transport_gap_ru")
    question = (ctx.card_input or {}).get("question") or {}
    road = next((road_answer_ru(f, question.get("text")) for f in question.get("facts") or []
                 if isinstance(f, dict) and f.get("kind") == "transport_search"), None)
    if road:   # «а с пересадкой?» in a state without flights: answered from what code searched, first
        head = road + " " + ("" if gap and "от " in road else (gap.rstrip(".") + ". " if gap else ""))
    elif gap:   # no road there (or another mode built): code's line first (steps.recover_transport)
        head += gap.rstrip(".") + ". "
    text = (f"{head}Советую «{_title(best)}»: {P.fmt_rub(best.totals.rub)} за всё{was}{_road(best)}"
            f"{_met_clause(ctx, best)}.")
    others = [f"«{_title(v)}» — {P.fmt_rub(v.totals.rub)}" for v in variants if v.id != best.id][:2]
    if others:
        text += " Ещё " + " и ".join(others) + "."
    text += _other_places_text(ctx)
    said = list(getattr(getattr(ctx.session, "history", None), "agent", None) or [])
    if said and said[-1].endswith(text):   # the same answer twice in a row: say it was checked again, short
        core = (gap or road or "").rstrip(".")
        labels = [a["label"] for a in ctx.extra_actions or [] if a.get("label")][:2]
        text = (f"Проверил ещё раз — всё так же{': ' + core[:1].lower() + core[1:] if core else ''}. "
                f"Ближе всего — «{_title(best)}», {P.fmt_rub(best.totals.rub)}{_road(best)}."
                + (f" Дальше: {' или '.join('«' + x + '»' for x in labels)} — кнопки ниже." if labels else ""))
    return text


def _place_price(p):
    way = f" {p['transport_ru']}" if p.get("transport_ru") == "поездом" else ""
    return f"{p['name']}{way} — от {P.fmt_rub(p['price_from_rub'])}"


def _other_places_text(ctx):
    """The other places code priced for this trip (steps.offer_other_places), one sentence."""
    places = getattr(ctx, "other_places", None)
    if places is None:
        return ""
    budget = ((ctx.session.brief.values().get("budget_rub") or {}).get("max"))
    fits = [p for p in places if p.get("within_budget") is not False]
    if fits:
        head = f" В {P.fmt_rub(budget)} укладываются и другие места: " if budget else " Можно и в другое место: "
        return head + ", ".join(_place_price(p) for p in fits) + " — кнопки ниже."
    if places:
        return (f" В бюджет не укладывается ни одно другое место, ближе всего {_place_price(places[0])}.")
    return " Других мест в этот бюджет не нашёл."


def _destinations_text(ctx):
    items = list(ctx.destination_items or [])[:3]
    if not items:
        return "Подходящих направлений не нашлось — расскажите чуть больше, что хочется, и я подберу."
    way = lambda i: " поездом" if (i.get("facts") or {}).get("transport") == "train" else ""  # noqa: E731

    def place(i):
        f = i.get("facts") or {}
        if not i.get("price_from_rub"):
            return i["name"]
        road = f" ({f['road_ru'].removeprefix('поездом, ')})" if f.get("long_train") and f.get("road_ru") else ""
        return f"{i['name']}{way(i)} — от {P.fmt_rub(i['price_from_rub'])}{road}"
    budget = (ctx.session.brief.values().get("budget_rub") or {}).get("max")
    if budget:   # code's within_budget: the places that fit are named, «ничего не укладывается» only when none does
        fits = [i for i in ctx.destination_items or [] if (i.get("facts") or {}).get("within_budget") is True][:3]
        if fits:
            return (f"В {P.fmt_rub(budget)} укладываются: " + ", ".join(place(i) for i in fits)
                    + ". Выберите направление — подберу варианты поездки.")
        return (f"В {P.fmt_rub(budget)} ничего не укладывается — вот самые доступные варианты: "
                + ", ".join(place(i) for i in items) + ". Выберите направление — подберу варианты поездки.")
    return "Куда можно поехать: " + ", ".join(place(i) for i in items) + ". Выберите направление — подберу варианты поездки."


def _compare_text(ctx):
    rec = (ctx.comparison or {}).get("recommended_id")
    v = ctx.session.variant(rec) if rec else None
    if v is None:
        return "Сравнил варианты — разница по цене, дороге и отелю в таблице ниже."
    return (f"Сравнил варианты в таблице ниже. Советую «{_title(v)}»: {P.fmt_rub(v.totals.rub)} за всё"
            f"{_road(v)}.")


INFO_TOPICS = (
    ("price", re.compile(r"входит\s+в\s+(?:эт\w+\s+)?цен|что\s+в\s+цен|из\s+чего\s+(?:складыва|состо)|цен\w*\s+включ", re.I)),
    ("transfer", re.compile(r"трансфер|такси|машин|забер|встрет", re.I)),
    ("refund", re.compile(r"возврат|вернут|сдать|обмен|поменять билет", re.I)),
    ("cancel", re.compile(r"отмен", re.I)),
    ("baggage", re.compile(r"багаж|чемодан", re.I)),
    ("flight", re.compile(r"рейс|вылет|прил[её]т|перел[её]т|самол|билет|пересад|прям", re.I)),
    ("hotel", re.compile(r"отел|засел|выезд|номер|заезд", re.I)),
    ("weather", re.compile(r"погод|море|вод[аы]|тепл|холод|дожд|купа", re.I)),
)


def _topic(question):
    return next((name for name, rx in INFO_TOPICS if rx.search(question or "")), None)


def _order_item_line(i):
    start, end = i.get("start"), i.get("end")
    if not start:
        return f"«{i['title']}»"
    way = P.transfer_way(i["title"]) if i.get("kind") == "transfer" else None
    line = (f"трансфер {way} — {_day(start)} в {_clock(start)}" if way
            else f"«{i['title']}» — {_day(start)} в {_clock(start)}")
    if i.get("kind") in ("flight", "train") and end:
        line += f", прибытие в {_clock(end)}"
    elif i.get("kind") == "hotel" and end:
        line = f"«{i['title']}» — заезд {_day(start)} с {_clock(start)}, выезд {_day(end)} до {_clock(end)}"
    return line


def _fare_word(leg):
    if leg.get("refundable") is True:
        word = "возвратные"
    elif leg.get("refundable") is False:
        word = "невозвратные"
    else:
        return None
    if leg.get("exchange_allowed") is False:
        word += ", без обмена"
    elif leg.get("exchange_fee_rub") == 0:
        word += ", обмен бесплатно"
    elif leg.get("exchange_fee_rub"):
        word += f", обмен за {P.fmt_rub(leg['exchange_fee_rub'])}"
    return word


def _variant_info(topic, facts):
    lines = []
    for f in facts:
        title, legs, stay = f.get("title") or f.get("id"), f.get("legs") or [], f.get("stay") or {}
        flights = [leg for leg in legs if leg.get("type") in ("flight", "train")]
        if topic == "refund":
            words = list(dict.fromkeys(w for w in (_fare_word(leg) for leg in flights) if w))
            if words:
                lines.append(f"в «{title}» билеты {' / '.join(words)}")
        elif topic == "cancel":
            c = stay.get("cancellation") or {}
            if c.get("free_until"):
                lines.append(f"в «{title}» отель можно отменить бесплатно до {_day(c['free_until'])}")
            elif c.get("penalty_pct") is not None:
                lines.append(f"в «{title}» отмена отеля со штрафом {c['penalty_pct']} %")
        elif topic == "baggage":
            kg = [leg.get("baggage_checked_kg") for leg in flights if leg.get("type") == "flight"]
            if kg:
                lines.append(f"в «{title}» " + ("багаж " + f"{kg[0]} кг" if kg[0] else "без багажа"))
        elif topic == "transfer":
            trs = [leg for leg in legs if leg.get("type") == "transfer"]
            if trs:
                lines.append(f"в «{title}» трансферы: " + ", ".join(
                    f"{leg.get('from')} → {leg.get('to')} {_day(leg['dep_date'])} в {leg.get('dep_time')}"
                    for leg in trs if leg.get("dep_date") and leg.get("dep_time")))
        elif topic == "hotel" and stay.get("check_in_date"):
            lines.append(f"в «{title}» заезд {_day(stay['check_in_date'])} с {stay.get('check_in_time')}, выезд "
                         f"{_day(stay['check_out_date'])} до {stay.get('check_out_time')}")
        elif flights:
            lines.append(f"в «{title}» " + "; ".join(
                f"{leg.get('flight_no') or leg.get('train_no') or leg.get('from')} {_day(leg['dep_date'])}: "
                f"{leg.get('dep_time')} – {leg.get('arr_time')}" for leg in flights if leg.get("dep_date")))
    lines = [ln for ln in lines if ln and not ln.endswith(": ")]
    if not lines:
        return None
    text = "; ".join(lines[:3])
    return text[:1].upper() + text[1:] + "."


PET_Q_RE = re.compile(r"собак|соба|кошк|\bкот|питом|животн|п[её]с\b|псом|щен", re.I)


def _pet_lines_text(facts, question):
    """A pet question with a cart / order whose ticket lines have the pet fee inside: name those lines."""
    if not PET_Q_RE.search(question or ""):
        return None
    for f in facts:
        if f.get("kind") == "cart":
            items, total = f.get("items") or [], f.get("total_rub")
        elif f.get("kind") == "order":
            items, total = (f.get("data") or {}).get("items") or [], (f.get("data") or {}).get("total_rub")
        else:
            continue
        lines = [i for i in items if i.get("includes_ru")]
        if lines:
            where = "корзины" if f.get("kind") == "cart" else "заказа"
            return ("Провоз питомца уже в цене билетов: " + "; ".join(
                f"«{i['title']}» — {i['includes_ru']}" for i in lines[:2]) + "." +
                (f" Итого {P.fmt_rub(total)} — это сумма строк {where}, отдельной строки за питомца нет."
                 if total else ""))
    return None


def _info_text(ctx):
    facts = [f for f in ctx.info_facts or [] if isinstance(f, dict)]
    topic = _topic(ctx.info_question or ctx.text or "")
    order = next((f for f in facts if f.get("kind") == "order"), None)
    variants = [f for f in facts if f.get("kind") == "variant"]
    if variants and ctx.session.stage in ("selection", "cart"):
        order = None                       # a question about the variants on screen, not the old order
    road = next((road_answer_ru(f, f"{ctx.info_question or ''} {ctx.text or ''}") for f in facts
                 if f.get("kind") == "transport_search"), None)
    pets = _pet_lines_text(facts, f"{ctx.info_question or ''} {ctx.text or ''}")
    if pets:   # «а где в корзине собака?»: the lines that hold the fee, the total is their sum
        return pets
    if road and order is None:   # «а с пересадкой?» / «а поездом?» about a place without a road
        said = list(getattr(getattr(ctx.session, "history", None), "agent", None) or [])
        if said and said[-1].endswith(road):   # asked again: not the same sentence twice in a row
            labels = [a["label"] for a in ctx.extra_actions or [] if a.get("label")][:2]
            road = ("Проверил ещё раз — ответ тот же, другой дороги по этому маршруту в данных не появилось. "
                    + (f"Дальше: {' или '.join('«' + x + '»' for x in labels)} — кнопки ниже." if labels
                       else "Можно выбрать другое направление или даты."))
        return road
    if order is not None:
        items = [i for i in (order.get("data") or {}).get("items") or [] if i.get("status") != "cancelled"]
        kinds = {"transfer": ("transfer",), "flight": ("flight", "train"), "refund": ("flight", "train"),
                 "baggage": ("flight", "train"), "hotel": ("hotel",), "cancel": ("hotel",)}.get(topic)
        picked = [i for i in items if kinds is None or i.get("kind") in kinds] or items
        terms = [t for t in order.get("terms") or [] if _fare_word(t)]
        if topic == "refund" and terms:
            return "По вашему заказу: " + "; ".join(f"«{t['title']}» — {_fare_word(t)}" for t in terms[:3]) + "."
        if picked:
            text = "По вашему заказу: " + "; ".join(_order_item_line(i) for i in picked[:4]) + "."
            if topic == "cancel" and (order.get("if_cancelled") or {}).get("refund_rub") is not None:
                q = order["if_cancelled"]
                text += f" Если отменить сейчас, вернётся {P.fmt_rub(q['refund_rub'])}"
                text += f", а {P.fmt_rub(q['lost_rub'])} не вернут." if q.get("lost_rub") else "."
            return text
    if variants:
        text = _variant_info(topic, variants)
        if text:
            return text
    hotel = next((f for f in facts if f.get("kind") == "hotel"), None)
    pol = ((hotel or {}).get("data") or {}).get("policies") or {}
    if hotel is not None and topic in ("hotel", None) and pol.get("check_in_from"):
        name = (hotel.get("data") or {}).get("name") or "отеле"
        return (f"В «{name}» заселение с {pol['check_in_from']}"
                + (f", выезд до {pol['check_out_until']}." if pol.get("check_out_until") else "."))
    clim = next((f for f in facts if f.get("kind") == "climate"), None)
    data = (clim or {}).get("data") or {}
    if clim is not None and data.get("air_max_c") is not None and topic in ("weather", None):
        text = f"В {P.MONTHS_PREP[int(clim['month']) - 1]} днём обычно до {data['air_max_c']}°"
        if data.get("sea_c") is not None:
            text += f", море около {data['sea_c']}°"
        return text + "."
    dests = next((f for f in facts if f.get("kind") == "destinations" and f.get("items")), None)
    if dests is not None and topic in ("price", None):
        priced = [i for i in dests["items"] if i.get("price_from_rub")]
        if priced:
            fits = [i for i in priced if i.get("within_budget")]
            text = f"В цену «от» входит {priced[0].get('price_includes') or 'дорога и отель'}."
            if fits:
                text += " В бюджет укладываются: " + ", ".join(
                    f"{i['name']}{' поездом' if i.get('transport_ru') == 'поездом' else ''} — от "
                    f"{P.fmt_rub(i['price_from_rub'])}" for i in fits[:3]) + "."
            return text
    warn = next((f for f in facts if f.get("kind") == "warnings" and f.get("items")), None)
    if warn is not None:
        return "Обратите внимание: " + str(warn["items"][0]).rstrip(".") + "."
    return ("Точного ответа на это у меня нет. Спросите, пожалуйста, иначе — про билеты, отель, даты или "
            "трансфер, — и я подскажу.")


def _aftercare_text(ctx):
    r = ctx.aftercare_result or {}
    kind = r.get("kind") or r.get("action")
    if kind == "early_checkin":
        t = r.get("requested_time") or r.get("time")
        regular = f" Обычное заселение — с {r['check_in_from']}." if r.get("check_in_from") else ""
        hotel = f" в «{r['hotel']}»" if r.get("hotel") else ""
        if r.get("status") == "granted":
            price = (f" Доплата — {P.fmt_rub(r['price_rub'])}." if r.get("price_rub")
                     else " Без доплаты." if r.get("price_rub") == 0 else "")
            return f"Готово: ранний заезд{hotel} в {t} подтвердили.{price}"
        if r.get("status") == "on_request":
            return f"Запрос на заезд{hotel} в {t} отправил — отель подтвердит позже, пока это не гарантия.{regular}"
        return f"Ранний заезд{hotel} в {t} отель не даёт.{regular}"
    if kind == "change_dates":
        nd, q = r.get("new_dates") or {}, r.get("quote") or {}
        when = P.fmt_date_range(nd["check_in"], nd["check_out"]) if nd.get("check_in") and nd.get("check_out") else ""
        if r.get("reason") == "tickets_not_exchangeable":
            c = r.get("if_cancelled") or {}
            text = (f"Перенести этот заказ нельзя: билеты без обмена. Если его отменить, вернётся "
                    f"{P.fmt_rub(c.get('refund_rub') or 0)}, а {P.fmt_rub(c.get('lost_rub') or 0)} не вернут.")
            trip = r.get("new_trip") or {}
            if trip.get("total_rub"):
                text += f" Та же поездка на {when} — {P.fmt_rub(trip['total_rub'])}."
            return text
        if not r.get("available"):
            why = next((i.get("message") for i in r.get("issues") or [] if i.get("message")), None)
            return f"На {when} перенести не получится" + (f": {why[:1].lower() + why[1:]}." if why else ".")
        money = ""
        if q.get("new_total_rub") is not None and q.get("old_total_rub") is not None:
            diff = int(q.get("difference_rub") or 0)
            money = f" Новая цена — {P.fmt_rub(q['new_total_rub'])} вместо {P.fmt_rub(q['old_total_rub'])}"
            money += (f", доплата {P.fmt_rub(diff)}" if diff > 0 else f", вернём {P.fmt_rub(-diff)}" if diff < 0 else "")
            money += (f", штраф {P.fmt_rub(q['penalty_rub'])}." if q.get("penalty_rub") else ", штрафа нет.")
        if r.get("applied"):
            moved = [m for m in r.get("moved") or [] if m.get("kind") == "transfer" and m.get("new_time")]
            tail = "".join(f" Трансфер {P.transfer_way(m['title']) or ''} теперь {_day(m['new_date'])} в "
                           f"{m['new_time']}.".replace("  ", " ") for m in moved[:2])
            return f"Перенёс: теперь {when}.{money}{tail}"
        return f"На {when} перенести можно.{money} Если подходит — напишите «переносим», и я подтвержу."
    if kind == "add_transfer":
        done = r.get("transfers") or []
        if done:
            return " ".join(f"Трансфер {P.transfer_way(d['title']) or ''} {'перенёс' if d.get('replaced') else 'добавил'}: "
                            f"{_day(d['start'])} в {d['time']}, {P.fmt_rub(d['price_rub'])}.".replace("  ", " ")
                            for d in done[:2])
        return f"Трансфер добавил в заказ — итого {P.fmt_rub(r.get('total_rub') or 0)}."
    return "С заказом всё в порядке — подробности ниже."


def code_text(ctx):
    """The message of a degraded turn (the card failed or its guard failed twice): a short useful Russian text
    built by code from the same card input — never about models or data."""
    card = ctx.present or ""
    try:
        if card == "present.variants":
            return _variants_text(ctx)
        if card == "present.update":
            return _variants_text(ctx, update=True)
        if card == "present.destinations":
            return _destinations_text(ctx)
        if card == "present.compare":
            return _compare_text(ctx)
        if card == "present.info":
            return _info_text(ctx)
        if card == "present.aftercare":
            return _aftercare_text(ctx)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        ctx.trace["errors"].append(f"code_text {card}: {type(exc).__name__}: {exc}")
        return {"present.info": "Точного ответа на это у меня нет — спросите, пожалуйста, иначе.",
                "present.aftercare": "Запрос по заказу выполнен — состояние заказа ниже."}.get(
            card, "Вот что получилось — подробности в карточках ниже.")
    if card == "present.fallback" and getattr(ctx, "unsold", None):
        from .steps import not_sold_fact
        return not_sold_fact(ctx.unsold)["line"] + ". Если нужна сама поездка — напишите куда и когда, подберу."
    return FALLBACK_TEXT if card == "present.fallback" else "Готово."


def build_message(ctx):
    if ctx.error and ctx.message:
        body = ctx.message
    elif ctx.message:
        body = ctx.message
    elif ctx.card_output and ctx.card_output.get("message"):
        body = ctx.card_output["message"]
    else:
        body = code_text(ctx)
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
            "degraded": ctx.degraded, **({"guide": t["guide"]} if t.get("guide") is not None else {})}


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
    order = order_out(order_src, order_extra, s.brief)
    response = {
        "session_id": s.id, "turn": s.turn, "stage": s.stage, "message": build_message(ctx),
        "chips": [chip_out(c) for c in ctx.chips] if not ctx.error else [],
        "questions": [question_out(q) for q in ctx.questions][:2] if not ctx.error else [],
        "cards": cards, "comparison": comparison, "cart": cart_out(cart, s.brief), "order": order,
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

