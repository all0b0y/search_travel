"""The core: Claude Code running the trip-planner skill, one session per conversation.

ClaudeCore calls the real CLI. DemoCore replays invented data through the same
contract so the system can be shown without network or API spend.
"""
import copy
import importlib.util
import json
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "trip-planner"
SCHEMA_TEXT = (SKILL / "schema" / "response.schema.json").read_text(encoding="utf-8")
TOOLS = ["Skill", "Agent", "Read", "Write", "Edit", "Bash", "WebSearch", "WebFetch", "mcp__kiwi", "mcp__frankfurter"]

_spec = importlib.util.spec_from_file_location("trip", SKILL / "scripts" / "trip.py")
trip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trip)


class CoreError(Exception):
    pass


def prepare_runtime(runtime):
    """The core's working directory: the skill where Claude Code finds it, plus the MCP config."""
    skills = runtime / ".claude" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    link = skills / "trip-planner"
    if not link.exists():
        link.symlink_to(SKILL, target_is_directory=True)
    mcp = runtime / ".mcp.json"
    if not mcp.exists():
        mcp.write_text((ROOT / ".mcp.json").read_text(encoding="utf-8"), encoding="utf-8")
    return runtime


class ClaudeCore:
    def __init__(self, runtime, timeout=1800):
        self.runtime = prepare_runtime(Path(runtime))
        self.timeout = timeout

    def ask(self, request, session_id=None):
        cmd = ["claude", "-p", json.dumps(request, ensure_ascii=False),
               "--output-format", "json", "--json-schema", SCHEMA_TEXT,
               "--mcp-config", ".mcp.json", "--allowedTools", *TOOLS]
        if session_id:
            cmd += ["--resume", session_id]
        try:
            done = subprocess.run(cmd, cwd=self.runtime, capture_output=True, text=True, timeout=self.timeout)
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
    "message": "Демо-режим: данные выдуманы. Уточню пару деталей, чтобы собрать маршруты.",
    "questions": [
        {"id": "depart", "text": "Когда летите?", "choices": ["12–15 ноября", "19–22 ноября"], "recommended": "12–15 ноября"},
        {"id": "value_of_hour_rub", "text": "Сколько готовы доплатить, чтобы доехать на час быстрее?", "choices": ["500 ₽", "1 500 ₽", "3 000 ₽"], "recommended": "1 500 ₽"},
        {"id": "constraints", "text": "Есть жёсткие условия?", "choices": ["Только ручная кладь", "Нужен багаж", "Нет"], "recommended": "Только ручная кладь"},
    ],
}

CHANGE_TEXT = {
    "done": "пройдено", "delayed": "задержка", "shifted": "перенесено", "plan_b": "переход на план Б",
    "missed": "не успеваете — нужна замена", "late_checkin": "поздний заезд — предупредите отель",
    "cancelled": "отменено — нужна замена", "replaced": "заменено",
}


class DemoCore:
    """Replays the Berlin → Lisbon test fixture; events run through the real trip.py logic."""

    fixture = SKILL / "tests" / "fixtures" / "berlin-lisbon.json"

    def __init__(self, runtime):
        self.runtime = Path(runtime)

    def ask(self, request, session_id=None):
        sid = session_id or f"demo-{uuid.uuid4().hex[:8]}"
        out = self.runtime / "trips" / sid
        if request.get("kind") == "event":
            return self._event(request, out), sid, {"cost_usd": 0}
        if "itinerary" in request:
            reply = dict(request["itinerary"], type="update", changes=[],
                         message="Демо-режим отвечает только на события: кнопки под маршрутом.")
            return reply, sid, {"cost_usd": 0}
        if session_id is None:
            return copy.deepcopy(DEMO_QUESTIONS), sid, {"cost_usd": 0}
        return self._itinerary(out), sid, {"cost_usd": 0}

    def _itinerary(self, out):
        doc = json.loads(self.fixture.read_text(encoding="utf-8"))
        doc.pop("_fixture", None)
        doc["recommended_variant"] = "v-cheap"
        doc["message"] = ("Демо-режим: данные выдуманы. Рекомендую «дешевле»: быстрее только «быстрее» — "
                          "на 48 мин за +26 600 ₽, при вашей цене часа 1 500 ₽ это не окупается.")
        doc["profile_updates"] = {"value_of_hour_rub": 1500, "baggage": "ручная кладь"}
        return self._finalize(doc, out)

    def _event(self, request, out):
        doc = copy.deepcopy(request["itinerary"])
        report = trip.apply_event(doc, request["event"])
        lines = [f"{c['step']}: {CHANGE_TEXT[c['change']]}" + (f" ({c['detail']})" if c.get("detail") else "") for c in report["changes"]]
        doc["message"] = "Демо-режим. " + ("; ".join(lines) or "Ничего не изменилось.")
        return self._finalize(doc, out)

    def _finalize(self, doc, out):
        out.mkdir(parents=True, exist_ok=True)
        draft = out / "draft.json"
        draft.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        trip.finalize(draft)
        return json.loads((out / "itinerary.json").read_text(encoding="utf-8"))
