"""Level-2 assertion types and named predicates: true and false examples."""
import json

import pytest

from evals.assertions import PREDICATES, evaluate, evaluate_all
from evals.common import CASES_DIR, number_pool, numbers_in_text, resolve

INP = json.loads((CASES_DIR / "c2_sochi_variants.json").read_text(encoding="utf-8"))["input"]
SCHEMA = None


def good_variants_output():
    return {
        "message": "Сочи, 12–19 ноября, вдвоём — вот три варианта. Советую «Быстрее»: 90 300 ₽, 6,5 ч в дороге за обе стороны.",
        "items": [
            {"id": "cheapest", "why": "Самый бюджетный: 56 400 ₽.", "sacrifices": ["Рейс туда в 06:40"], "risks": [],
             "not_for": ["Тем, кто хочет жить у самого моря"]},
            {"id": "fastest", "why": "Меньше всего времени в дороге.", "sacrifices": ["Дороже на 33 900 ₽"], "risks": [],
             "not_for": ["Тем, кто ищет ночную жизнь рядом"]},
            {"id": "comfort", "why": "Полупансион и бесплатная отмена.", "sacrifices": ["Самый дорогой: 131 200 ₽"],
             "risks": ["Заселение в 16:20"], "not_for": ["Тем, кто хочет гулять пешком"]},
        ],
        "recommended_id": "fastest",
        "upsell": [],
    }


def ev(a, out, inp=INP, schema=None):
    return evaluate(a, out, inp, schema)


def test_numbers_in_text_russian_formatting():
    assert numbers_in_text("Итого 90 300 ₽ и 6,5 ч, рейс в 06:40, 12–19 ноября") == [90300, 6.5, 6, 40, 12, 19]
    assert numbers_in_text("за 1\u00a0234\u00a0567 ₽") == [1234567]
    pool = number_pool({"t": "2026-11-12T06:40+03:00", "x": 6.47, "d": -2200})
    assert {2026, 11, 12, 6, 40, 6.5, 2200} <= pool


def test_resolve_paths():
    data = {"patch": {"set": {"destination.name": "Сочи"}}, "items": [{"id": "a"}, {"id": "b"}]}
    assert resolve(data, "/patch/set/destination.name") == [("/patch/set/destination.name", "Сочи")]
    assert [v for _, v in resolve(data, "/items/*/id")] == ["a", "b"]
    assert resolve(data, "/items/5/id") == []


def test_schema_assertion(skill_dir):
    schema = json.loads((skill_dir / "schema/present.variants.schema.json").read_text(encoding="utf-8"))
    assert ev({"type": "schema"}, good_variants_output(), schema=schema)["ok"]
    bad = dict(good_variants_output(), extra=1)
    assert not ev({"type": "schema"}, bad, schema=schema)["ok"]


def test_path_assertions():
    out = {"intent": "new_trip", "patch": {"set": {"dates.month": 11, "destination.name": "сочи"}, "unset": []}, "image": None}
    assert ev({"type": "path_equals", "path": "/intent", "value": "new_trip"}, out)["ok"]
    assert not ev({"type": "path_equals", "path": "/intent", "value": "refine"}, out)["ok"]
    assert ev({"type": "path_equals", "path": "/patch/set/destination.name", "value": "Сочи", "casefold": True}, out)["ok"]
    assert not ev({"type": "path_equals", "path": "/patch/set/budget_rub.max", "value": 1}, out)["ok"]
    assert ev({"type": "path_in", "path": "/patch/set/dates.month", "values": [11, 12]}, out)["ok"]
    assert not ev({"type": "path_in", "path": "/patch/set/dates.month", "values": [5]}, out)["ok"]
    assert not ev({"type": "path_in", "path": "/patch/set/party.segment", "values": ["family"]}, out)["ok"]
    assert ev({"type": "path_in", "path": "/patch/set/party.segment", "values": ["family"], "allow_absent": True}, out)["ok"]
    assert ev({"type": "path_absent", "path": "/patch/set/budget_rub.max"}, out)["ok"]
    assert ev({"type": "path_absent", "path": "/image"}, out)["ok"]
    assert not ev({"type": "path_absent", "path": "/patch/set/dates.month"}, out)["ok"]


