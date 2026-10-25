"""Held-out level-3 gaps (evals/runs/20261025-121227-L3-holdout), offline on the real mock + real data:

- h10: a change of trip kind at selection («по России, поездом») kept Bukhara and rebuilt stay-only variants;
- h01: Muscat suggested from Novosibirsk in December, then choose_destination answered no_data twice
  (root cause: every OVB -> MCT flight of the check-in day lands the next day, so nothing paired with the stay);
- h01 / s05: with a budget and every variant above it, «другие числа / отель попроще» got «подборка та же».
"""
import pytest

from agent.checks import blocking, check_variant
from agent.engine import route_intent, wants_cheaper, wants_other_dates
from agent.opendata import train_reachable
from agent.steps import Domain

from .fakes import u
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)


def variants_of(h):
    return h.session().variants


def no_blocks(h):
    s = h.session()
    for v in s.variants:
        assert blocking(check_variant(v, s.brief, h.engine.skill.rules)) == [], (v.id, check_variant(v, s.brief, h.engine.skill.rules))


def h01_destinations(h):
    h.understand(u(set={"origin.city": "Новосибирск", "party.segment": "couple", "party.adults": 2,
                        "dates.month": 12, "dates.nights": 7, "traits.add": ["warm", "sun"]}))
    r = h.turn("Устали от слякоти. Солнца бы в декабре, мы с мужем, бюджет скромный")
    ids = [c["id"] for c in r["cards"]["destinations"]]
    assert "muscat" in ids, ids
    return ids


# ---------------------------------------------------------------- 2. a suggested destination leads to variants

def test_h01_muscat_from_novosibirsk_builds(live):
    h = live
    h01_destinations(h)
    r = h.act("choose_destination", destination_id="muscat")
    assert r.get("error") is None, r["message"]
    assert r["stage"] == "selection" and r["cards"]["variants"]
    assert any("overnight outbound" in n for n in r["debug"]["notes"])
    for v in variants_of(h):
        out = next(s for s in v.steps if s.type == "flight")
        stay = next(s for s in v.steps if s.type == "stay")
        assert out.end[:10] <= stay.start[:10]            # lands on the check-in day: no lost first night
    no_blocks(h)


def _never_builds(*args, **kwargs):
    return []


def test_nothing_builds_tries_window_then_says_what_is_missing(live):
    h = live
    ids = h01_destinations(h)
    h.engine.domain = Domain(build_variants=_never_builds)
    r = h.act("choose_destination", destination_id="muscat")
    assert r["error"]["code"] == "no_data"
    msg = r["message"]
    assert "нет данных" not in msg and "не хватает билетов или отелей" not in msg
    assert "Новосибирск — Маскат" in msg and "стыкуются с отелем" in msg, msg
    assert "±3" in msg
    notes = r["debug"]["notes"]
    assert any(n.startswith("rescue: nothing within") for n in notes), notes
    offered = [a["payload"]["destination_id"] for a in r["actions"] if a["type"] == "choose_destination"]
    assert offered and "muscat" not in offered and set(offered) <= set(ids)
    # the same dead end again is not the same sentence
    r2 = h.act("choose_destination", destination_id="muscat")
    assert r2["error"]["code"] == "no_data" and r2["message"] != msg
    assert any(a["type"] == "choose_destination" for a in r2["actions"])


def test_no_hotels_says_so_plainly(live, monkeypatch):
    h = live
    h01_destinations(h)
    real_call = h.engine.ostrovok.call

    def no_hotels(tool, payload):
        out = real_call(tool, payload)
        return {**out, "offers": []} if tool == "search_hotels" else out
    monkeypatch.setattr(h.engine.ostrovok, "call", no_hotels)
    r = h.act("choose_destination", destination_id="muscat")
    assert r["error"]["code"] == "no_data"
    assert "свободных отелей" in r["message"] and "не нашлось" in r["message"], r["message"]
    assert any(a["type"] == "choose_destination" for a in r["actions"])


# ---------------------------------------------------------------- 3. fitting a budget when nothing fits

def _dubai(h):
    h.understand(u(set={"origin.city": "Новосибирск", "party.segment": "couple", "party.adults": 2,
                        "destination.name": "Дубай", "dates.month": 12, "dates.nights": 8,
                        "prefs.direct_only": True}))
    r = h.turn("Тогда давайте Дубай на 8 ночей в декабре, только прямые рейсы")
    assert r["cards"]["variants"], r["message"]
    return min(v.totals.rub for v in variants_of(h))


