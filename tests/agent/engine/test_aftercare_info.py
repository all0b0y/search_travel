"""Live L3 failures reproduced offline: date refines, post-purchase date changes, seasons, wishes, info facts."""
import json
from datetime import date

import pytest

from agent.engine import season_month

from .fakes import FakeOstrovok, u

# ---------------------------------------------------------------- helpers


def _to_selection(h, **dates):
    sets = {"destination.name": "Сочи", "party.adults": 2, **dates}
    h.understand(u(set=sets))
    r = h.turn("Сочи, двое взрослых")
    assert r["cards"]["variants"] and not r.get("error"), r["message"]
    return r


def _stay(r):
    v = r["cards"]["variants"][0]
    stay = next(s for s in v["steps"] if s["type"] == "stay")
    return stay["start"][:10], stay["end"][:10]


def _buy(h, **dates):
    _to_selection(h, **(dates or {"dates.start": "2026-11-12", "dates.end": "2026-11-19"}))
    r = h.act("choose_variant", variant_id="fastest")
    r = h.act("checkout", travellers=[{"name": "Анна", "birth_date": "1990-01-01"},
                                       {"name": "Иван", "birth_date": "1989-05-05"}], contact={"email": "a@b.c"})
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r["order"]["status"] == "paid"
    return r


def _last_input(h, card):
    req = next(r for r in reversed(h.backend.requests) if r.card == card)
    return json.loads(req.user)


# ---------------------------------------------------------------- 1. date refine never ends in bad_request (s04)

def test_refine_new_start_drops_stale_inferred_end(h):
    _to_selection(h, **{"dates.month": 11})               # dates inferred from the quote: 12-19 November
    assert h.session().brief.source_of("dates.end") == "inferred"
    h.understand(u(intent="refine", set={"dates.start": "2026-12-01", "dates.month": 12, "dates.nights": 4,
                                         "dates.flexible_days": 3}))
    r = h.turn("а можно в начале декабря, на 4–5 ночей?")
    assert not r.get("error"), r["message"]
    assert _stay(r) == ("2026-12-01", "2026-12-05")
    d = h.session().brief.values()["dates"]
    assert (d["start"], d["end"], d["nights"]) == ("2026-12-01", "2026-12-05", 4)


def test_refine_month_and_nights_only_moves_inferred_dates(h):
    _to_selection(h, **{"dates.month": 11})
    h.understand(u(intent="refine", set={"dates.month": 12, "dates.nights": 4, "dates.flexible_days": 3,
                                         "party.adults": 2}))
    r = h.turn("покажите на начало декабря, 4–5 ночей, нас двое")
    assert not r.get("error"), r["message"]
    start, end = _stay(r)
    assert start.startswith("2026-12") and (date.fromisoformat(end) - date.fromisoformat(start)).days == 4


def test_refine_with_end_before_start_is_repaired(h):
    _to_selection(h, **{"dates.month": 11})
    h.understand(u(intent="refine", set={"dates.start": "2026-12-01", "dates.end": "2026-11-19", "dates.nights": 5}))
    r = h.turn("с 1 декабря на 5 ночей")
    assert not r.get("error"), r["message"]
    assert _stay(r) == ("2026-12-01", "2026-12-06")


def test_refine_nights_after_exact_dates_keeps_start(h):
    _to_selection(h, **{"dates.start": "2026-11-12", "dates.end": "2026-11-19"})
    h.understand(u(intent="refine", set={"dates.nights": 4}))
    r = h.turn("давайте на 4 ночи")
    assert not r.get("error"), r["message"]
    assert _stay(r) == ("2026-11-12", "2026-11-16")


# ---------------------------------------------------------------- 3. seasons (s07)

@pytest.mark.parametrize("season,today,month", [
    ("летом", "2026-10-25", 7), ("весной", "2026-10-25", 4), ("зимой", "2026-10-25", 12),
    ("зимой", "2026-12-05", 1), ("зимой", "2027-01-10", 1), ("осенью", "2026-10-25", 11),
    ("осенью", "2026-10-05", 10), ("осенью", "2026-11-25", 9), ("осенью", "2027-03-01", 9)])
def test_season_month_rule(season, today, month):
    assert season_month(f"куда-нибудь {season}", date.fromisoformat(today)) == month


def test_season_month_ignores_explicit_month_names():
    assert season_month("летом, в августе", date(2026, 10, 25)) is None
    assert season_month("хочу в Стамбул на майские", date(2026, 10, 25)) is None
    assert season_month("на 12–19 ноября", date(2026, 10, 25)) is None


