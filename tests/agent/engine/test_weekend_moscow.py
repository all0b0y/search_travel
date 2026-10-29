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
    from datetime import datetime, timezone
    from agent.server import Api

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

    api = Api(h.engine, debug=True, files_root=h.engine.files_dir)
    downloaded = {}
    for name, content_type in (("ics", "text/calendar"), ("page", "text/html")):
        assert response["files"][name]
        reply = api.dispatch("GET", response["files"][name])
        assert reply.status == 200 and reply.content_type.startswith(content_type)
        downloaded[name] = reply.data.decode("utf-8")
    calendar = downloaded["ics"].replace("\r\n ", "")
    assert calendar.count("BEGIN:VEVENT") == len(selected.steps)
    for field, attribute in (("DTSTART", "start"), ("DTEND", "end")):
        actual = [line for line in calendar.split("\r\n") if line.startswith(field + ":")]
        expected = [field + ":" + datetime.fromisoformat(getattr(step, attribute)).astimezone(
            timezone.utc).strftime("%Y%m%dT%H%M%SZ") for step in selected.steps]
        assert actual == expected
    for step in selected.steps:
        assert step.offer_id in calendar and step.offer_id in downloaded["page"]
        assert datetime.fromisoformat(step.start).strftime("%d.%m %H:%M") in downloaded["page"]
        assert datetime.fromisoformat(step.end).strftime("%d.%m %H:%M") in downloaded["page"]
    price = f"{selected.totals.rub:,}".replace(",", " ") + " ₽"
    assert price in downloaded["page"]
    assert '<b>3</b><span>ночи в отеле</span>' in downloaded["page"]


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


def test_short_break_prices_stay_consistent_after_transfer_requote(live):
    from datetime import datetime
    from agent.checks import check_variant_set
    from ostrovok_mcp.offer_ids import decode
    from ostrovok_mcp.transfers import build as transfer_offer

    h = live
    h.new(profile_id=None)
    _, variants = ask(h, "Из Москвы в Сочи вдвоем с 25 декабря на две ночи, до 60 тысяч",
                      **{"origin.city": "Москва", "destination.name": "Сочи",
                         "dates.start": "2026-12-25", "dates.nights": 2,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000})
    assert check_variant_set(variants) == []
    minimum = min(v.totals.rub for v in variants)
    for variant in variants:
        assert variant.facts["total_rub"] == variant.totals.rub, {
            "id": variant.id, "facts": variant.facts,
            "total": variant.totals.rub, "steps": [(s.type, s.price.rub) for s in variant.steps]}
        assert variant.facts["ref_min_rub"] == minimum
        assert variant.facts["delta_rub_vs_cheapest"] == variant.totals.rub - minimum
        assert variant.facts["price_transfers_rub"] == sum(
            s.price.rub for s in variant.steps if s.type == "transfer")
        for step in variant.steps:
            if step.type == "transfer":
                _, params = decode(step.offer_id, "transfer")
                quote = transfer_offer(params)
                assert quote is not None and params["at"] == step.start
                assert step.duration_estimate_min == quote["duration_min"]
                assert step.price.rub == quote["price_rub"]
        arrival = next(s.end for s in variant.steps if s.type == "transfer")
        departure = next(s.start for s in reversed(variant.steps) if s.type == "transfer")
        assert variant.facts["destination_hours"] == round(
            (datetime.fromisoformat(departure) - datetime.fromisoformat(arrival)).total_seconds() / 3600, 1)


@pytest.mark.parametrize("budget", [30000, 100000])
def test_weekend_extra_time_card_does_not_undercut_the_fastest(live, budget):
    from agent.checks import check_variant_set

    h = live
    h.new(profile_id=None)
    _, variants = ask(h, f"Из Москвы в Калининград поездом на три ночи с 20 ноября, до {budget} рублей",
                      **{"origin.city": "Москва", "destination.name": "Калининград",
                         "dates.start": "2026-11-20", "dates.nights": 3,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": budget,
                         "scope": ["trains", "stay", "transfers"]})
    assert check_variant_set(variants) == [], [
        (v.id, v.totals.rub, v.totals.door_to_door_hours, v.facts.get("destination_hours")) for v in variants]


