"""Carts, checkout, payment and post-purchase operations (runtime state DB).

Cart rules:
- one open cart per session; `cart_add` creates it when missing;
- an offer is rebuilt from its id alone (offer_ids); a tampered id or an offer that no longer
  flies / is sold out is rejected;
- adding an offer for the same slot replaces the old item (same flight on another fare tier,
  same hotel and dates with another room or meal plan, same transfer with another kind, same
  train with another class) — this is how room_upgrade / meal_upgrade / flexible_fare apply;
- issues the mock can see: party size mismatches, overlapping stays/transport, arrival or
  departure days vs hotel dates, early arrival before check-in, transfer pickup vs flight times.
Checkout validates travellers against the items (count, airline age categories by birth date
at the trip start, an adult for every infant, hotel children policy and room fit) and creates
(or, for the same still-unpaid cart, updates) an order in `pending_payment`. `pay` refuses an
order with blocking issues.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db, flights, geo, hotels, rng, trains, transfers
from .db import ServiceError, jdump, jload
from .offer_ids import decode

BUILDERS = {"flight": flights.build, "train": trains.build, "hotel": hotels.build, "transfer": transfers.build}
CLASS_RU = {"seat": "сидячий", "platzkart": "плацкарт", "coupe": "купе", "sv": "СВ"}
KIND_RU = {"taxi": "такси", "shuttle": "шаттл", "public": "общественный транспорт", "private": "индивидуальный"}
TRAIN_CHANGE_FEE = 400  # per passenger, ticket exchange
ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DIGITS = "0123456789"


def _d(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%d.%m")


# ------------------------------------------------------------------ offers -> items

def rebuild(kind: str, offer_id: str) -> tuple[dict, dict]:
    _, params = decode(offer_id, expect_kind=kind)
    try:
        offer = BUILDERS[kind](params)
    except ServiceError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise ServiceError("invalid_offer_id", f"offer id params are incomplete: {exc}") from None
    if offer is None:
        raise ServiceError("offer_unavailable", "the offer is not available any more (sold out or not operated)")
    return offer, params


def item_payload(kind: str, offer: dict, params: dict) -> dict:
    """title, price, start, end, details (snapshot + party + slot key) for a cart/order item."""
    if kind == "flight":
        title = f"Перелёт {offer['origin_iata']} → {offer['dest_iata']}, {offer['flight_no']}"
        if offer["stops"]:
            title += f", пересадка в {offer['segments'][0]['dest_iata']}"
        party = {"adults": params["a"], "children": params["c"], "infants": params["i"]}
        slot = ["flight", params["o"], params["d"], params.get("h"), params["dt"], params["s"]]
        start, end, price = offer["dep"], offer["arr"], offer["price_rub"]
    elif kind == "train":
        title = (f"Поезд {offer['train_no']} {offer['from_station']} → {offer['to_station']}, "
                 f"{CLASS_RU[offer['car_class']]}")
        party = {"passengers": params["p"]}
        slot = ["train", params["o"], params["d"], params["dt"], params["s"]]
        start, end, price = offer["dep"], offer["arr"], offer["price_rub"]
    elif kind == "hotel":
        nights = (date.fromisoformat(params["co"]) - date.fromisoformat(params["ci"])).days
        title = f"{offer['name']}: {offer['room']['name']}, {nights} ноч., {hotels.MEAL_RU[offer['meals']]}"
        party = {"adults": params["a"], "children_ages": params["ages"]}
        slot = ["hotel", params["h"], params["ci"], params["co"]]
        start, end, price = offer["check_in"], offer["check_out"], offer["price_total_rub"]
    else:
        where = offer["_point"].get("iata") or offer["_point"].get("station") or "точка"
        route = f"«{offer['_hotel_name']}» → {where}" if offer["_reverse"] else f"{where} → «{offer['_hotel_name']}»"
        title = f"Трансфер ({KIND_RU[offer['kind']]}): {route}"
        party = {"pax": params["pax"]}
        slot = ["transfer", jdump(params["fr"]), params["h"], params["rv"], params["at"]]
        start, end, price = offer["_start"], offer["_end"], offer["price_rub"]
        offer = {**{k: v for k, v in offer.items() if not k.startswith("_")},
                 "point": offer["_point"], "hotel_id": offer["_hotel_id"], "reverse": offer["_reverse"],
                 "km": offer["_km"], "start": start, "end": end}
    if kind == "hotel":
        offer = {**offer, "destination_id": (hotels.hotel(params["h"]) or {}).get("destination_id")}
    details = {"offer": offer, "params": params, "party": party, "slot_key": jdump(slot)}
    return {"title": title, "price_rub": int(price), "start": start, "end": end, "details": details}


def party_size(kind: str, party: dict) -> int:
    if kind == "flight":
        return party["adults"] + party["children"] + party["infants"]
    if kind == "train":
        return party["passengers"]
    if kind == "hotel":
        return party["adults"] + len(party["children_ages"])
    return party["pax"]


# ------------------------------------------------------------------ issues

def _issue(code: str, message: str, severity: str = "warning", item_ids: list[str] | None = None) -> dict:
    return {"code": code, "message": message, "severity": severity, "item_ids": item_ids or []}


def compat_issues(items: list[dict]) -> list[dict]:
    """Compatibility problems between the items of a cart / order (see module docstring)."""
    issues: list[dict] = []
    live = [i for i in items if i.get("status", "pending") != "cancelled"]
    for it in live:
        it["_s"] = geo.parse_iso(it["start"]) if it.get("start") else None
        it["_e"] = geo.parse_iso(it["end"]) if it.get("end") else None
    sizes = {}
    for it in live:
        if it["kind"] != "transfer":
            sizes.setdefault(party_size(it["kind"], it["details"]["party"]), []).append(it["item_id"])
    if len(sizes) > 1:
        desc = ", ".join(f"{n} чел." for n in sorted(sizes))
        issues.append(_issue("party_mismatch", f"В позициях корзины разный состав: {desc}",
                             "warning", [i for ids in sizes.values() for i in ids]))
    transport = sorted([i for i in live if i["kind"] in ("flight", "train") and i["_s"]], key=lambda i: i["_s"])
    stays = sorted([i for i in live if i["kind"] == "hotel" and i["_s"]], key=lambda i: i["_s"])
    for a, b in zip(transport, transport[1:]):
        if b["_s"] < a["_e"]:
            issues.append(_issue("transport_overlap", f"«{b['title']}» отправляется раньше, чем прибывает «{a['title']}»",
                                 "blocking", [a["item_id"], b["item_id"]]))
    for a, b in zip(stays, stays[1:]):
        if b["_s"].date() < a["_e"].date():
            issues.append(_issue("dates_overlap", f"Проживание в «{b['title']}» начинается до выезда из «{a['title']}»",
                                 "warning", [a["item_id"], b["item_id"]]))
    for st in stays:
        ci, co = st["_s"], st["_e"]
        arrivals = [t for t in transport if t["_s"].date() <= ci.date()]
        departures = [t for t in transport if t["_s"].date() > ci.date()]
        if arrivals:
            t = arrivals[-1]
            arr = t["_e"].astimezone(ci.tzinfo)
            if arr.date() > ci.date():
                issues.append(_issue("dates_mismatch", f"Прибытие {_d(t['end'])}, а заезд {ci.strftime('%d.%m')}: "
                                     "первая ночь оплачена, но не используется", "warning", [t["item_id"], st["item_id"]]))
            elif arr.date() < ci.date():
                issues.append(_issue("night_without_hotel", f"Прибытие {_d(t['end'])}, а заезд только "
                                     f"{ci.strftime('%d.%m')}: ночь без отеля", "warning", [t["item_id"], st["item_id"]]))
            elif (ci - arr) >= timedelta(hours=3):
                hours = int((ci - arr).total_seconds() // 3600)
                issues.append(_issue("early_arrival", f"Прибытие в {arr.strftime('%H:%M')}, заселение с "
                                     f"{ci.strftime('%H:%M')}: ждать около {hours} ч (можно запросить ранний заезд)",
                                     "info", [t["item_id"], st["item_id"]]))
        if departures:
            t = departures[0]
            dep = t["_s"].astimezone(co.tzinfo)
            if dep.date() > co.date():
                issues.append(_issue("night_without_hotel", f"Выезд из отеля {co.strftime('%d.%m')}, а отправление "
                                     f"{_d(t['start'])}: ночь без отеля", "warning", [st["item_id"], t["item_id"]]))
            elif dep.date() < co.date():
                issues.append(_issue("dates_mismatch", f"Отправление {_d(t['start'])}, а выезд {co.strftime('%d.%m')}: "
                                     "последняя ночь оплачена, но не используется", "warning", [st["item_id"], t["item_id"]]))
            elif dep < co:
                issues.append(_issue("departure_before_checkout", f"Отправление в {dep.strftime('%H:%M')}, "
                                     f"до расчётного часа {co.strftime('%H:%M')}", "info", [st["item_id"], t["item_id"]]))
    hotel_ids = {st["details"]["params"]["h"] for st in stays}
    for tr in [i for i in live if i["kind"] == "transfer"]:
        off = tr["details"]["offer"]
        if hotel_ids and off["hotel_id"] not in hotel_ids:
            issues.append(_issue("transfer_hotel_mismatch", f"«{tr['title']}» едет не в отель из корзины",
                                 "warning", [tr["item_id"]]))
        iata = off["point"].get("iata")
        if not iata:
            continue
        if not off["reverse"]:
            arriving = [f for f in transport if f["kind"] == "flight" and f["details"]["offer"]["dest_iata"] == iata
                        and abs((tr["_s"] - f["_e"]).total_seconds()) < 86400]
            if arriving:
                f = min(arriving, key=lambda f: abs((tr["_s"] - f["_e"]).total_seconds()))
                gap = (tr["_s"] - f["_e"]).total_seconds() / 60
                need = 45 if f["details"]["offer"]["international"] else 30
                if gap < 0:
                    issues.append(_issue("transfer_too_early", "Трансфер подаётся раньше посадки самолёта",
                                         "blocking", [f["item_id"], tr["item_id"]]))
                elif gap < need:
                    issues.append(_issue("transfer_too_early", f"Между посадкой и трансфером {int(gap)} мин — "
                                         f"нужно не меньше {need} (багаж, контроль)", "warning",
                                         [f["item_id"], tr["item_id"]]))
                elif gap > 240:
                    issues.append(_issue("transfer_wait", f"После посадки ждать трансфер {int(gap // 60)} ч",
                                         "info", [f["item_id"], tr["item_id"]]))
        else:
            leaving = [f for f in transport if f["kind"] == "flight" and f["details"]["offer"]["origin_iata"] == iata
                       and abs((f["_s"] - tr["_e"]).total_seconds()) < 86400]
            if leaving:
                f = min(leaving, key=lambda f: abs((f["_s"] - tr["_e"]).total_seconds()))
                gap = (f["_s"] - tr["_e"]).total_seconds() / 60
                need = 180 if f["details"]["offer"]["international"] else 120
                if gap < need:
                    sev = "blocking" if gap < 40 else "warning"
                    issues.append(_issue("transfer_too_late", f"Трансфер привозит в аэропорт за {max(0, int(gap))} мин "
                                         f"до вылета — нужно не меньше {need}", sev, [tr["item_id"], f["item_id"]]))
    for it in live:
        it.pop("_s", None)
        it.pop("_e", None)
    return issues


# ------------------------------------------------------------------ carts

def _items(conn, table: str, key: str, value: str) -> list[dict]:
    cols = "item_id, offer_id, kind, title, price_rub, start_at, end_at, details"
    if table == "order_items":
        cols += ", booking_number, status"
    rows = conn.execute(f"SELECT {cols} FROM {table} WHERE {key} = ? ORDER BY start_at, item_id", (value,)).fetchall()
    out = []
    for r in rows:
        d = {"item_id": r["item_id"], "offer_id": r["offer_id"], "kind": r["kind"], "title": r["title"],
             "price_rub": r["price_rub"], "start": r["start_at"], "end": r["end_at"], "details": jload(r["details"], {})}
        if table == "order_items":
            d["booking_number"] = r["booking_number"]
            d["status"] = r["status"]
        out.append(d)
    return out


def cart_out(conn, cart_id: str) -> dict:
    row = conn.execute("SELECT * FROM carts WHERE cart_id = ?", (cart_id,)).fetchone()
    if row is None:
        raise ServiceError("cart_not_found", f"no cart {cart_id!r}")
    items = _items(conn, "cart_items", "cart_id", cart_id)
    return {"cart_id": cart_id, "session_id": row["session_id"], "status": row["status"], "items": items,
            "total_rub": sum(i["price_rub"] for i in items), "issues": compat_issues([dict(i) for i in items]),
            "synthetic": True}


def _open_cart(conn, session_id: str) -> str | None:
    row = conn.execute("SELECT cart_id FROM carts WHERE session_id = ? AND status = 'open'", (session_id,)).fetchone()
    return row["cart_id"] if row else None


def cart_create(session_id: str, now: str) -> dict:
    with db.state() as conn:
        cart_id = _open_cart(conn, session_id)
        if cart_id is None:
            cart_id = _new_id(conn, "carts", "cart_id", "c_", session_id, now)
            conn.execute("INSERT INTO carts (cart_id, session_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (cart_id, session_id, "open", now, now))
        return cart_out(conn, cart_id)


def _new_id(conn, table: str, col: str, prefix: str, *parts) -> str:
    n = 0
    while True:
        cand = prefix + rng.hexid(*parts, n, n=12)
        if conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", (cand,)).fetchone() is None:
            return cand
        n += 1


def cart_add(session_id: str, offer_id: str, kind: str, now: str) -> dict:
    offer, params = rebuild(kind, offer_id)
    payload = item_payload(kind, offer, params)
    with db.state() as conn:
        cart_id = _open_cart(conn, session_id)
        if cart_id is None:
            cart_id = _new_id(conn, "carts", "cart_id", "c_", session_id, now)
            conn.execute("INSERT INTO carts (cart_id, session_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (cart_id, session_id, "open", now, now))
        for it in _items(conn, "cart_items", "cart_id", cart_id):
            if it["offer_id"] == offer_id:
                return cart_out(conn, cart_id)
            if it["details"].get("slot_key") == payload["details"]["slot_key"]:
                conn.execute("DELETE FROM cart_items WHERE item_id = ?", (it["item_id"],))
        item_id = "ci_" + rng.hexid(cart_id, offer_id, n=12)
        conn.execute("INSERT INTO cart_items (item_id, cart_id, offer_id, kind, title, price_rub, start_at, end_at, "
                     "details, added_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (item_id, cart_id, offer_id, kind, payload["title"], payload["price_rub"], payload["start"],
                      payload["end"], jdump(payload["details"]), now))
        conn.execute("UPDATE carts SET updated_at = ? WHERE cart_id = ?", (now, cart_id))
        return cart_out(conn, cart_id)


def cart_remove(session_id: str, offer_id: str, kind: str) -> dict:
    with db.state() as conn:
        cart_id = _open_cart(conn, session_id)
        if cart_id is None:
            raise ServiceError("cart_not_found", f"no open cart for session {session_id!r}")
        conn.execute("DELETE FROM cart_items WHERE cart_id = ? AND offer_id = ? AND kind = ?", (cart_id, offer_id, kind))
        return cart_out(conn, cart_id)


def cart_get(session_id: str) -> dict:
    with db.state() as conn:
        cart_id = _open_cart(conn, session_id)
        if cart_id is None:
            row = conn.execute("SELECT cart_id FROM carts WHERE session_id = ? ORDER BY updated_at DESC, rowid DESC "
                               "LIMIT 1", (session_id,)).fetchone()
            if row is None:
                raise ServiceError("cart_not_found", f"no cart for session {session_id!r}")
            cart_id = row["cart_id"]
        return cart_out(conn, cart_id)


# ------------------------------------------------------------------ checkout / pay

def age_on(birth: date, day: date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def traveller_issues(items: list[dict], travellers: list[dict]) -> list[dict]:
    issues: list[dict] = []
    starts = [geo.parse_iso(i["start"]).date() for i in items if i.get("start")]
    trip_start = min(starts) if starts else None
    births = [date.fromisoformat(str(t["birth_date"])) for t in travellers]
    if trip_start is None:
        return issues
    if any(b > trip_start for b in births):
        issues.append(_issue("invalid_birth_date", "Дата рождения позже даты поездки", "blocking"))
        return issues
    ages = [age_on(b, trip_start) for b in births]
    n_adults18 = len([a for a in ages if a >= 18])
    if n_adults18 == 0:
        issues.append(_issue("no_adult", "Нужен хотя бы один взрослый старше 18 лет", "blocking"))
    for it in items:
        need = party_size(it["kind"], it["details"]["party"])
        if it["kind"] != "transfer" and need != len(travellers):
            issues.append(_issue("travellers_count", f"Путешественников {len(travellers)}, а «{it['title']}» "
                                 f"оформлено на {need} чел.", "blocking", [it["item_id"]]))
    for it in [i for i in items if i["kind"] == "flight"]:
        day = geo.parse_iso(it["start"]).date()
        a_ = [age_on(b, day) for b in births]
        cat = (len([x for x in a_ if x >= 12]), len([x for x in a_ if 2 <= x < 12]), len([x for x in a_ if x < 2]))
        p = it["details"]["party"]
        if cat[2] != p["infants"]:
            issues.append(_issue("infant_fare", f"В билетах младенцев без места: {p['infants']}, а по датам рождения — "
                                 f"{cat[2]}: билеты нужно переоформить", "blocking", [it["item_id"]]))
        elif (cat[0], cat[1]) != (p["adults"], p["children"]):
            issues.append(_issue("child_fare", f"Тарифы билетов (взрослых {p['adults']}, детских {p['children']}) "
                                 f"не совпадают с возрастами (взрослых {cat[0]}, детей 2–11 лет {cat[1]})",
                                 "warning", [it["item_id"]]))
        if cat[2] > len([x for x in a_ if x >= 18]):
            issues.append(_issue("infant_without_adult", "На каждого младенца без места нужен отдельный взрослый",
                                 "blocking", [it["item_id"]]))
    for it in [i for i in items if i["kind"] == "hotel"]:
        p = it["details"]["params"]
        day = date.fromisoformat(p["ci"])
        a_ = [age_on(b, day) for b in births]
        kids = sorted(x for x in a_ if x < 18)
        adults = len(a_) - len(kids)
        pol = hotels.policy_of(p["h"])
        if kids and not pol["children_allowed"]:
            issues.append(_issue("hotel_age_limit", "Отель не принимает детей", "blocking", [it["item_id"]]))
            continue
        if kids and pol["min_child_age"] is not None and kids[0] < pol["min_child_age"]:
            issues.append(_issue("hotel_age_limit", f"Отель принимает детей с {pol['min_child_age']} лет",
                                 "blocking", [it["item_id"]]))
            continue
        room = next((r for r in hotels.rooms_of(p["h"]) if r["room_id"] == p["r"]), None)
        if room and adults >= 1 and len(a_) == p["a"] + len(p["ages"]):
            fit = hotels.room_fit(room, adults, kids, pol)
            if fit is None or fit[0] != p["n"]:
                issues.append(_issue("child_bed", "Номер не вмещает состав по датам рождения — нужен другой номер",
                                     "blocking", [it["item_id"]]))
            elif fit[1] > p["x"]:
                issues.append(_issue("child_bed", f"По возрастам нужна доп. кровать ({pol['extra_bed_price_rub']} ₽ "
                                     "за ночь), её нет в брони", "warning", [it["item_id"]]))
        if any(x < 2 for x in kids):
            cot = pol["infant_cot"]
            if cot == "none":
                issues.append(_issue("child_bed", "В отеле нет детских кроваток", "warning", [it["item_id"]]))
            elif cot == "paid":
                issues.append(_issue("child_bed", "Детская кроватка в отеле платная", "info", [it["item_id"]]))
            elif cot == "on_request":
                issues.append(_issue("child_bed", "Детская кроватка — по запросу, отель подтвердит", "info",
                                     [it["item_id"]]))
    return issues


def checkout(cart_id: str, travellers: list[dict], contact: dict, now: str) -> dict:
    with db.state() as conn:
        cart = conn.execute("SELECT * FROM carts WHERE cart_id = ?", (cart_id,)).fetchone()
        if cart is None:
            raise ServiceError("cart_not_found", f"no cart {cart_id!r}")
        existing = conn.execute("SELECT * FROM orders WHERE cart_id = ? ORDER BY created_at DESC LIMIT 1",
                                (cart_id,)).fetchone()
        if cart["status"] == "checked_out" and (existing is None or existing["status"] != "pending_payment"):
            raise ServiceError("cart_checked_out", "the cart is already paid; create a new cart")
        items = _items(conn, "cart_items", "cart_id", cart_id)
        if not items:
            raise ServiceError("cart_empty", "the cart has no items")
        issues = compat_issues([dict(i) for i in items]) + traveller_issues(items, travellers)
        total = sum(i["price_rub"] for i in items)
        if existing is not None and existing["status"] == "pending_payment":
            order_id = existing["order_id"]
            conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
            conn.execute("DELETE FROM travellers WHERE order_id = ?", (order_id,))
            conn.execute("UPDATE orders SET total_rub = ?, contact = ?, issues = ?, updated_at = ? WHERE order_id = ?",
                         (total, jdump(contact), jdump(issues), now, order_id))
            _event(conn, order_id, now, "travellers_updated", {"issues": len(issues)})
        else:
            order_id = _new_id(conn, "orders", "order_id", "o_", cart_id, now)
            conn.execute("INSERT INTO orders (order_id, cart_id, session_id, status, total_rub, contact, issues, "
                         "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (order_id, cart_id, cart["session_id"], "pending_payment", total, jdump(contact),
                          jdump(issues), now, now))
            _event(conn, order_id, now, "created", {"items": len(items), "issues": len(issues)})
        for it in items:
            conn.execute("INSERT INTO order_items (item_id, order_id, offer_id, kind, title, price_rub, booking_number, "
                         "status, start_at, end_at, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         ("oi_" + rng.hexid(order_id, it["offer_id"], n=12), order_id, it["offer_id"], it["kind"],
                          it["title"], it["price_rub"], None, "pending", it["start"], it["end"], jdump(it["details"])))
        for pos, t in enumerate(travellers):
            conn.execute("INSERT INTO travellers (order_id, position, name, birth_date) VALUES (?,?,?,?)",
                         (order_id, pos, t["name"], str(t["birth_date"])))
        conn.execute("UPDATE carts SET status = 'checked_out', updated_at = ? WHERE cart_id = ?", (now, cart_id))
        return {"order_id": order_id, "status": "pending_payment", "total_rub": total, "issues": issues,
                "synthetic": True}


def _event(conn, order_id: str, at: str, kind: str, data: dict) -> None:
    conn.execute("INSERT INTO order_events (order_id, at, kind, data) VALUES (?,?,?,?)", (order_id, at, kind, jdump(data)))


def _booking_number(order_id: str, item: dict) -> str:
    seed = (order_id, item["item_id"], "booking")
    if item["kind"] == "flight":
        return rng.code(ALNUM, 6, *seed)
    if item["kind"] == "hotel":
        return "OST-" + rng.code(DIGITS, 8, *seed)
    if item["kind"] == "train":
        return "RZD-" + rng.code(DIGITS, 10, *seed)
    return "TRF-" + rng.code(ALNUM, 6, *seed)


def _order_row(conn, order_id: str):
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None:
        raise ServiceError("order_not_found", f"no order {order_id!r}")
    return row


def pay(order_id: str, method: str, now: str) -> dict:
    with db.state() as conn:
        row = _order_row(conn, order_id)
        items = _items(conn, "order_items", "order_id", order_id)
        if row["status"] == "pending_payment":
            blocking = [i for i in jload(row["issues"], []) if i.get("severity") == "blocking"]
            if blocking:
                raise ServiceError("blocking_issues", "order has blocking issues: "
                                   + "; ".join(i["code"] for i in blocking))
            for it in items:
                num = _booking_number(order_id, it)
                conn.execute("UPDATE order_items SET booking_number = ?, status = 'confirmed' WHERE item_id = ?",
                             (num, it["item_id"]))
            conn.execute("UPDATE orders SET status = 'paid', updated_at = ? WHERE order_id = ?", (now, order_id))
            _event(conn, order_id, now, "paid", {"method": method, "total_rub": row["total_rub"]})
            items = _items(conn, "order_items", "order_id", order_id)
        elif row["status"] == "cancelled":
            raise ServiceError("order_cancelled", "the order is cancelled")
        return {"order_id": order_id, "status": "paid",
                "bookings": [{"item_id": i["item_id"], "booking_number": i["booking_number"]} for i in items
                             if i["booking_number"]], "synthetic": True}


def order_out(conn, order_id: str) -> dict:
    row = _order_row(conn, order_id)
    items = _items(conn, "order_items", "order_id", order_id)
    travellers = [{"name": r["name"], "birth_date": r["birth_date"]} for r in
                  conn.execute("SELECT name, birth_date FROM travellers WHERE order_id = ? ORDER BY position",
                               (order_id,)).fetchall()]
    events = [{"at": r["at"], "kind": r["kind"], "data": jload(r["data"], {})} for r in
              conn.execute("SELECT at, kind, data FROM order_events WHERE order_id = ? ORDER BY id", (order_id,))]
    return {"order_id": order_id, "cart_id": row["cart_id"], "session_id": row["session_id"], "status": row["status"],
            "items": items, "travellers": travellers, "contact": jload(row["contact"], {}),
            "total_rub": row["total_rub"], "issues": jload(row["issues"], []), "events": events, "synthetic": True}


def order_get(order_id: str) -> dict:
    with db.state() as conn:
        return order_out(conn, order_id)


def _require_paid(row) -> None:
    if row["status"] not in ("paid", "changed"):
        raise ServiceError("order_not_paid", f"order status is {row['status']}; the action needs a paid order")


def _hotel_item(items: list[dict]) -> dict:
    hs = [i for i in items if i["kind"] == "hotel" and i["status"] != "cancelled"]
    if not hs:
        raise ServiceError("no_hotel", "the order has no hotel")
    return hs[0]


# ------------------------------------------------------------------ post-purchase

def request_early_checkin(order_id: str, time_hhmm: str, now: str) -> dict:
    with db.state() as conn:
        row = _order_row(conn, order_id)
        _require_paid(row)
        items = _items(conn, "order_items", "order_id", order_id)
        it = _hotel_item(items)
        p = it["details"]["params"]
        pol = hotels.policy_of(p["h"])
        req, regular = geo.hhmm_to_min(time_hhmm), geo.hhmm_to_min(pol["check_in_from"])
        prev = it["details"].get("early_checkin") or {}
        prev_charged = prev.get("price_rub") if prev.get("status") == "granted" else 0
        if req >= regular:
            status, price = "granted", 0
        else:
            r = rng.unit("early_checkin", p["h"], p["ci"], time_hhmm)
            hours = (regular - req) / 60
            nights = max(1, (date.fromisoformat(p["co"]) - date.fromisoformat(p["ci"])).days)
            night_price = pricing_round(it["price_rub"] / nights)
            price = pol["early_checkin_price_rub"] if pol["early_checkin_available"] else None
            if req < 6 * 60:  # before 06:00 the hotel sells the previous night
                price = night_price
            if pol["early_checkin_available"]:
                status = "granted" if (hours <= 4 and r < 0.75) else ("on_request" if r < 0.92 else "refused")
            else:
                status = "on_request" if r < 0.35 else "refused"
            if status == "refused":
                price = None
        charged = price if status == "granted" and price else 0
        details = {**it["details"], "early_checkin": {"time": time_hhmm, "status": status, "price_rub": price}}
        delta = (charged or 0) - (prev_charged or 0)
        conn.execute("UPDATE order_items SET details = ?, price_rub = price_rub + ? WHERE item_id = ?",
                     (jdump(details), delta, it["item_id"]))
        conn.execute("UPDATE orders SET total_rub = total_rub + ?, updated_at = ? WHERE order_id = ?",
                     (delta, now, order_id))
        _event(conn, order_id, now, f"early_checkin_{status}", {"time": time_hhmm, "price_rub": price})
        return {"order_id": order_id, "status": status, "price_rub": price, "synthetic": True}


def pricing_round(x: float) -> int:
    return int(round(x / 10.0)) * 10


def _shift_iso(value: str, days: int) -> str:
    return geo.iso(geo.parse_iso(value) + timedelta(days=days))


def _rebuild_shifted(kind: str, params: dict, days: int, today: str) -> dict | None:
    new = dict(params, t=today)
    if kind == "flight" or kind == "train":
        day = date.fromisoformat(params["dt"]) + timedelta(days=days)
        new["dt"] = day.isoformat()
        offer = BUILDERS[kind](new)
        if offer is not None:
            return {"params": new, "offer": offer}
        if kind == "flight":  # same route and tier, the departure closest in time that day
            first_to = new.get("h") or new["d"]
            want = flights._slot(new["o"], first_to, params["s"])[0]
            for s in sorted(flights.active_slots(new["o"], first_to, day),
                            key=lambda s: abs(flights._slot(new["o"], first_to, s)[0] - want)):
                cand = dict(new, s=s)
                offer = flights.build(cand)
                if offer:
                    return {"params": cand, "offer": offer}
        else:
            for s in range(5):
                cand = dict(new, s=s)
                offer = trains.build(cand)
                if offer:
                    return {"params": cand, "offer": offer}
        return None
    if kind == "transfer":
        new["at"] = _shift_iso(params["at"], days)
        offer = transfers.build(new)
        return {"params": new, "offer": offer} if offer else None
    return None


def change_dates(order_id: str, check_in: date, check_out: date, confirm: bool, now: str) -> dict:
    today = geo.parse_iso(now).date()
    with db.state() as conn:
        row = _order_row(conn, order_id)
        _require_paid(row)
        items = _items(conn, "order_items", "order_id", order_id)
        hotel_it = _hotel_item(items)
        hp = hotel_it["details"]["params"]
        old_ci, old_co = date.fromisoformat(hp["ci"]), date.fromisoformat(hp["co"])
        d_in, d_out = (check_in - old_ci).days, (check_out - old_co).days
        issues: list[dict] = []
        if check_in < today:
            issues.append(_issue("dates_in_past", "Новая дата заезда уже прошла", "blocking"))
            return {"order_id": order_id, "available": False, "quote": None, "applied": False, "order": None,
                    "issues": issues, "synthetic": True}
        changes: list[dict] = []  # {item, params, offer, fee}
        available = True
        new_hotel_params = dict(hp, ci=check_in.isoformat(), co=check_out.isoformat(), t=today.isoformat())
        try:
            new_hotel = hotels.build(new_hotel_params)
        except (KeyError, ValueError):
            new_hotel = None
        if new_hotel is None:
            available = False
            issues.append(_issue("hotel_unavailable", "На новые даты этого номера нет", "blocking", [hotel_it["item_id"]]))
        else:
            pol = hotels.policy_of(hp["h"])
            free_until = hotel_it["details"]["offer"]["cancellation"].get("free_until")
            free = free_until is not None and geo.parse_iso(now) <= geo.parse_iso(free_until)
            penalty = 0 if free else pricing_round(hotel_it["price_rub"] * int(pol["penalty_pct"]) / 100
                                                   if pol["free_cancellation_days"] is not None else hotel_it["price_rub"])
            if penalty:
                issues.append(_issue("change_penalty", f"Бесплатная отмена уже недоступна: штраф отеля {penalty} ₽",
                                     "warning", [hotel_it["item_id"]]))
            changes.append({"item": hotel_it, "params": new_hotel_params, "offer": new_hotel, "fee": penalty})
        for it in items:
            if it["kind"] in ("hotel",) or it["status"] == "cancelled":
                continue
            start = geo.parse_iso(it["start"]).date()
            delta = d_in if start <= old_ci else d_out
            if delta == 0:
                continue
            if it["kind"] == "flight" and it["details"]["offer"]["fare"]["change_fee_rub"] is None:
                issues.append(_issue("flight_change_not_allowed", f"«{it['title']}»: тариф без обмена — "
                                     "перелёт останется на прежнюю дату", "warning", [it["item_id"]]))
                continue
            res = _rebuild_shifted(it["kind"], it["details"]["params"], delta, today.isoformat())
            if res is None:
                available = False
                issues.append(_issue(f"{it['kind']}_unavailable", f"«{it['title']}»: на новую дату нет такого варианта",
                                     "blocking", [it["item_id"]]))
                continue
            fee = 0
            if it["kind"] == "flight":
                fee = it["details"]["offer"]["fare"]["change_fee_rub"] or 0
            elif it["kind"] == "train":
                fee = TRAIN_CHANGE_FEE * it["details"]["party"]["passengers"]
            changes.append({"item": it, "params": res["params"], "offer": res["offer"], "fee": fee})
        old_total = row["total_rub"]
        fees = sum(c["fee"] for c in changes)
        new_payloads = [(c, item_payload(c["item"]["kind"], c["offer"], c["params"])) for c in changes]
        new_total = old_total - sum(c["item"]["price_rub"] for c in changes) + sum(p["price_rub"] for _, p in new_payloads) + fees
        quote = {"old_total_rub": old_total, "new_total_rub": max(0, new_total), "difference_rub": new_total - old_total,
                 "penalty_rub": fees}
        applied, order = False, None
        if confirm and available:
            for c, pl in new_payloads:
                details = dict(pl["details"])
                if c["item"]["kind"] == "hotel" and c["item"]["details"].get("early_checkin"):
                    issues.append(_issue("early_checkin_reset", "Ранний заезд нужно запросить заново на новую дату",
                                         "info", [c["item"]["item_id"]]))
                conn.execute("UPDATE order_items SET offer_id = ?, title = ?, price_rub = ?, start_at = ?, end_at = ?, "
                             "details = ?, status = 'changed' WHERE item_id = ?",
                             (pl["details"]["offer"]["offer_id"], pl["title"], pl["price_rub"] + c["fee"], pl["start"],
                              pl["end"], jdump(details), c["item"]["item_id"]))
            conn.execute("UPDATE orders SET status = 'changed', total_rub = ?, updated_at = ? WHERE order_id = ?",
                         (max(0, new_total), now, order_id))
            _event(conn, order_id, now, "dates_changed", {"check_in": check_in.isoformat(),
                   "check_out": check_out.isoformat(), "difference_rub": new_total - old_total, "fees_rub": fees})
            applied = True
            order = order_out(conn, order_id)
            new_issues = compat_issues([dict(i) for i in order["items"]])
            if new_issues:
                issues.extend(new_issues)
        return {"order_id": order_id, "available": available, "quote": quote, "applied": applied, "order": order,
                "issues": issues, "synthetic": True}


def add_transfer(order_id: str, offer_id: str, now: str) -> dict:
    offer, params = rebuild("transfer", offer_id)
    payload = item_payload("transfer", offer, params)
    with db.state() as conn:
        row = _order_row(conn, order_id)
        if row["status"] == "cancelled":
            raise ServiceError("order_cancelled", "the order is cancelled")
        items = _items(conn, "order_items", "order_id", order_id)
        if any(i["offer_id"] == offer_id for i in items):
            return order_out(conn, order_id)
        item_id = "oi_" + rng.hexid(order_id, offer_id, n=12)
        paid = row["status"] in ("paid", "changed")
        it = {"item_id": item_id, "kind": "transfer"}
        conn.execute("INSERT INTO order_items (item_id, order_id, offer_id, kind, title, price_rub, booking_number, "
                     "status, start_at, end_at, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (item_id, order_id, offer_id, "transfer", payload["title"], payload["price_rub"],
                      _booking_number(order_id, it) if paid else None, "confirmed" if paid else "pending",
                      payload["start"], payload["end"], jdump(payload["details"])))
        conn.execute("UPDATE orders SET total_rub = total_rub + ?, updated_at = ? WHERE order_id = ?",
                     (payload["price_rub"], now, order_id))
        _event(conn, order_id, now, "transfer_added", {"item_id": item_id, "price_rub": payload["price_rub"]})
        order = order_out(conn, order_id)
        order["issues"] = _merge_issues(order["issues"], compat_issues([dict(i) for i in order["items"]]))
        return order


def _merge_issues(a: list[dict], b: list[dict]) -> list[dict]:
    seen, out = set(), []
    for i in a + b:
        k = (i["code"], i["message"])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out
