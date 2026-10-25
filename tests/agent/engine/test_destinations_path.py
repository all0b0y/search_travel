"""Destinations path (entry A) on the REAL committed data, offline: real OpenData + the in-process Ostrovok
mock + the real domain; only the model is scripted (FakeBackend). Reproduces the level-3 failures:

- s02: «хочу в горы ... покататься» became variants in Гори (Gori): a wish turned into a place;
- s03: «лучше на поезде ... из Петербурга» kept suggesting Istanbul/Athens/Rome by plane;
- s22: «35 тысяч на двоих ... поезд или автобус, по России» kept suggesting foreign places for 70-95k.
"""
import pytest

from agent.engine import Engine
from agent.flags import parse_flags
from agent.jev import FakeJev
from agent.llm import FakeBackend
from agent.mcp_client import InProcessOstrovokClient
from agent.opendata import OpenData, gc_km, train_reachable
from agent.prefetch import Prefetcher
from agent.profiles import ProfileStore
from agent.sessions import SessionStore
from agent.steps import Domain

from tests.agent.domain.factory import make_opendata

from .conftest import Harness, skill
from .fakes import PRESENT_CARDS, echo_present, u

GORI = {"id": "gori", "name_ru": "Гори", "name_en": "Gori", "country": "Грузия", "country_code": "GE",
        "kind": "city", "lat": 41.981847, "lon": 44.111781, "tags": ["city"], "airports": ["TBS"]}

pytestmark = pytest.mark.skipif(not OpenData(overlay=None).available, reason="data/opendata.sqlite missing")


@pytest.fixture
def real(response_schema, pinned_now, tmp_path):
    """Harness on real data; the overlay holds the lazily built «Гори» of the live run."""
    overlay = tmp_path / "opendata_extra.sqlite"
    make_opendata(overlay, [GORI])
    lazy_calls = []

    def lazy(name):
        lazy_calls.append(name)
        return None

    def factory(flags="stage2"):
        backend = FakeBackend()
        for card in PRESENT_CARDS:
            backend.scripts.setdefault(card, echo_present)
        od = OpenData(overlay=overlay)
        engine = Engine(skill=skill(), backend=backend, jev=FakeJev(fail=True), ostrovok=InProcessOstrovokClient(),
                        opendata=od, flags=parse_flags(flags), store=SessionStore(":memory:"),
                        profiles=ProfileStore(":memory:"), domain=Domain(), prefetcher=Prefetcher(synchronous=True),
                        lazy_city=lazy, files_dir=str(tmp_path))
        h = Harness(engine, backend, engine.ostrovok, response_schema, pinned_now)
        h.new()
        h.od, h.lazy_calls = od, lazy_calls
        return h
    return factory


def user_set(h, **paths):
    """Set user values on the brief directly (prefs.domestic: the words -> pref mapping is the understand
    card's job; here only the destinations path is under test)."""
    from agent.state import apply_patch
    session = h.session()
    session.brief, _ = apply_patch(session.brief, {"set": {k.replace("__", "."): v for k, v in paths.items()}})
    h.engine.store.put(session)


def card_items(h, resp):
    """The destination items the present card got (ids in card order, with every number)."""
    ids = [c["id"] for c in resp["cards"]["destinations"]]
    assert ids, resp["message"]
    return ids


def last_input(h):
    req = [r for r in h.backend.requests if r.card == "present.destinations"][-1]
    import json
    return json.loads(req.user)


# ---------------------------------------------------------------- s02: wishes are never places

def test_s02_gory_is_a_wish_not_gori(real):
    h = real()
    h.understand(u(set={"destination.name": "горы", "dates.month": 12, "dates.nights": 7, "party.segment": "solo",
                        "traits.add": ["mountains", "ski"]}))
    r = h.turn("хочу в горы на недельку в декабре, один, покататься")
    assert r["stage"] == "inspiration", r["message"]
    ids = card_items(h, r)
    assert "gori" not in ids and not r["cards"]["variants"]
    assert h.session().brief.values()["destination"] is None
    assert h.lazy_calls == []                          # no lazy city build for a wish
    tags = {d["id"]: d["tags"] for d in h.od.destinations()}
    assert all("ski" in tags[i] for i in ids), ids     # «покататься» in December -> ski places


