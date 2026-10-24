"""Stores, hooks, prefetcher, payload helpers and Jev policy units."""
import threading
import time

import pytest

from agent.hooks import Hooks, as_issue
from agent.jev import FakeJev
from agent.jev_policy import ask_or_assume, guard_card_output, is_short_reply, route_short_reply
from agent.payloads import assumed_paths, build_chips, build_question, history_items, party_label, record_shown
from agent.prefetch import Prefetcher
from agent.profiles import DEMO_PROFILE, Profile, ProfileStore, apply_profile
from agent.sessions import SessionStore
from agent.state import Brief, Session, apply_patch, assume

from .conftest import skill


def test_session_store_round_trip_and_threads(tmp_path, pinned_now):
    store = SessionStore(tmp_path / "s.sqlite")
    s = Session.new(now=pinned_now, debug=True)
    s.brief, _ = apply_patch(s.brief, {"set": {"destination.name": "Сочи"}})
    store.put(s)
    assert store.get(s.id) == s and store.exists(s.id) and store.get("s_none") is None

    def put_many(n):
        for i in range(20):
            x = Session.new(now=pinned_now, session_id=f"s_{n}_{i}")
            store.put(x)
    threads = [threading.Thread(target=put_many, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store.ids()) == 81
    store.close()
    assert SessionStore(tmp_path / "s.sqlite").get(s.id) == s


def test_profiles_demo_and_apply():
    store = ProfileStore(":memory:")
    demo = store.get("demo")
    assert demo.home_city == "Москва" and store.get(None) is None and store.get("x") is None
    store.put(Profile(id="p1", home_city="Казань", preferences={"meals": "breakfast"}))
    b = apply_profile(Brief(), store.get("p1"))
    assert b.values()["origin"]["city"] == "Казань" and b.source_of("origin") == "profile"
    assert b.values()["prefs"]["meals"] == "breakfast" and b.source_of("prefs.meals") == "profile"
    user, _ = apply_patch(Brief(), {"set": {"origin.city": "Сочи", "prefs.meals": "none"}})
    b = apply_profile(user, store.get("p1"))
    assert b.values()["origin"]["city"] == "Сочи" and b.values()["prefs"]["meals"] == "none"
    assert apply_profile(Brief(), DEMO_PROFILE).values()["origin"]["iata"] == ["SVO", "DME", "VKO"]


def test_hooks_verdicts_and_isolation():
    hooks = Hooks()

    class C:
        trace = {"hooks": []}
    hooks.register("after_variants", lambda c: [{"code": "x", "severity": "warning", "message": "m"}], "validate")
    hooks.register("after_variants", lambda c: 1 / 0, "analytics", "boom")
    issues = hooks.run("after_variants", C)
    assert issues == [{"code": "x", "severity": "warn", "message": "m", "data": {}}]
    assert [r["verdict"] for r in C.trace["hooks"]] == ["warn", "error"]
    with pytest.raises(ValueError):
        hooks.register("nope", print, "validate")
    h2 = Hooks()
    h2.register("turn_end", lambda c: 1 / 0, "validate")
    with pytest.raises(ZeroDivisionError):     # a validate hook that crashes is a programming error
        h2.run("turn_end", C)
    assert as_issue({"code": "b", "severity": "blocking"})["severity"] == "block"


def test_prefetcher_background_and_dedupe():
    pf = Prefetcher(max_workers=2)
    assert pf.submit("s1", "k", lambda: (time.sleep(0.05), None)[1])
    assert not pf.submit("s1", "k", lambda: None)
    pf.submit("s1", "boom", lambda: 1 / 0)
    pf.wait("s1", 5)
    assert "boom" in pf.errors("s1") and pf.drain("s1") == {}
    pf.shutdown()
    assert not pf.submit("s2", "k", lambda: None)


def test_assumed_paths_and_chips():
    sk = skill()
    b = assume(Brief(), "origin", {"city": "Москва", "iata": []}, "profile")
    b = assume(b, "party.segment", "couple", "default")
    b = assume(b, "party.adults", 2, "default")
    b, _ = apply_patch(b, {"set": {"prefs.meals": "breakfast"}})
    paths = assumed_paths(b)
    assert {"origin.city", "party.segment", "party.adults"} <= set(paths) and "prefs.meals" not in paths
    chips = build_chips(sk, b, exclude_fields={"party.segment"})
    assert [c.field for c in chips] == ["origin.city", "party.adults"]


def test_build_question_recommendation_order():
    sk = skill()
    q = build_question(sk, "dates.nights", profile_value=None, pattern_value=6)
    assert [c.value for c in q.choices if c.recommended] == [5]          # nearest to the pattern
    q = build_question(sk, "dates.nights", profile_value=10, pattern_value=6)
    assert [c.value for c in q.choices if c.recommended] == [10]         # profile wins
    q = build_question(sk, "party.children_ages")
    assert not any(c.recommended for c in q.choices)                     # default null


def test_history_interleaves_and_labels(pinned_now):
    s = Session.new(now=pinned_now)
    for i in range(7):
        s.remember_user(f"u{i}")
        s.remember_agent(f"a{i}")
    items = history_items(s)
    assert len(items) == 9 and items[-1] == {"role": "agent", "text": "a6"} and items[-2]["text"] == "u6"
    assert record_shown(s, "variant", [("cheapest", "Дешевле"), ("comfort", "Комфортнее")]) == {
        "cheapest": "1", "comfort": "2"}
    assert s.find_shown("variant", "2").id == "comfort"
    assert party_label({"adults": 2, "children": 3}) == "2 взрослых и 3 ребёнка"


def test_jev_policy_fail_open_and_thresholds(pinned_now):
    sk = skill()
    s = Session.new(now=pinned_now)
    q = build_question(sk, "party.segment")
    s.pending_questions = [q]
    trace = []
    assert is_short_reply("вдвоём", sk) and not is_short_reply("мы поедем вдвоём с женой в ноябре точно", sk)
    assert route_short_reply(FakeJev(fail=True).decide, s, "вдвоём", sk, trace) is None and trace[-1]["ok"] is False
    assert trace[-1]["answers"] is None
    assert route_short_reply(lambda *a, **k: 1 / 0, s, "вдвоём", sk, trace) is None and "error" in trace[-1]
    pick = {"route": {"type": "choice", "choice": "q_party__1", "confidence": 0.7}}
    assert route_short_reply(FakeJev(answers=pick).decide, s, "вдвоём", sk, trace) is None   # below 0.8
    assert trace[-1]["answers"] == pick and trace[-1]["accepted"] is False
    pick["route"]["confidence"] = 0.9
    assert route_short_reply(FakeJev(answers=pick).decide, s, "вдвоём", sk, trace) == {
        "type": "answer_question", "payload": {"question_id": "q_party", "value": "couple"}}
    # segment criteria from segments.json reach Jev
    jev = FakeJev(answers=pick)
    route_short_reply(jev.decide, s, "вдвоём", sk, trace)
    assert sk.segments["couple"]["jev_criteria"] in jev.calls[0]["questions"]["route"]["criteria"]["q_party__1"]
    ask, assumed = ask_or_assume(FakeJev(fail=True).decide, Brief(), [q], sk, trace)
    assert ask == [q] and assumed == []
    ask, assumed = ask_or_assume(FakeJev(answers={"q_party": {"type": "noul", "noul": 0.9}}).decide, Brief(), [q], sk,
                                 trace)
    assert ask == [] and assumed == [("party.segment", "couple")]
    assert guard_card_output(FakeJev(fail=True).decide, "present.info", {}, {"message": "x"}, sk, trace) == []
    out = guard_card_output(FakeJev(answers={"unsupported": {"type": "noul", "noul": 0.6}}).decide, "present.info", {},
                            {"message": "x"}, sk, trace)
    assert out and out[0]["severity"] == "block"


def test_offer_ids_of_follows_the_step_of_an_issue():
    from types import SimpleNamespace
    from agent.steps import _offer_ids_of
    v = SimpleNamespace(id="cheapest", steps=[SimpleNamespace(id="s1", offer_id="fl-1"),
                                               SimpleNamespace(id="s3", offer_id="ho-9")])
    issues = [{"code": "nights", "severity": "block", "data": {"variant_id": "cheapest", "step": "s3"}},
              {"code": "budget", "severity": "block", "data": {"variant_id": "cheapest", "offer_id": "x"}}]
    assert _offer_ids_of(issues, [v]) == {"ho-9", "x"}


def test_attention_skips_info_and_joins_readably():
    from agent.steps import attention
    issues = [{"severity": "info", "message": "Отправление в 10:10, до расчётного часа 12:00"},
              {"severity": "warning", "message": "Между посадкой и трансфером 20 мин — нужно не меньше 45."},
              {"severity": "blocking", "message": "Трансфер подаётся раньше посадки самолёта"}]
    assert attention(issues) == ("между посадкой и трансфером 20 мин — нужно не меньше 45; "
                                 "трансфер подаётся раньше посадки самолёта.")
    assert attention([issues[0]]) == ""


def test_calm_risks_drops_emoji_only_from_risk_sentences():
    from agent.respond import calm_risks
    text = ("Советую «Быстрее»: 105 080 ₽ и бесплатная отмена 🌴 У «Дешевле» пересадка всего 40 минут 🌴 "
            "Хорошей поездки! ☀️")
    assert calm_risks(text) == ("Советую «Быстрее»: 105 080 ₽ и бесплатная отмена 🌴 У «Дешевле» пересадка всего 40 минут "
                                "Хорошей поездки! ☀️")
    assert calm_risks("Обратный вылет ночью, в 05:10 😴.") == "Обратный вылет ночью, в 05:10."
    assert calm_risks("Без эмодзи.") == "Без эмодзи."


def test_dates_changeable_only_with_exchangeable_flights():
    from types import SimpleNamespace
    from agent.steps import _dates_changeable
    fixed = {"kind": "flight", "status": "confirmed", "details": {"offer": {"fare": {"change_fee_rub": None}}}}
    flex = {"kind": "flight", "status": "confirmed", "details": {"offer": {"fare": {"change_fee_rub": 2500}}}}
    hotel = {"kind": "hotel", "status": "confirmed", "details": {}}
    assert not _dates_changeable(SimpleNamespace(order={"items": [fixed, hotel]}))
    assert _dates_changeable(SimpleNamespace(order={"items": [flex, hotel]}))
    assert _dates_changeable(SimpleNamespace(order={"items": [hotel]}))
