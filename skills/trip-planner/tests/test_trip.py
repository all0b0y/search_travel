import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("trip", HERE.parent / "scripts" / "trip.py")
trip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trip)

FIXTURE = json.loads((HERE / "fixtures" / "berlin-lisbon.json").read_text(encoding="utf-8"))


def run(doc):
    with tempfile.TemporaryDirectory() as tmp:
        draft = Path(tmp) / "draft.json"
        draft.write_text(json.dumps(doc), encoding="utf-8")
        report = trip.finalize(draft)
        out = json.loads((Path(tmp) / "itinerary.json").read_text(encoding="utf-8")) if (Path(tmp) / "itinerary.json").exists() else None
        calendars = {p.name: p.read_text(encoding="utf-8") for p in Path(tmp).glob("*.ics")}
    return report, out, calendars


def checks(report):
    return {(p["check"], p["variant"], p["step"]) for p in report["problems"]}


def variant(doc, vid):
    return next(v for v in doc["variants"] if v["id"] == vid)


def step(doc, vid, sid):
    return next(s for s in variant(doc, vid)["steps"] if s["id"] == sid)


class FinalizeFixture(unittest.TestCase):
    def setUp(self):
        self.report, self.out, self.calendars = run(copy.deepcopy(FIXTURE))

    def test_fixture_passes(self):
        self.assertEqual(self.report["problems"], [])

    def test_rubles_and_totals(self):
        self.assertEqual(step(self.out, "v-cheap", "s2")["price"]["rub"], 17100)
        self.assertEqual(variant(self.out, "v-cheap")["totals"], {"rub": 80085, "hours": 9.0, "effective_rub": 93585})

    def test_reminders_lead_the_step(self):
        first = variant(self.out, "v-cheap")["reminders"][0]
        self.assertEqual(first["at"], "2026-11-12T05:00:00+01:00")
        self.assertIn("Berlin, Kreuzberg → BER", first["text"])

    def test_one_calendar_per_variant_with_alarms(self):
        self.assertEqual(set(self.calendars), {"trip-v-cheap.ics", "trip-v-fast.ics", "trip-v-comfort.ics"})
        ics = self.calendars["trip-v-cheap.ics"]
        self.assertEqual(ics.count("BEGIN:VEVENT"), 7)
        self.assertEqual(ics.count("BEGIN:VALARM"), 7)
        self.assertIn("DTSTART:20261112T041000Z", ics)

    def test_output_is_a_valid_response(self):
        self.assertEqual(trip.validate_doc(self.out), [])
        self.assertEqual(self.out["type"], "itinerary")


class FinalizeProblems(unittest.TestCase):
    def setUp(self):
        self.doc = copy.deepcopy(FIXTURE)

    def test_tight_airport_buffer(self):
        step(self.doc, "v-cheap", "s1")["end"] = "2026-11-12T06:10:00+01:00"
        report, _, _ = run(self.doc)
        self.assertIn(("timing", "v-cheap", "s2"), checks(report))

    def test_taxi_without_margin(self):
        step(self.doc, "v-cheap", "s1")["duration_estimate_min"] = 40
        report, _, _ = run(self.doc)
        self.assertIn(("timing", "v-cheap", "s1"), checks(report))

    def test_broken_chain(self):
        step(self.doc, "v-cheap", "s3")["to"] = "Another hotel"
        report, _, _ = run(self.doc)
        self.assertIn(("chain", "v-cheap", "s4"), checks(report))

    def test_missing_rate(self):
        step(self.doc, "v-cheap", "s2")["price"]["currency"] = "USD"
        report, _, _ = run(self.doc)
        self.assertIn(("price", "v-cheap", "s2"), checks(report))

    def test_over_budget(self):
        self.doc["brief"]["budget_rub"] = 100000
        report, _, _ = run(self.doc)
        self.assertIn(("brief", "v-comfort", None), checks(report))

    def test_departure_outside_window(self):
        self.doc["brief"]["depart"] = {"from": "2026-11-13", "to": "2026-11-13"}
        report, _, _ = run(self.doc)
        self.assertIn(("brief", "v-cheap", "s2"), checks(report))

    def test_cheapest_label_must_be_cheapest(self):
        step(self.doc, "v-cheap", "s4")["price"]["amount"] = 1000
        report, _, _ = run(self.doc)
        self.assertIn(("variants", "v-cheap", None), checks(report))

    def test_missing_variants_is_reported(self):
        del self.doc["variants"]
        report, _, _ = run(self.doc)
        self.assertFalse(report["ok"])


class RiskyConnection(unittest.TestCase):
    """Land in Lisbon, taxi to the station, catch a train: the train is missable."""

    def setUp(self):
        self.doc = copy.deepcopy(FIXTURE)
        steps = variant(self.doc, "v-cheap")["steps"]
        steps[2:3] = [
            {"id": "t1", "type": "taxi", "from": "LIS", "to": "Oriente", "start": "2026-11-12T11:50:00+00:00", "end": "2026-11-12T12:10:00+00:00", "duration_estimate_min": 15, "price": {"amount": 15, "currency": "EUR", "estimate": True, "checked_at": "x"}, "link": "https://m.uber.com/ul/", "status": "planned"},
            {"id": "t2", "type": "train", "from": "Oriente", "to": "Hotel Alfama Lisboa", "start": "2026-11-12T12:50:00+00:00", "end": "2026-11-12T13:20:00+00:00", "price": {"amount": 5, "currency": "EUR", "checked_at": "x"}, "link": "https://www.omio.com/x", "status": "planned"},
        ]

    def test_risky_train_needs_plan_b(self):
        report, out, _ = run(self.doc)
        self.assertIn(("plan_b", "v-cheap", "t2"), checks(report))
        self.assertEqual(step(out, "v-cheap", "t2")["risk"], {"slack_min": 10, "risky": True})

    def test_plan_b_clears_it_and_gets_rubles(self):
        train = variant(self.doc, "v-cheap")["steps"][3]
        backup = dict(train, id="t2b", start="2026-11-12T13:50:00+00:00", end="2026-11-12T14:20:00+00:00")
        train["plan_b"] = {"trigger": "рейс задержан больше чем на 15 мин", "step": backup}
        report, out, _ = run(self.doc)
        self.assertNotIn(("plan_b", "v-cheap", "t2"), checks(report))
        self.assertEqual(step(out, "v-cheap", "t2")["plan_b"]["step"]["price"]["rub"], 475)


class Links(unittest.TestCase):
    def test_affiliate_added_only_when_configured(self):
        conf = {"booking.com": {"param": "aid", "value": "123"}}
        self.assertEqual(trip.with_affiliate("https://www.booking.com/hotel/x.html?lang=ru", conf), "https://www.booking.com/hotel/x.html?lang=ru&aid=123")
        self.assertEqual(trip.with_affiliate("https://www.kiwi.com/x", conf), "https://www.kiwi.com/x")
        self.assertEqual(trip.with_affiliate("https://www.booking.com/x", {"booking.com": {"param": "aid", "value": ""}}), "https://www.booking.com/x")


class Validate(unittest.TestCase):
    def test_questions_reply(self):
        reply = {"type": "questions", "message": "Уточню пару деталей", "questions": [{"id": "budget_rub", "text": "Бюджет?", "recommended": "150 000 ₽"}]}
        self.assertEqual(trip.validate_doc(reply), [])

    def test_missing_message(self):
        self.assertEqual([p["detail"] for p in trip.validate_doc({"type": "questions"})], ["$: missing message"])

    def test_bad_enum(self):
        self.assertTrue(trip.validate_doc({"type": "chat", "message": "x"}))


if __name__ == "__main__":
    unittest.main()