def test_season_reaches_brief_and_shows_a_chip(make):
    h = make(flags="stage1")                                # chips off in stage 1: the season chip still shows
    h.understand(u(set={"destination.name": "Сочи", "dates.nights": 7, "party.segment": "couple"}))   # month dropped
    r = h.turn("Сочи, неделю летом, вдвоём")
    assert not r.get("error"), r["message"]
    d = h.session().brief.values()["dates"]
    assert d["month"] == 7 and d["start"].startswith("2027-07")
    chip = next(c for c in r["chips"] if c["field"] == "dates.month")
    assert chip["value"] == 7 and "июл" in chip["label"]
    assert {o["value"] for o in chip["options"]} == {6, 7, 8}
    # the chip changes the month
    r = h.act("set_chip", field="dates.month", value=8)
    assert not r.get("error"), r["message"]
    assert h.session().brief.values()["dates"]["start"].startswith("2027-08")


def test_season_model_month_inside_season_is_kept_by_code_rule(h):
    h.understand(u(set={"destination.name": "Сочи", "dates.month": 9, "party.adults": 2}))
    h.turn("Сочи осенью, вдвоём")
    assert h.session().brief.values()["dates"]["month"] == 11      # today 2026-10-24 -> the next autumn month


# ---------------------------------------------------------------- 4. wishes are not places (s02)

def test_wish_as_destination_becomes_trait(h):
    od = h.engine.opendata
    real = od.geocode
    # the live geocoder matched «горы» to the town Гори: a wish must never reach it
    od.geocode = lambda name: ({"id": "sochi", "name": "Гори", "country": "Грузия", "in_catalog": True}
                               if str(name).casefold() == "горы" else real(name))
    h.understand(u(set={"destination.name": "горы", "dates.month": 12, "dates.nights": 7, "party.segment": "solo",
                        "traits.add": ["ski", "snow"]}))
    r = h.turn("в горы на недельку в декабре, один")
    s = h.session()
    assert s.brief.values()["destination"] is None
    assert {"mountains", "ski", "snow"} <= set(s.brief.values()["traits"])
    assert r["cards"]["destinations"] and not r["cards"]["variants"]


# ---------------------------------------------------------------- 6. new prefs

def test_no_night_flights_pref_reaches_brief_as_a_relaxable_pref(h):
    h.understand(u(set={"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
                        "prefs.no_night_flights": True, "prefs.arrive_before": "14:00"}))
    h.turn("Сочи 12–19 ноября, прилететь до обеда, без ночных рейсов")
    v = h.session().brief.values()
    assert v["prefs"]["no_night_flights"] is True and v["prefs"]["arrive_before"] == "14:00"
    # ranking reads the pref itself (relaxed with a risk when nothing passes); the never-relaxed constraint token
    # would turn «без ночных» into no_data when only night flights exist
    assert "no_night_flights" not in (v["constraints"] or [])


# ---------------------------------------------------------------- 2. post purchase (s16)

@pytest.mark.parametrize("params", [
    {"new_dates": {"check_in": "2026-11-21", "check_out": "2026-11-24"}},
    {"new_dates": {"start": "2026-11-21", "end": "2026-11-24"}},
    {"start": "2026-11-21", "end": "2026-11-24"},
    {"check_in": "2026-11-21", "check_out": "2026-11-24"}])
def test_change_dates_reads_new_dates(h, params):
    _buy(h)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": params}))
    r = h.turn("заезд 21 ноября, выезд 24 ноября. Сколько доплата?")
    calls = [p for t, p in h.ostrovok.calls if t == "order_change_dates"]
    assert calls and calls[-1]["new_dates"] == {"check_in": "2026-11-21", "check_out": "2026-11-24"}
    assert "На какие даты" not in r["message"]
    result = _last_input(h, "present.aftercare")["result"]
    assert result["new_dates"] == {"check_in": "2026-11-21", "check_out": "2026-11-24"}


def test_change_dates_falls_back_to_patch_dates(h):
    _buy(h)
    h.understand(u(intent="post_purchase", set={"dates.start": "2026-11-21", "dates.end": "2026-11-24"},
                   pp={"kind": "change_dates", "params": {}}))
    r = h.turn("перенесите на 21–24 ноября")
    assert h.ostrovok.count("order_change_dates") == 1 and "На какие даты" not in r["message"]


