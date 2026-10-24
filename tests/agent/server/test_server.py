"""agent/server.py: routes, validation, size limit, files, fixture mode (offline)."""
import base64
import json
import socket
import threading

import jsonschema
import pytest

from agent.server import (FIXTURE_ORDER, IMAGE_MAX_BYTES, Api, FixtureEngine, make_handler, main as server_main)

from .conftest import FakeEngine, fixture

SMALL_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


def post(api, path, body):
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    return api.dispatch("POST", path, raw)


def new_session(api, **body):
    reply = post(api, "/api/session", body)
    assert reply.status == 200, reply.json()
    return reply.json()["session_id"]


@pytest.fixture
def api(tmp_path, fake_engine):
    return Api(fake_engine, files_root=tmp_path / "files")


def test_session_and_text_turn(api, fake_engine, response_schema):
    sid = new_session(api, profile_id="demo")
    assert fake_engine.created == [{"profile_id": "demo", "debug": False}]
    reply = post(api, "/api/turn", {"session_id": sid, "text": "Сочи, 12–19 ноября, вдвоём"})
    assert reply.status == 200
    body = reply.json()
    jsonschema.validate(body, response_schema)
    assert body["session_id"] == sid
    assert fake_engine.requests[-1] == (sid, {"session_id": sid, "text": "Сочи, 12–19 ноября, вдвоём"})


def test_action_and_image_turns_reach_the_engine(api, fake_engine):
    sid = new_session(api)
    action = {"type": "choose_variant", "payload": {"variant_id": "fastest"}}
    assert post(api, "/api/turn", {"session_id": sid, "action": action}).status == 200
    assert fake_engine.requests[-1][1]["action"] == action
    assert post(api, "/api/turn", {"session_id": sid, "image": SMALL_PNG, "text": "хочу сюда"}).status == 200
    assert fake_engine.requests[-1][1]["image"] == SMALL_PNG


@pytest.mark.parametrize("body, needle", [
    ({"session_id": "s_x"}, "текст, картинка или действие"),
    ({"session_id": "bad id", "text": "привет"}, "session_id"),
    ({"session_id": "s_x", "text": ""}, "не должно быть пустым"),
    ({"session_id": "s_x", "text": "a", "extra": 1}, "лишнее поле"),
    ({"session_id": "s_x", "action": {"type": "fly_away", "payload": {}}}, "Неизвестный тип действия"),
    ({"session_id": "s_x", "action": {"type": "choose_variant", "payload": {"variant_id": "best"}}},
     "неверный формат"),
    ({"session_id": "s_x", "action": {"type": "checkout", "payload": {"travellers": [], "contact": None}}},
     "action.payload"),
    ({"session_id": "s_x", "image": "data:text/plain;base64,AAAA"}, "data URL"),
])
def test_invalid_turns_get_400_with_russian_message(api, fake_engine, body, needle):
    reply = post(api, "/api/turn", body)
    assert reply.status == 400
    err = reply.json()["error"]
    assert err["code"] == "bad_request"
    assert needle in err["message"], err
    assert fake_engine.requests == []


def test_non_json_and_bad_session_body(api):
    assert post(api, "/api/turn", b"{not json").status == 400
    assert post(api, "/api/turn", b"[1, 2]").status == 400
    assert post(api, "/api/session", {"debug": "yes"}).status == 400
    assert post(api, "/api/session", {"profile_id": "../etc"}).status == 400
    assert post(api, "/api/session", {"who": 1}).status == 400