def test_h01_budget_moves_dates_in_the_month(live):
    h = live
    low = _dubai(h)
    assert low > 180000, low                              # the live run: 186 870 vs 180 000
    h.understand(u(intent="refine", set={"budget_rub.max": 180000}))
    r = h.turn("поищите, чтобы с прямыми рейсами уложиться в 180 тысяч")
    assert r.get("error") is None, r["message"]
    vs = variants_of(h)
    best = min(vs, key=lambda v: v.totals.rub)
    assert best.totals.rub <= 180000, [v.totals.rub for v in vs]
    assert best.facts.get("budget_fit") in ("dates", "hotel", "dates_and_hotel")
    assert best.facts.get("budget_rub") == 180000
    note = next(n.text for n in best.risks if n.code == "budget_fit")
    assert "так укладываемся в 180 000 ₽" in note, note
    stay = next(s for s in best.steps if s.type == "stay")
    assert stay.start[5:7] == "12"                        # still December
    for v in vs:   # hard prefs stay: direct flights only
        assert v.facts.get("out_stops") == 0 and v.facts.get("back_stops") == 0, (v.id, v.facts.get("out_stops"))
    assert "rebuild: nothing changed" not in " ".join(r["debug"]["notes"])
    no_blocks(h)
    # the update card sees what changed
    inp = [r_ for r_ in h.backend.requests if r_.card == "present.update"][-1]
    import json
    payload = json.loads(inp.user)
    assert any(c["field"].startswith("dates.") for c in payload["changes"]) or best.facts["budget_fit"] == "hotel"


def test_s05_budget_cheaper_hotels_keep_user_prefs(live):
    h = live
    h.understand(u(set={"origin.city": "Москва", "party.segment": "couple", "party.adults": 2,
                        "destination.name": "Лимасол", "dates.month": 5, "dates.nights": 7,
                        "prefs.beach_max_m": 300, "prefs.max_travel_hours": 5}))
    h.turn("Лимасол в мае, и чтобы пляж был ближе к отелю")
    assert min(v.totals.rub for v in variants_of(h)) > 200000
    h.understand(u(intent="refine", set={"budget_rub.max": 200000}))
    r = h.turn("Дороговато. У нас бюджет до 200 тысяч на двоих, а тут всё выше. Есть что-то подешевле?")
    assert r.get("error") is None, r["message"]
    vs = variants_of(h)
    assert min(v.totals.rub for v in vs) <= 200000, [v.totals.rub for v in vs]
    brief = h.session().brief
    assert brief.values()["prefs"]["beach_max_m"] == 300 and brief.source_of("prefs.beach_max_m") == "user"
    assert all(v.facts.get("budget_fit") for v in vs)
    no_blocks(h)


def test_explicit_other_dates_without_values_searches(live):
    h = live
    _dubai(h)
    before = [(v.id, v.totals.rub, [s.start[:10] for s in v.steps]) for v in variants_of(h)]
    h.understand(u(intent="refine", set={}))
    r = h.turn("а можно на другие числа в декабре?")
    assert r.get("error") is None, r["message"]
    after = [(v.id, v.totals.rub, [s.start[:10] for s in v.steps]) for v in variants_of(h)]
    notes = r["debug"]["notes"]
    assert any(n.startswith("wish: other dates") for n in notes), notes
    assert after != before or all(v.facts.get("budget_fit") == "other_dates_not_found" for v in variants_of(h))
    stay = next(s for s in variants_of(h)[0].steps if s.type == "stay")
    assert stay.start[5:7] == "12"


def test_budget_not_reachable_says_what_was_tried(live):
    h = live
    _dubai(h)
    h.understand(u(intent="refine", set={"budget_rub.max": 60000}))
    r = h.turn("а уложиться в 60 тысяч?")
    assert r.get("error") is None, r["message"]
    vs = variants_of(h)
    assert all(v.facts.get("budget_fit") == "not_found" for v in vs)
    note = next(n.text for n in vs[0].risks if n.code == "budget_fit")
    assert "Пробовал другие числа" in note and "60 000 ₽" in note
    h.understand(u(intent="refine", set={}))
    r = h.turn("пересоберите, чтобы было в 60 тысяч")
    h.understand(u(intent="refine", set={}))
    r = h.turn("пересоберите ещё раз, в 60 тысяч")
    assert "подборка та же" not in r["message"], r["message"]


@pytest.mark.parametrize("text", ["сдвиньте даты на другие числа в декабре", "а другие даты есть?",
                                  "можно перенести поездку?"])
def test_other_dates_words(text):
    assert wants_other_dates(text)


@pytest.mark.parametrize("text", ["есть что-то подешевле?", "отель попроще", "а что-нибудь дешевле есть"])
def test_cheaper_words(text):
    assert wants_cheaper(text)


