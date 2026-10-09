import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from system.app import App, AppError
from system.core import DemoCore
from system.server import build, make_handler
from system.store import Store


class RecordingCore:
    """Answers with fixed replies and remembers what it was asked."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def ask(self, request, session_id=None):
        self.requests.append((request, session_id))
        return self.replies.pop(0), "s-1", {"cost_usd": 0.1}


def itinerary():
    return {"type": "itinerary", "message": "m", "variants": [{"id": "v1", "label": "cheapest", "tradeoff": "t", "steps": []}],
            "profile_updates": {"value_of_hour_rub": 1500}}


class AppFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.store = Store(self.runtime / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_profile_and_trip_in_progress(self):
        core = RecordingCore({"type": "questions", "message": "q"}, itinerary(), {"type": "update", "message": "u"})
        app = App(core, self.store, self.runtime)
        key = app.start("anna")
        app.message(key, "в Лиссабон")
        app.message(key, "13–16 ноября")
        self.assertEqual(core.requests[1][1], "s-1")
        self.assertEqual(self.store.profile("anna"), {"value_of_hour_rub": 1500})
        app.choose(key, "v1")
        app.message(key, "рейс задержали")
        request = core.requests[2][0]
        self.assertEqual(request["itinerary"]["chosen_variant"], "v1")
        self.assertEqual(request["profile"], {"value_of_hour_rub": 1500})

    def test_events_need_a_chosen_variant(self):
        app = App(RecordingCore(), self.store, self.runtime)
        key = app.start("anna")
        with self.assertRaises(AppError):
            app.event(key, {"type": "step_done", "step_id": "s1"})
        with self.assertRaises(AppError):
            app.choose(key, "v1")

    def test_file_urls_stay_inside_trips(self):
        app = App(RecordingCore(), self.store, self.runtime)
        self.assertEqual(app.file_url("trips/x/trip-v1.ics"), "/files/x/trip-v1.ics")
        self.assertIsNone(app.file_url("/etc/passwd"))


class DemoOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app = build(cls.tmp.name, demo=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, True))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as res:
            return json.load(res)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as res:
            return res.status, res.read()

    def test_whole_trip(self):
        key = self.post("/api/start", {"user": "demo"})["conversation"]
        self.assertEqual(self.post("/api/message", {"conversation": key, "text": "в Лиссабон"})["reply"]["type"], "questions")
        view = self.post("/api/message", {"conversation": key, "text": "12–15 ноября"})
        self.assertEqual(view["reply"]["type"], "itinerary")
        status, body = self.get(view["urls"]["calendars"]["v-cheap"])
        self.assertEqual(status, 200)
        self.assertIn(b"BEGIN:VCALENDAR", body)
        self.post("/api/choose", {"conversation": key, "variant": "v-cheap"})
        view = self.post("/api/event", {"conversation": key, "event": {"type": "delay", "step_id": "s2", "delay_min": 120}})
        self.assertEqual(view["reply"]["type"], "update")
        self.assertEqual([c["change"] for c in view["reply"]["changes"]], ["delayed", "shifted"])
        self.assertEqual(view["chosen_variant"], "v-cheap")

    def test_page_and_traversal(self):
        status, body = self.get("/")
        self.assertIn("Поездка от двери до двери", body.decode())
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/files/../data/users/demo.json")
        self.assertEqual(e.exception.code, 404)

    def test_unknown_conversation(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/message", {"conversation": "nope", "text": "x"})
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
