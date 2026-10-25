"""Second live level-3 run, domain level: no night without a bed, paid-but-unused first night, gendered
dorms, guaranteed infant cots, one-way travel hours in the totals."""
import copy

from agent.checks import blocking, check_stay, check_variant
from agent.ranking import build_variants, is_gendered_room

from .factory import DISTRICTS, RULES, SEGMENTS, TODAY, brief, flight, hotel, sea_brief, sea_results, train

TZ = "+03:00"


def build(b, results, r=RULES):
    return build_variants(b, results, DISTRICTS, r, TODAY, segments=SEGMENTS)


def codes(notes):
    return [n.code for n in notes]


def _cot(h, value):
    h = copy.deepcopy(h)
    h["kids_policy"]["infant_cot"] = value
    return h


# ---------------------------------------------------------------- nights without a bed

def test_flight_landing_the_evening_before_check_in_is_never_paired():
    res = sea_results()
    res["flights_out"].append(flight("EVE", "SVO", "AYT", f"2026-11-11T15:00:00{TZ}", f"2026-11-11T19:10:00{TZ}",
                                     5000, international=True))   # far cheaper, lands the day before check-in
    vs = build(sea_brief(), res)
    assert vs
    assert all("EVE" not in [s.offer_id for s in v.steps] for v in vs)


def test_return_the_morning_after_check_out_is_never_paired_but_a_red_eye_is():
    res = sea_results()
    res["flights_back"].append(flight("MORNING", "AYT", "SVO", f"2026-11-20T07:30:00{TZ}", f"2026-11-20T11:40:00{TZ}",
                                      1000, international=True))
    vs = build(sea_brief(), res)
    assert all("MORNING" not in [s.offer_id for s in v.steps] for v in vs)   # a night without a bed
    res = sea_results()
    res["flights_back"].append(flight("REDEYE", "AYT", "SVO", f"2026-11-20T01:30:00{TZ}", f"2026-11-20T05:40:00{TZ}",
                                      1000, international=True))
    vs = build(sea_brief(), res)
    # a night flight after the check-out day is a normal (cheap) return: the night is spent travelling
    assert any("REDEYE" in [s.offer_id for s in v.steps] for v in vs)


def test_overnight_train_arriving_on_the_check_in_morning_is_fine():
    b = brief({"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
               "dates.end": "2026-12-07", "party.adults": 1, "scope": ["trains", "stay"]})
    res = {"trains_out": [train("T1", "Москва Казанская", "Казань", f"2026-12-03T21:00:00{TZ}",
                                f"2026-12-04T08:30:00{TZ}", 4000)],
           "trains_back": [train("T2", "Казань", "Москва Казанская", f"2026-12-07T15:00:00{TZ}",
                                 f"2026-12-08T03:00:00{TZ}", 4000)],
           "hotels": [hotel("H", "h", "Отель", 20000, "2026-12-04", "2026-12-07", cap_adults=1)],
           "destination": {"id": "kazan", "name": "Казань"}}
    vs = build(b, res)
    assert vs and vs[0].steps[0].offer_id == "T1"
    assert blocking(check_variant(vs[0], b, RULES)) == []


def test_check_stay_blocks_a_night_without_a_bed():
    steps = [{"id": "s1", "type": "flight", "from": "SVO", "to": "IST", "start": "2026-11-30T14:00:00+03:00",
              "end": "2026-11-30T18:33:00+03:00"},
             {"id": "s2", "type": "stay", "from": "H", "to": "H", "start": "2026-12-01T14:00:00+03:00",
              "end": "2026-12-05T12:00:00+03:00"}]
    found = check_stay(steps, RULES)
    assert [i["code"] for i in blocking(found)] == ["night_without_hotel"]
    assert "ночь без отеля" in found[0]["message"]
    steps[0]["start"], steps[0]["end"] = "2026-12-01T06:00:00+03:00", "2026-12-01T10:00:00+03:00"
    assert blocking(check_stay(steps, RULES)) == []


def test_check_stay_warns_on_a_paid_but_unused_first_night():
    steps = [{"id": "s1", "type": "flight", "from": "SVO", "to": "SSH", "start": "2026-11-07T21:00:00+03:00",
              "end": "2026-11-08T02:10:00+03:00"},
             {"id": "s2", "type": "stay", "from": "H", "to": "H", "start": "2026-11-08T02:10:00+03:00",
              "end": "2026-11-14T12:00:00+03:00"}]
    found = check_stay(steps, RULES)
    assert "unused_first_night" in [i["code"] for i in found] and blocking(found) == []


def test_unused_first_night_loses_to_an_equal_variant():
    res = sea_results()
    # same price as the cheapest outbound (F3, 18000) but lands after midnight: first night unused
    res["flights_out"].append(flight("NIGHT", "VKO", "AYT", f"2026-11-12T21:00:00{TZ}", f"2026-11-13T01:10:00{TZ}",
                                     18000, international=True))
    vs = build(sea_brief(), res)
    cheapest = next(v for v in vs if v.id == "cheapest")
    assert cheapest.steps[0].offer_id != "NIGHT"
    res["flights_out"] = [o for o in res["flights_out"] if o["offer_id"] != "F3"]
    res["flights_out"].append(flight("NIGHT2", "VKO", "AYT", f"2026-11-12T21:00:00{TZ}", f"2026-11-13T01:10:00{TZ}",
                                     9000, international=True))
    vs = build(sea_brief(), res)
    cheapest = next(v for v in vs if v.id == "cheapest")
    if cheapest.steps[0].offer_id == "NIGHT2":   # still allowed when really cheaper, but said so
        assert "unused_first_night" in codes(cheapest.risks)


