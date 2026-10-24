"""Offline fakes for the engine tests: Ostrovok mock, open data, ENGINE-B domain, present-card echo."""
import json
from datetime import date, datetime, timedelta

from agent.mcp_client import ToolError, validate_input, validate_output
from agent.state import (DistrictProfile, Fit, Note, Price, Question, Choice, Step, Totals, UpsellCandidate, Variant,
                         VariantHotel)

TZ = "+03:00"

DESTS = {
    "sochi": {"id": "sochi", "name_ru": "Сочи", "name_en": "Sochi", "country": "Россия", "kind": "resort",
              "tags": ["sea", "beach", "mountains", "warm"], "airports": ["AER"], "lat": 43.6, "lon": 39.7,
              "description_ru": "Море и горы."},
    "antalya": {"id": "antalya", "name_ru": "Анталья", "name_en": "Antalya", "country": "Турция", "kind": "resort",
                "tags": ["sea", "beach", "warm"], "airports": ["AYT"], "lat": 36.9, "lon": 30.7,
                "description_ru": "Тёплое море."},
    "kazan": {"id": "kazan", "name_ru": "Казань", "name_en": "Kazan", "country": "Россия", "kind": "city",
              "tags": ["city", "history", "food"], "airports": ["KZN"], "lat": 55.8, "lon": 49.1,
              "description_ru": "Кремль и еда."},
}
HOTELS = [  # (suffix, name, stars, rating, per night, meals, penalty, pets, early price, osm)
    ("h1", "Гостевой дом Лаванда", 2, 8.1, 4000, "none", 100, False, 1500, "node/1"),
    ("h2", "Отель Ривьера", 4, 8.7, 8000, "breakfast", 30, True, 2500, "node/2"),
    ("h3", "Гранд Отель", 5, 9.1, 15000, "half_board", 0, False, None, "node/3"),
]


def _dt(day, hhmm):
    return f"{day}T{hhmm}:00{TZ}"