def test_max_items_and_regex_absent():
    out = good_variants_output()
    assert ev({"type": "max_items", "path": "/items", "max": 3}, out)["ok"]
    assert not ev({"type": "max_items", "path": "/items", "max": 2}, out)["ok"]
    assert ev({"type": "regex_absent", "path": "$text", "pattern": "страховк"}, out)["ok"]
    assert not ev({"type": "regex_absent", "path": "$text", "pattern": "полупансион"}, out)["ok"]
    assert not ev({"type": "regex_absent", "path": "/message", "pattern": "сочи"}, out)["ok"]


def test_numbers_subset():
    out = good_variants_output()
    assert ev({"type": "numbers_subset"}, out)["ok"]
    bad = dict(out, message="Всего 87 000 ₽ за двоих!")
    r = ev({"type": "numbers_subset"}, bad)
    assert not r["ok"] and "87000" in r["detail"]
    assert ev({"type": "numbers_subset", "allow": [87000]}, bad)["ok"]
    assert ev({"type": "numbers_subset"}, dict(out, message="Вот 3 варианта"))["ok"]  # small counts allowed


def test_ids_subset():
    out = good_variants_output()
    assert ev({"type": "ids_subset"}, out)["ok"]
    assert not ev({"type": "ids_subset"}, dict(out, recommended_id="custom-7"))["ok"]
    assert not ev({"type": "ids_subset"}, dict(out, upsell=[{"candidate_id": "u9", "line": "x"}]))["ok"]


def test_unknown_types_fail():
    assert not ev({"type": "nope"}, {})["ok"]
    assert not ev({"type": "predicate", "name": "nope"}, {})["ok"]


P = lambda name, out, inp=INP, **args: PREDICATES[name](out, inp, **args)[0]  # noqa: E731


def test_patch_predicates():
    out = {"patch": {"set": {"destination.name": "Сочи", "origin.city": "Москва"}, "unset": []}}
    assert P("patch_has_no_assumptions", out, allowed=["destination.name", "origin.city"])
    assert not P("patch_has_no_assumptions", out, allowed=["destination.name"])
    assert not P("patch_has_no_assumptions", {"patch": {"set": {}, "unset": ["prefs.meals"]}}, allowed=[])
    assert P("patch_empty", {"patch": {"set": {}, "unset": []}})
    assert not P("patch_empty", out)
    assert P("pet_recorded", {"patch": {"set": {"party.pets": ["собака"]}}})
    assert P("pet_recorded", {"patch": {"set": {"constraints.add": ["pet:dog"]}}})
    assert not P("pet_recorded", {"patch": {"set": {"party.pets": ["кот"]}}})


def test_value_predicates():
    out = {"patch": {"set": {"traits.add": ["sea", "warm"]}}, "image": {"place": {"name": "Стамбул", "confidence": 0.93}},
           "refs": [{"kind": "destination", "ref": "2"}]}
    assert P("contains", out, path="/patch/set/traits.add", values=["sea"])
    assert P("contains", out, path="/patch/set/traits.add", values=["ski", "sea"], mode="any")
    assert not P("contains", out, path="/patch/set/traits.add", values=["ski", "sea"])
    assert P("number_between", out, path="/image/place/confidence", min=0.8)
    assert not P("number_between", out, path="/image/place/confidence", max=0.5)
    assert not P("number_between", out, path="/image/nothing", min=0)
    assert P("matches", out, path="/image/place/name", pattern="стамбул")
    assert not P("matches", out, path="/image/place/name", pattern="анталья")
    assert P("refs_include", out, kind="destination", ref="2")
    assert not P("refs_include", out, kind="variant", ref="2")


def test_text_predicates():
    out = good_variants_output()
    assert P("asks_at_most", out, n=1)
    assert not P("asks_at_most", dict(out, message="Едем? Летим? Плывём?"), n=1)
    assert P("mentions_any", out, words=["советую"])
    assert not P("mentions_any", out, words=["тюлень"])
    assert P("mentions_any", out, words=["полупансион"], path="$text")
    assert P("mentions_risk", out)
    no_risk = json.loads(json.dumps(out))
    no_risk["items"][2]["risks"] = []
    assert not P("mentions_risk", no_risk)
    assert P("mentions_risk", no_risk, keywords=["16:20"]) is False  # keyword not in message
    assert P("mentions_risk", dict(no_risk, message="Заселение в 16:20 — учтите."), keywords=["16:20"])
    assert P("keeps_notes", out, field="not_for")
    dropped = json.loads(json.dumps(out))
    dropped["items"][1]["not_for"] = []
    assert not P("keeps_notes", dropped, field="not_for")
    assert P("recommended_in_items", out)
    assert not P("recommended_in_items", dict(out, recommended_id=None))
    assert P("recommended_in_items", dict(out, recommended_id=None), allow_null=True)
    assert P("max_chars", out, n=600)
    assert not P("max_chars", out, n=10)


