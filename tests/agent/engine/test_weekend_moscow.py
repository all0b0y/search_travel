"""The Moscow couple short-break flow over the real mock and catalog, without model/network calls."""
import json

import pytest

from .fakes import u
from .test_scopes import ask, live, no_blocks  # noqa: F401

TRAVELLERS = [{"name": "Анна Тестовая", "birth_date": "1993-02-10"},
              {"name": "Иван Тестовый", "birth_date": "1991-06-15"}]


@pytest.mark.parametrize("live", ["stage1", "stage3", "stage5"], indirect=True)
@pytest.mark.parametrize("city,nights", [("Санкт-Петербург", 2), ("Санкт-Петербург", 3), ("Казань", 2), (None, 2)])
def test_couple_weekend_rebuild_compare_cart_and_purchase(live, city, nights):
    h = live
    patch = {"origin.city": "Москва", "dates.start": "2026-11-20", "dates.nights": nights,
             "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000,
             "prefs.max_travel_hours": 5}
    if city:
        response, variants = ask(h, f"Из Москвы вдвоем в {city} с 20 ноября на {nights} ночи", **patch,
                                 **{"destination.name": city})
    else:
        h.understand(u(set={**patch, "traits.add": ["city", "food"], "prefs.transport": "train"}))
        response = h.turn("Из Москвы вдвоем на выходные, с 20 ноября на две ночи, до 60000. Куда поехать поездом?")
        assert response["stage"] == "inspiration" and response["cards"]["destinations"], response["message"]
        destination = response["cards"]["destinations"][0]["id"]
        response = h.act("choose_destination", destination_id=destination)
        assert response["stage"] == "selection", response["message"]
        variants = h.session().variants
    assert all(v.facts["nights"] == nights for v in variants)
    assert all(v.facts.get("destination_hours", 0) > 0 for v in variants)
    assert all(v.totals.rub <= 60000 for v in variants)
    assert all(v.hotel and not v.facts.get("dorm_bed") for v in variants)
    no_blocks(h, variants)

    h.understand(u(intent="refine", set={"dates.shift_days": 7}))
    response = h.turn("Давайте на неделю позже, остальное оставляем")
    assert response.get("error") is None, response["message"]
    variants = h.session().variants
    values = h.session().brief.values()
    assert values["origin"]["city"] == "Москва"
    assert values["party"]["adults"] == 2 and values["party"]["segment"] == "couple"
    assert values["budget_rub"]["max"] == 60000 and values["prefs"]["max_travel_hours"] == 5
    assert variants and all(v.facts["nights"] == nights for v in variants)
    assert all(v.facts["check_in_date"] == "2026-11-27" for v in variants)
    no_blocks(h, variants)

    if len(variants) > 1:
        response = h.act("compare", variant_ids=[v.id for v in variants])
        row = next(row for row in response["comparison"]["rows"] if row["key"] == "destination_time")
        assert row["label"] == "На месте, включая ночи"
        assert set(row["values"]) == {v.id for v in variants}
        if row["best_id"]:
            assert h.session().variant(row["best_id"]).facts["destination_hours"] == max(
                v.facts["destination_hours"] for v in variants)

    selected = max(variants, key=lambda v: v.facts["destination_hours"])
    total = selected.totals.rub
    response = h.act("choose_variant", variant_id=selected.id)
    assert response["stage"] == "cart", response["message"]
    assert response["cart"]["total_rub"] == total
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "weekend@example.com"})
    assert response["stage"] == "checkout", response["message"]
    order = response["order"]
    assert order["total_rub"] == total
    stored = h.ostrovok.call("order_get", {"order_id": order["order_id"]})
    assert len(stored["travellers"]) == 2 and stored["total_rub"] == total
    response = h.act("pay", order_id=order["order_id"])
    assert response["stage"] == "ordered", response["message"]
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == total
    response = h.act("pay", order_id=order["order_id"])
    assert response["order"]["order_id"] == order["order_id"]
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == total


