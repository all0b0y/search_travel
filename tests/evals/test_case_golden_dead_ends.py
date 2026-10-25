"""The dead-end / pet-fee / budget cases (evals/authoring/cases_dead_ends.py) are satisfiable: a good output passes
every assertion, the failure each case is about fails it."""
import json

from evals.common import CASES_DIR

from .test_case_golden import run


def _input(cid):
    return json.loads((CASES_DIR / f"{cid}.json").read_text(encoding="utf-8"))["input"]


def _variant_items(inp, why="Поездом."):
    return [{"id": i["id"], "why": why, "sacrifices": [], "risks": [], "not_for": []} for i in inp["items"]]


def test_de1_train_and_leave_day():
    cid = "de1_variants_no_flights_rebuilt_by_train"
    inp = _input(cid)
    good = {"message": "Самолётов в Геленджик из Самары нет — собрал поездом. Советую «Дешевле»: 66 050 ₽ за всё, "
                       "поездом, в пути 1 сутки 3 ч, выезд 4 июля — за день до заезда. Подскажите бюджет на всех и "
                       "на сколько ночей?",
            "items": _variant_items(inp), "recommended_id": "cheapest", "upsell": []}
    assert run(cid, good) == (True, [])
    decimal = dict(good, message="Советую «Дешевле»: 66 050 ₽, туда 27,1 ч, обратно 24,1 ч.")
    ok, failed = run(cid, decimal)
    assert not ok and {"regex_absent", "predicate"} <= {f["type"] for f in failed}
    no_day = dict(good, message="Советую «Дешевле»: 66 050 ₽ за всё, поездом, в пути 1 сутки 3 ч.")
    assert not run(cid, no_day)[0]


def test_de2_plane_fixed():
    cid = "de2_variants_plane_fixed_offers_train"
    inp = _input(cid)
    good = {"message": "Самолётов по маршруту Екатеринбург — Краснодар в данных нет; поездом поездка — от 53 370 ₽ — "
                       "«Поехать поездом» кнопкой ниже. Пока советую «Дешевле»: 31 680 ₽ за проживание. Можно и в "
                       "другое место: Казань — от 77 950 ₽, Москва — от 109 710 ₽ — кнопки ниже. Какой бюджет на "
                       "всех, примерно?",
            "items": _variant_items(inp, why="Самое недорогое проживание."), "recommended_id": "cheapest",
            "upsell": []}
    assert run(cid, good) == (True, [])
    built = dict(good, message="Самолётов нет — собрал поездом. Советую «Дешевле»: 31 680 ₽.")
    assert not run(cid, built)[0]


def test_de3_pet_fee_line():
    cid = "de3_info_cart_pet_fee_line"
    good = {"message": "Собака уже в цене билетов: «Перелёт DME → AER, UN7545» — в т.ч. собака в салоне — 2 250 ₽, "
                       "«Перелёт AER → VKO, UN4865» — в т.ч. 2 460 ₽. Итого 52 130 ₽ — это сумма строк корзины.",
            "items": [], "recommended_id": None, "upsell": []}
    assert run(cid, good) == (True, [])
    bad = dict(good, message="Доплата за собаку в корзину не входит — её оплатите в аэропорту.")
    assert not run(cid, bad)[0]


def test_de4_two_fit():
    cid = "de4_destinations_two_fit_the_budget"
    inp = _input(cid)
    items = [{"id": i["id"], "why": i["name"], "watch_out": None} for i in inp["items"]]
    good = {"message": "В 25 000 ₽ укладываются Самара поездом — от 21 360 ₽ и Казань поездом — от 23 790 ₽. "
                       "Остальное дороже бюджета: Бухара — от 63 170 ₽.",
            "items": items, "recommended_id": inp["items"][0]["id"], "upsell": []}
    assert run(cid, good) == (True, [])
    bad = dict(good, message="В 25 000 ₽ ничего не укладывается — вот самые доступные варианты: Самара поездом — "
                             "от 21 360 ₽ и Казань поездом — от 23 790 ₽.")
    ok, failed = run(cid, bad)
    assert not ok and any(f["type"] == "regex_absent" for f in failed)
    nearly = dict(good, message="В 25 000 ₽ почти ничего не укладывается: Самара поездом — от 21 360 ₽, Казань — "
                                "от 23 790 ₽.")
    assert not run(cid, nearly)[0]


def test_de5_connecting():
    cid = "de5_info_connecting_after_no_build"
    good = {"message": "С пересадкой тоже нет: рейсов по маршруту Екатеринбург — Краснодар в данных нет — ни прямых, "
                       "ни с пересадкой. Поездом та же поездка — от 21 310 ₽, кнопка «Поехать поездом» ниже.",
            "items": [], "recommended_id": None, "upsell": []}
    assert run(cid, good) == (True, [])
    bad = dict(good, message="С пересадкой можно — через Москву, поищу.")
    assert not run(cid, bad)[0]
    unseen = dict(good, message="Билетов я пока не смотрел.")
    assert not run(cid, unseen)[0]
