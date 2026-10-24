"""check_card_output: numbers ⊂ input (Russian formats), ids ⊂ input, questions, tone, length."""
import json

import pytest

from agent.guards import allowed_from, bad_numbers, check_card_output, tokens
from agent.ranking import build_variants

from .factory import ROOT, DISTRICTS, RULES, SEGMENTS, TODAY, sea_brief, sea_results

INPUT = {
    "today": "2026-10-24",
    "stage": "selection",
    "brief": {"dates": {"start": "2026-11-12", "end": "2026-11-19", "nights": 7}, "party": {"adults": 2}},
    "items": [
        {"id": "cheapest", "label": "1", "title": "Дешевле", "totals": {"rub": 84300, "door_to_door_hours": 6.5},
         "steps": [{"type": "flight", "from": "SVO", "to": "AER", "start": "2026-11-12T06:40+03:00",
                    "end": "2026-11-12T09:05+03:00", "carrier": "Пример Авиа", "price_rub": 11200}],
         "facts": {"distance_beach_m": 900, "transfer_out_min": 50, "hotel_rating": 8.1, "out_number": "SU1234"},
         "sacrifices": [{"code": "slower", "text": "+1,5 ч в пути", "data": {"delta_hours": 1.5}}]},
        {"id": "comfort", "label": "2", "title": "Комфортнее", "totals": {"rub": 156000, "door_to_door_hours": 5},
         "facts": {"delta_rub_vs_cheapest": 71700, "hotel_rating": 9.3}},
    ],
    "questions": [{"id": "q_party", "text": "Кто едет?"}],
    "upsell_candidates": [{"candidate_id": "u-cheapest-night", "kind": "transfer", "price_rub": 2500,
                           "reason": "Прилёт ночью"}],
}


def out(message="Вот варианты.", items=None, rec="cheapest", upsell=None):
    return {"message": message,
            "items": items if items is not None else [{"id": "cheapest", "why": "Дёшево", "sacrifices": [], "risks": [],
                                                       "not_for": []}],
            "recommended_id": rec, "upsell": upsell or []}


def issue_codes(o, rules=RULES, card_input=INPUT):
    return [i["code"] for i in check_card_output("present.variants", card_input, o, rules)]


ALLOWED = allowed_from(INPUT)


@pytest.mark.parametrize("text", [
    "Всего 84 300 ₽", "Всего 84 300 ₽", "Всего 84 300 ₽", "Всего 84300 ₽", "Всего 84300₽",
    "около 84 тыс. ₽", "около 84 тыс ₽", "примерно 84 тысячи", "84к за всё", "84k", "84,3 тыс. ₽",
    "0,16 млн за комфорт", "156 000 ₽", "156 тыс.", "дороже на 71 700 ₽",
    "6,5 ч в пути", "6.5 часа", "6 ч 30 мин", "+1,5 ч", "вылет в 06:40", "вылет в 6:40", "прилёт в 09:05",
    "12–19 ноября", "с 12 по 19.11", "12.11 – 19.11.2026", "7 ночей", "до моря 900 м", "до моря 0,9 км",
    "рейтинг 8,1", "рейтинг 9.3", "трансфер 50 мин", "трансфер 2 500 ₽", "рейс SU1234", "рейс SU 1234",
    "на 2 взрослых, 3 варианта", "в 2026 году", "−71 700 ₽", "11 200 ₽ за билет",
])
def test_allowed_formats(text):
    assert bad_numbers(text, ALLOWED) == [], text


@pytest.mark.parametrize("text,bad", [
    ("Всего 85 000 ₽", "85 000"),
    ("около 90 тыс. ₽", "90 тыс."),
    ("84,9 тыс. ₽", "84,9 тыс."),
    ("вылет в 07:15", "07:15"),
    ("с 13 по 19 ноября", "13"),
    ("до моря 350 м", "350"),
    ("рейтинг 8,7", "8,7"),
    ("скидка 15%", "15"),
    ("в 2027 году", "2027"),
    ("по 42 150 ₽ на человека", "42 150"),
    ("2 млн за всё", "2 млн"),
    ("75к", "75к"),
])
def test_invented_numbers(text, bad):
    assert bad in bad_numbers(text, ALLOWED), (text, bad_numbers(text, ALLOWED))