@pytest.mark.parametrize("live", ["stage5"], indirect=True)
def test_weekend_api_accepts_the_complete_purchase_flow(live, tmp_path):
    from agent.server import Api

    h = live
    api = Api(h.engine, debug=True, files_root=tmp_path)

    def post(**request):
        reply = api.dispatch("POST", "/api/turn", json.dumps({"session_id": h.sid, **request}).encode())
        assert reply.status == 200, reply.json()
        response = reply.json()
        assert not response.get("error"), response["message"]
        assert not list(h.validator.iter_errors(response))
        return response

    h.understand(u(set={"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                        "dates.start": "2026-11-20", "dates.nights": 2,
                        "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000}))
    response = post(text="Из Москвы в Петербург вдвоем на две ночи с 20 ноября до 60 тысяч")
    assert response["stage"] == "selection"
    variants = h.session().variants
    if len(variants) > 1:
        response = post(action={"type": "compare", "payload": {"variant_ids": [v.id for v in variants]}})
        assert any(row["key"] == "destination_time" for row in response["comparison"]["rows"])
    selected = max(variants, key=lambda v: v.facts["destination_hours"])
    response = post(action={"type": "choose_variant", "payload": {"variant_id": selected.id}})
    assert response["cart"]["total_rub"] == selected.totals.rub
    response = post(action={"type": "checkout", "payload": {
        "travellers": TRAVELLERS, "contact": {"email": "weekend-api@example.com"}}})
    order_id = response["order"]["order_id"]
    response = post(action={"type": "pay", "payload": {"order_id": order_id}})
    assert response["stage"] == "ordered" and response["order"]["status"] == "paid"
    assert response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("live", ["stage3", "stage5"], indirect=True)
def test_weekend_cart_rebuild_uses_new_dates_budget_and_train(live):
    h = live
    _, variants = ask(h, "В Петербург вдвоем на две ночи с 20 ноября, до 60 тысяч",
                      **{"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                         "dates.start": "2026-11-20", "dates.nights": 2,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000})
    h.act("choose_variant", variant_id=variants[0].id)
    h.understand(u(intent="refine", set={"dates.shift_days": 7, "dates.nights": 3,
                                         "budget_rub.max": 40000, "prefs.transport": "train"}))
    response = h.turn("Пересоберите на неделю позже, на три ночи, только поездом и до 40 тысяч")
    assert response["stage"] == "selection" and not response.get("error"), response["message"]
    variants = h.session().variants
    assert variants and all(v.facts["nights"] == 3 for v in variants)
    assert all(v.facts["check_in_date"] == "2026-11-27" for v in variants)
    assert all(v.steps[0].type == v.steps[-1].type == "train" for v in variants)
    assert all(v.totals.rub <= 40000 for v in variants)
    dates = h.session().brief.values()["dates"]
    assert (dates["start"], dates["end"], dates["nights"]) == ("2026-11-27", "2026-11-30", 3)
    no_blocks(h, variants)
    selected = variants[0]
    response = h.act("choose_variant", variant_id=selected.id)
    assert {item["offer_id"] for item in response["cart"]["items"]} == {
        step.offer_id for step in selected.steps}
    assert response["cart"]["total_rub"] == selected.totals.rub
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "weekend-refine@example.com"})
    assert response["stage"] == "checkout", response["message"]
    order_id = response["order"]["order_id"]
    response = h.act("pay", order_id=order_id)
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("before", ["cart", "checkout"])
def test_old_purchase_button_cannot_buy_the_trip_before_rebuild(live, before):
    h = live
    _, variants = ask(h, "Из Москвы в Петербург на две ночи вдвоем с 20 ноября",
                      **{"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                         "dates.start": "2026-11-20", "dates.nights": 2,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000})
    h.act("choose_variant", variant_id=variants[0].id)
    order_id = None
    if before == "checkout":
        response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "weekend-old@example.com"})
        order_id = response["order"]["order_id"]
    h.understand(u(intent="refine", set={"dates.shift_days": 7}))
    response = h.turn("Пересоберите на неделю позже")
    assert response["stage"] == "selection", response["message"]
    if order_id:
        response = h.act("pay", order_id=order_id)
        stored = h.ostrovok.call("order_get", {"order_id": order_id})
        assert stored["status"] == "pending_payment"
    else:
        response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "weekend-old@example.com"})
        assert h.session().order_id is None
    assert response.get("error") and response["stage"] == "selection", response["message"]
    selected = h.session().variants[0]
    assert selected.facts["check_in_date"] == "2026-11-27"
    h.act("choose_variant", variant_id=selected.id)
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "weekend-new@example.com"})
    new_id = response["order"]["order_id"]
    assert new_id != order_id
    response = h.act("pay", order_id=new_id)
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub
