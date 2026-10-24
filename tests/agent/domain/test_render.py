"""render: one .ics and one standalone HTML page per variant (ported trip.py calendar/page)."""
import re
from datetime import datetime, timedelta, timezone

from agent.ranking import build_variants
from agent.render import calendar, page, reminder_text, render_all, render_variant

from .factory import DISTRICTS, RULES, SEGMENTS, TODAY, sea_brief, sea_results
from .test_checks import doc, raw_variant, variant

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone(timedelta(hours=3)))


def lisbon():
    v, _ = variant(raw_variant(doc(), "v-cheap"))
    return v


def test_calendar_ported():
    ics = calendar("trip-1", lisbon(), RULES, NOW)
    assert ics.count("BEGIN:VEVENT") == 7 and ics.count("BEGIN:VALARM") == 7
    assert "DTSTART:20261112T041000Z" in ics
    assert "DTSTAMP:20261024T090000Z" in ics
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))
    assert "TRIGGER:-PT10M" in ics and "TRIGGER:-PT60M" in ics


def test_reminder_text_ported():
    first = lisbon().steps[0].to_check_dict()
    assert "Berlin\\, Kreuzberg → BER" not in reminder_text(first, RULES)
    assert "Berlin, Kreuzberg → BER" in reminder_text(first, RULES)
    assert reminder_text(first, RULES).endswith("05:10")


def test_rules_reminders_override():
    r = {**RULES, "reminders": {"taxi": {"lead_min": 15, "text": "Такси в {time}"}}}
    ics = calendar("t", lisbon(), r, NOW)
    assert "TRIGGER:-PT15M" in ics and "Такси в 05:10" in ics


def test_page_is_standalone_light_dark_and_escaped():
    vs = build_variants(sea_brief(), sea_results(), DISTRICTS, RULES, TODAY, segments=SEGMENTS)
    v = vs[0]
    v.steps[2].to = "Отель <script>alert(1)</script>"
    html = page(v, title="Анталья — Дешевле", now=NOW)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "prefers-color-scheme:dark" in html
    assert not re.search(r"(src|href)=\"?https?://", html) and "<link" not in html and "@import" not in html
    assert "80 600 ₽" in html and "Чем жертвуете" in html and "Риски" in html and "Кому не подойдёт" in html
    assert "синтетические" in html


def test_render_writes_files_and_returns_urls(tmp_path):
    vs = build_variants(sea_brief(), sea_results(), DISTRICTS, RULES, TODAY, segments=SEGMENTS)
    files = render_variant("s_test/../x", vs[0], sea_brief(), RULES, out_root=tmp_path, now=NOW)
    assert files == {"ics": "/files/s_test_.._x/trip-cheapest.ics", "page": "/files/s_test_.._x/trip-cheapest.html"}
    folder = tmp_path / "s_test_.._x"
    assert (folder / "trip-cheapest.ics").read_text(encoding="utf-8").count("BEGIN:VEVENT") == len(vs[0].steps)
    assert "<title>Анталья — Дешевле — маршрут</title>" in (folder / "trip-cheapest.html").read_text(encoding="utf-8")
    all_files = render_all("s1", vs, sea_brief(), RULES, out_root=tmp_path, now=NOW)
    assert set(all_files) == {v.id for v in vs}


def test_deterministic_with_pinned_now(tmp_path):
    v = lisbon()
    assert calendar("t", v, RULES, NOW) == calendar("t", v, RULES, NOW)
    assert page(v, now=NOW) == page(v, now=NOW)
