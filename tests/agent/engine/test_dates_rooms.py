"""Second live level-3 run, search & ranking failures, end to end over the real mock + real data
(the `live` harness of test_scopes.py; the model is a scripted FakeBackend):

- s25: flexible_days made transport and hotel dates disagree -> no_data; flexible days now shift the
  whole trip (check-in = arrival day, check-out = check-in + nights) and never mix dates;
- s04 / s20: an outbound arriving the evening before check-in (a night without a bed) is never offered;
- s02: gender-specific dorm rooms are never offered (we never know the traveller's gender);
- s17: «кроватка» (constraint `crib`) keeps hotels with a guaranteed cot (free, then paid);
- s01: totals.out_hours / back_hours, max_travel_hours is a one-way limit.
"""
from datetime import date

import pytest

from agent.checks import blocking, check_variant
from agent.ranking import is_gendered_room

from .test_scopes import ask, kinds, live, no_blocks  # noqa: F401  (live is a fixture)


def _stay(v):
    return next(s for s in v.steps if s.type == "stay")


def _assert_dates_consistent(v, nights):
    """The outbound reaches the stay on the check-in day (an overnight leg that leaves the evening
    before is fine), the return leaves on the check-out day, the hotel has `nights` nights."""
    f = v.facts
    ci, co = date.fromisoformat(f["check_in_date"]), date.fromisoformat(f["check_out_date"])
    assert (co - ci).days == nights, (v.id, ci, co)
    transport = [s for s in v.steps if s.type in ("flight", "train")]
    if transport:
        assert date.fromisoformat(transport[0].end[:10]) >= ci, (v.id, transport[0].end, ci)
        assert date.fromisoformat(transport[-1].start[:10]) <= co, (v.id, transport[-1].start, co)
        arrival = [s for s in v.steps if s.type in ("flight", "train", "transfer")]
        before_stay = v.steps[[s.type for s in v.steps].index("stay") - 1]
        assert date.fromisoformat(before_stay.end[:10]) >= ci, (v.id, before_stay.end, ci, arrival)


# ---------------------------------------------------------------- s25: flexible days

S25 = {"destination.name": "Санкт-Петербург", "dates.start": "2026-12-01", "dates.nights": 3,
       "party.segment": "friends", "party.adults": 3}


def test_s25_flexible_days_still_builds_variants(live):
    h = live
    _, vs = ask(h, "Три подруги, хотим в Питер на 3 ночи в начале декабря, ±3 дня",
                **S25, **{"dates.flexible_days": 3})
    assert vs
    for v in vs:
        _assert_dates_consistent(v, 3)
    no_blocks(h, vs)


def test_s25_flexible_days_prefers_the_users_start(live):
    """With options on the user's own dates, the trip stays on them unless another day is clearly better."""
    h = live
    _, exact = ask(h, "Питер на 3 ночи с 1 декабря", **S25)
    h.new()
    _, flex = ask(h, "Питер на 3 ночи с 1 декабря, ±3 дня", **S25, **{"dates.flexible_days": 3})
    ins = {v.facts["check_in_date"] for v in flex}
    assert len(ins) == 1, "never mix dates across the shown variants"
    if ins == {"2026-12-01"}:
        assert min(v.totals.rub for v in flex) <= min(v.totals.rub for v in exact) * 1.001
    else:   # shifted: only because it is cheaper, and said so
        assert min(v.totals.rub for v in flex) < min(v.totals.rub for v in exact)
        assert all(v.facts.get("dates_shifted_days") for v in flex)
        assert all("dates_shifted" in [n.code for n in v.risks] for v in flex)


# ---------------------------------------------------------------- s04 / s20: no night without a bed

@pytest.mark.parametrize("dest,start,nights", [("Стамбул", "2026-12-01", 4), ("Дубай", "2026-12-15", 4)])
def test_no_night_without_a_bed_before_check_in(live, dest, start, nights):
    h = live
    _, vs = ask(h, f"{dest}, {nights} ночи с {start}, вдвоём, ±3 дня",
                **{"destination.name": dest, "dates.start": start, "dates.nights": nights,
                   "party.segment": "couple", "party.adults": 2, "dates.flexible_days": 3})
    for v in vs:
        _assert_dates_consistent(v, nights)
        codes = [i["code"] for i in check_variant(v, h.session().brief, h.engine.skill.rules)]
        assert "night_without_hotel" not in codes, (v.id, codes)
    no_blocks(h, vs)


# ---------------------------------------------------------------- s02: gendered dorms

def test_s02_no_gendered_dorm_for_a_solo_traveller(live):
    h = live
    _, vs = ask(h, "хочу в горы на недельку в декабре, один, покататься",
                **{"destination.name": "Приэльбрусье", "dates.start": "2026-12-11", "dates.end": "2026-12-18",
                   "party.segment": "solo", "party.adults": 1})
    for v in vs:
        room = v.facts.get("room_name") or ""
        assert not is_gendered_room({"name": room}), (v.id, room)
    no_blocks(h, vs)


def test_gendered_room_names_of_the_catalog():
    assert is_gendered_room({"name": "Женский общий номер"})
    assert is_gendered_room({"name": "2 номера: Мужской общий номер"})
    assert is_gendered_room({"name": "Female dorm"}) and is_gendered_room({"name": "Male dorm"})
    assert not is_gendered_room({"name": "Место в общем номере"})
    assert not is_gendered_room({"name": "Стандартный двухместный"})


# ---------------------------------------------------------------- s17: guaranteed cot

def test_s17_crib_keeps_hotels_with_a_guaranteed_cot(live):
    h = live
    _, vs = ask(h, "Анталья, 7–14 ноября, мы с женой и малыш полтора года, всё включено, кроватка в номере",
                **{"destination.name": "Анталья", "dates.start": "2026-11-07", "dates.end": "2026-11-14",
                   "party.segment": "family", "party.adults": 2, "party.children": 1,
                   "party.children_ages": [1], "prefs.meals": "all_inclusive", "constraints.add": ["crib"]})
    for v in vs:
        codes = [n.code for n in v.risks]
        assert v.facts.get("infant_cot") in ("free", "paid") or "relaxed_pref" in codes, (v.id, v.facts.get("infant_cot"))
    assert any(v.facts.get("infant_cot") in ("free", "paid") for v in vs), "the catalog has such hotels in Antalya"
    no_blocks(h, vs)


# ---------------------------------------------------------------- s01: one-way hours

def test_s01_one_way_hours_in_totals(live):
    h = live
    _, vs = ask(h, "Шарм-эш-Шейх, 7–14 ноября, с ребёнком 4 лет, перелёт не дольше 5 часов",
                **{"destination.name": "Шарм-эш-Шейх", "dates.start": "2026-11-07", "dates.end": "2026-11-14",
                   "party.segment": "family", "party.adults": 2, "party.children": 1, "party.children_ages": [4],
                   "prefs.max_travel_hours": 5})
    for v in vs:
        t = v.totals
        assert t.out_hours is not None and t.back_hours is not None
        assert abs(t.out_hours + t.back_hours - t.door_to_door_hours) <= 0.15, t
        codes = [n.code for n in v.risks]
        assert max(t.out_hours, t.back_hours) <= 5 or "relaxed_pref" in codes, (v.id, t, codes)
    r = h.session().variants
    assert r and all(v.totals.out_hours is not None for v in r)
