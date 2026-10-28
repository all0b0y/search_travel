"""The Moscow couple short-break flow over the real mock and catalog, without model/network calls."""
import pytest

from .fakes import u
from .test_scopes import ask, live, no_blocks  # noqa: F401

TRAVELLERS = [{"name": "Анна Тестовая", "birth_date": "1993-02-10"},
              {"name": "Иван Тестовый", "birth_date": "1991-06-15"}]


@pytest.mark.parametrize("live", ["stage1", "stage3"], indirect=True)
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
