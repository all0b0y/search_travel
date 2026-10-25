"""UI actions, compare, info, fallback, hooks/reruns, budget, Jev fail-open, errors, debug, prefetch."""
import json

import pytest

from agent.engine import BudgetExceeded
from agent.jev import FakeJev
from agent.llm import LLMError
from agent.steps import Ctx, Domain

from .fakes import FakeOstrovok, echo_present, fake_domain, u
from .test_entries import _buy, _to_selection

TRAVELLERS = [{"name": "Анна", "birth_date": "1990-01-01"}, {"name": "Иван", "birth_date": "1989-05-05"}]


def _destinations(h):
    h.understand(u(set={"traits.add": ["sea"], "dates.month": 11}))
    return h.turn("хочу к морю в ноябре")


# ---------------------------------------------------------------- actions

def test_answer_question_and_set_chip(h):
    r = _destinations(h)
    q = next(q for q in r["questions"] if q["field"] == "party.segment")
    calls = h.backend.count("understand")
    r = h.act("answer_question", question_id=q["id"], value="family")
    assert h.backend.count("understand") == calls          # no understand for actions
    s = h.session()
    assert s.brief.values()["party"]["segment"] == "family" and s.brief.source_of("party.segment") == "user"
    assert s.brief.values()["party"]["children"] == 1      # segment default (decision 2)
    assert r["stage"] == "inspiration" and r["cards"]["destinations"]
    r = h.act("set_chip", field="origin.city", value="Казань")
    assert h.session().brief.values()["origin"]["city"] == "Казань"


def test_choose_destination_then_variant_into_cart(h):
    r = _destinations(h)
    did = r["cards"]["destinations"][0]["id"]
    r = h.act("choose_destination", destination_id=did)
    assert r["stage"] == "selection" and len(r["cards"]["variants"]) == 3
    assert h.session().brief.values()["destination"]["id"] == did
    r = h.act("choose_variant", variant_id="cheapest")
    assert r["stage"] == "cart" and r["cart"]["total_rub"] == sum(i["price_rub"] for i in r["cart"]["items"])
    assert [a["type"] for a in r["actions"]] == ["checkout"]
    # a different variant replaces the cart content
    r2 = h.act("choose_variant", variant_id="comfort")
    v = h.session().variant("comfort")
    assert {i["offer_id"] for i in r2["cart"]["items"]} == {st.offer_id for st in v.steps}


def test_checkout_with_infant_rechecks_party(make):
    seen = []

    def check(variant, brief, rules):
        seen.append(brief.values()["party"])
        return []
    h = make(domain=Domain(**fake_domain(check_variant=check)))
    _to_selection(h)
    h.act("choose_variant", variant_id="fastest")
    travellers = TRAVELLERS + [{"name": "Малыш", "birth_date": "2026-03-01"}]
    r = h.act("checkout", travellers=travellers, contact={"phone": "+7000"})
    party = h.session().brief.values()["party"]
    assert party["children_ages"] == [0] and party["children"] == 1        # exact ages from checkout
    assert seen[-1]["children_ages"] == [0]                                # after_travellers re-check ran
    codes = {i["code"] for i in r["order"]["issues"]}
    # another party than the cart was priced for: the variant is re-priced for the travellers in this turn
    # (third level-3 run); the party_changed warning stays only when that re-price is impossible
    assert "infant_fare" in codes and ("party_changed" in codes or r["message"].startswith("Пересчитал на троих"))
    assert any(hk["event"] == "after_travellers" for hk in r["debug"]["hooks"])
    assert any(a["type"] == "pay" for a in r["actions"])


def test_checkout_blocking_issue_hides_pay(make):
    h = make(domain=Domain(**fake_domain(check_variant=lambda v, b, r: [
        {"code": "infant_bed", "severity": "block", "message": "Нет кроватки", "data": {}}]
        if (b.values()["party"].get("children_ages") or []) else [])))
    _to_selection(h)
    h.act("choose_variant", variant_id="fastest")
    r = h.act("checkout", travellers=TRAVELLERS + [{"name": "М", "birth_date": "2026-03-01"}], contact={"email": "x@y"})
    assert any(i["severity"] == "blocking" for i in r["order"]["issues"])
    assert not any(a["type"] == "pay" for a in r["actions"])


def test_full_path_and_files(h):
    r = _buy(h)
    assert r["files"]["ics"].endswith(".ics") and r["files"]["page"].endswith(".html")
    assert {a["payload"]["kind"] for a in r["actions"] if a["type"] == "order_action"} == {"early_checkin", "add_transfer"}
    early = next(a for a in r["actions"] if a["type"] == "order_action" and a["payload"]["kind"] == "early_checkin")
    r = h.turn(action={"type": "order_action", "payload": early["payload"]})
    assert r["stage"] == "aftercare" and h.ostrovok.count("order_request_early_checkin") == 1
    r = h.act("order_action", order_id=h.session().order_id, kind="add_transfer", params={})
    assert any(i["kind"] == "transfer" for i in r["order"]["items"])


def test_checkout_text_without_travellers_asks(h):
    _to_selection(h)
    h.act("choose_variant", variant_id="cheapest")
    h.understand(u(intent="checkout"))
    r = h.turn("оформляем")
    assert "дат" in r["message"] and any(a["type"] == "checkout" for a in r["actions"])
    assert h.ostrovok.count("checkout") == 0


def test_text_choose_variant_by_reference(h):
    _to_selection(h)
    h.understand(u(intent="cart", refs=[{"kind": "variant", "ref": "2"}]))
    r = h.turn("беру второй")
    assert r["stage"] == "cart" and h.session().chosen_variant_id == "fastest"


# ---------------------------------------------------------------- compare / info / fallback

def test_compare_action(h):
    _to_selection(h)
    r = h.act("compare", variant_ids=["cheapest", "comfort"])
    c = r["comparison"]
    assert c["variant_ids"] == ["cheapest", "comfort"]
    price = next(row for row in c["rows"] if row["key"] == "price")
    assert price["best_id"] == "cheapest" and set(price["values"]) == {"cheapest", "comfort"}
    assert h.backend.requests[-1].card == "present.compare"
    assert json.loads(h.backend.requests[-1].user)["dimensions"][0] == "price"   # assumed district is no priority


def test_info_from_tools_only(h):
    _to_selection(h)
    h.understand(u(intent="info", info="Можно ли с собакой в отель?"))
    r = h.turn("а с собакой в отель можно?")
    payload = json.loads(h.backend.requests[-1].user)
    kinds = {f["kind"] for f in payload["facts"]}
    assert {"hotel", "climate"} <= kinds and payload["question"].startswith("Можно")
    assert h.ostrovok.count("get_hotel") >= 1 and r["message"].startswith("Ответ")


def test_info_without_anything_known_has_empty_facts(h):
    h.understand(u(intent="info", info="Какая погода?"))
    h.turn("какая погода?")
    assert json.loads(h.backend.requests[-1].user)["facts"] == []


@pytest.mark.parametrize("intent,reason", [("smalltalk", "smalltalk"), ("out_of_scope", "out_of_scope")])
def test_fallback(h, intent, reason):
    h.understand(u(intent=intent))
    r = h.turn("расскажи анекдот")
    payload = json.loads(h.backend.requests[-1].user)
    assert payload["reason"] == reason and payload["message"] == "расскажи анекдот"
    assert r["cards"] == {"destinations": [], "variants": [], "hotels": []}


def test_unknown_destination_is_honest(h):
    h.understand(u(set={"destination.name": "Мухосранск"}))
    r = h.turn("хочу в Мухосранск")
    assert r["message"].startswith("«Мухосранск» в демо-каталоге пока нет")
    assert r["stage"] == "inspiration" and r["cards"]["destinations"]
    assert h.session().brief.values()["destination"] is None


def test_no_offers_is_no_data(make):
    h = make(ostrovok=FakeOstrovok(no_hotels=True))
    h.understand(u(set={"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19"}))
    r = h.turn("Сочи 12-19 ноября")
    assert r["error"] == {"code": "no_data", "retryable": False}
    assert "не нашлось" in r["message"] and not r["cards"]["variants"]


