"""upsell_candidates / offers_for_variant: only sold kinds, only with a real offer, capped, with reasons."""
import json

from agent.ranking import build_variants
from agent.upsell import offers_for_variant, upsell_candidates

from .factory import ROOT, DISTRICTS, RULES, SEGMENTS, TODAY, brief, flight, hotel, sea_brief, sea_results, transfer

UPSELL = {"rules": [
    {"id": "night_arrival_kids_transfer", "kind": "transfer", "label": "Добавить трансфер",
     "when": {"arrival_after": "22:00", "children": True, "variant_has_transfer": False},
     "reason": "Прилёт в {arrival_time}, а ночью с ребёнком ловить такси — так себе: трансфер встретит у выхода, {price_rub} ₽."},
    {"id": "early_arrival_checkin", "kind": "early_checkin", "label": "Ранний заезд",
     "when": {"arrival_before_checkin_min": 180},
     "reason": "Вы приезжаете в {arrival_time}, а заселение с {checkin_time}. Ранний заезд — {price_rub} ₽."},
    {"id": "sea_kids_insurance", "kind": "insurance", "label": "Страховка", "when": {"children": True},
     "placeholder": True, "reason": "Подумайте о страховке."},
    {"id": "nonref_flex", "kind": "flexible_fare", "label": "Гибкий тариф", "when": {"non_refundable": True},
     "reason": "Билет невозвратный; гибкий тариф — +{price_rub} ₽, и планы можно поменять."},
    {"id": "meals", "kind": "meal_upgrade", "label": "С ужинами", "when": {"meals_below": "half_board", "nights_min": 5},
     "reason": "На {nights} ночей удобнее с ужинами: {meals} за +{price_rub} ₽."},
    {"id": "room", "kind": "room_upgrade", "label": "Номер получше", "when": {"segment_any": ["couple"]},
     "reason": "{room} — +{price_rub} ₽."},
    {"id": "weird", "kind": "transfer", "label": "x", "when": {"moon_phase": "full"}, "reason": "x"},
]}


def night_family():
    tz = "+03:00"
    res = {
        "flights_out": [flight("NF1", "VKO", "AER", f"2026-11-12T21:00:00{tz}", f"2026-11-12T23:30:00{tz}", 30000)],
        "flights_back": [flight("NB1", "AER", "VKO", f"2026-11-19T15:00:00{tz}", f"2026-11-19T17:30:00{tz}", 30000),
                         flight("NB1x", "AER", "VKO", f"2026-11-19T15:00:00{tz}", f"2026-11-19T17:30:00{tz}", 34000,
                                refundable=True, change_fee=0, flight_no="NB1")],
        "hotels": [hotel("NH1", "n1", "Отель Магнолия", 50000, "2026-11-12", "2026-11-19", cap_adults=2,
                         cap_children=1)],
        "transfers_out": {"n1": [transfer("NT1", "private", 35, 2200), transfer("NT0", "taxi", 30, 1500)]},
        "transfers_back": {},
    }
    for o in res["flights_back"]:
        o["flight_no"] = "NB1"
    b = brief({"origin.city": "Москва", "destination.name": "Сочи", "dates.start": "2026-11-12",
               "dates.end": "2026-11-19", "party.segment": "family", "party.adults": 2, "party.children_ages": [4],
               "scope": ["flights", "stay"]})
    return b, res


