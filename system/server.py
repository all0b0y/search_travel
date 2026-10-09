"""HTTP server: the web page and a small JSON API over App.

  python3 -m system.server [--port 8000] [--demo] [--runtime runtime]
"""
import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .app import App, AppError
from .core import ClaudeCore, CoreError, DemoCore, prepare_runtime
from .store import Store

STATIC = Path(__file__).resolve().parent / "static"


def make_handler(app, demo):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path):
            data = path.read_bytes()
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix == ".ics":
                kind = "text/calendar"
            self.send_response(200)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self.send_file(STATIC / "index.html")
            if self.path == "/api/config":
                return self.send_json(200, {"demo": demo})
            if self.path.startswith("/files/"):
                target = (app.trips / self.path[len("/files/"):].split("?")[0]).resolve()
                if app.trips in target.parents and target.is_file():
                    return self.send_file(target)
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/start":
                    return self.send_json(200, {"conversation": app.start(body.get("user") or "guest")})
                key = body.get("conversation", "")
                if self.path == "/api/message":
                    return self.send_json(200, app.message(key, body["text"], body.get("attachments")))
                if self.path == "/api/choose":
                    return self.send_json(200, app.choose(key, body["variant"]))
                if self.path == "/api/event":
                    return self.send_json(200, app.event(key, body["event"]))
                self.send_json(404, {"error": "not found"})
            except (KeyError, json.JSONDecodeError) as e:
                self.send_json(400, {"error": f"bad request: {e}"})
            except AppError as e:
                self.send_json(409, {"error": str(e)})
            except CoreError as e:
                self.send_json(502, {"error": str(e)})

        def log_message(self, fmt, *args):
            pass

    return Handler


def build(runtime, demo):
    runtime = prepare_runtime(Path(runtime).resolve())
    core = DemoCore(runtime) if demo else ClaudeCore(runtime)
    return App(core, Store(runtime / "data"), runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--demo", action="store_true", help="replay invented data instead of calling Claude Code")
    args = parser.parse_args()
    app = build(args.runtime, args.demo)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app, args.demo))
    print(f"http://127.0.0.1:{args.port}" + ("  (demo: invented data)" if args.demo else ""))
    server.serve_forever()


if __name__ == "__main__":
    main()
