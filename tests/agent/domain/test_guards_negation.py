"""change_claim negations (agent/guards.py, shared by the pipeline's cards and the guide): a claim phrase in a
sentence that denies it («прямых рейсов нет», «без прямых», «прямыми не долететь», «только с пересадкой») is no claim;
the same phrase stated as done without the evidence is still blocked — for every kind of the CLAIMS table.
"""
import pytest

from agent.guards import change_claim_issues


def item(vid="cheapest", stops=1, **facts):
    return {"id": vid, "steps": [{"type": "flight"}, {"type": "stay"}, {"type": "flight"}],
            "facts": {"out_kind": "flight", "out_stops": stops, "back_kind": "flight", "back_stops": stops, **facts}}


def kinds(message, card="present.variants", items=None, **extra):
    inp = {"items": items if items is not None else [item()], "brief": {"prefs": {}}, **extra}
    return [i["data"]["kind"] for i in change_claim_issues(card, inp, {"message": message, "items": []})
            if i["code"] == "change_claim"]


@pytest.mark.parametrize("message", [
    "Прямых рейсов из Новосибирска в Калининград в июне нет.",
    "Нет прямых рейсов в эти даты — только с пересадкой, от 36 370 ₽.",
    "Без прямых рейсов: туда летим с пересадкой в Москве.",
    "Прямыми рейсами туда в июне не долететь.",
    "Прямые рейсы туда не летают, зато с пересадкой — от 36 370 ₽.",
    "Прямыми рейсами — только с пересадкой в Москве.",
    "Прямыми рейсами туда никак.",
    "Долететь прямыми рейсами в эти даты невозможно.",
    "Прямые рейсы в данных отсутствуют.",
])
def test_a_denied_direct_flight_is_no_claim(message):
    assert kinds(message) == []


@pytest.mark.parametrize("message", ["Собрал с прямыми рейсами — так быстрее.", "Летите прямым рейсом, 3,1 ч.",
                                     "Все варианты без пересадок."])
def test_a_false_direct_claim_is_still_blocked(message):
    assert kinds(message) == ["direct"]
    assert kinds(message, items=[item(stops=0)]) == []          # true when the shown variant is direct


@pytest.mark.parametrize("kind,claim,denied,ok_facts", [
    ("kitchen", "Номер с кухней — готовьте сами.", "Номеров с кухней в этом отеле нет.", {"kitchen": True}),
    ("cot", "Детская кроватка есть, бесплатно.", "Кроватку отель не гарантирует.", {"infant_cot": "free"}),
    ("step_free", "Отель без ступенек, удобно с коляской.", "Без ступенек вариантов нигде нет.", {"step_free": True}),
    ("lift", "В отеле есть лифт.", "Лифта в данных нет, есть лифт или нет — неизвестно.", {"lift": True}),
])
def test_state_claims_both_ways(kind, claim, denied, ok_facts):
    assert kinds(claim) == [kind]
    assert kinds(denied) == []
    assert kinds(claim, items=[item(**ok_facts)]) == []


def test_night_claims_both_ways():
    night = [item(out_night=True)]
    assert kinds("Все рейсы без ночных перелётов.", items=night) == ["night"]
    assert kinds("Без ночных рейсов собрать не получилось.", items=night) == []
    assert kinds("Все рейсы без ночных перелётов.", items=[item()]) == []
    removed = "Убрал все ночные рейсы."
    assert kinds(removed, card="present.update", items=[item()]) == ["night_removed"]
    assert kinds("Убрал все ночные рейсы — не вышло, их нет в других датах.", card="present.update",
                 items=[item()]) == []
    assert kinds(removed, card="present.update", items=[item()],
                 changes=[{"field": "prefs.no_night_flights", "before": None, "after": True}]) == []


@pytest.mark.parametrize("kind,claim,denied,field", [
    ("dates_moved", "Перенёс поездку на 10 декабря.", "Перенести на 10 декабря нельзя — рейсов нет.", "dates.start"),
    ("party", "Пересчитал цены на троих.", "Пересчитал на троих — не вышло, мест нет.", "party.adults"),
])
def test_change_claims_both_ways(kind, claim, denied, field):
    assert kinds(claim, card="present.update") == [kind]
    assert kinds(denied, card="present.update") == []
    assert kinds(claim, card="present.update", changes=[{"field": field, "before": 1, "after": 2}]) == []


def test_negative_claims_keep_their_evidence_rule():
    # «нет аэропорта» is itself a negative claim: it needs dest_airports false, a negation does not excuse it
    assert kinds("У Зеленоградска нет своего аэропорта.") == ["no_airport"]
    assert kinds("У Зеленоградска нет своего аэропорта.", items=[item(dest_airports=False)]) == []
    assert kinds("Билеты я пока не искал.", items=[item(transport_gap="none")]) == ["no_tickets"]
    assert kinds("Билеты я пока не искал.") == []