def test_s02_wish_without_traits_still_gives_its_trait(real):
    h = real()
    h.understand(u(set={"destination.name": "в горы", "dates.month": 12, "party.segment": "solo"}))
    r = h.turn("хочу в горы")
    assert r["stage"] == "inspiration" and "gori" not in card_items(h, r)
    b = h.session().brief
    assert b.values()["destination"] is None and "mountains" in b.values()["traits"]
    assert b.source_of("traits") == "user" and h.lazy_calls == []


def test_real_place_still_resolves(real):
    h = real()
    h.understand(u(set={"destination.name": "Гори", "dates.start": "2026-12-05", "dates.end": "2026-12-12",
                        "party.segment": "solo"}))
    h.turn("хочу в Гори")
    assert (h.session().brief.values()["destination"] or {}).get("id") == "gori"


# ---------------------------------------------------------------- s03: train only from Saint Petersburg

def _assert_train_items(h, card_input, origin_rec):
    items = card_input["items"]
    assert items
    for i in items:
        rec = h.od.destination(i["id"])
        assert rec["country_code"] == "RU" and train_reachable(origin_rec, rec), i["id"]
        f = i["facts"]
        assert f["transport"] == "train" and "flight_price_rub" not in f
        assert i["price_from_rub"] == f["train_price_rub"] + f["stay_price_rub"]
        assert isinstance(i["travel_time_h"], float) and i["travel_time_h"] > 0


def test_s03_train_preference_suggests_only_train_destinations(real):
    h = real()
    h.understand(u(set={"origin.city": "Санкт-Петербург", "party.segment": "couple", "traits.add": ["food", "city"],
                        "dates.start": "2026-10-31", "dates.end": "2026-11-01"}))
    r = h.turn("Хочется на выходные куда-нибудь вкусно поесть и погулять, вдвоём")
    first = card_items(h, r)
    countries = {h.od.destination(i)["country_code"] for i in first}
    assert countries - {"RU"}, first                      # by plane: foreign places are fine
    h.understand(u(intent="refine", set={"prefs.transport": "train"}))
    r = h.turn("Ой, а самолёты мы не очень любим, лучше на поезде. Есть что-нибудь, куда можно доехать поездом?")
    assert r["stage"] == "inspiration", r["message"]
    ids = card_items(h, r)
    spb = h.od.destination("saint-petersburg")
    _assert_train_items(h, last_input(h), spb)
    assert "saint-petersburg" not in ids
    assert all(gc_km(spb["lat"], spb["lon"], h.od.destination(i)["lat"], h.od.destination(i)["lon"]) <= 1000
               for i in ids)


# ---------------------------------------------------------------- s22: budget and «по России»

def _prices(card_input):
    return [i["price_from_rub"] for i in card_input["items"]]