def test_night_arrival_with_kids_gets_transfer():
    b, res = night_family()
    v = build_variants(b, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    assert [t["offer_id"] for t in offers["transfer"]] == ["NT0", "NT1"]
    cands = upsell_candidates(b, v, offers, UPSELL, rules=RULES)
    kinds = [c.kind for c in cands]
    assert kinds[0] == "transfer" and len(cands) <= 2
    c = cands[0]
    assert c.offer_id == "NT0" and c.price_rub == 1500 and c.id == "u-cheapest-night_arrival_kids_transfer"
    assert c.variant_id == "cheapest"
    assert "23:30" in c.reason and "1 500 ₽" in c.reason


def test_never_more_than_max_upsell():
    b, res = night_family()
    v = build_variants(b, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    assert len(upsell_candidates(b, v, offers, UPSELL, rules={**RULES, "max_upsell": 1})) == 1
    assert len(upsell_candidates(b, v, offers, UPSELL, rules={**RULES, "max_upsell": 0})) == 0


def test_flexible_fare_from_same_flight():
    b, res = night_family()
    v = build_variants(b, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    assert offers["flexible_fare"] == [{"offer_id": "NB1x", "price_rub": 4000, "direction": "back"}]
    rules_only_flex = {"rules": [r for r in UPSELL["rules"] if r["id"] == "nonref_flex"]}
    (c,) = upsell_candidates(b, v, offers, rules_only_flex, rules=RULES)
    assert c.offer_id == "NB1x" and "+4 000 ₽" in c.reason


def test_no_offer_no_candidate():
    b, res = night_family()
    res["transfers_out"] = {}
    v = build_variants(b, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    assert offers["transfer"] == []
    assert "transfer" not in [c.kind for c in upsell_candidates(b, v, offers, UPSELL, rules=RULES)]


def test_placeholder_and_unknown_kinds_and_conditions_never_fire():
    b, res = night_family()
    v = build_variants(b, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = {"transfer": [{"offer_id": "X", "price_rub": 1}], "insurance": [{"offer_id": "I", "price_rub": 1}]}
    weird_only = {"rules": [r for r in UPSELL["rules"] if r["id"] in ("weird", "sea_kids_insurance")]}
    assert upsell_candidates(b, v, offers, weird_only, rules=RULES) == []


def test_conditions_must_all_hold():
    b, res = night_family()
    b2 = brief({"origin.city": "Москва", "dates.start": "2026-11-12", "dates.end": "2026-11-19", "party.adults": 2,
                "scope": ["flights", "stay"]})  # no children
    v = build_variants(b2, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    assert "night_arrival_kids_transfer" not in [c.rule_id for c in upsell_candidates(b2, v, offers, UPSELL, rules=RULES)]


def test_variant_with_transfer_gets_no_transfer():
    b = sea_brief(**{"party.segment": "family", "party.children_ages": [4]})
    res = sea_results()
    for h in res["hotels"]:
        h["room"]["capacity_children"] = 1
    v = build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)[0]
    assert any(s.type == "transfer" for s in v.steps)
    assert offers_for_variant(v, res)["transfer"] == []


def test_room_and_meal_upgrades_from_same_hotel():
    b = sea_brief()
    res = sea_results()
    res["hotels"] = [h for h in res["hotels"] if h["hotel_id"] == "h2"]
    cheap = build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)[0]
    assert cheap.hotel.offer_id == "H2"
    offers = offers_for_variant(cheap, res)
    assert offers["room_upgrade"] == [{"offer_id": "H2b", "price_rub": 12000, "room_name": "Номер с видом на море",
                                       "room_id": "sea"}]
    assert offers["meal_upgrade"] == [{"offer_id": "H2c", "price_rub": 9000, "meals": "half_board"}]
    rules_ = {"rules": [r for r in UPSELL["rules"] if r["kind"] in ("meal_upgrade", "room_upgrade")]}
    cands = upsell_candidates(b, cheap, offers, rules_, rules=RULES)
    assert [(c.kind, c.offer_id, c.price_rub) for c in cands] == [("meal_upgrade", "H2c", 9000),
                                                                  ("room_upgrade", "H2b", 12000)]
    assert cands[0].reason == "На 7 ночей удобнее с ужинами: полупансион за +9 000 ₽."
    assert cands[1].reason == "Номер с видом на море — +12 000 ₽."


def test_early_checkin_reason_uses_arrival_at_hotel():
    tz = "+03:00"
    res = sea_results()
    res["flights_out"] = [flight("E1", "SVO", "AYT", f"2026-11-12T02:00:00{tz}", f"2026-11-12T06:10:00{tz}", 15000,
                                 international=True)]
    b = sea_brief()
    vs = build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)
    cheap = vs[0]
    assert cheap.hotel.hotel_id == "h1" and cheap.facts["wait_before_checkin_min"] >= 180
    offers = offers_for_variant(cheap, res)
    assert offers["early_checkin"] == [{"offer_id": None, "price_rub": 1500}]
    early = {"rules": [r for r in UPSELL["rules"] if r["kind"] == "early_checkin"]}
    (c,) = upsell_candidates(b, cheap, offers, early, rules=RULES)
    assert cheap.facts["arrival_at_hotel_time"] in c.reason and "14:00" in c.reason and "1 500 ₽" in c.reason


def test_cap_falls_back_to_upsell_file_then_2():
    b, res = night_family()
    v = build_variants(b, res, {}, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    assert len(upsell_candidates(b, v, offers, {**UPSELL, "max_upsell": 1})) == 1
    assert len(upsell_candidates(b, v, offers, UPSELL)) <= 2


SKILL_UPSELL = json.loads((ROOT / "skills/trip-agent/upsell.json").read_text(encoding="utf-8"))


def test_skill_rules_fill_their_templates():
    """The real upsell.json: every candidate's reason is fully filled from the offer (no braces left)."""
    b = sea_brief(**{"party.segment": "family", "party.children_ages": [4], "mood.add": ["romantic"],
                     "prefs.district": "quiet", "prefs.free_cancellation": True})
    res = sea_results()
    res["hotels"] = [h for h in res["hotels"] if h["hotel_id"] == "h2"]
    for h in res["hotels"]:
        h["room"]["capacity_children"] = 1
        h["cancellation"]["penalty_pct"] = 0
    res["flights_out"].append(dict(res["flights_out"][0], offer_id="F1x", price_rub=26000,
                                   fare={"refundable": True, "change_fee_rub": 0}))
    v = build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)[0]
    offers = offers_for_variant(v, res)
    cands = upsell_candidates(b, v, offers, SKILL_UPSELL, rules={**RULES, "max_upsell": 10})
    assert cands, offers
    for c in cands:
        assert "{" not in c.reason and c.price_rub is not None and c.variant_id == v.id
    kinds = {c.kind for c in cands}
    assert "room_upgrade" in kinds and "meal_upgrade" in kinds
    room = next(c for c in cands if c.kind == "room_upgrade")
    assert room.rule_id == "romantic_room_upgrade"  # the high-floor rule needs a high-floor room offer
    assert "Номер с видом на море" in room.reason


def test_high_floor_rule_needs_a_high_floor_room():
    b = sea_brief(**{"prefs.district": "quiet"})
    res = sea_results()
    res["hotels"] = [h for h in res["hotels"] if h["hotel_id"] == "h2"]
    floor_rule = {"rules": [r for r in SKILL_UPSELL["rules"] if r["id"] == "quiet_high_floor"]}
    v = build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)[0]
    assert upsell_candidates(b, v, offers_for_variant(v, res), floor_rule, rules=RULES) == []
    res["hotels"][1]["room"].update(room_id="high", name="Номер на высоком этаже")
    (c,) = upsell_candidates(b, v, offers_for_variant(v, res), floor_rule, rules=RULES)
    assert c.offer_id == "H2b" and "12 000 ₽" in c.reason


def test_upgrades_from_engine_extra_search():
    """Room/meal offers found by search_hotels(hotel_id, all_rooms) live under results["hotel_upgrades"]."""
    b = sea_brief()
    res = sea_results()
    upgrades = [h for h in res["hotels"] if h["offer_id"] in ("H2b", "H2c")]
    res["hotels"] = [h for h in res["hotels"] if h["offer_id"] == "H2"]
    v = build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)[0]
    assert offers_for_variant(v, res)["room_upgrade"] == []
    res["hotel_upgrades"] = {"h2": upgrades}
    offers = offers_for_variant(v, res)
    assert [o["offer_id"] for o in offers["room_upgrade"]] == ["H2b"]
    assert [o["offer_id"] for o in offers["meal_upgrade"]] == ["H2c"]