def test_tool_unavailable(make):
    h = make(ostrovok=FakeOstrovok(fail_tools={"search_hotels"}))
    h.understand(u(set={"destination.name": "Сочи", "dates.start": "2026-11-12", "dates.end": "2026-11-19"}))
    r = h.turn("Сочи 12-19 ноября")
    assert r["error"] == {"code": "tool_unavailable", "retryable": True}


def test_understand_llm_error_is_honest(h):
    h.understand("not json", "still not json")
    r = h.turn("привет")
    assert r["error"]["code"] == "invalid_model_output" and r["error"]["retryable"]
    h.understand(RuntimeError("down"))
    r = h.turn("привет")
    assert r["error"]["code"] == "model_unavailable"


def test_unknown_session_and_bad_request(h):
    r = h.engine.handle("s_nope", {"session_id": "s_nope", "text": "hi"})
    assert r["error"]["code"] == "session_not_found"
    r = h.engine.handle(h.sid, {"session_id": h.sid})
    assert r["error"]["code"] == "bad_request"


# ---------------------------------------------------------------- reruns, degradation, budget

def test_blocking_check_reruns_once_then_degrades(make):
    calls = []

    def build(brief, results, districts, rules, today):
        from .fakes import fake_build_variants
        calls.append(len(results["flights_out"]))
        return fake_build_variants(brief, results, districts, rules, today)

    def check(variant, brief, rules):
        return [{"code": "tight", "severity": "block", "message": "Стыковка впритык",
                 "data": {"offer_id": variant.steps[0].offer_id}}]
    h = make(domain=Domain(**fake_domain(build_variants=build, check_variant=check)))
    r = _to_selection(h)
    assert calls[1] < calls[0]                   # one rerun of build_variants without the failing offers
    s = h.session()
    assert any(n.code == "tight" for v in s.variants for n in v.risks)       # stated honestly
    assert r["debug"]["degraded"] is True


def test_card_guard_reruns_once_then_degrades(make):
    h = make(domain=Domain(**fake_domain(check_card_output=lambda c, i, o, r: [
        {"code": "invented_number", "severity": "block", "message": "число 999", "data": {}}]
        if c == "present.variants" else [])))
    r = _to_selection(h)
    assert h.backend.count("present.variants") == 2           # one rerun
    assert "invented_number" in h.backend.requests[-1].system
    assert r["debug"]["degraded"] and r["debug"]["budget"]["reruns"] == {"present.variants": 1}
    assert r["cards"]["variants"][0]["why"].startswith("«Дешевле»")       # code text, nothing invented
    assert r["message"].startswith("Советую «")              # code text: the recommended variant with its price


def test_present_llm_error_degrades(make):
    from agent.llm import FakeBackend
    h = make(backend=FakeBackend(scripts={"present.variants": [RuntimeError("down")]}))
    r = _to_selection(h)
    assert r["cards"]["variants"] and "error" not in r and r["debug"]["degraded"]


def test_budget_is_enforced(h):
    ctx = Ctx(engine=h.engine, session=h.session(), request={})
    ctx.flash_calls = 2
    with pytest.raises(BudgetExceeded):
        h.engine._call_card(ctx, "present.fallback", {})
    ctx.trace["budget"]["reruns"] = {"present.fallback": 1}
    with pytest.raises(BudgetExceeded):
        h.engine._call_card(ctx, "present.fallback", {}, rerun_issues=[{"code": "x", "message": "y"}])


# ---------------------------------------------------------------- Jev

def test_jev_routing_resolves_short_reply_without_flash(make):
    def answers(state, questions):
        opts = questions["route"]["criteria"]
        key = next(k for k in opts if k.startswith("q_party") and k.endswith("__2"))
        return {"route": {"type": "choice", "choice": key, "confidence": 0.95}}
    h = make(jev=FakeJev(answers=answers))
    _destinations(h)
    n = h.backend.count("understand")
    r = h.turn("с детьми")
    assert h.backend.count("understand") == n
    assert h.session().brief.values()["party"]["segment"] == "family"
    assert r["debug"]["jev"][0]["use"] == "routing" and r["debug"]["jev"][0]["accepted"]