def test_tokens_kinds():
    kinds = [k for k, _, _ in tokens("84 000 ₽, 84 тыс., 06:40, 12.11, 6,5 ч")]
    assert sorted(kinds) == sorted(["scaled", "time", "dm", "grouped", "num"])


def test_preposition_k_is_not_thousands():
    assert bad_numbers("иди к 5 к морю", ALLOWED) == []


def test_clean_output_passes():
    o = out("Советую первый: 84 300 ₽ и 6,5 ч в пути, вылет в 06:40. Кто едет?",
            items=[{"id": "cheapest", "why": "Дешевле всех — 84 300 ₽", "sacrifices": ["+1,5 ч в пути"],
                    "risks": ["Ранний вылет в 06:40"], "not_for": ["До моря 900 м"]},
                   {"id": "comfort", "why": "Отель с рейтингом 9,3", "sacrifices": ["+71 700 ₽"], "risks": [],
                    "not_for": []}],
            upsell=[{"candidate_id": "u-cheapest-night", "line": "Трансфер за 2 500 ₽ встретит у выхода"}])
    assert check_card_output("present.variants", INPUT, o, RULES) == []


def test_invented_price_in_item_blocks():
    o = out(items=[{"id": "cheapest", "why": "Всего 79 000 ₽", "sacrifices": [], "risks": [], "not_for": []}])
    issues = check_card_output("present.variants", INPUT, o, RULES)
    assert [(i["code"], i["severity"], i["data"]["where"]) for i in issues] == [("invented_number", "block", "cheapest.why")]


def test_invented_time_in_risks_blocks():
    o = out(items=[{"id": "cheapest", "why": "x", "sacrifices": [], "risks": ["Пересадка 40 мин"], "not_for": []}])
    assert issue_codes(o) == ["invented_number"]


def test_foreign_number_warns():
    o = out(items=[{"id": "cheapest", "why": "Рейтинг 9,3", "sacrifices": [], "risks": [], "not_for": []}])
    issues = check_card_output("present.variants", INPUT, o, RULES)
    assert [(i["code"], i["severity"]) for i in issues] == [("foreign_number", "warn")]


def test_unknown_ids_block():
    assert "unknown_id" in issue_codes(out(items=[{"id": "fastest", "why": "x", "sacrifices": [], "risks": [], "not_for": []}]))
    assert "unknown_recommended" in issue_codes(out(rec="custom-1"))
    assert issue_codes(out(rec=None)) == []
    dup = [{"id": "cheapest", "why": "x", "sacrifices": [], "risks": [], "not_for": []}] * 2
    assert "duplicate_id" in issue_codes(out(items=dup))


def test_upsell_must_come_from_candidates():
    assert "unknown_upsell" in issue_codes(out(upsell=[{"candidate_id": "u-insurance", "line": "Страховка"}]))
    many = [{"candidate_id": "u-cheapest-night", "line": "a"}] * 3
    assert "too_many_upsell" in issue_codes(out(upsell=many))
    no_cands = dict(INPUT, upsell_candidates=[])
    assert "unknown_upsell" in issue_codes(out(upsell=[{"candidate_id": "u-cheapest-night", "line": "a"}]),
                                           card_input=no_cands)


def test_questions():
    assert issue_codes(out("Кто едет?")) == []
    assert issue_codes(out("Кто едет? А когда?")) == ["extra_question"]
    assert issue_codes(out("Кто едет? Когда? Откуда?")) == ["too_many_questions"]
    assert issue_codes(out("Серьёзно?!")) == []


def test_jokes_and_emoji():
    assert issue_codes(out("Море ждёт 😄 Чемодан уже скучает.")) == []
    assert "too_many_jokes" in issue_codes(out("Море ждёт 😄 А чемодан хохочет :) Шучу."))
    assert "joke_on_money" in issue_codes(out("Невозвратный билет, но кто же отменяет отпуск 😉"))
    assert "too_many_emoji" in issue_codes(out("🌊🌴☀️"))
    assert issue_codes(out("Отель 4 звезды у моря.")) == []
    assert "too_many_emoji" in issue_codes(out("Отель ★★★★ у моря."))


def test_length():
    assert issue_codes(out("")) == ["message_empty"]
    assert "message_long" in issue_codes(out("а" * 1501))
    assert issue_codes(out("а" * 1000)) == ["message_longish"]
    assert issue_codes(out("а" * 1000), rules={**RULES, "guards": {"message_soft_max_chars": 1200}}) == []


def test_small_ints_limit_is_a_rule():
    assert bad_numbers("3 варианта", ALLOWED) == []
    strict = allowed_from(INPUT)
    assert bad_numbers("3 варианта", strict, small_max=1) == ["3"]


def test_real_variant_payload_round_trip():
    """Notes the code writes only use numbers present in the variant facts, so a card copying them passes."""
    vs = build_variants(sea_brief(), sea_results(), DISTRICTS, RULES, TODAY, segments=SEGMENTS)
    card_input = {"items": [v.model_dump(mode="json") for v in vs], "questions": [], "upsell_candidates": []}
    items = [{"id": v.id, "why": f"{v.title}: {v.facts['total_rub']:,} ₽".replace(",", " "),
              "sacrifices": [n.text for n in v.sacrifices][:4], "risks": [n.text for n in v.risks][:4],
              "not_for": [n.text for n in v.not_for][:4]} for v in vs]
    o = {"message": "Три варианта.", "items": items, "recommended_id": vs[0].id, "upsell": []}
    issues = check_card_output("present.variants", card_input, o, RULES)
    assert [i for i in issues if i["severity"] == "block"] == []


SKILL_RULES = json.loads((ROOT / "skills/trip-agent/rules.json").read_text(encoding="utf-8"))


def test_skill_guard_settings():
    ok = ("Советую первый: 84 300 ₽ и 6,5 ч в пути, вылет в 06:40. Море тёплое, отель тихий, до пляжа 900 м. "
          "Второй вариант с рейтингом 9,3 — для тех, кто хочет комфорта. Кто едет?")
    assert check_card_output("present.variants", INPUT, out(ok), SKILL_RULES) == []
    short = [i["code"] for i in check_card_output("present.variants", INPUT, out("Вот."), SKILL_RULES)]
    assert short == ["message_short"]
    long_ = [i["code"] for i in check_card_output("present.variants", INPUT, out("а" * 1100), SKILL_RULES)]
    assert long_ == ["message_longish"]
    too_long = [i["code"] for i in check_card_output("present.variants", INPUT, out("а" * 1300), SKILL_RULES)]
    assert too_long == ["message_long"]


def test_banned_phrases_and_exclamations():
    o = out("Отличный вопрос! " + "Вот варианты на 84 300 ₽ — первый проще, второй с рейтингом 9,3. " * 3)
    codes = [i["code"] for i in check_card_output("present.variants", INPUT, o, SKILL_RULES)]
    assert "banned_phrase" in codes
    o = out("Ура! Море! " + "Вот варианты на 84 300 ₽, первый проще, второй с рейтингом 9,3. " * 3)
    assert "too_many_exclamations" in [i["code"] for i in check_card_output("present.variants", INPUT, o, SKILL_RULES)]


@pytest.mark.parametrize("text", ["Комфорт вдвое дороже", "в 2 раза быстрее", "в полтора раза дороже",
                                  "на треть дешевле", "на 20% дешевле"])
def test_ratios_are_computed_claims(text):
    assert "computed_claim" in issue_codes(out(text))


def test_question_mark_rule_only_for_listed_cards():
    r = {**RULES, "guard": {"question_mark_needs_questions": ["present.variants"]}}
    no_q = dict(INPUT, questions=[])
    assert [i["code"] for i in check_card_output("present.variants", no_q, out("Берём?"), r)] == ["extra_question"]
    assert check_card_output("present.info", no_q, out("Берём?"), r) == []
