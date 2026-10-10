"""The core: Claude Code running the trip-planner skill, one session per conversation.

ClaudeCore calls the real CLI. DemoCore replays invented data through the same
contract so the system can be shown without network or API spend.
"""
import copy
import importlib.util
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "trip-planner"
SCHEMA_TEXT = (SKILL / "schema" / "response.schema.json").read_text(encoding="utf-8")
TOOLS = ["Skill", "Agent", "Read", "Write", "Edit", "Bash", "WebSearch", "WebFetch"]

_spec = importlib.util.spec_from_file_location("trip", SKILL / "scripts" / "trip.py")
trip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trip)


class CoreError(Exception):
    pass


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "provider"


def prepare_runtime(runtime):
    """The core's working directory, with the skill where Claude Code finds it."""
    skills = runtime / ".claude" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    link = skills / "trip-planner"
    if not link.exists():
        link.symlink_to(SKILL, target_is_directory=True)
    return runtime


class ClaudeCore:
    def __init__(self, runtime, timeout=1800):
        self.runtime = prepare_runtime(Path(runtime))
        self.timeout = timeout

    def ask(self, request, session_id=None, secrets=None):
        """secrets: {env name: key} of this user, handed to the process environment only."""
        mcp = {slug(p["name"]): {"type": "http", "url": p["url"]}
               for p in request.get("providers", []) if p["access"] == "mcp" and p.get("url")}
        cmd = ["claude", "-p", json.dumps(request, ensure_ascii=False),
               "--output-format", "json", "--json-schema", SCHEMA_TEXT,
               "--allowedTools", *TOOLS, *(f"mcp__{name}" for name in mcp)]
        if mcp:
            cmd += ["--mcp-config", json.dumps({"mcpServers": mcp})]
        if session_id:
            cmd += ["--resume", session_id]
        env = {**os.environ, **(secrets or {})}
        try:
            done = subprocess.run(cmd, cwd=self.runtime, env=env, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise CoreError(f"core timed out after {self.timeout} s") from e
        try:
            data = json.loads(done.stdout)
        except json.JSONDecodeError as e:
            raise CoreError(f"core exited {done.returncode}: {done.stderr.strip()[:500]}") from e
        reply = data.get("structured_output")
        if data.get("is_error") or not reply:
            raise CoreError(f"core failed: {data.get('subtype')} {str(data.get('result'))[:500]}")
        problems = trip.validate_doc(reply)
        if problems:
            raise CoreError(f"reply breaks the contract: {problems[:3]}")
        meta = {"cost_usd": data.get("total_cost_usd"), "duration_ms": data.get("duration_ms")}
        return reply, data["session_id"], meta


DEMO_QUESTIONS = {
    "type": "questions",
    "message": "Демо-режим: данные выдуманы. Уточню детали и где вам удобно искать и заказывать.",
    "questions": [
        {"id": "depart", "text": "Когда летите?", "choices": ["12–15 ноября", "19–22 ноября"], "recommended": "12–15 ноября"},
        {"id": "value_of_hour_rub", "text": "Сколько готовы доплатить, чтобы доехать на час быстрее?", "choices": ["500 ₽", "1 500 ₽", "3 000 ₽"], "recommended": "1 500 ₽"},
        {"id": "providers.flights", "text": "Где искать билеты?", "choices": ["Kiwi.com", "Duffel", "Skyscanner", "Любой — ищи сам"], "recommended": "Kiwi.com"},
        {"id": "providers.taxi", "text": "Каким такси пользуетесь?", "choices": ["Uber", "Bolt", "FreeNow", "Любой — ищи сам"], "recommended": "Uber"},
    ],
}

DEMO_SETUP = {
    "type": "setup",
    "message": "Демо-режим. Вот через что буду искать. Duffel даёт настоящие билеты, но нужен ваш ключ; без него найду рейсы через Kiwi.com.",
    "connections": [
        {"category": "flights", "name": "Kiwi.com", "access": "mcp", "url": "https://mcp.kiwi.com", "ready": True},
        {"category": "flights", "name": "Duffel", "access": "api_key", "url": "https://api.duffel.com", "ready": False,
         "env": "PROVIDER_DUFFEL_KEY", "need": "Зарегистрируйтесь в Duffel, создайте тестовый токен в разделе Developers и вставьте его сюда.",
         "signup_url": "https://duffel.com", "docs_url": "https://duffel.com/docs"},
        {"category": "stays", "name": "Booking.com", "access": "site_only", "url": "https://www.booking.com", "ready": True},
        {"category": "taxi", "name": "Uber", "access": "site_only", "url": "https://m.uber.com", "ready": True},
        {"category": "rates", "name": "ЦБ РФ", "access": "public_api", "url": "https://www.cbr.ru/scripts/XML_daily.asp", "ready": True},
    ],
}

class DemoCore:
    """Replays the Berlin → Lisbon test fixture; events run through the real trip.py logic."""

    fixture = SKILL / "tests" / "fixtures" / "berlin-lisbon.json"

    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.turns = {}

    def ask(self, request, session_id=None, secrets=None):
        sid = session_id or f"demo-{uuid.uuid4().hex[:8]}"
        out = self.runtime / "trips" / sid
        meta = {"cost_usd": 0}
        if request.get("kind") == "event":
            return self._event(request, out), sid, meta
        if "itinerary" in request:
            reply = dict(request["itinerary"], type="update", changes=[],
                         message="Демо-режим отвечает только на события: кнопки под маршрутом.")
            return reply, sid, meta
        turn = self.turns[sid] = self.turns.get(sid, 0) + 1
        if turn == 1:
            return copy.deepcopy(DEMO_QUESTIONS), sid, meta
        if turn == 2 and not request.get("providers"):
            return copy.deepcopy(DEMO_SETUP), sid, meta
        return self._itinerary(out), sid, meta

    def _itinerary(self, out):
        doc = json.loads(self.fixture.read_text(encoding="utf-8"))
        doc.pop("_fixture", None)
        doc["recommended_variant"] = "v-cheap"
        doc["message"] = ("Демо-режим: данные выдуманы. Рекомендую «дешевле»: «быстрее» экономит 48 мин за +26 600 ₽, "
                          "а при вашей цене часа 1 500 ₽ это не окупается.")
        doc["profile_updates"] = {"value_of_hour_rub": 1500, "baggage": "ручная кладь"}
        return self._finalize(doc, out)

    def _event(self, request, out):
        doc = copy.deepcopy(request["itinerary"])
        report = trip.apply_event(doc, request["event"])
        doc["message"] = "Демо-режим. " + ("Маршрут обновлён: изменения ниже." if report["changes"] else "Ничего не изменилось.")
        return self._finalize(doc, out)

    def _finalize(self, doc, out):
        out.mkdir(parents=True, exist_ok=True)
        draft = out / "draft.json"
        draft.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        trip.finalize(draft)
        return json.loads((out / "itinerary.json").read_text(encoding="utf-8"))