def test_s22_budget_first_then_domestic_train(real):
    h = real()
    h.understand(u(set={"origin.city": "Москва", "party.segment": "friends", "party.adults": 2,
                        "budget_rub.max": 35000, "dates.month": 11, "dates.nights": 3}))
    r = h.turn("Студенты, нас двое, 35 тысяч на всё, хотим куда-нибудь на 2–3 дня из Москвы в ноябре")
    card_items(h, r)
    inp = last_input(h)
    prices = _prices(inp)
    fits = [p is not None and p <= 35000 for p in prices]
    flags = [i["facts"].get("within_budget") for i in inp["items"]]
    assert flags == [p is not None and p <= 35000 if p is not None else None for p in prices]
    if any(fits):   # fitting items come first
        assert fits == sorted(fits, reverse=True)
    else:           # nothing fits: cheapest first, every item flagged
        known = [p for p in prices if p is not None]
        assert known == sorted(known) and all(f is False for f in flags if f is not None)
    # «по России, поезд или автобус подойдёт, до 35 тысяч на двоих»
    user_set(h, prefs__domestic=True)
    h.understand(u(intent="refine", set={"prefs.transport": "train"}))
    r = h.turn("Нам и поезд или автобус подойдёт, по России есть что-нибудь до 35 тысяч на двоих?")
    ids = card_items(h, r)
    inp = last_input(h)
    assert all(h.od.destination(i)["country_code"] == "RU" for i in ids)
    _assert_train_items(h, inp, h.od.destination("moscow"))
    prices = _prices(inp)
    assert prices[0] is not None and prices[0] <= 35000, prices
    assert inp["items"][0]["facts"]["within_budget"] is True
    fits = [p is not None and p <= 35000 for p in prices]
    assert fits == sorted(fits, reverse=True)
    if sum(fits) >= 3:   # enough fitting: nothing far above the budget is shown
        assert all(p is not None and p <= 35000 * 1.3 for p in prices)


def test_domestic_only_without_train(real):
    h = real()
    user_set(h, prefs__domestic=True)
    h.understand(u(set={"origin.city": "Москва", "party.segment": "couple", "traits.add": ["sea", "warm"],
                        "dates.month": 11}))
    r = h.turn("к морю, но по России")
    ids = card_items(h, r)
    assert all(h.od.destination(i)["country_code"] == "RU" for i in ids), ids


def test_nothing_fits_budget_shows_cheapest_and_flags(real):
    h = real()
    h.understand(u(set={"origin.city": "Москва", "party.segment": "couple", "traits.add": ["sea", "warm"],
                        "budget_rub.max": 5000, "dates.month": 11, "dates.nights": 7}))
    r = h.turn("к тёплому морю за 5 тысяч")
    card_items(h, r)
    inp = last_input(h)
    known = [p for p in _prices(inp) if p is not None]
    assert known and known == sorted(known) and min(known) > 5000
    assert all(i["facts"].get("within_budget") is False for i in inp["items"] if i["price_from_rub"] is not None)


def test_order_by_budget_rules():
    from agent.steps import order_by_budget

    def item(i, p):
        return {"id": i, "price_from_rub": p, "facts": {}}
    items = [item("a", 50000), item("b", 30000), item("c", None), item("d", 20000), item("e", 34000),
             item("f", 44000), item("g", 40000)]
    out = order_by_budget([dict(i, facts={}) for i in items], 35000, 6)
    assert [i["id"] for i in out] == ["b", "d", "e", "g", "f"][:5]   # fits in rank order, then <= 1.3x; c, a dropped
    assert [i["facts"]["within_budget"] for i in out] == [True, True, True, False, False]
    few = order_by_budget([dict(i, facts={}) for i in items], 25000, 6)
    assert [i["id"] for i in few] == ["d", "b", "e", "g", "f", "a"]  # one fits: kept first, the rest cheapest first
    none = order_by_budget([dict(i, facts={}) for i in items], 10000, 3)
    assert [i["id"] for i in none] == ["d", "b", "e"] and all(i["facts"]["within_budget"] is False for i in none)
    assert [i["id"] for i in order_by_budget([dict(i, facts={}) for i in items], None, 3)] == ["a", "b", "c"]


def test_card_input_keeps_the_binding_shape(real):
    """New facts (within_budget, transport, train_price_rub) live inside items[].facts: no new card-input keys."""
    from evals.card_inputs import validate_card_input
    h = real()
    h.understand(u(set={"origin.city": "Москва", "party.segment": "couple", "budget_rub.max": 30000,
                        "prefs.transport": "train", "dates.month": 11, "dates.nights": 2}))
    card_items(h, h.turn("поездом, до 30 тысяч"))
    inp = last_input(h)
    assert validate_card_input("present.destinations", inp) == []
    assert all(i["facts"]["transport"] == "train" and "within_budget" in i["facts"] for i in inp["items"])