def test_change_dates_confirm_reuses_quoted_dates(h):
    _buy(h)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates",
                                               "params": {"new_dates": {"check_in": "2026-11-21",
                                                                        "check_out": "2026-11-24"}}}))
    h.turn("а на 21–24 ноября?")
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"confirm": True}}))
    r = h.turn("переносим")
    calls = [p for t, p in h.ostrovok.calls if t == "order_change_dates"]
    assert len(calls) == 2 and calls[-1]["confirm"] is True
    assert calls[-1]["new_dates"] == {"check_in": "2026-11-21", "check_out": "2026-11-24"}
    assert "На какие даты" not in r["message"]


class FixedFareOstrovok(FakeOstrovok):
    """Flights booked on a fare without exchange: the mock refuses to move the trip."""

    def checkout(self, p):
        out = super().checkout(p)
        for item in self.orders[out["order_id"]]["items"]:
            if item["kind"] == "flight":
                item["details"]["offer"] = {"fare": {"refundable": False, "change_fee_rub": None}}
        return out

    def order_change_dates(self, p):
        o = self.orders[p["order_id"]]
        flight = next(i for i in o["items"] if i["kind"] == "flight")
        return {"order_id": o["order_id"], "available": False, "issues": [
            {"code": "flight_change_not_allowed", "message": f"«{flight['title']}»: тариф без обмена",
             "severity": "blocking", "item_ids": [flight["item_id"]]}]}


def test_unmovable_order_starts_new_search_on_new_dates(make):
    h = make(ostrovok=FixedFareOstrovok())
    _buy(h)
    order_id = h.session().order_id
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"start": "2026-11-21",
                                                                                  "end": "2026-11-24"}}))
    r = h.turn("ну давай подбери на 21–24 ноября, сколько выйдет?")
    assert not r.get("error"), r["message"]
    assert r["stage"] == "selection" and r["cards"]["variants"]
    assert _stay(r) == ("2026-11-21", "2026-11-24")
    assert "остаётся" in r["message"]                      # the paid order is kept, said plainly
    assert h.session().order_id == order_id
    assert h.backend.requests[-1].card == "present.variants"
    # a new selection after an order can go to a new cart
    r = h.act("choose_variant", variant_id="cheapest")
    assert r["stage"] == "cart" and r["cart"]["items"]


def test_unmovable_order_shift_also_offers_new_trip(make):
    h = make(ostrovok=FixedFareOstrovok())
    _buy(h)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {"shift_days": 1}}))
    r = h.turn("можно всё сдвинуть на день позже?")
    assert not r.get("error") and r["cards"]["variants"]
    assert _stay(r) == ("2026-11-13", "2026-11-20")


def test_change_dates_without_any_dates_asks_once(h):
    _buy(h)
    h.understand(u(intent="post_purchase", pp={"kind": "change_dates", "params": {}}))
    r = h.turn("хочу перенести даты")
    assert "дат" in r["message"] and h.ostrovok.count("order_change_dates") == 0


# ---------------------------------------------------------------- 5. info facts carry transport and cancellation (s07, s24)

def test_info_facts_have_flights_and_cancellation(h):
    _to_selection(h, **{"dates.start": "2026-11-12", "dates.end": "2026-11-19"})
    h.understand(u(intent="info", info="Билеты невозвратные? Рейсы прямые?"))
    h.turn("а билеты невозвратные? рейсы прямые?")
    facts = _last_input(h, "present.info")["facts"]
    variants = [f for f in facts if f["kind"] == "variant"]
    assert {f["id"] for f in variants} == {"cheapest", "fastest", "comfort"}
    leg = variants[0]["legs"][0]
    assert leg["type"] == "flight" and leg["flight_no"].startswith("PA") and leg["carrier"] == "Пример Авиа"
    assert leg["dep_time"] == "06:40" and leg["dep_date"] == "2026-11-12" and leg["direct"] is True
    assert leg["refundable"] is False and leg["exchange_fee_rub"] == 3000 and leg["baggage_checked_kg"] == 20
    stay = variants[0]["stay"]
    assert stay["name"] and stay["cancellation"]["penalty_pct"] in (0, 30, 100)


def test_info_facts_after_choice_only_chosen_variant(h):
    _to_selection(h, **{"dates.start": "2026-11-12", "dates.end": "2026-11-19"})
    h.act("choose_variant", variant_id="comfort")
    h.understand(u(intent="info", info="До какого числа можно бесплатно отменить?"))
    h.turn("до какого числа можно бесплатно отменить?")
    facts = _last_input(h, "present.info")["facts"]
    assert [f["id"] for f in facts if f["kind"] == "variant"] == ["comfort"]
    stay = next(f for f in facts if f["kind"] == "variant")["stay"]
    assert stay["cancellation"]["free_until"] == "2026-11-12T12:00:00+03:00"     # Гранд Отель: free cancellation
