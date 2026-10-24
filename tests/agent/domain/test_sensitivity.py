"""sensitivity(): ask only when an assumed field's alternatives change the result."""
from agent.ranking import build_variants, sensitivity
from agent.state import Variant

from .factory import DISTRICTS, QUESTIONS, RULES, SEGMENTS, TODAY, brief, sea_results

SKILL = {"questions": QUESTIONS, "rules": RULES}


def v(vid, rub, score, hotel="h1"):
    return Variant.model_validate({
        "id": vid, "totals": {"rub": rub, "door_to_door_hours": 10},
        "steps": [{"id": "s1", "type": "stay", "from": "x", "to": "x", "start": "2026-11-12T14:00:00+03:00",
                   "end": "2026-11-19T12:00:00+03:00", "price": {"amount": rub, "currency": "RUB", "checked_at": "x"},
                   "link": "x", "offer_id": f"o-{hotel}"}],
        "fit": {"score": score}})


BASE = [v("cheapest", 80000, 0.6, "h1"), v("comfort", 120000, 0.8, "h2")]


def assumed_brief():
    return brief({"destination.name": "Анталья", "dates.start": "2026-11-12", "dates.end": "2026-11-19"},
                 assumed={"party.segment": ("couple", "pattern"), "origin.city": ("Москва", "profile"),
                          "dates.nights": (7, "default")})


def test_no_question_when_nothing_changes():
    assert sensitivity(assumed_brief(), BASE, lambda b: BASE, SKILL) == []


def test_top_change_asks_with_recommended_choice():
    def rerun(b):
        seg = b.party.value.segment
        return [v("cheapest", 80000, 0.9, "h3"), BASE[1]] if seg == "family" else BASE
    qs = sensitivity(assumed_brief(), BASE, rerun, SKILL)
    assert [q.id for q in qs] == ["q_party"]
    q = qs[0]
    assert q.field == "party.segment" and q.text == QUESTIONS["party.segment"]["text"]
    rec = [c.value for c in q.choices if c.recommended]
    assert rec == ["couple"]  # assumed from a pattern, and questions.json recommends from pattern
    assert "family" in q.reason


def test_price_move_over_threshold_asks():
    def rerun(b):
        nights = b.dates.value.nights
        return [BASE[0], v("comfort", int(120000 * nights / 7), 0.8, "h2")]
    qs = sensitivity(assumed_brief(), BASE, rerun, SKILL)
    assert [q.id for q in qs] == ["q_nights"]
    assert [c.value for c in qs[0].choices if c.recommended] == [7]  # default


def test_small_price_move_is_ignored():
    def rerun(b):
        return [BASE[0], v("comfort", 125000, 0.8, "h2")]  # +4 %
    assert sensitivity(assumed_brief(), BASE, rerun, SKILL) == []


def test_user_fields_are_never_asked():
    b = brief({"destination.name": "Анталья", "party.segment": "couple", "origin.city": "Москва",
               "dates.nights": 7, "budget_rub.max": 150000})
    calls = []

    def rerun(x):
        calls.append(x)
        return [v("cheapest", 1, 1.0, "zz")]
    assert sensitivity(b, BASE, rerun, SKILL) == []
    assert calls == []


def test_cap_and_order_by_impact():
    def rerun(b):
        out = list(BASE)
        if b.source_of("party.segment") == "user" and b.party.value.segment == "solo":
            out = [v("cheapest", 80000, 0.95, "h9"), BASE[1]]          # top changes: impact > 1
        if b.source_of("origin.city") == "user" and b.origin.value.city != "Москва":
            out = [BASE[0], v("comfort", 180000, 0.8, "h2")]           # +50 %
        if b.source_of("dates.nights") == "user" and b.dates.value.nights != 7:
            out = [BASE[0], v("comfort", 140000, 0.8, "h2")]           # +17 %
        return out
    qs = sensitivity(assumed_brief(), BASE, rerun, SKILL)
    assert [q.id for q in qs] == ["q_party", "q_origin"]
    one = sensitivity(assumed_brief(), BASE, rerun, {"questions": QUESTIONS, "rules": {**RULES, "max_questions": 1}})
    assert [q.id for q in one] == ["q_party"]


def test_children_ages_only_with_children():
    b = assumed_brief()
    called = []
    sensitivity(b, BASE, lambda x: called.append(x.party.value.children if x.party.value else 0) or BASE, SKILL)
    assert all(c == 0 for c in called)


def test_rerun_errors_are_skipped():
    def rerun(b):
        raise RuntimeError("search failed")
    assert sensitivity(assumed_brief(), BASE, rerun, SKILL) == []


def test_destination_items_supported():
    items = [{"id": "antalya", "price_from_rub": 90000}, {"id": "dubai", "price_from_rub": 120000}]

    def rerun(b):
        return list(reversed(items)) if b.party.value.segment == "friends" else items
    qs = sensitivity(assumed_brief(), items, rerun, SKILL)
    assert [q.id for q in qs] == ["q_party"]


def test_with_real_builder():
    """Origin does not change the fake search; nights do (the hotel price scales with nights)."""
    base_results = sea_results()

    def search(b):
        res = sea_results()
        nights = b.dates.value.nights or 7
        for h in res["hotels"]:
            h["price_total_rub"] = h["price_total_rub"] * nights // 7
        return build_variants(b, res, DISTRICTS, RULES, TODAY, segments=SEGMENTS)

    b = brief({"origin.city": "Москва", "destination.name": "Анталья", "dates.start": "2026-11-12",
               "dates.end": "2026-11-19", "party.segment": "couple", "party.adults": 2,
               "budget_rub.max": 300000},
              assumed={"dates.nights": (7, "default")})
    b = b.model_copy(update={"origin": b.origin.model_copy(update={"source": "profile"})})
    vs = build_variants(b, base_results, DISTRICTS, RULES, TODAY, segments=SEGMENTS)
    qs = sensitivity(b, vs, search, SKILL)
    assert [q.id for q in qs] == ["q_nights"]