class FakeOstrovok:
    """Implements every tool with tiny deterministic data; validates payloads with the real contract."""

    def __init__(self, fail_tools=(), no_hotels=False):
        self.calls = []
        self.fail_tools = set(fail_tools)
        self.no_hotels = no_hotels
        self.offers = {}
        self.carts = {}
        self.orders = {}
        self.events = []

    def count(self, tool=None):
        return sum(1 for t, _ in self.calls if tool is None or t == tool)

    def tools_called(self):
        return [t for t, _ in self.calls]

    def call(self, tool, payload):
        clean = validate_input(tool, payload)
        self.calls.append((tool, clean))
        if tool in self.fail_tools:
            raise ToolError(tool, "down", code="unavailable")
        out = getattr(self, tool)(clean)
        return validate_output(tool, out)

    # ---- catalog
    def quote_destinations(self, p):
        quotes = []
        for did in p["destination_ids"]:
            if did not in DESTS:
                quotes.append({"destination_id": did, "available": False})
                continue
            if p.get("dates"):
                ci, co = date.fromisoformat(p["dates"]["check_in"]), date.fromisoformat(p["dates"]["check_out"])
            else:
                today = date.fromisoformat(p["today"])
                year = today.year if p["month"] >= today.month else today.year + 1
                ci = date(year, p["month"], 12)
                co = ci + timedelta(days=p.get("nights") or 7)
            pax = p["party"]["adults"] + len(p["party"]["children_ages"])
            nights = (co - ci).days
            fl, st = 12000 * pax, 4000 * nights
            quotes.append({"destination_id": did, "available": True, "nights": nights, "check_in": ci.isoformat(),
                           "check_out": co.isoformat(), "flight_price_rub": fl, "stay_price_rub": st,
                           "total_rub": fl + st})
        return {"quotes": quotes}

    def search_hotels(self, p):
        if self.no_hotels:
            return {"offers": [], "total_found": 0}
        ci, co = p["check_in"], p["check_out"]
        nights = (date.fromisoformat(co) - date.fromisoformat(ci)).days
        f = p.get("filters") or {}
        offers = []
        for suf, name, stars, rating, night, meals, pen, pets, early, osm in HOTELS:
            if f.get("pets") and not pets:
                continue
            hid = f"{p['destination_id']}_{suf}"
            if p.get("hotel_id") and p["hotel_id"] != hid:
                continue
            oid = f"ht|{hid}|{ci}|{co}|{p['adults']}|{','.join(map(str, p.get('children_ages') or []))}"
            price = int(night * nights * (1 + 0.3 * (p["adults"] - 1)))
            offer = {"offer_id": oid, "hotel_id": hid, "osm_id": osm, "name": name, "kind": "hotel", "stars": stars,
                     "rating": rating, "reviews": 100, "lat": 43.6, "lon": 39.7,
                     "room": {"room_id": "std", "name": "Стандарт", "capacity_adults": 3, "capacity_children": 2},
                     "meals": meals,
                     "cancellation": {"free_until": _dt(ci, "12:00") if pen == 0 else None, "penalty_pct": pen},
                     "price_total_rub": price, "pets_allowed": pets, "kids_policy": {"children_allowed": True},
                     "early_checkin": {"available": early is not None, "price_rub": early},
                     "check_in": _dt(ci, "14:00"), "check_out": _dt(co, "12:00"), "distance_beach_m": 300,
                     "distance_center_m": 1000}
            self.offers[oid] = ("hotel", price, offer["check_in"], offer["check_out"], f"{name}, {nights} ночей")
            offers.append(offer)
        return {"offers": offers, "total_found": len(offers)}

    def get_hotel(self, p):
        suf = p["hotel_id"].split("_")[-1]
        h = next(h for h in HOTELS if h[0] == suf)
        return {"hotel_id": p["hotel_id"], "osm_id": h[9], "destination_id": p["hotel_id"].split("_")[0],
                "name": h[1], "kind": "hotel", "stars": h[2], "rating": h[3], "lat": 43.6, "lon": 39.7,
                "policies": {"check_in_from": "14:00", "check_out_until": "12:00", "pets_allowed": h[7],
                             "kids_policy": {"children_allowed": True},
                             "early_checkin": {"available": h[8] is not None, "price_rub": h[8]}},
                "rooms": [{"room_id": "std", "name": "Стандарт", "capacity_adults": 3}]}

    def search_flights(self, p):
        o, d, day = p["origin_iata"][0], p["dest_iata"][0], p["date"]
        pax = p["adults"] + p["children"]
        offers = []
        for i, (dep, arr, price, mins) in enumerate((("06:40", "09:05", 11000, 145), ("10:15", "12:35", 15000, 140))):
            oid = f"fl|{o}|{d}|{day}|{i}|{pax}|{p['infants']}"
            offer = {"offer_id": oid, "carrier": "Пример Авиа", "flight_no": f"PA{100 + i}", "origin_iata": o,
                     "dest_iata": d, "segments": [{"origin_iata": o, "dest_iata": d, "dep": _dt(day, dep),
                                                   "arr": _dt(day, arr), "carrier": "Пример Авиа",
                                                   "flight_no": f"PA{100 + i}"}],
                     "dep": _dt(day, dep), "arr": _dt(day, arr), "duration_min": mins, "stops": 0,
                     "international": False, "baggage": {"checked_kg": 20, "cabin_kg": 10},
                     "fare": {"refundable": False, "change_fee_rub": 3000}, "price_rub": price * pax, "date": day}
            self.offers[oid] = ("flight", price * pax, offer["dep"], offer["arr"], f"Перелёт {o} → {d}")
            offers.append(offer)
        return {"offers": offers}

    def search_trains(self, p):
        return {"offers": [], "note": "нет поездов в демо"}

    def search_transfers(self, p):
        point = p["from"].get("iata") or p["from"].get("station") or "pt"
        oid = f"tr|{p['to']['hotel_id']}|{point}|{p['at'][:16]}|{int(p.get('reverse', False))}"
        start = datetime.fromisoformat(p["at"])
        self.offers[oid] = ("transfer", 1500, start.isoformat(), (start + timedelta(minutes=40)).isoformat(),
                            "Такси")
        return {"offers": [{"offer_id": oid, "kind": "taxi", "duration_min": 40, "price_rub": 1500,
                            "meeting_point": "У выхода"}]}

    # ---- cart / orders
    def _cart(self, sid):
        cart = self.carts.setdefault(sid, {"cart_id": f"c_{sid}", "session_id": sid, "status": "open", "items": []})
        cart["total_rub"] = sum(i["price_rub"] for i in cart["items"])
        cart["issues"] = []
        return cart

    def cart_create(self, p):
        cart = self._cart(p["session_id"])
        if cart["status"] == "checked_out":
            self.carts.pop(p["session_id"])
            cart = self._cart(p["session_id"])
        return cart

    def cart_get(self, p):
        return self._cart(p["session_id"])

    def cart_add(self, p):
        cart = self._cart(p["session_id"])
        kind, price, start, end, title = self.offers.get(p["offer_id"], (p["kind"], 1000, None, None, "Доп"))
        cart["items"].append({"item_id": f"ci_{len(cart['items']) + 1}", "offer_id": p["offer_id"], "kind": p["kind"],
                              "title": title, "price_rub": price, "start": start, "end": end})
        return self._cart(p["session_id"])

    def cart_remove(self, p):
        cart = self._cart(p["session_id"])
        cart["items"] = [i for i in cart["items"] if i["offer_id"] != p["offer_id"]]
        return self._cart(p["session_id"])

    def checkout(self, p):
        cart = next(c for c in self.carts.values() if c["cart_id"] == p["cart_id"])
        cart["status"] = "checked_out"
        oid = f"o_{len(self.orders) + 1}"
        now = date.fromisoformat(p["now"][:10])
        issues = []
        for t in p["travellers"]:
            b = date.fromisoformat(t["birth_date"])
            if (now - b).days < 730:
                issues.append({"code": "infant_fare", "message": "Младенческий билет уже в цене.", "severity": "info",
                               "item_ids": []})
        items = [{**{k: i[k] for k in ("item_id", "offer_id", "kind", "title", "price_rub", "start", "end")},
                  "status": "pending",
                  "details": {"hotel_id": i["offer_id"].split("|")[1]} if i["kind"] == "hotel" else (
                      {"dest_iata": i["offer_id"].split("|")[2]} if i["kind"] == "flight" else {})}
                 for i in cart["items"]]
        self.orders[oid] = {"order_id": oid, "cart_id": cart["cart_id"], "session_id": cart["session_id"],
                            "status": "pending_payment", "items": items, "travellers": p["travellers"],
                            "contact": p["contact"], "total_rub": sum(i["price_rub"] for i in items),
                            "issues": issues, "events": [{"at": p["now"], "kind": "created"}]}
        return {"order_id": oid, "total_rub": self.orders[oid]["total_rub"], "issues": issues}

    def pay(self, p):
        o = self.orders[p["order_id"]]
        o["status"] = "paid"
        for i in o["items"]:
            i["status"], i["booking_number"] = "confirmed", f"BK-{i['item_id']}"
        o["events"].append({"at": p["now"], "kind": "paid"})
        return {"order_id": o["order_id"], "bookings": [{"item_id": i["item_id"], "booking_number": i["booking_number"]}
                                                        for i in o["items"]]}

    def order_get(self, p):
        return self.orders[p["order_id"]]

    def order_request_early_checkin(self, p):
        o = self.orders[p["order_id"]]
        o["events"].append({"at": p["now"], "kind": "early_checkin_requested",
                            "data": {"time": p["time"], "status": "on_request"}})
        return {"order_id": o["order_id"], "status": "on_request", "price_rub": 2500}

    def order_change_dates(self, p):
        o = self.orders[p["order_id"]]
        return {"order_id": o["order_id"], "available": True,
                "quote": {"old_total_rub": o["total_rub"], "new_total_rub": o["total_rub"] + 1000,
                          "difference_rub": 1000}}

    def order_add_transfer(self, p):
        o = self.orders[p["order_id"]]
        o["items"].append({"item_id": f"oi_t{len(o['items'])}", "offer_id": p["offer_id"], "kind": "transfer",
                           "title": "Такси", "price_rub": 1500, "status": "confirmed"})
        o["total_rub"] += 1500
        return o

    def insights_popular_destinations(self, p):
        return {"items": [{"destination_id": "sochi", "share": 0.3, "n": 40}]}

    def insights_segment_preferences(self, p):
        return {"segment": p["segment"], "available": False}

    def insights_log_event(self, p):
        self.events.append(p["event"])
        return {"ok": True}