def test_jev_routing_low_confidence_and_failure_fall_back_to_understand(make):
    h = make(jev=FakeJev(answers={"route": {"type": "choice", "choice": "none", "confidence": 0.99}}))
    _destinations(h)
    h.understand(u(intent="answer", set={"party.segment": "solo"}))
    h.turn("один")
    assert h.backend.count("understand") == 2
    h2 = make(jev=FakeJev(fail=True))
    _destinations(h2)
    h2.understand(u(intent="answer", set={"party.segment": "solo"}))
    r = h2.turn("один")
    assert h2.backend.count("understand") == 2 and r["debug"]["jev"][0]["ok"] is False


def test_jev_ask_or_assume_drops_safe_question(make):
    def answers(state, questions):
        return {k: {"type": "noul", "noul": 0.95} for k in questions if k.startswith("q_")} or \
            {k: {"type": "noul", "noul": 0.1} for k in questions}
    h = make(jev=FakeJev(answers=answers))
    r = _destinations(h)
    assert not any(q["field"] == "party.segment" for q in r["questions"])
    assert h.session().brief.source_of("party.segment") == "inferred"
    assert any(c["field"] == "party.segment" for c in r["chips"])


def test_jev_guard_blocks_then_rerun(make):
    state = {"n": 0}

    def answers(st, questions):
        if "unsupported" in questions:
            state["n"] += 1
            return {"unsupported": {"type": "noul", "noul": 0.9 if state["n"] == 1 else 0.1}}
        return None
    h = make(jev=FakeJev(answers=answers))
    r = _to_selection(h)
    assert h.backend.count("present.variants") == 2 and not r["debug"]["degraded"]


def test_stage1_has_no_chips_or_sensitivity_questions(make):
    h = make(flags="stage1")
    r = _destinations(h)
    assert r["chips"] == [] and r["questions"] == []


# ---------------------------------------------------------------- debug, prefetch, analytics

def test_debug_only_when_requested(h):
    sid = h.new(debug=False)
    h.understand(u(intent="smalltalk"))
    r = h.turn("привет", sid=sid)
    assert "debug" not in r
    sid = h.new(debug=True)
    h.understand(u(intent="smalltalk"))
    r = h.turn("привет", sid=sid)
    d = r["debug"]
    assert d["stage_path"] == ["inspiration", "inspiration"] and d["flags"]["preset"] == "stage5"
    call = d["calls"][0]
    assert {"card", "model", "provider", "tokens", "latency_ms", "cost"} <= set(call)


def test_prefetch_makes_next_search_cached(h):
    r = _destinations(h)
    leader = r["cards"]["destinations"][0]["id"]
    before = len(h.ostrovok.calls)
    r = h.act("choose_destination", destination_id=leader)
    new = [t for t, p in h.ostrovok.calls[before:]
           if t in ("search_flights", "search_hotels") and not p.get("hotel_id")]   # hotel_id: upsell rooms
    assert new == [] and r["cards"]["variants"]
    assert any("prefetch: merged" in n for n in r["debug"]["notes"])


def test_analytics_events_have_no_personal_data(h):
    _buy(h)
    assert h.ostrovok.events
    for e in h.ostrovok.events:
        assert not ({"name", "email", "phone", "birth_date", "session_id", "order_id"} & set(e["data"]))


def test_history_and_pending_questions(h):
    _destinations(h)
    s = h.session()
    assert s.history.user == ["хочу к морю в ноябре"] and len(s.history.agent) == 1
    assert s.pending_questions and s.pending_questions[0].field == "party.segment"
    h.understand(u(intent="answer", set={"party.segment": "solo"}))
    h.turn("один")
    payload = json.loads(h.backend.requests[-2].user)          # the understand input of turn 2
    assert payload["history"][0] == {"role": "user", "text": "хочу к морю в ноябре"}
    assert payload["pending_questions"][0]["field"] == "party.segment"
    assert echo_present  # keep import used
