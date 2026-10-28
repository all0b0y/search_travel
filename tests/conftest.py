"""Shared fixtures. Unit tests never touch the network or a real model."""
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.llm import FakeBackend
from agent.state import Session

ROOT = Path(__file__).resolve().parent.parent
MSK = timezone(timedelta(hours=3))
PINNED_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=MSK)


@pytest.fixture(autouse=True)
def _no_network_no_key(monkeypatch, tmp_path):
    """Any socket connection fails loudly; the real key is never visible to tests."""
    def guard(*args, **kwargs):
        raise RuntimeError("network access is not allowed in unit tests")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TRIP_AGENT_FLAGS", raising=False)
    monkeypatch.setenv("OSTROVOK_STATE_DB", str(tmp_path / "ostrovok_state.sqlite"))


@pytest.fixture
def root():
    return ROOT


@pytest.fixture
def pinned_now():
    return PINNED_NOW


@pytest.fixture
def session(pinned_now):
    return Session.new(now=pinned_now, session_id="s_test")


@pytest.fixture
def fake_backend():
    return FakeBackend()


@pytest.fixture
def response_schema():
    return json.loads((ROOT / "agent/contracts/response.schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def request_schema():
    return json.loads((ROOT / "agent/contracts/request.schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def skill_dir():
    return ROOT / "skills" / "trip-agent"