class FakeOpenData:
    def __init__(self):
        self.calls = []

    def destination(self, dest_id):
        self.calls.append(("destination", dest_id))
        return dict(DESTS[dest_id]) if dest_id in DESTS else None

    def geocode(self, name):
        self.calls.append(("geocode", name))
        key = str(name).casefold()
        for d in DESTS.values():
            if key in (d["name_ru"].casefold(), d["name_en"].casefold(), d["id"]):
                return {"id": d["id"], "name": d["name_ru"], "country": d["country"], "in_catalog": True}
        return None

    def origin_airports(self, city):
        return ["SVO", "VKO"] if city == "Москва" else []

    def find_destinations(self, traits, month, origin_city, budget=None, max_travel_hours=None, segment=None, limit=8):
        self.calls.append(("find_destinations", tuple(traits), month))
        out = []
        for d in DESTS.values():
            match = [t for t in traits if t in d["tags"]]
            if traits and not match:
                continue
            out.append({"id": d["id"], "name": d["name_ru"], "country": d["country"], "kind": d["kind"],
                        "tags": d["tags"], "airports": d["airports"], "match": match,
                        "weather": {"air_max_c": 22, "sea_c": 21, "rain_days": 6}, "travel_time_h": 3.5,
                        "description_ru": d["description_ru"]})
        return out[:limit]

    def climate(self, dest_id, month):
        return {"air_max_c": 20, "sea_c": 19, "rain_days": 8} if dest_id in DESTS else None

    def district_profile(self, osm_id):
        scores = {"node/1": (0.2, 0.8, 0.4, 0.3), "node/2": (0.5, 0.5, 0.6, 0.7), "node/3": (0.7, 0.3, 0.5, 0.9)}
        s = scores.get(osm_id)
        if s is None:
            return None
        sc = dict(zip(("lively", "quiet", "family", "walkable"), s))
        return {"osm_id": osm_id, "labels": [k for k, v in sc.items() if v >= 0.6], "scores": sc,
                "counts": {"cafe": 5}}

    def travel_time(self, origin_city, dest_id):
        return {"hours": 3.5}