@pytest.mark.parametrize("text", ["беру Дешевле", "чем Дешевле отличается от Быстрее?", "даты подходят"])
def test_not_wish_words(text):
    assert not wants_cheaper(text) and not wants_other_dates(text)


def test_info_with_other_dates_is_a_rebuild():
    intent, _, note = route_intent("info", "selection", "а на другие числа в декабре что-то есть?",
                                   {"set": {}, "unset": []})
    assert intent == "refine" and note


# ---------------------------------------------------------------- 1. a change of trip kind goes back to destinations

def _h10_bukhara(h):
    h.understand(u(set={"origin.city": "Екатеринбург", "party.segment": "solo", "party.adults": 1,
                        "budget_rub.max": 25000, "dates.month": 11, "dates.nights": 3}))
    r = h.turn("Студент, 25 тысяч на всё, куда можно смотаться на 3 дня из Екатеринбурга?")
    ids = [c["id"] for c in r["cards"]["destinations"]]
    assert "bukhara" in ids, ids
    r = h.act("choose_destination", destination_id="bukhara")
    assert r["stage"] == "selection" and r["cards"]["variants"], r["message"]
    return r


def _assert_train_destinations(h, r):
    assert r.get("error") is None, r["message"]
    assert r["stage"] == "inspiration" and not r["cards"]["variants"], r["message"]
    ids = [c["id"] for c in r["cards"]["destinations"]]
    assert ids and "bukhara" not in ids
    od = h.engine.opendata
    ekb = od.destination("yekaterinburg") or od.destination("ekaterinburg")
    for i in ids:
        rec = od.destination(i)
        assert rec["country_code"] == "RU" and train_reachable(ekb, rec), i
    assert h.session().brief.values()["destination"] is None
    assert any("destination bukhara dropped" in n for n in r["debug"]["notes"]), r["debug"]["notes"]
    return ids


def test_h10_domestic_by_train_drops_bukhara(live):
    h = live
    _h10_bukhara(h)
    h.understand(u(intent="refine", set={"prefs.domestic": True, "prefs.transport": "train"}))
    r = h.turn("40 тысяч это сильно больше моих 25, а по россии что-нибудь дешевле есть, например поездом?")
    ids = _assert_train_destinations(h, r)
    import json
    inp = json.loads([q for q in h.backend.requests if q.card == "present.destinations"][-1].user)
    prices = [i["price_from_rub"] for i in inp["items"]]
    flags = [i["facts"].get("within_budget") for i in inp["items"]]
    assert flags[0] is (prices[0] is not None and prices[0] <= 25000)
    assert all(i["facts"]["transport"] == "train" for i in inp["items"])
    assert len(ids) >= 1
    # the place that fits leads to a real train trip within the budget (never stay-only «поездом»)
    fit = next(i["id"] for i in inp["items"] if i["facts"].get("within_budget"))
    r = h.act("choose_destination", destination_id=fit)
    assert r.get("error") is None and r["cards"]["variants"], r["message"]
    for v in variants_of(h):
        kinds = [s.type for s in v.steps]
        assert kinds[0] == "train" and kinds[-1] == "train" and "stay" in kinds, (v.id, kinds)
        assert not v.facts.get("no_transport")
    assert min(v.totals.rub for v in variants_of(h)) <= 25000


def test_h10_train_chip_drops_bukhara(live):
    h = live
    _h10_bukhara(h)
    r = h.act("set_chip", field="prefs.transport", value="train")
    _assert_train_destinations(h, r)


def test_domestic_only_drops_a_foreign_destination(live):
    h = live
    _h10_bukhara(h)
    h.understand(u(intent="refine", set={"prefs.domestic": True}))
    r = h.turn("а по России что-нибудь?")
    assert r["stage"] == "inspiration", r["message"]
    ids = [c["id"] for c in r["cards"]["destinations"]]
    assert ids and all(h.engine.opendata.destination(i)["country_code"] == "RU" for i in ids)


def test_other_country_without_a_name(live):
    h = live
    _h10_bukhara(h)
    h.understand(u(intent="refine", set={}))
    r = h.turn("а в другую страну можно?")
    assert r["stage"] == "inspiration", r["message"]
    ids = [c["id"] for c in r["cards"]["destinations"]]
    assert ids and all(h.engine.opendata.destination(i)["country_code"] != "UZ" for i in ids)


def test_named_destination_with_train_is_kept(live):
    """A destination named in the same message is never dropped (the user chose it on purpose)."""
    h = live
    h.understand(u(set={"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
                        "dates.end": "2026-12-07", "party.adults": 1, "prefs.transport": "train"}))
    r = h.turn("Казань поездом 4–7 декабря")
    assert r["stage"] == "selection" and (h.session().brief.values()["destination"] or {}).get("id") == "kazan"
