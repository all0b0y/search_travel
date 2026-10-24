"""FakeEngine and a fake card backend for dry runs and tests.

FakeEngine follows the Engine facade of AGENT_PLAN.md ("Module interfaces"):
    new_session(profile_id=None, debug=False, now=None) -> session_id
    handle(session_id, request) -> Response dict (response.schema.json)
    get_session(session_id) -> agent.state.Session
It replays the contract fixtures (inspiration -> selection -> cart -> checkout -> ordered ->
aftercare) so the whole level-3 pipeline runs offline. Nothing here is a real recommendation.
"""
import copy
import json
import re

from agent.llm import FakeBackend
from agent.state import Price, Session, Step, Totals, Variant, VariantHotel, DistrictProfile, apply_patch

from ..common import FIXTURES_DIR, read_json


def fixture(name):
    return read_json(FIXTURES_DIR / f"{name}.json")


def variants_from_cards(cards, checked_at):
    """Variant cards of a response -> agent.state.Variant models (for get_session())."""
    out = []
    for c in cards:
        steps = [Step(id=s["id"], type=s["type"], **{"from": s["from"]}, to=s["to"], start=s["start"], end=s["end"],
                      price=Price(amount=s.get("price_rub") or 0, checked_at=checked_at, rub=s.get("price_rub")),
                      link="", offer_id=s.get("offer_id"), title=s.get("title"), carrier=s.get("carrier"))
                 for s in c["steps"]]
        h = c.get("hotel")
        hotel = VariantHotel(hotel_id=h["hotel_id"], name=h["name"], stars=h.get("stars"), rating=h.get("rating"),
                             district=DistrictProfile(labels=h["district"]["labels"],
                                                      scores=h["district"]["scores"])) if h else None
        out.append(Variant(id=c["id"], title=c["title"], steps=steps,
                           totals=Totals(rub=c["totals"]["rub"], door_to_door_hours=c["totals"]["door_to_door_hours"]),
                           hotel=hotel, facts={}))
    return out


class _Profiles(dict):
    def put(self, profile_id, profile):
        self[profile_id] = profile


class FakeEngine:
    INSPIRATION_RE = re.compile(r"куда|море|тепл|горы|сюда|так же|вдохнов", re.IGNORECASE)

    def __init__(self, fail_on_turn=None):
        self.sessions = {}
        self.profiles = _Profiles()
        self.fail_on_turn = fail_on_turn
        self.requests = []

    def new_session(self, profile_id=None, debug=False, now=None):
        s = Session.new(now=now, profile_id=profile_id, debug=debug)
        self.sessions[s.id] = s
        return s.id

    def get_session(self, session_id):
        return self.sessions[session_id]

    def _respond(self, s, name, stage, cards_to_session=False):
        resp = copy.deepcopy(fixture(name))
        resp["session_id"] = s.id
        resp["turn"] = s.turn
        resp["stage"] = stage
        s.stage = stage
        if cards_to_session and resp["cards"]["variants"]:
            s.variants = variants_from_cards(resp["cards"]["variants"], s.clock().isoformat())
            s.brief, _ = apply_patch(s.brief, {"set": {"destination.name": "Сочи", "party.adults": 2,
                                                       "dates.start": "2026-11-12", "dates.end": "2026-11-19"}})
        if resp.get("order"):
            s.order_id = resp["order"]["order_id"]
        if resp.get("cart"):
            s.cart_id = resp["cart"]["cart_id"]
        if s.debug:
            card = {"inspiration": "present.destinations", "selection": "present.variants",
                    "aftercare": "present.aftercare"}.get(name)
            calls = [{"card": "understand", "model": "fake/model", "provider": "fake", "latency_ms": 3,
                      "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0}}] if s.turn == 1 else []
            if card:
                calls.append({"card": card, "model": "fake/model", "provider": "fake", "latency_ms": 4,
                              "usage": {"input_tokens": 20, "output_tokens": 30, "cost_usd": 0.0}})
            resp["debug"] = {"stage_path": [stage], "calls": calls, "hooks": [], "timings_ms": {"total": 7.0},
                             "jev": [{"question": "routing", "answers": None, "fallback": True}] if s.turn == 2 else [],
                             "flags": {"preset": "stage1"}}
        else:
            resp.pop("debug", None)
        return resp

    def handle(self, session_id, request):
        self.requests.append(request)
        s = self.sessions[session_id]
        s.turn += 1
        if self.fail_on_turn and s.turn == self.fail_on_turn:
            raise RuntimeError("FakeEngine: scripted failure")
        action = request.get("action")
        if action:
            t = action["type"]
            if t == "choose_variant":
                s.chosen_variant_id = action["payload"]["variant_id"]
                return self._respond(s, "cart", "cart")
            if t == "checkout":
                return self._respond(s, "checkout_issues", "checkout")
            if t == "pay":
                return self._respond(s, "ordered", "ordered")
            if t == "order_action":
                return self._respond(s, "aftercare", "aftercare")
            if t == "compare":
                return self._respond(s, "comparison", "selection")
            return self._respond(s, "selection", "selection", cards_to_session=True)
        if s.stage in ("ordered", "aftercare"):
            return self._respond(s, "aftercare", "aftercare")
        text = request.get("text") or ""
        if s.turn == 1 and (request.get("image") or self.INSPIRATION_RE.search(text)):
            return self._respond(s, "inspiration", "inspiration")
        return self._respond(s, "selection", "selection", cards_to_session=True)


# ---------------------------------------------------------------- fake card backend

def _present_output(card, payload):
    items = payload.get("items") or []
    out_items = []
    for it in items[:3]:
        entry = {"id": it["id"], "why": "Подходит под ваш запрос."}
        if card == "present.destinations":
            entry["watch_out"] = None
        elif card in ("present.variants", "present.update", "present.compare"):
            entry.update({"sacrifices": [n["text"] for n in it.get("sacrifices", [])][:2],
                          "risks": [n["text"] for n in it.get("risks", [])][:2],
                          "not_for": [n["text"] for n in it.get("not_for", [])][:2]})
        out_items.append(entry)
    if card in ("present.compare",) and len(out_items) < 2:
        out_items = out_items * 2
    rec = payload.get("recommended_hint") or (out_items[0]["id"] if out_items else None)
    if card in ("present.fallback",):
        return {"message": "Я помогаю с поездками — давайте вернёмся к ним.", "items": [], "recommended_id": None,
                "upsell": []}
    if card in ("present.info", "present.aftercare"):
        return {"message": "Готово, вот что известно.", "items": [], "recommended_id": None, "upsell": []}
    return {"message": "Вот что нашлось.", "items": out_items, "recommended_id": rec, "upsell": []}


def fake_card_reply(req):
    payload = json.loads(req.user)
    if req.card == "understand":
        return {"intent": "new_trip", "patch": {"set": {}, "unset": []}, "refs": [], "image": None,
                "info_question": None, "post_purchase": None}
    return _present_output(req.card, payload)


def dry_run_backend(cards):
    """A FakeBackend answering every card with a minimal schema-valid output."""
    return FakeBackend(scripts={c: fake_card_reply for c in cards}, model="fake/model", provider="fake")