def test_upsell_predicate():
    out = good_variants_output()
    assert P("no_upsell_without_candidate", out)
    assert not P("no_upsell_without_candidate", dict(out, upsell=[{"candidate_id": "u1", "line": "x"}]))
    assert not P("no_upsell_without_candidate", dict(out, message="Советую докупить страховку."))
    inp = dict(INP, upsell_candidates=[{"candidate_id": "u1", "variant_id": "fastest", "kind": "transfer", "label": "Т",
                                        "price_rub": 1, "reason": "r"}])
    assert P("no_upsell_without_candidate", dict(out, upsell=[{"candidate_id": "u1", "line": "Трансфер"}]), inp)
    assert not P("no_upsell_without_candidate", dict(out, upsell=[{"candidate_id": "u2", "line": "x"}]), inp)


def test_tone_predicates():
    out = good_variants_output()
    assert P("tone_no_bureaucratese", out)
    assert not P("tone_no_bureaucratese", dict(out, message="Данный вариант является оптимальным."))
    assert P("at_most_one_joke", dict(out, message="Море ждёт 😉"))
    assert not P("at_most_one_joke", dict(out, message="Море ждёт 😉 и солнце 🌞"))
    assert not P("at_most_one_joke", dict(out, message="Ха-ха, шучу :)"))
    assert P("money_risks_plain", out)
    assert not P("money_risks_plain", dict(out, message="Отмена невозвратная 😅 но зато дёшево."))
    assert P("russian_only", out)
    assert not P("russian_only", dict(out, message="Here are three great options for you"))
    assert P("russian_only", dict(out, message="Летим рейсом Пример Авиа из SVO"))  # SVO is in the input


def test_honesty_predicates():
    assert P("admits_no_data", {"message": "По Казани цены пока нет — предложений не нашлось."})
    assert not P("admits_no_data", {"message": "Казань — отличный выбор!"})
    inp = {"items": [{"id": "kazan", "price_from_rub": None}]}
    assert P("null_price_not_invented", {"items": [{"id": "kazan", "why": "Вкусная еда"}]}, inp)
    assert not P("null_price_not_invented", {"items": [{"id": "kazan", "why": "От 30 000 ₽"}]}, inp)


def test_combinators():
    out = {"intent": "refine"}
    a_ok = {"type": "path_equals", "path": "/intent", "value": "refine"}
    a_no = {"type": "path_equals", "path": "/intent", "value": "choose"}
    assert P("any_of", out, assertions=[a_no, a_ok])
    assert not P("any_of", out, assertions=[a_no])
    assert P("none_of", out, assertions=[a_no])
    assert not P("none_of", out, assertions=[a_ok])


def test_evaluate_all_reports_each():
    ok, res = evaluate_all([{"type": "max_items", "path": "/items", "max": 2}, {"type": "numbers_subset"}],
                           good_variants_output(), INP)
    assert not ok
    assert [r["ok"] for r in res] == [False, True]
    assert res[0]["label"] == "/items" and "3 items" in res[0]["detail"]


def test_every_registered_predicate_is_callable():
    for name, fn in PREDICATES.items():
        assert callable(fn), name


def test_any_of_passes_when_one_reading_passes():
    out = {"intent": "new_trip", "patch": {"set": {"party.segment": "solo"}}}
    either = {"type": "any_of", "of": [{"type": "path_equals", "path": "/patch/set/party.adults", "value": 1},
                                       {"type": "path_equals", "path": "/patch/set/party.segment", "value": "solo"}]}
    assert ev(either, out)["ok"]
    neither = {"type": "any_of", "of": [{"type": "path_equals", "path": "/patch/set/party.adults", "value": 2},
                                        {"type": "path_equals", "path": "/patch/set/party.segment", "value": "couple"}]}
    assert not ev(neither, out)["ok"]
    assert ev({"type": "path_present", "path": "/patch/set/party.segment"}, out)["ok"]
    assert not ev({"type": "path_present", "path": "/patch/set/party.adults"}, out)["ok"]
