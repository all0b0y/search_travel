import json

import jsonschema
import pytest

from agent.engine import Engine
from agent.flags import parse_flags
from agent.jev import FakeJev
from agent.llm import FakeBackend
from agent.prefetch import Prefetcher
from agent.profiles import ProfileStore
from agent.sessions import SessionStore
from agent.skill_loader import load_skill
from agent.state import Session
from agent.steps import Domain

from .fakes import PRESENT_CARDS, FakeOpenData, FakeOstrovok, echo_present, fake_domain

_SKILL = None


def skill():
    global _SKILL
    if _SKILL is None:
        _SKILL = load_skill()
    return _SKILL


class Harness:
    def __init__(self, engine, backend, ostrovok, response_schema, pinned_now):
        self.engine, self.backend, self.ostrovok = engine, backend, ostrovok
        self.validator = jsonschema.Draft7Validator(response_schema)
        self.pinned_now = pinned_now
        self.sid = None

    def new(self, profile_id="demo", debug=True):
        self.sid = self.engine.new_session(profile_id=profile_id, debug=debug, now=self.pinned_now)
        return self.sid

    def understand(self, *outputs):
        self.backend.add("understand", *outputs)

    def turn(self, text=None, action=None, image=None, sid=None):
        sid = sid or self.sid
        req = {"session_id": sid}
        if text is not None:
            req["text"] = text
        if image is not None:
            req["image"] = image
        if action is not None:
            req["action"] = action
        resp = self.engine.handle(sid, req)
        errors = sorted(self.validator.iter_errors(resp), key=lambda e: list(e.path))
        assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
        session = self.engine.get_session(sid)
        assert Session.from_json(session.to_json()) == session          # JSON round trip
        assert json.loads(session.to_json())["id"] == sid
        budget = session.trace[-1]["budget"]
        assert budget.get("flash_calls", 0) <= 2
        assert all(n <= 1 for n in (budget.get("reruns") or {}).values())
        return resp

    def session(self):
        return self.engine.get_session(self.sid)

    def act(self, type_, **payload):
        return self.turn(action={"type": type_, "payload": payload})


def make_engine(flags="stage5", backend=None, jev=None, domain=None, ostrovok=None, prefetcher=None, tmp_path=None):
    backend = backend or FakeBackend()
    for card in PRESENT_CARDS:
        backend.scripts.setdefault(card, echo_present)
    ostrovok = ostrovok or FakeOstrovok()
    engine = Engine(skill=skill(), backend=backend, jev=jev or FakeJev(fail=True), ostrovok=ostrovok,
                    opendata=FakeOpenData(), flags=parse_flags(flags), store=SessionStore(":memory:"),
                    profiles=ProfileStore(":memory:"), domain=domain or Domain(**fake_domain()),
                    prefetcher=prefetcher or Prefetcher(synchronous=True), lazy_city=False,
                    files_dir=str(tmp_path) if tmp_path else None)
    return engine, backend, ostrovok


@pytest.fixture
def make(response_schema, pinned_now, tmp_path):
    def factory(**kw):
        engine, backend, ostrovok = make_engine(tmp_path=tmp_path, **kw)
        h = Harness(engine, backend, ostrovok, response_schema, pinned_now)
        h.new()
        return h
    return factory


@pytest.fixture
def h(make):
    return make()