@pytest.mark.parametrize("live", ["stage3,guide"], indirect=True)
def test_guide_can_choose_more_time_from_code_facts_and_complete_purchase(live):
    from .test_guide import Script, calls, reply

    h = live

    def choose(context, request):
        choices = context["default_plan"].get("variants") or context["screen"]["variants"]
        best = max(choices,
                   key=lambda v: v.get("facts", {}).get("destination_hours", 0))
        return reply(f"Советую «{best['title']}»: этот вариант оставляет больше времени на месте, включая ночи.",
                     rec=best["id"])

    script = Script(h, choose)
    h.understand(u(set={"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                        "dates.start": "2026-11-20", "dates.nights": 2,
                        "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000}))
    response = h.turn("Из Москвы в Петербург вдвоем на две ночи с 20 ноября. Хотим больше времени на месте")
    assert response["debug"]["guide"]["used"] is True
    variants = h.session().variants
    for compact in script.ctx()["default_plan"]["variants"]:
        assert compact["facts"]["destination_hours"] == h.session().variant(compact["id"]).facts["destination_hours"]
    selected = max(variants, key=lambda v: v.facts["destination_hours"])
    assert response["debug"]["guide"]["recommended_id"] == selected.id
    followup = Script(h, choose)
    h.understand(u(intent="info", info="В каком варианте больше времени на месте?"))
    response = h.turn("А в каком варианте больше времени на месте?")
    assert response["debug"]["guide"]["used"] is True
    for compact in followup.ctx()["screen"]["variants"]:
        assert compact["facts"]["destination_hours"] == h.session().variant(compact["id"]).facts["destination_hours"]
    assert response["debug"]["guide"]["recommended_id"] == selected.id

    def choose_rebuilt(context, request):
        result = context["tool_results"][-1]["result"]
        assert result["ok"] is True
        best = max(result["variants"], key=lambda v: v["facts"]["destination_hours"])
        return reply(f"Если сдвинуть поездку и остаться на три ночи, советую «{best['title']}»: больше времени на месте.",
                     rec=best["id"])

    Script(h, calls(("rebuild", {"set": {"dates.shift_days": 7, "dates.nights": 3,
                                         "budget_rub.max": 40000, "prefs.transport": "train"}})), choose_rebuilt)
    h.understand(u(intent="refine"))
    response = h.turn("А если на неделю позже и на три ночи, поездом до сорока тысяч?")
    assert response["debug"]["guide"]["used"] is True
    variants = h.session().variants
    assert all(v.facts["nights"] == 3 and v.facts["check_in_date"] == "2026-11-27" for v in variants)
    assert all(v.totals.rub <= 40000 for v in variants)
    selected = h.session().variant(response["debug"]["guide"]["recommended_id"])
    h.act("choose_variant", variant_id=selected.id)
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "weekend-guide@example.com"})
    response = h.act("pay", order_id=response["order"]["order_id"])
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("phase", ["cart", "checkout"])
def test_comparing_unchanged_weekend_options_keeps_purchase_available(live, phase):
    h = live
    _, variants = ask(h, "Из Москвы в Петербург вдвоем с 20 ноября на две ночи",
                      **{"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                         "dates.start": "2026-11-20", "dates.nights": 2,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000})
    selected = variants[0]
    h.act("choose_variant", variant_id=selected.id)
    if phase == "checkout":
        response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "compare@example.com"})
        order_id = response["order"]["order_id"]
    before = [tuple(step.offer_id for step in v.steps) for v in variants]
    response = h.act("compare", variant_ids=[v.id for v in variants])
    assert response["comparison"]
    assert before == [tuple(step.offer_id for step in v.steps) for v in h.session().variants]
    if phase == "cart":
        response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "compare@example.com"})
        assert response["stage"] == "checkout", response["message"]
        order_id = response["order"]["order_id"]
    response = h.act("pay", order_id=order_id)
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("live", ["stage1", "stage3", "stage5"], indirect=True)
@pytest.mark.parametrize("city,destination_id", [("Казань", "kazan"), ("Сочи", "sochi")])
def test_weekend_destination_change_after_checkout_buys_only_the_new_trip(live, city, destination_id):
    h = live
    _, variants = ask(h, "Из Москвы в Петербург вдвоем с 20 ноября на три ночи",
                      **{"origin.city": "Москва", "destination.name": "Санкт-Петербург",
                         "dates.start": "2026-11-20", "dates.nights": 3,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 100000})
    h.act("choose_variant", variant_id=variants[0].id)
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "switch@example.com"})
    old_order = response["order"]["order_id"]
    h.understand(u(intent="refine", set={"destination.name": city, "dates.nights": 2}))
    response = h.turn(f"Лучше в {city} на две ночи, остальное оставьте")
    assert response["stage"] == "selection" and not response.get("error"), response["message"]
    values = h.session().brief.values()
    assert values["destination"]["id"] == destination_id
    assert values["origin"]["city"] == "Москва" and values["party"]["adults"] == 2
    assert values["party"]["segment"] == "couple" and values["budget_rub"]["max"] == 100000
    variants = h.session().variants
    assert variants and all(v.destination_id == destination_id and v.facts["nights"] == 2 for v in variants)
    assert all(v.facts["check_in_date"] == "2026-11-20" for v in variants)
    no_blocks(h, variants)
    response = h.act("pay", order_id=old_order)
    assert response.get("error"), response["message"]
    assert h.ostrovok.call("order_get", {"order_id": old_order})["status"] == "pending_payment"
    selected = variants[0]
    response = h.act("choose_variant", variant_id=selected.id)
    assert {item["offer_id"] for item in response["cart"]["items"]} == {
        step.offer_id for step in selected.steps}
    assert response["cart"]["total_rub"] == selected.totals.rub
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "switch@example.com"})
    new_order = response["order"]["order_id"]
    assert new_order != old_order
    response = h.act("pay", order_id=new_order)
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("budget", [39000, 60000])
def test_weekend_extra_time_keeps_its_budget_after_a_night_transfer_replacement(live, budget):
    h = live
    h.new(profile_id=None)
    _, variants = ask(h, f"Из Москвы в Калининград вдвоем с 1 января на две ночи до {budget}",
                      **{"origin.city": "Москва", "destination.name": "Калининград",
                         "dates.start": "2027-01-01", "dates.nights": 2,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": budget})
    extra = [v for v in variants if v.id == "custom-3"]
    assert all(v.totals.rub <= budget for v in extra), [(v.id, v.totals.rub) for v in variants]
    if budget == 60000:
        assert extra, "a sufficient budget must keep the extra-time option available"
        selected = extra[0]
    else:
        selected = min(variants, key=lambda v: v.totals.rub)
        assert selected.totals.rub <= budget
    no_blocks(h, variants)
    response = h.act("choose_variant", variant_id=selected.id)
    assert response["cart"]["total_rub"] == selected.totals.rub
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "budget-quote@example.com"})
    response = h.act("pay", order_id=response["order"]["order_id"])
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("missing", [None, "out", "back", "both"])
def test_weekend_extra_time_requires_both_timed_transfer_quotes(live, monkeypatch, missing):
    from agent import steps

    h = live
    h.new(profile_id=None)
    original = steps._parallel
    injected = []

    def quote(ctx, jobs, *args, **kwargs):
        found = original(ctx, jobs, *args, **kwargs)
        for key, (tool, payload) in jobs.items():
            reverse = bool(payload.get("reverse"))
            unavailable = missing == "both" or (missing == "out" and not reverse) or (missing == "back" and reverse)
            if key.startswith("custom-3|") and tool == "search_transfers" and unavailable:
                found[key] = {"offers": []}
                injected.append(key)
        return found

    monkeypatch.setattr(steps, "_parallel", quote)
    _, variants = ask(h, "Из Москвы в Нижний Новгород вдвоем с 20 ноября на три ночи до 60000",
                      **{"origin.city": "Москва", "destination.name": "Нижний Новгород",
                         "dates.start": "2026-11-20", "dates.nights": 3,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000})
    extra = [v for v in variants if v.id == "custom-3"]
    if missing:
        assert injected
        assert not extra, "an absent timed quote cannot support the extra-time claim"
        selected = min(variants, key=lambda v: v.totals.rub)
    else:
        assert extra and extra[0].facts["destination_hours"] > 0
        assert sum(step.type == "transfer" for step in extra[0].steps) == 2
        selected = extra[0]
    no_blocks(h, variants)
    response = h.act("choose_variant", variant_id=selected.id)
    assert response["cart"]["total_rub"] == selected.totals.rub
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "missing-quote@example.com"})
    response = h.act("pay", order_id=response["order"]["order_id"])
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == selected.totals.rub


