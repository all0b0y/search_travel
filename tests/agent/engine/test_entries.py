"""Entries A-F through Engine.handle with fakes (no network, no model)."""
from .fakes import u

DATA_URL = "data:image/jpeg;base64,AAAA"


def image(want="here", name=None, conf=0.0, landscape=("beach",), water="sea", climate="warm"):
    place = {"name": name, "country": None, "confidence": conf} if name else None
    return {"want": want, "place": place, "traits": {"landscape": list(landscape), "water": water, "climate": climate,
                                                      "setting": "resort", "vibe": ["relax"]}}


def test_entry_c_three_variants(h):
    h.understand(u(set={"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
                        "party.adults": 2}))
    r = h.turn("Сочи, 12–19 ноября, двое взрослых")
    assert r["stage"] == "selection"
    assert [v["id"] for v in r["cards"]["variants"]] == ["cheapest", "fastest", "comfort"]
    assert [v["label"] for v in r["cards"]["variants"]] == ["1", "2", "3"]
    assert sum(v["recommended"] for v in r["cards"]["variants"]) == 1
    v = r["cards"]["variants"][0]
    assert v["totals"]["rub"] == sum(s["price_rub"] for s in v["steps"])     # numbers from code
    s = h.session()
    assert s.brief.values()["party"]["segment"] == "couple"                 # inferred from 2 adults
    assert s.brief.source_of("party.segment") == "inferred"
    assert s.brief.values()["origin"]["city"] == "Москва" and s.brief.source_of("origin") == "profile"
    assert [i.label for i in s.shown.variants] == ["1", "2", "3"]
    assert {t for t in h.ostrovok.tools_called()} >= {"search_flights", "search_hotels", "search_transfers"}
    assert any(a["type"] == "choose_variant" for a in r["actions"])
    assert r["debug"]["calls"][0]["card"] == "understand" and len(r["debug"]["calls"]) == 2
    assert h.backend.count() == 2


def test_entry_a_destinations(h):
    h.understand(u(set={"traits.add": ["sea", "warm"], "dates.month": 11, "party.segment": "family",
                        "budget_rub.max": 150000}))
    r = h.turn("к тёплому морю в ноябре с ребёнком, недорого")
    assert r["stage"] == "inspiration"
    cards = r["cards"]["destinations"]
    assert {c["id"] for c in cards} == {"sochi", "antalya"}
    assert all(c["price_from_rub"] for c in cards) and cards[0]["label"] == "1"
    assert cards[0]["party_label"] == "2 взрослых и ребёнок"
    s = h.session()
    party = s.brief.values()["party"]
    assert party["children"] == 1 and party["children_ages"] and s.brief.source_of("party.children_ages") == "default"
    assert [a["type"] for a in r["actions"]].count("choose_destination") == 2
    assert r["chips"], "stage5: assumptions are chips"
    assert {c["field"] for c in r["chips"]} >= {"origin.city"}
    card_input = h.backend.requests[-1].user
    assert '"assumed"' in card_input and '"assumptions"' in card_input


def test_entry_a_image_confident_place_goes_to_variants(h):
    h.understand(u(image=image(name="Сочи", conf=0.95)))
    r = h.turn(image=DATA_URL)
    assert r["stage"] == "selection" and r["cards"]["variants"]
    s = h.session()
    assert s.brief.values()["destination"]["id"] == "sochi"
    assert s.brief.source_of("destination") == "inferred"
    assert h.backend.requests[0].images == [DATA_URL]


def test_entry_a_image_mid_confidence_asks_to_confirm(h):
    h.understand(u(image=image(name="Сочи", conf=0.6)))
    r = h.turn(image=DATA_URL)
    assert r["stage"] == "inspiration"
    assert r["cards"]["destinations"][0]["id"] == "sochi"
    q = r["questions"][0]
    assert q["field"] == "destination.name" and q["choices"][0]["value"] == "Сочи" and q["choices"][0]["recommended"]
    assert "Сочи" in q["text"]
    # answering "yes" goes to variants
    r2 = h.act("answer_question", question_id=q["id"], value="Сочи")
    assert r2["stage"] == "selection" and r2["cards"]["variants"]


def test_entry_a_image_similar_never_route_b(h):
    h.understand(u(image=image(want="similar", name="Сочи", conf=0.95)))
    r = h.turn("хочу туда, где так же", image=DATA_URL)
    assert r["stage"] == "inspiration" and r["cards"]["destinations"][0]["id"] == "sochi"
    assert not any(q["field"] == "destination.name" for q in r["questions"])


def test_entry_a_image_traits_only(h):
    h.understand(u(image=image(conf=0.0)))
    r = h.turn(image=DATA_URL)
    assert r["stage"] == "inspiration"
    s = h.session()
    assert {"beach", "sea", "warm"} <= set(s.brief.values()["traits"])
    assert s.brief.source_of("traits") == "inferred"
    assert "relax" in s.brief.values()["mood"]


