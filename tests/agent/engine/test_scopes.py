"""Scopes and ticket prefs end to end: real engine, real ranking/checks, the in-process Ostrovok mock over the
real catalog (data/ostrovok_catalog.sqlite) and real open data; the model is a scripted FakeBackend.

Reproduces the first live level-3 failures: s11 (tickets only), s09 (Kazan by train), s18 (Sapsan),
s07 (free cancellation incl. tickets), s24 (arrive before 14:00, no night flights).
"""
from pathlib import Path

import pytest

from agent.checks import blocking, check_variant
from agent.engine import Engine
from agent.flags import parse_flags
from agent.jev import FakeJev
from agent.llm import FakeBackend
from agent.prefetch import Prefetcher
from agent.profiles import ProfileStore
from agent.sessions import SessionStore

from .conftest import Harness, skill
from .fakes import PRESENT_CARDS, echo_present, u

ROOT = Path(__file__).resolve().parents[3]
DATA = [ROOT / "data" / "ostrovok_catalog.sqlite", ROOT / "data" / "opendata.sqlite"]
pytestmark = pytest.mark.skipif(not all(p.exists() for p in DATA), reason="real data DBs not built")

TRAVELLERS1 = [{"name": "Иван Петров", "birth_date": "1990-01-01"}]


@pytest.fixture
def live(request, response_schema, pinned_now, tmp_path, monkeypatch):
    from agent.mcp_client import InProcessOstrovokClient
    from agent.opendata import OpenData
    from ostrovok_mcp import db
    monkeypatch.setenv("OSTROVOK_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("OSTROVOK_CATALOG_EXTRA_DB", str(tmp_path / "no_catalog_extra.sqlite"))
    monkeypatch.setenv("OPENDATA_EXTRA_DB", str(tmp_path / "no_opendata_extra.sqlite"))
    db.reset_caches()
    backend = FakeBackend()
    for card in PRESENT_CARDS:
        backend.scripts.setdefault(card, echo_present)
    engine = Engine(skill=skill(), backend=backend, jev=FakeJev(fail=True), ostrovok=InProcessOstrovokClient(),
                    opendata=OpenData(path=str(ROOT / "data" / "opendata.sqlite"),
                                      overlay=str(tmp_path / "no_opendata_extra.sqlite")),
                    flags=parse_flags(getattr(request, "param", "stage1")), store=SessionStore(":memory:"), profiles=ProfileStore(":memory:"),
                    prefetcher=Prefetcher(synchronous=True), lazy_city=False, files_dir=str(tmp_path))
    h = Harness(engine, backend, engine.ostrovok, response_schema, pinned_now)
    h.new()
    yield h
    db.reset_caches()


def ask(h, text, **patch):
    h.understand(u(set=patch))
    r = h.turn(text)
    assert r.get("error") is None, (r["message"], r.get("error"))
    assert r["stage"] == "selection" and r["cards"]["variants"], r["message"]
    return r, h.session().variants


def no_blocks(h, variants):
    brief, rules = h.session().brief, h.engine.skill.rules
    for v in variants:
        assert blocking(check_variant(v, brief, rules)) == [], (v.id, check_variant(v, brief, rules))


def kinds(v):
    return [s.type for s in v.steps]


# ---------------------------------------------------------------- s11: tickets only

def test_s11_flights_only_to_cart_and_pay(live):
    h = live
    r, vs = ask(h, "только билеты Москва — Екатеринбург на 20 ноября, обратно 23-го, я один",
                **{"origin.city": "Москва", "destination.name": "Екатеринбург", "dates.start": "2026-11-20",
                   "dates.end": "2026-11-23", "party.segment": "solo", "party.adults": 1, "scope": ["flights"]})
    assert all(kinds(v) == ["flight", "flight"] and v.hotel is None for v in vs)
    assert all(v.steps[0].start[:10] == "2026-11-20" and v.steps[1].start[:10] == "2026-11-23" for v in vs)
    assert len({v.id for v in vs}) == len(vs) >= 2
    no_blocks(h, vs)
    r = h.act("choose_variant", variant_id=vs[0].id)
    assert r["stage"] == "cart", r["message"]
    assert {i["kind"] for i in r["cart"]["items"]} == {"flight"} and len(r["cart"]["items"]) == 2
    r = h.act("checkout", travellers=TRAVELLERS1, contact={"email": "a@b.c"})
    assert r["stage"] == "checkout" and r["order"], r["message"]
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r["stage"] == "ordered", r["message"]


@pytest.mark.parametrize("live", ["stage5"], indirect=True)
def test_tickets_only_with_every_stage_flag(live):
    """sensitivity, upsell, insights on: nothing assumes a hotel."""
    r, vs = ask(live, "только билеты в Казань 4 декабря, обратно 7-го",
                **{"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
                   "dates.end": "2026-12-07", "party.adults": 1, "scope": ["flights", "trains"]})
    assert all("stay" not in kinds(v) for v in vs)
    no_blocks(live, vs)
    r = live.act("choose_variant", variant_id=vs[-1].id)
    assert r["stage"] == "cart" and {i["kind"] for i in r["cart"]["items"]} <= {"flight", "train"}, r["message"]


def test_trains_only_scope(live):
    _, vs = ask(live, "только поезд Москва — Казань 4 декабря, обратно 7-го",
                **{"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
                   "dates.end": "2026-12-07", "party.adults": 1, "scope": ["trains"]})
    assert all(kinds(v) == ["train", "train"] and v.hotel is None for v in vs)
    assert all(v.steps[0].start[:10] == "2026-12-04" for v in vs)   # tickets only: the user's travel day
    assert {v.id for v in vs} >= {"cheapest", "comfort"}             # comfort = a better car class
    no_blocks(live, vs)


def test_flights_and_trains_without_stay(live):
    _, vs = ask(live, "билеты в Питер и обратно, поезд или самолёт",
                **{"origin.city": "Москва", "destination.name": "Санкт-Петербург", "dates.start": "2026-11-27",
                   "dates.end": "2026-11-29", "party.adults": 1, "scope": ["flights", "trains"]})
    assert all("stay" not in kinds(v) and len(kinds(v)) == 2 for v in vs)
    no_blocks(live, vs)


# ---------------------------------------------------------------- s09 / s18: trains with a stay

def test_s09_kazan_by_train_with_stay(live):
    h = live
    _, vs = ask(h, "Казань на поезде, 4–7 декабря, едем вдвоём с подругой",
                **{"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
                   "dates.end": "2026-12-07", "party.adults": 2, "prefs.transport": "train"})
    for v in vs:
        assert kinds(v)[0] == "train" and kinds(v)[-1] == "train" and "stay" in kinds(v)
        stay = next(s for s in v.steps if s.type == "stay")
        assert stay.start[:10] == "2026-12-04" and stay.end[:10] == "2026-12-07"
    no_blocks(h, vs)


def test_s18_sapsan_to_st_petersburg(live):
    h = live
    _, vs = ask(h, "а Сапсаном можно? вылет в 5:40 не оч",
                **{"origin.city": "Москва", "destination.name": "Санкт-Петербург", "dates.start": "2026-11-27",
                   "dates.end": "2026-11-29", "party.segment": "friends", "party.adults": 4,
                   "prefs.transport": "train", "constraints.add": ["no_night_flights"],
                   "scope": ["flights", "trains", "stay", "transfers"]})
    assert all(kinds(v)[0] == "train" and kinds(v)[-1] == "train" for v in vs)
    assert all("no_transport" not in [n.code for n in v.risks] for v in vs)
    assert any("Сапсан" in (s.carrier or "") for v in vs for s in v.steps)
    no_blocks(h, vs)


# ---------------------------------------------------------------- s07 / s24: ticket prefs

def test_s07_free_cancellation_includes_tickets(live):
    h = live
    _, vs = ask(h, "Калининград 1–8 июля вдвоём, нужна полная бесплатная отмена, и билеты тоже",
                **{"origin.city": "Москва", "destination.name": "Калининград", "dates.start": "2027-07-01",
                   "dates.end": "2027-07-08", "party.adults": 2, "prefs.free_cancellation": True})
    for v in vs:
        codes = [n.code for n in v.risks]
        flights = [s for s in v.steps if s.type == "flight"]
        assert flights
        refundable = [v.facts.get("out_refundable"), v.facts.get("back_refundable")]
        assert all(refundable) or "non_refundable_only" in codes, (v.id, refundable, codes)
        assert all(refundable), "the mock has flex fares on this route"
    no_blocks(h, vs)


def _understand_knows(path):
    return path in skill().card("understand").schema["properties"]["patch"]["properties"]["set"]["properties"]


@pytest.mark.skipif(not _understand_knows("prefs.arrive_before"),
                    reason="understand schema does not list prefs.arrive_before yet (UNDERSTAND slice)")
def test_s24_arrive_before_lunch_no_night_flights(live):
    h = live
    _, vs = ask(h, "Минск, 13–16 декабря, лечу с мамой, прилететь до обеда (до 14:00), ночные рейсы не предлагать",
                **{"origin.city": "Москва", "destination.name": "Минск", "dates.start": "2026-12-13",
                   "dates.end": "2026-12-16", "party.adults": 2, "prefs.arrive_before": "14:00",
                   "prefs.no_night_flights": True})
    for v in vs:
        flights = [s for s in v.steps if s.type == "flight"]
        assert flights[0].end[11:16] <= "14:00", (v.id, flights[0].end)
        for s in flights:
            for t in (s.start[11:16], s.end[11:16]):
                assert "06:00" <= t < "23:00", (v.id, s.start, s.end)
        assert "relaxed_pref" not in [n.code for n in v.risks]
    no_blocks(h, vs)