def test_image_size_limit_413(api, fake_engine):
    sid = new_session(api)
    big = "data:image/jpeg;base64," + "A" * ((IMAGE_MAX_BYTES // 3 + 10) * 4)
    reply = post(api, "/api/turn", {"session_id": sid, "image": big})
    assert reply.status == 413
    assert "МБ" in reply.json()["error"]["message"]
    assert fake_engine.requests == []


def test_corrupt_base64_is_400(api):
    sid = new_session(api)
    reply = post(api, "/api/turn", {"session_id": sid, "image": "data:image/png;base64,@@@@"})
    assert reply.status == 400 and "base64" in reply.json()["error"]["message"]


def test_session_state_only_for_debug(tmp_path):
    engine = FakeEngine()
    api = Api(engine, files_root=tmp_path)
    plain = new_session(api)
    assert api.dispatch("GET", f"/api/session/{plain}").status == 404
    dbg = new_session(api, debug=True)
    reply = api.dispatch("GET", f"/api/session/{dbg}")
    assert reply.status == 200 and reply.json()["id"] == dbg
    debug_api = Api(engine, debug=True, files_root=tmp_path)
    assert debug_api.dispatch("GET", f"/api/session/{plain}").status == 200
    assert debug_api.dispatch("GET", "/api/session/s_missing").status == 404


def test_files_served_safely(tmp_path, fake_engine):
    files = tmp_path / "files"
    (files / "o_1").mkdir(parents=True)
    (files / "o_1" / "trip.ics").write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n", encoding="utf-8")
    (files / "o_1" / "index.html").write_text("<h1>Поездка</h1>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    (files / "link.txt").symlink_to(tmp_path / "secret.txt")
    api = Api(fake_engine, files_root=files)
    ics = api.dispatch("GET", "/files/o_1/trip.ics")
    assert ics.status == 200 and ics.content_type.startswith("text/calendar")
    assert b"VCALENDAR" in ics.data
    page = api.dispatch("GET", "/files/o_1/index.html")
    assert page.status == 200 and page.content_type.startswith("text/html")
    for bad in ("/files/../secret.txt", "/files/o_1/../../secret.txt", "/files/%2e%2e/secret.txt",
                "/files/o_1/%2e%2e/%2e%2e/secret.txt", "/files//etc/passwd", "/files/link.txt",
                "/files/o_1", "/files/", "/files/o_1/missing.ics", "/files/..%5csecret.txt"):
        assert api.dispatch("GET", bad).status == 404, bad


def test_mockup_and_config_routes(api):
    for path in ("/", "/debug"):
        reply = api.dispatch("GET", path)
        assert reply.status == 200 and reply.content_type.startswith("text/html")
        assert b"sendAction" in reply.data
    config = api.dispatch("GET", "/api/config").json()
    assert config["mode"] == "engine" and config["image_max_bytes"] == IMAGE_MAX_BYTES
    assert api.dispatch("GET", "/nope").status == 404
    assert api.dispatch("DELETE", "/api/turn").status == 405


def test_fixture_routes(api):
    listing = api.dispatch("GET", "/api/fixtures").json()
    assert listing["order"] == list(FIXTURE_ORDER)
    assert set(FIXTURE_ORDER) <= set(listing["fixtures"])
    assert api.dispatch("GET", "/api/fixtures/cart").json() == fixture("cart")
    assert api.dispatch("GET", "/api/fixtures/../request").status == 404
    assert api.dispatch("GET", "/api/fixtures/missing").status == 404


def test_fixture_mode_walks_all_fixtures_in_order(tmp_path, response_schema):
    api = Api(FixtureEngine(), mode="fixtures", files_root=tmp_path)
    sid = new_session(api)
    stages = []
    for i, name in enumerate(list(FIXTURE_ORDER) + [FIXTURE_ORDER[0]]):
        body = post(api, "/api/turn", {"session_id": sid, "text": f"ход {i}"}).json()
        jsonschema.validate(body, response_schema)
        expected = fixture(name)
        assert body["session_id"] == sid and body["turn"] == i + 1
        assert body["message"] == expected["message"] and body["stage"] == expected["stage"]
        assert "debug" not in body
        stages.append(body["stage"])
    assert stages[0] == "inspiration" and stages[-1] == "inspiration"   # wraps around
    unknown = post(api, "/api/turn", {"session_id": "s_unknown", "text": "привет"}).json()
    jsonschema.validate(unknown, response_schema)
    assert unknown["error"]["code"] == "session_not_found"


def test_fixture_mode_debug_block_is_labelled(tmp_path, response_schema):
    api = Api(FixtureEngine(), mode="fixtures", debug=True, files_root=tmp_path)
    sid = new_session(api)
    body = post(api, "/api/turn", {"session_id": sid, "text": "привет"}).json()
    jsonschema.validate(body, response_schema)
    assert body["debug"]["fixture"] == "inspiration"
    assert body["debug"]["stage_path"][0] == "fixture:inspiration"
    assert body["debug"]["example"] is True and "не настоящий" in body["debug"]["note"]
    state = api.dispatch("GET", f"/api/session/{sid}").json()
    assert state["turn"] == 1 and state["requests"][0]["text"] == "привет"


def test_contract_violations_are_flagged_in_debug(tmp_path):
    class Broken(FakeEngine):
        def handle(self, session_id, request):
            response = super().handle(session_id, request)
            response["stage"] = "nowhere"
            response["debug"] = {"stage_path": [], "calls": [], "hooks": [], "timings_ms": {}, "jev": [],
                                 "flags": {}}
            return response
    api = Api(Broken(), debug=True, files_root=tmp_path)
    sid = new_session(api)
    body = post(api, "/api/turn", {"session_id": sid, "text": "x"}).json()
    assert any("stage" in e for e in body["debug"]["contract_errors"])


def test_engine_crash_is_500_in_russian(tmp_path):
    class Crashing(FakeEngine):
        def handle(self, session_id, request):
            raise RuntimeError("boom")
    api = Api(Crashing(), files_root=tmp_path)
    sid = new_session(api)
    reply = post(api, "/api/turn", {"session_id": sid, "text": "x"})
    assert reply.status == 500 and reply.json()["error"]["message"] == "Внутренняя ошибка сервера."


# ---------------------------------------------------------------- real HTTP parsing over a socketpair

class _Server:
    def __init__(self):
        self.server_name, self.server_port = "127.0.0.1", 0


def http(api, raw: bytes) -> bytes:
    """Run the real request handler on one end of a socketpair (no connect(), no network)."""
    ours, theirs = socket.socketpair()
    handler = make_handler(api)
    def serve():
        try:
            handler(theirs, ("127.0.0.1", 50000), _Server())
        finally:
            try:
                theirs.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            theirs.close()
    worker = threading.Thread(target=serve)
    worker.start()
    ours.sendall(raw)
    ours.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        data = ours.recv(65536)
        if not data:
            break
        chunks.append(data)
    worker.join(5)
    ours.close()
    return b"".join(chunks)


def test_http_round_trip_and_cors(tmp_path):
    api = Api(FixtureEngine(), mode="fixtures", files_root=tmp_path)
    body = b'{"debug": false}'
    raw = http(api, b"POST /api/session HTTP/1.1\r\nHost: x\r\nOrigin: http://localhost:5173\r\n"
                    b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    head, _, payload = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200") or head.startswith(b"HTTP/1.1 200")
    assert b"Access-Control-Allow-Origin: http://localhost:5173" in head
    assert json.loads(payload)["session_id"].startswith("s_fixture_")
    raw = http(api, b"GET /debug HTTP/1.1\r\nHost: x\r\nOrigin: http://evil.example\r\n\r\n")
    head, _, payload = raw.partition(b"\r\n\r\n")
    assert b" 200 " in head.split(b"\r\n")[0] and b"Access-Control-Allow-Origin" not in head
    assert b"<html" in payload
    raw = http(api, b"OPTIONS /api/turn HTTP/1.1\r\nHost: x\r\nOrigin: http://127.0.0.1:8787\r\n\r\n")
    assert b" 204 " in raw.split(b"\r\n")[0] and b"Access-Control-Allow-Methods" in raw


def test_http_oversized_body_is_413_without_reading(tmp_path):
    api = Api(FixtureEngine(), mode="fixtures", files_root=tmp_path)
    raw = http(api, b"POST /api/turn HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\n\r\n{}")
    assert b" 413 " in raw.split(b"\r\n")[0]


def test_main_without_engine_module_suggests_fixtures(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "engine" and level == 1 or name == "agent.engine":
            raise ImportError("no engine yet")
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr("agent.env.load_env", lambda *a, **k: [])
    with pytest.raises(SystemExit, match="--fixtures"):
        server_main(["--port", "0"])