# ---------------------------------------------------------------- fake ENGINE-B domain

def _step(sid, type_, frm, to, start, end, price, offer_id, today, title=None, carrier=None):
    return Step(id=sid, type=type_, **{"from": frm}, to=to, start=start, end=end,
                price=Price(amount=price, checked_at=str(today)), link=f"mock://{offer_id}", offer_id=offer_id,
                title=title, carrier=carrier)


def fake_build_variants(brief, results, districts, rules, today):
    hotels = results.get("hotels") or []
    if not hotels:
        return []
    outs, backs = results.get("flights_out") or [], results.get("flights_back") or []
    picks = {"cheapest": min(hotels, key=lambda h: h["price_total_rub"]),
             "fastest": sorted(hotels, key=lambda h: h["price_total_rub"])[len(hotels) // 2],
             "comfort": max(hotels, key=lambda h: (h["stars"], h["rating"]))}
    variants = []
    for n, (vid, h) in enumerate(picks.items()):
        steps, hours = [], 0.0
        fo = (outs[0] if vid == "cheapest" else outs[-1]) if outs else None
        fb = backs[0] if backs else None
        if fo:
            steps.append(_step("s1", "flight", fo["origin_iata"], fo["dest_iata"], fo["dep"], fo["arr"],
                               fo["price_rub"], fo["offer_id"], today, carrier=fo["carrier"]))
            hours += fo["duration_min"] / 60
        tr = (results.get("transfers_out") or {}).get(h["hotel_id"]) or []
        if tr and fo:
            t = tr[0]
            end = (datetime.fromisoformat(fo["arr"]) + timedelta(minutes=t["duration_min"] + 30)).isoformat()
            steps.append(_step("s2", "transfer", fo["dest_iata"], h["name"], fo["arr"], end, t["price_rub"],
                               t["offer_id"], today))
            hours += t["duration_min"] / 60
        steps.append(_step("s3", "stay", h["name"], h["name"], h["check_in"], h["check_out"], h["price_total_rub"],
                           h["offer_id"], today, title="Проживание"))
        if fb:
            steps.append(_step("s5", "flight", fb["origin_iata"], fb["dest_iata"], fb["dep"], fb["arr"],
                               fb["price_rub"], fb["offer_id"], today))
            hours += fb["duration_min"] / 60
        d = districts.get(h["osm_id"]) or {}
        variants.append(Variant(
            id=vid, title={"cheapest": "Дешевле", "fastest": "Быстрее", "comfort": "Комфортнее"}[vid],
            destination_id=(results.get("destination") or {}).get("id"), steps=steps,
            totals=Totals(rub=sum(int(s.price.amount) for s in steps), door_to_door_hours=round(hours, 1)),
            hotel=VariantHotel(hotel_id=h["hotel_id"], offer_id=h["offer_id"], name=h["name"], stars=h["stars"],
                               rating=h["rating"], district=DistrictProfile(labels=d.get("labels", []),
                                                                            scores=d.get("scores", {}))),
            facts={"meals": h["meals"]},
            sacrifices=[Note(code="early", text="Ранний вылет")] if vid == "cheapest" else [],
            fit=Fit(score=[0.6, 0.8, 0.7][n])))
    return variants


def fake_check_variant(variant, brief, rules):
    return []


def fake_sensitivity(brief, variants, rerun, skill):
    if brief.source_of("party.segment") == "user":
        return []
    rerun(brief)   # exercise the closure
    spec = skill.questions["party.segment"]
    return [Question(id=spec["id"], field="party.segment", text=spec["text"],
                     choices=[Choice(label=c["label"], value=c["value"], recommended=c["value"] == "couple")
                              for c in spec["choices"]], reason="fake")]


def fake_offers_for_variant(variant, results):
    hotel = next((h for h in results.get("hotels") or [] if variant.hotel and h["offer_id"] == variant.hotel.offer_id),
                 None)
    early = (hotel or {}).get("early_checkin") or {}
    return {"early_checkin": [{"offer_id": None, "price_rub": early["price_rub"]}] if early.get("available") else []}


def fake_upsell_candidates(brief, variant, offers, upsell_rules):
    return [UpsellCandidate(id=f"u-{variant.id}-early", rule_id="early", kind="early_checkin", label="Ранний заезд",
                            reason=f"Ранний заезд за {o['price_rub']} ₽.", price_rub=o["price_rub"])
            for o in offers.get("early_checkin") or []][:1]


def fake_check_card_output(card, card_input, output, rules):
    return []


def fake_render_variant(session_id, variant, brief=None, rules=None, out_root=None, now=None):
    return {"ics": f"/files/{session_id}/trip-{variant.id}.ics", "page": f"/files/{session_id}/trip-{variant.id}.html"}


def fake_domain(**overrides):
    fns = {"build_variants": fake_build_variants, "check_variant": fake_check_variant,
           "sensitivity": fake_sensitivity, "upsell_candidates": fake_upsell_candidates,
           "offers_for_variant": fake_offers_for_variant, "check_card_output": fake_check_card_output,
           "render_variant": fake_render_variant}
    fns.update(overrides)
    return fns


# ---------------------------------------------------------------- scripted model

def u(intent="new_trip", set=None, unset=None, refs=None, image=None, info=None, pp=None):
    """An understand output."""
    return {"intent": intent, "patch": {"set": dict(set or {}), "unset": list(unset or [])},
            "refs": list(refs or []), "image": image, "info_question": info, "post_purchase": pp}


def echo_present(req):
    """Schema-valid present output that only copies ids from the input."""
    data = json.loads(req.user)
    items = data.get("items") or []
    card = req.card
    if card == "present.fallback":
        return {"message": "Я про поездки — давайте подберу.", "items": [], "recommended_id": None, "upsell": []}
    if card == "present.destinations":
        out = [{"id": i["id"], "why": f"Подходит: {i['name']}", "watch_out": None} for i in items]
        return {"message": "Вот направления.", "items": out, "recommended_id": items[0]["id"] if items else None,
                "upsell": []}
    if card in ("present.variants", "present.update", "present.compare"):
        out = [{"id": i["id"], "why": f"Вариант {i['title']}", "sacrifices": [], "risks": [], "not_for": []}
               for i in items]
        cands = data.get("upsell_candidates") or []
        ups = [{"candidate_id": c["candidate_id"], "line": c["reason"]} for c in cands[:1]] if card != "present.compare" else []
        rec = data.get("recommended_hint") or (items[0]["id"] if items else None)
        return {"message": f"Готово: {card}.", "items": out, "recommended_id": rec, "upsell": ups}
    return {"message": f"Ответ: {card}.", "items": [], "recommended_id": None, "upsell": []}


PRESENT_CARDS = ("present.destinations", "present.variants", "present.update", "present.compare", "present.info",
                 "present.aftercare", "present.fallback")