def test_entry_b_dates_inferred_from_quote(h):
    h.understand(u(set={"destination.name": "Сочи", "dates.month": 11}))
    r = h.turn("хочу в Сочи в ноябре")
    assert r["cards"]["variants"]
    d = h.session().brief.values()["dates"]
    assert d["start"] == "2026-11-12" and d["end"] == "2026-11-19"
    assert h.session().brief.source_of("dates.start") == "inferred"


def test_entry_d_scope_stay_only(h):
    h.understand(u(set={"destination.name": "Казань", "dates.nights": 3, "scope": ["stay"]}))
    r = h.turn("отель в Казани на 3 ночи")
    assert r["cards"]["variants"]
    assert set(h.ostrovok.tools_called()) <= {"quote_destinations", "search_hotels", "get_hotel", "insights_segment_preferences",
                                               "insights_log_event"}
    assert any(c["field"] == "scope" and {"label": "отель + дорога + трансфер",
                                          "value": ["flights", "stay", "transfers"]} in c["options"]
               for c in r["chips"])
    assert all(s["type"] == "stay" for v in r["cards"]["variants"] for s in v["steps"])


def _to_selection(h):
    h.understand(u(set={"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19",
                        "party.adults": 2}))
    return h.turn("Сочи, 12–19 ноября, двое взрослых")


def test_entry_e_shift_days_against_shown_dates(h):
    _to_selection(h)
    h.understand(u(intent="refine", set={"dates.shift_days": 2}))
    before = len(h.ostrovok.calls)
    r = h.turn("а если на 2 дня позже")
    d = h.session().brief.values()["dates"]
    assert (d["start"], d["end"]) == ("2026-11-14", "2026-11-21")
    assert r["cards"]["variants"] and h.backend.requests[-1].card == "present.update"
    called = {t for t, _ in h.ostrovok.calls[before:]}
    assert {"search_flights", "search_hotels"} <= called
    import json
    payload = json.loads(h.backend.requests[-1].user)
    assert payload["changes"][0] == {"field": "dates.shift_days", "before": 0, "after": 2}
    assert {"flights", "hotels"} <= set(payload["rebuilt"])


def test_entry_e_shift_without_anything_shown_asks(h):
    h.understand(u(intent="refine", set={"dates.shift_days": 2}))
    r = h.turn("на 2 дня позже")
    assert "дат" in r["message"] and not r["cards"]["variants"]
    assert h.backend.count() == 1


def test_entry_e_dog_reruns_only_hotels_and_transfers(h):
    _to_selection(h)
    h.understand(u(intent="refine", set={"party.pets": ["dog"], "constraints.add": ["pet:dog"]}))
    before = len(h.ostrovok.calls)
    r = h.turn("с собакой")
    called = {t for t, _ in h.ostrovok.calls[before:]}
    assert "search_hotels" in called and "search_flights" not in called
    assert called <= {"search_hotels", "search_transfers", "insights_log_event", "insights_segment_preferences"}
    assert all(v["hotel"]["hotel_id"].endswith("h2") for v in r["cards"]["variants"])   # only pet-friendly


def test_entry_e_budget_down_reuses_cache(h):
    _to_selection(h)
    h.understand(u(intent="refine", set={"budget_rub.max": 60000}))
    before = len(h.ostrovok.calls)
    r = h.turn("подешевле, до 60 тысяч")
    searches = [t for t, _ in h.ostrovok.calls[before:] if t.startswith("search_")]
    assert searches == [] and r["cards"]["variants"]


def _buy(h):
    _to_selection(h)
    r = h.act("choose_variant", variant_id="fastest")
    assert r["stage"] == "cart" and r["cart"]["items"]
    r = h.act("checkout", travellers=[{"name": "Анна", "birth_date": "1990-01-01"},
                                       {"name": "Иван", "birth_date": "1989-05-05"}], contact={"email": "a@b.c"})
    assert r["stage"] == "checkout" and r["order"]["status"] == "pending_payment"
    r = h.act("pay", order_id=r["order"]["order_id"])
    assert r["stage"] == "ordered" and r["order"]["status"] == "paid"
    return r


def test_entry_f_early_checkin_after_purchase(h):
    _buy(h)
    h.understand(u(intent="post_purchase", pp={"kind": "early_checkin", "params": {}}))
    r = h.turn("можно заселиться пораньше?")
    assert r["stage"] == "aftercare"
    assert h.ostrovok.count("order_request_early_checkin") == 1
    assert h.backend.requests[-1].card == "present.aftercare"
    assert any(e["kind"] == "early_checkin_requested" for e in r["order"]["events"])