@pytest.mark.parametrize("live", ["stage5"], indirect=True)
@pytest.mark.parametrize("city,variant_id,missing_snapshot", [("Санкт-Петербург", "cheapest", False),
    ("Нижний Новгород", "cheapest", False), ("Нижний Новгород", "custom-3", False),
    ("Нижний Новгород", "cheapest", True)])
def test_weekend_paid_exports_include_the_upgraded_room(live, monkeypatch, city, variant_id, missing_snapshot):
    import copy
    import html
    from agent.server import Api

    h = live
    h.new(profile_id=None)
    _, variants = ask(h, f"Из Москвы в {city} вдвоем с 20 ноября на три ночи, романтика до 60000",
                      **{"origin.city": "Москва", "destination.name": city,
                         "dates.start": "2026-11-20", "dates.nights": 3,
                         "party.segment": "couple", "party.adults": 2, "budget_rub.max": 60000,
                         "mood.add": ["romantic"]})
    selected, extra = next((v, c) for v in variants for c in v.upsell_candidates
                           if v.id == variant_id and c.kind == "room_upgrade" and c.offer_id and c.price_rub > 0)
    base_offer, base_price = selected.hotel.offer_id, selected.totals.rub
    response = h.act("add_to_cart", variant_id=selected.id, extras=[extra.offer_id])
    total = base_price + extra.price_rub
    assert response["cart"]["total_rub"] == total
    assert [item["offer_id"] for item in response["cart"]["items"] if item["kind"] == "hotel"] == [extra.offer_id]
    response = h.act("checkout", travellers=TRAVELLERS, contact={"email": "paid-upgrade@example.com"})
    order_id = response["order"]["order_id"]
    call, called = h.ostrovok.call, []

    def record(tool, payload):
        called.append(tool)
        result = call(tool, payload)
        if missing_snapshot and tool == "order_get" and result.get("status") == "paid":
            result = copy.deepcopy(result)
            for item in result["items"]:
                if item["kind"] == "hotel":
                    item["details"].pop("offer", None)
        return result

    monkeypatch.setattr(h.ostrovok, "call", record)
    response = h.act("pay", order_id=order_id)
    assert response["order"]["status"] == "paid" and response["order"]["total_rub"] == total
    assert "pay" in called and not any(tool.startswith("search_") for tool in called)
    if missing_snapshot:
        assert response["files"] == {"ics": None, "page": None}
        assert h.session().variant(selected.id).hotel.offer_id == base_offer
        return
    order = h.ostrovok.call("order_get", {"order_id": order_id})
    api = Api(h.engine, debug=True, files_root=h.engine.files_dir)
    files = {}
    for kind in ("page", "ics"):
        assert response["files"][kind], response.get("debug")
        reply = api.dispatch("GET", response["files"][kind])
        assert reply.status == 200
        files[kind] = reply.data.decode("utf-8").replace("\r\n ", "")
    paid_price = f"{total:,}".replace(",", " ") + " ₽"
    assert f"<b>{paid_price}</b><span>итого</span>" in files["page"]
    room = next(item for item in order["items"] if item["kind"] == "hotel")["details"]["offer"]["room"]["name"]
    assert html.escape(room) in files["page"] and room in files["ics"]
    for item in order["items"]:
        assert item["offer_id"] in files["page"] and item["offer_id"] in files["ics"]
    assert base_offer not in files["page"] and base_offer not in files["ics"]
    assert h.session().variant(selected.id).hotel.offer_id == base_offer
    assert h.session().variant(selected.id).totals.rub == base_price
