"""Shared helpers for the server tests: a FakeEngine replaying the contract fixtures."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "agent" / "contracts" / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeEngine:
    """Engine facade stand-in: records calls, answers with a chosen fixture."""

    def __init__(self, answer="selection", debug_block=None):
        self.answer = answer
        self.debug_block = debug_block
        self.sessions = {}
        self.requests = []
        self.created = []

    def new_session(self, profile_id=None, debug=False, now=None):
        sid = f"s_fake_{len(self.sessions) + 1}"
        self.sessions[sid] = {"id": sid, "profile_id": profile_id, "debug": debug, "turn": 0}
        self.created.append({"profile_id": profile_id, "debug": debug})
        return sid

    def handle(self, session_id, request):
        self.requests.append((session_id, request))
        response = fixture(self.answer)
        response["session_id"] = session_id
        if self.sessions.get(session_id, {}).get("debug") and self.debug_block is not None:
            response["debug"] = self.debug_block
        return response

    def get_session(self, session_id):
        if session_id not in self.sessions:
            raise KeyError(session_id)
        return self.sessions[session_id]


@pytest.fixture
def fake_engine():
    return FakeEngine()
