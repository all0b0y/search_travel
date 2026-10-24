"""HTTP API over the Engine facade + the debug mockup (AGENT_PLAN.md section 6).

    python -m agent.server [--port 8787] [--host 127.0.0.1] [--fixtures] [--debug] [--flags stage3]
                           [--backend openrouter|codex] [--files runtime/files]

Routes
  POST /api/session            {profile_id?, debug?}                  -> {session_id}
  POST /api/turn               {session_id, text?, image?, action?}  -> Response (response.schema.json)
  GET  /api/session/<id>       session state (debug sessions or a --debug server only)
  GET  /api/config             {mode, debug, image_max_bytes, fixture_order}
  GET  /api/fixtures           {order, fixtures}; GET /api/fixtures/<name> -> that fixture as is
  GET  /files/<path>           files under runtime/files (.ics, route pages); no path traversal
  GET  / and /debug            web_debug/index.html
Server-level failures answer {"error": {"code", "message" (Russian), "details"?}}: 400 invalid input,
404 unknown route/file/session state, 413 body or image too large, 500 engine crash. Turn-level
problems come back from the engine as a normal Response with `error` set (HTTP 200).

--fixtures runs without the engine: every turn returns the next fixture of FIXTURE_ORDER (wrapping),
with session_id/turn rewritten to the live session. Requests are still validated against the contract.
CORS: only http(s) origins on localhost / 127.0.0.1 / [::1] are echoed back.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import re
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "agent" / "contracts"
FIXTURES_DIR = CONTRACTS / "fixtures"
WEB_DEBUG = ROOT / "web_debug"
DEFAULT_FILES = ROOT / "runtime" / "files"
FIXTURE_ORDER = ("inspiration", "selection", "comparison", "cart", "checkout_issues", "ordered", "aftercare",
                 "error")
IMAGE_MAX_CHARS = 8_000_000                  # request.schema.json image maxLength (the data URL)
IMAGE_MAX_BYTES = (IMAGE_MAX_CHARS - 40) * 3 // 4   # ~6 MB decoded, consistent with the schema limit
BODY_MAX_BYTES = 9 * 1024 * 1024             # whole request body (a 6 MB image is ~8 MB of base64)
LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")
SESSION_PATH = re.compile(r"^/api/session/(s_[A-Za-z0-9_]+)$")
CONTENT_TYPES = {".ics": "text/calendar; charset=utf-8", ".html": "text/html; charset=utf-8",
                 ".htm": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8"}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- request validation (Russian messages)

FIELD_RU = {"session_id": "session_id", "text": "текст", "image": "картинка", "action": "действие"}


def _path(error):
    return ".".join(str(p) for p in error.absolute_path) or "запрос"


def russian_error(error) -> str:
    """One jsonschema error -> a short Russian sentence (the technical detail goes to `details`)."""
    where = _path(error)
    v = error.validator
    if v == "required":
        missing = re.findall(r"'([^']+)' is a required property", error.message)
        return f"В «{where}» не хватает поля «{missing[0] if missing else '?'}»."
    if v == "additionalProperties":
        extra = re.findall(r"'([^']+)'", error.message)
        return f"В «{where}» лишнее поле: {', '.join(extra) or '?'}."
    if v == "pattern":
        if where == "image":
            return "Картинка должна быть data URL вида data:image/(png|jpeg|webp|gif);base64,…"
        if where == "session_id":
            return "session_id должен выглядеть как s_… (получите его через POST /api/session)."
        return f"Поле «{where}» имеет неверный формат."
    if v in ("enum", "const"):
        return f"Недопустимое значение в «{where}»."
    if v == "type":
        return f"Поле «{where}» имеет неверный тип."
    if v == "minLength":
        return f"Поле «{where}» не должно быть пустым."
    if v == "maxLength":
        return "Текст слишком длинный (до 2000 символов)." if where == "text" else f"Поле «{where}» слишком длинное."
    if v == "minItems":
        return f"В «{where}» слишком мало элементов."
    if v == "maxItems":
        return f"В «{where}» слишком много элементов."
    if v == "uniqueItems":
        return f"В «{where}» элементы повторяются."
    if v == "anyOf" and where == "запрос":
        return "Нужен текст, картинка или действие."
    if v == "anyOf" and where.endswith("contact"):
        return "Укажите контакт: email или телефон."
    if v == "oneOf" and where == "action":
        return "Неизвестный тип действия."
    return f"Некорректное значение в «{where}»."


class RequestValidator:
    def __init__(self, schema=None):
        self.schema = schema or load_json(CONTRACTS / "request.schema.json")
        self.validator = jsonschema.Draft7Validator(self.schema)
        self.action_types = [d["properties"]["type"]["const"] for d in
                             (self.schema["definitions"][ref["$ref"].rsplit("/", 1)[1]]
                              for ref in self.schema["definitions"]["action"]["oneOf"])]

    def errors(self, body):
        """-> list of (russian, technical) for a turn request; [] when valid."""
        errors = sorted(self.validator.iter_errors(body), key=lambda e: (len(list(e.absolute_path)), e.message))
        out = []
        for e in errors:
            if e.validator == "oneOf" and list(e.absolute_path) == ["action"] and isinstance(body.get("action"), dict):
                # a known action type: report the precise problem of that action's own schema
                kind = body["action"].get("type")
                if kind in self.action_types:
                    sub = {"$ref": f"#/definitions/action_{kind}", "definitions": self.schema["definitions"]}
                    for se in jsonschema.Draft7Validator(sub).iter_errors(body["action"]):
                        se.path.extendleft(["action"])
                        out.append((russian_error(se), f"{_path(se)}: {se.message}"))
                    continue
            out.append((russian_error(e), f"{_path(e)}: {e.message[:300]}"))
        return out


def image_size_bytes(data_url):
    """Decoded size of a base64 data URL (approximate, from its length)."""
    payload = data_url.split(",", 1)[1] if "," in data_url else ""
    return (len(payload) * 3) // 4 - payload[-2:].count("=")


def image_is_decodable(data_url):
    payload = data_url.split(",", 1)[1] if "," in data_url else ""
    try:
        base64.b64decode(payload, validate=True)
        return bool(payload)
    except (binascii.Error, ValueError):
        return False


# ---------------------------------------------------------------- fixture engine

class FixtureEngine:
    """The Engine facade without a model: each turn returns the next fixture (for the UI developer)."""

    def __init__(self, fixtures_dir=FIXTURES_DIR, order=FIXTURE_ORDER, debug_sample=None):
        self.fixtures = {name: load_json(Path(fixtures_dir) / f"{name}.json") for name in order}
        self.order = list(order)
        sample_path = WEB_DEBUG / "sample_debug.json"
        self.debug_sample = debug_sample if debug_sample is not None else (
            load_json(sample_path) if sample_path.is_file() else None)
        self.sessions: dict = {}
        self._lock = threading.Lock()
        self._counter = 0

    def new_session(self, profile_id=None, debug=False, now=None):
        with self._lock:
            self._counter += 1
            sid = f"s_fixture_{self._counter}"
            self.sessions[sid] = {"id": sid, "profile_id": profile_id, "debug": bool(debug), "turn": 0,
                                  "created_at": (now or datetime.now().astimezone()).isoformat(),
                                  "mode": "fixtures", "requests": []}
            return sid

    def get_session(self, session_id):
        if session_id not in self.sessions:
            raise KeyError(session_id)
        return self.sessions[session_id]

    def handle(self, session_id, request):
        session = self.sessions.get(session_id)
        if session is None:
            return self.not_found(session_id)
        with self._lock:
            name = self.order[session["turn"] % len(self.order)]
            session["turn"] += 1
            summary = {k: v for k, v in request.items() if k != "image"}
            if "image" in request:
                summary["image"] = f"<data URL, {len(request['image'])} chars>"
            session["requests"] = (session["requests"] + [summary])[-20:]
            response = json.loads(json.dumps(self.fixtures[name]))
            response["session_id"] = session_id
            response["turn"] = session["turn"]
            if session["debug"]:
                response["debug"] = self.debug_block(name)
            return response

    def debug_block(self, name):
        block = {"stage_path": [f"fixture:{name}"], "calls": [], "hooks": [], "timings_ms": {}, "jev": [],
                 "flags": {"mode": "fixtures"}}
        if self.debug_sample:
            block = json.loads(json.dumps(self.debug_sample))
            block["stage_path"] = [f"fixture:{name}"] + list(block.get("stage_path") or [])
        block["fixture"] = name
        return block

    @staticmethod
    def not_found(session_id):
        return {"session_id": session_id, "turn": 0, "stage": "inspiration",
                "message": "Не нашёл такую сессию — начните новую.",
                "chips": [], "questions": [], "cards": {"destinations": [], "variants": [], "hotels": []},
                "comparison": None, "cart": None, "order": None, "actions": [], "files": {"ics": None, "page": None},
                "error": {"code": "session_not_found", "retryable": False}}


# ---------------------------------------------------------------- the API (transport-free)

class Reply:
    def __init__(self, status, body=None, content_type="application/json; charset=utf-8", raw=None, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        if raw is not None:
            self.data = raw
        else:
            self.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.content_type = content_type

    def json(self):
        return json.loads(self.data.decode("utf-8"))


def error_reply(status, code, message, details=None):
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return Reply(status, body)


class Api:
    """Routing and validation; the HTTP handler only moves bytes. Engine calls are serialised by a lock."""

    def __init__(self, engine, mode="engine", debug=False, files_root=DEFAULT_FILES, web_root=WEB_DEBUG,
                 image_max_bytes=IMAGE_MAX_BYTES, fixtures_dir=FIXTURES_DIR, validate_responses=None):
        self.engine = engine
        self.mode = mode
        self.debug = debug
        self.files_root = Path(files_root).resolve()
        self.web_root = Path(web_root)
        self.image_max_bytes = image_max_bytes
        self.fixtures_dir = Path(fixtures_dir)
        self.validator = RequestValidator()
        self.response_validator = jsonschema.Draft7Validator(load_json(CONTRACTS / "response.schema.json"))
        self.validate_responses = debug if validate_responses is None else validate_responses
        self.debug_sessions: set = set()
        self.lock = threading.Lock()

    # -------------------------------------------------------------- dispatch
    def dispatch(self, method, raw_path, body=b"") -> Reply:
        path = urlsplit(raw_path).path
        try:
            if method == "GET":
                return self.get(path)
            if method == "POST":
                return self.post(path, body)
            return error_reply(405, "method_not_allowed", "Метод не поддерживается.")
        except Exception as exc:  # noqa: BLE001 - never leave the client without an answer
            traceback.print_exc(file=sys.stderr)
            return error_reply(500, "internal", "Внутренняя ошибка сервера.", [f"{type(exc).__name__}: {exc}"])

    def get(self, path):
        if path in ("/", "/debug", "/debug/", "/index.html"):
            return self.static("index.html")
        if path == "/api/config":
            return Reply(200, {"mode": self.mode, "debug": self.debug, "image_max_bytes": self.image_max_bytes,
                               "fixture_order": list(FIXTURE_ORDER)})
        if path == "/api/fixtures":
            names = sorted(p.stem for p in self.fixtures_dir.glob("*.json"))
            return Reply(200, {"order": list(FIXTURE_ORDER), "fixtures": names})
        if path.startswith("/api/fixtures/"):
            name = path[len("/api/fixtures/"):]
            if re.fullmatch(r"[a-z_]+", name) and (self.fixtures_dir / f"{name}.json").is_file():
                return Reply(200, load_json(self.fixtures_dir / f"{name}.json"))
            return error_reply(404, "not_found", "Нет такой фикстуры.")
        match = SESSION_PATH.match(path)
        if match:
            return self.session_state(match.group(1))
        if path.startswith("/files/"):
            return self.file(path[len("/files/"):])
        return error_reply(404, "not_found", "Не найдено.")

    def post(self, path, body):
        if path not in ("/api/session", "/api/turn"):
            return error_reply(404, "not_found", "Не найдено.")
        if len(body) > BODY_MAX_BYTES:
            return error_reply(413, "too_large", "Запрос слишком большой.")
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_reply(400, "bad_request", "Тело запроса должно быть JSON-объектом.")
        if not isinstance(data, dict):
            return error_reply(400, "bad_request", "Тело запроса должно быть JSON-объектом.")
        return self.new_session(data) if path == "/api/session" else self.turn(data)

    # -------------------------------------------------------------- handlers
    def new_session(self, data):
        profile_id = data.get("profile_id")
        debug = data.get("debug", self.debug)
        if profile_id is not None and (not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",
                                                                                            profile_id)):
            return error_reply(400, "bad_request", "profile_id: латиница, цифры, _ или -, до 64 символов.")
        if not isinstance(debug, bool):
            return error_reply(400, "bad_request", "debug должен быть true или false.")
        extra = set(data) - {"profile_id", "debug"}
        if extra:
            return error_reply(400, "bad_request", f"Лишние поля: {', '.join(sorted(extra))}.")
        with self.lock:
            session_id = self.engine.new_session(profile_id=profile_id, debug=debug)
        if debug:
            self.debug_sessions.add(session_id)
        return Reply(200, {"session_id": session_id})

    def turn(self, data):
        image = data.get("image")
        if isinstance(image, str):
            if image_size_bytes(image) > self.image_max_bytes or len(image) > IMAGE_MAX_CHARS:
                mb = round(self.image_max_bytes / 1_000_000)
                return error_reply(413, "image_too_large",
                                   f"Картинка больше {mb} МБ — уменьшите её и пришлите снова.")
        errors = self.validator.errors(data)
        if errors:
            return error_reply(400, "bad_request", errors[0][0], [tech for _, tech in errors[:8]])
        if isinstance(image, str) and not image_is_decodable(image):
            return error_reply(400, "bad_request", "Картинка повреждена: base64 не читается.")
        with self.lock:
            response = self.engine.handle(data["session_id"], data)
        if self.validate_responses:
            problems = [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message[:200]}"
                        for e in self.response_validator.iter_errors(response)]
            if problems:
                print(f"[server] response violates response.schema.json: {problems[:5]}", file=sys.stderr)
                if isinstance(response.get("debug"), dict):
                    response["debug"]["contract_errors"] = problems[:20]
        return Reply(200, response)

    def session_state(self, session_id):
        if not (self.debug or session_id in self.debug_sessions):
            return error_reply(404, "not_found", "Состояние сессии доступно только в режиме отладки.")
        try:
            with self.lock:
                session = self.engine.get_session(session_id)
        except KeyError:
            return error_reply(404, "session_not_found", "Нет такой сессии.")
        if session is None:
            return error_reply(404, "session_not_found", "Нет такой сессии.")
        if hasattr(session, "to_json"):
            session = json.loads(session.to_json())
        elif hasattr(session, "model_dump"):
            session = session.model_dump(mode="json")
        return Reply(200, session)

    def static(self, name):
        target = self.web_root / name
        if not target.is_file():
            return error_reply(404, "not_found", "Макет не найден (web_debug/index.html).")
        return Reply(200, raw=target.read_bytes(), content_type="text/html; charset=utf-8")

    def file(self, rel):
        rel = unquote(rel)
        if not rel or "\x00" in rel or rel.startswith(("/", "\\")) or "\\" in rel or \
                any(part in ("..", "") for part in rel.split("/")):
            return error_reply(404, "not_found", "Файл не найден.")
        target = (self.files_root / rel).resolve()
        try:
            target.relative_to(self.files_root)
        except ValueError:
            return error_reply(404, "not_found", "Файл не найден.")
        if not target.is_file():
            return error_reply(404, "not_found", "Файл не найден.")
        kind = CONTENT_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or \
            "application/octet-stream"
        headers = {"X-Content-Type-Options": "nosniff"}
        if target.suffix.lower() == ".ics":
            headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
        return Reply(200, raw=target.read_bytes(), content_type=kind, headers=headers)


# ---------------------------------------------------------------- HTTP transport

def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TripAgent/0.1"

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and LOCAL_ORIGIN.match(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, reply: Reply):
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Content-Length", str(len(reply.data)))
            self.send_header("Cache-Control", "no-store")
            for key, value in reply.headers.items():
                self.send_header(key, value)
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(reply.data)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._send(api.dispatch("GET", self.path))

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(error_reply(400, "bad_request", "Неверный Content-Length."))
            if length > BODY_MAX_BYTES:
                self.close_connection = True
                return self._send(error_reply(413, "too_large", "Запрос слишком большой: картинка до 6 МБ."))
            body = self.rfile.read(length) if length > 0 else b""
            self._send(api.dispatch("POST", self.path, body))

        def log_message(self, fmt, *args):
            if api.debug:
                sys.stderr.write("[server] " + (fmt % args) + "\n")

    return Handler


def build_engine(flags=None, backend=None):
    """The real Engine (lazy import: ENGINE-A may land after this module)."""
    from .env import load_env
    load_env(ROOT / ".env")
    try:
        from .engine import Engine
    except ImportError as exc:
        raise SystemExit(f"agent.engine is not available ({exc}); run with --fixtures") from exc
    from .flags import parse_flags
    kwargs = {"flags": parse_flags(flags)}
    if backend == "codex":
        from .llm_codex import CodexBackend
        kwargs["backend"] = CodexBackend(effort="high")
    return Engine(**kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--fixtures", action="store_true", help="answer with the contract fixtures, no engine")
    parser.add_argument("--debug", action="store_true", help="debug sessions by default; session state endpoint")
    parser.add_argument("--flags", default=None, help="TRIP_AGENT_FLAGS spec, e.g. stage3")
    parser.add_argument("--backend", choices=("openrouter", "codex"), default=None)
    parser.add_argument("--files", default=str(DEFAULT_FILES))
    args = parser.parse_args(argv)
    if args.fixtures:
        engine, mode = FixtureEngine(), "fixtures"
    else:
        engine, mode = build_engine(args.flags, args.backend), "engine"
    api = Api(engine, mode=mode, debug=args.debug, files_root=args.files)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(api))
    print(f"http://{args.host}:{args.port}/debug  ({mode}{', debug' if args.debug else ''})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