# ---------------------------------------------------------------- dates shift (flexible_days)

def test_date_shift_becomes_a_fact_and_a_note():
    res = sea_results()
    res["date_shift"] = {"days": 2, "flex_days": 3, "requested_check_in": "2026-11-10", "reason": "cheaper"}
    vs = build(sea_brief(), res)
    for v in vs:
        assert v.facts["dates_shifted_days"] == 2
        assert codes(v.risks)[0] == "dates_shifted"
        assert "12.11" in v.risks[0].text and "10.11" in v.risks[0].text


# ---------------------------------------------------------------- gendered dorms

def test_gendered_dorm_never_offered():
    b = brief({"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-12",
               "dates.end": "2026-11-19", "party.segment": "solo", "party.adults": 1})
    res = sea_results()
    res["hotels"].append(hotel("HF", "hf", "Хостел", 5000, "2026-11-12", "2026-11-19", cap_adults=1, kind="hostel",
                               room_name="Женский общий номер"))
    res["hotels"].append(hotel("HM", "hm", "Хостел-2", 6000, "2026-11-12", "2026-11-19", cap_adults=1, kind="hostel",
                               room_name="Место в общем номере"))
    vs = build(b, res)
    names = [v.facts.get("room_name") for v in vs]
    assert "Женский общий номер" not in names
    assert "Место в общем номере" in names   # a mixed dorm is fine for a solo traveller
    assert is_gendered_room({"name": "Мужской общий номер"}) and not is_gendered_room({"name": "Семейный номер"})


# ---------------------------------------------------------------- infant cot

def _family(**extra):
    user = {"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-12",
            "dates.end": "2026-11-19", "party.segment": "family", "party.adults": 2, "party.children": 1,
            "party.children_ages": [1], "constraints.add": ["crib"]}
    user.update(extra)
    return brief(user)


def _family_results(cots):
    res = sea_results()
    res["hotels"] = [_cot(hotel(h["offer_id"], h["hotel_id"], h["name"], h["price_total_rub"], "2026-11-12", "2026-11-19",
                                stars=h["stars"], rating=h["rating"], meals=h["meals"], cap_adults=2, cap_children=1,
                                beach=h["distance_beach_m"]), cots.get(h["hotel_id"], "on_request"))
                     for h in res["hotels"]]
    return res


def test_crib_keeps_only_guaranteed_cots():
    vs = build(_family(), _family_results({"h2": "paid", "h3": "free"}))
    assert vs and all(v.facts["infant_cot"] in ("free", "paid") for v in vs)
    assert all("relaxed_pref" not in codes(v.risks) for v in vs)


def test_crib_prefers_free_over_paid_for_comfort():
    vs = build(_family(), _family_results({"h1": "free", "h2": "paid", "h3": "paid"}))
    comfort = [v for v in vs if v.id == "comfort"]
    by_cot = {v.facts["infant_cot"] for v in vs}
    assert "free" in by_cot
    assert not comfort or comfort[0].fit is not None


def test_crib_relaxed_with_a_note_when_no_hotel_guarantees_one():
    vs = build(_family(), _family_results({}))
    assert vs
    for v in vs:
        notes = [n for n in v.risks if n.code == "relaxed_pref"]
        assert notes and "кроватка только по запросу" in notes[0].text, codes(v.risks)


def test_crib_relaxation_drops_the_fewest_prefs_cot_last():
    """No hotel has both all-inclusive and a guaranteed cot: one pref is dropped, not both — the meals
    (RELAX_ORDER drops a toddler's cot last), and the note says which."""
    b = _family(**{"prefs.meals": "all_inclusive"})
    vs = build(b, _family_results({"h1": "free"}))   # h1 has a cot but no meals; h3 is all-inclusive
    assert vs and all(v.facts["infant_cot"] == "free" for v in vs)
    for v in vs:
        fields = [n.data.get("field") for n in v.risks if n.code == "relaxed_pref"]
        assert fields == ["prefs.meals"], fields


# ---------------------------------------------------------------- one-way hours

def test_totals_carry_one_way_hours():
    for v in build(sea_brief(), sea_results()):
        t = v.totals
        assert t.out_hours is not None and t.back_hours is not None
        assert abs(t.out_hours + t.back_hours - t.door_to_door_hours) <= 0.1
        assert v.facts["out_door_hours"] == t.out_hours and v.facts["back_door_hours"] == t.back_hours


def test_stay_only_has_no_one_way_hours():
    b = brief({"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-12",
               "dates.end": "2026-11-19", "party.adults": 2, "scope": ["stay"]})
    for v in build(b, sea_results()):
        assert v.totals.out_hours is None and v.totals.back_hours is None


def test_max_travel_hours_is_one_way():
    """5 h one way fits a ~5 h + 5 h round trip: nothing is relaxed although door_to_door_hours > 5."""
    vs = build(sea_brief(**{"prefs.max_travel_hours": 6}), sea_results())
    assert vs
    for v in vs:
        assert max(v.totals.out_hours, v.totals.back_hours) <= 6
        assert v.totals.door_to_door_hours > 6
        assert "relaxed_pref" not in codes(v.risks)


def test_max_travel_hours_relaxed_keeps_the_nearest_journeys():
    vs = build(sea_brief(**{"prefs.max_travel_hours": 2}), sea_results())
    assert vs
    best = min(max(v.totals.out_hours, v.totals.back_hours) for v in vs)
    for v in vs:
        assert max(v.totals.out_hours, v.totals.back_hours) <= best + 1.0 + 0.05
        assert any(n.code == "relaxed_pref" and n.data.get("field") == "prefs.max_travel_hours" for n in v.risks)
