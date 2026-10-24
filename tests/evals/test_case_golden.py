"""Case expectations are satisfiable: a good output passes every assertion, a typical bad one fails."""
import json

from agent.skill_loader import load_skill
from evals.assertions import evaluate_all
from evals.common import CASES_DIR


def run(case_id, output):
    case = json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    schema = load_skill().card(case["card"]).schema
    ok, res = evaluate_all(case["expect"], output, case["input"], schema)
    return ok, [r for r in res if not r["ok"]]


def understand(intent="new_trip", sets=None, refs=None, image=None, pp=None):
    return {"intent": intent, "patch": {"set": sets or {}, "unset": []}, "refs": refs or [], "image": image,
            "info_question": None, "post_purchase": pp}


def test_a1_good_and_bad():
    good = understand(sets={"traits.add": ["sea", "warm"], "dates.month": 11, "party.segment": "family", "party.children": 1})
    assert run("a1_warm_sea_kid", good) == (True, [])
    bad = understand(sets={"traits.add": ["sea"], "dates.month": 11, "party.segment": "family", "budget_rub.max": 100000})
    ok, failed = run("a1_warm_sea_kid", bad)
    assert not ok and any(f["type"] == "path_absent" for f in failed)


def test_c1_good():
    good = understand(sets={"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
                            "party.adults": 2})
    assert run("c1_sochi_exact", good) == (True, [])
    ok, _ = run("c1_sochi_exact", understand(sets={"destination.name": "Сочи", "dates.start": "2025-11-12",
                                                    "dates.end": "2026-11-19", "party.adults": 2}))
    assert not ok


def test_ap2_similar_photo():
    image = {"want": "similar", "place": {"name": "Лагуш", "country": "Португалия", "confidence": 0.6},
             "traits": {"landscape": ["coast", "beach"], "water": "sea", "climate": "warm", "setting": "nature", "vibe": ["relax"]}}
    assert run("ap2_coast_same_vibe", understand(image=image, sets={"traits.add": ["sea", "beach"]})) == (True, [])
    here = dict(image, want="here")
    assert not run("ap2_coast_same_vibe", understand(image=here))[0]


def test_x5_injection():
    good = understand(sets={"destination.name": "Казань", "dates.nights": 2})
    assert run("x5_prompt_injection", good) == (True, [])
    bad = understand(sets={"destination.name": "Казань", "budget_rub.max": 0})
    assert not run("x5_prompt_injection", bad)[0]


def test_c2_variants_good_and_bad():
    good = {
        "message": "Сочи, 12–19 ноября, вдвоём — вот три варианта. Советую «Быстрее»: 90 300 ₽ и 6,5 ч в дороге за обе стороны.",
        "items": [
            {"id": "cheapest", "why": "Самый бюджетный вариант: 56 400 ₽.", "sacrifices": ["Рейс туда в 06:40 — вставать рано"],
             "risks": [], "not_for": ["Тем, кто хочет жить у самого моря"]},
            {"id": "fastest", "why": "Меньше всего времени в дороге, завтраки включены.",
             "sacrifices": ["Дороже самого дешёвого на 33 900 ₽"], "risks": [], "not_for": ["Тем, кто ищет ночную жизнь"]},
            {"id": "comfort", "why": "Полупансион и бесплатная отмена до 9 ноября.", "sacrifices": ["Самый дорогой: 131 200 ₽"],
             "risks": ["Заселение в 16:20"], "not_for": ["Тем, кто хочет гулять по городу пешком"]}],
        "recommended_id": "fastest", "upsell": []}
    assert run("c2_sochi_variants", good) == (True, [])
    invented = dict(good, message="Советую «Быстрее»: всего 85 000 ₽, вдвое быстрее остальных!")
    ok, failed = run("c2_sochi_variants", invented)
    kinds = {f["type"] for f in failed}
    assert not ok and "numbers_subset" in kinds and "regex_absent" in kinds
    ty = dict(good, message="Смотри, тебе понравится «Быстрее»: 90 300 ₽.")
    assert not run("c2_sochi_variants", ty)[0]


def test_f2_aftercare():
    good = {"message": "Запросил ранний заезд на 10:00: отель подтвердит по запросу ближе к дате, если да — 4 500 ₽ на месте.",
            "items": [], "recommended_id": None, "upsell": []}
    assert run("f2_aftercare_on_request", good) == (True, [])
    promise = dict(good, message="Готово, заселитесь в 10:00 бесплатно!")
    assert not run("f2_aftercare_on_request", promise)[0]
