"""Profile history is private, backward compatible and recorded only after success."""
from agent.profiles import PastTrip, Profile, ProfileStore
from agent.views import for_profile, record

from .fakes import u
from .test_scopes import ask, live  # noqa: F401 - the fixture


def test_old_profile_json_and_paid_order_deduplication():
    store = ProfileStore(":memory:")
    profile = Profile.model_validate_json(
        '{"id":"old","past_trips":[{"destination_id":"kazan","month":6,"year":2025,"nights":3}]}')
    store.put(profile)
    assert store.get("old").past_trips[0].price_rub is None
    trip = PastTrip(order_id="o_one", price_rub=42000, party={"adults": 2}, stars=4,
                    transport="train", destination_kind="city", paid_at="2026-10-24T12:00:00+03:00")
    assert store.add_paid_trip("old", trip) is True
    assert store.add_paid_trip("old", trip) is False
    assert store.add_paid_trip("unknown", trip) is False
    assert store.get("old").past_trips == [profile.past_trips[0], trip]
    store.close()


def test_view_history_is_scoped_to_profile_and_persists():
    record("a", [("hotel", "h1", "opened"), ("hotel", "h1", "opened")], "2026-10-24T12:00:00+03:00")
    record("b", [("destination", "kazan", "shown")], "2026-10-24T12:00:00+03:00")
    assert len(for_profile("a")) == 1
    assert for_profile("a")[0]["ref_id"] == "h1"
    assert for_profile("b")[0]["ref_id"] == "kazan"


def trip(h):
    return ask(h, "В Казань поездом, 4–7 декабря, я один",
               **{"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-12-04",
                  "dates.end": "2026-12-07", "party.adults": 1, "party.segment": "solo", "prefs.transport": "train"})


def test_turns_record_shown_opened_carted_and_paid_once(live):
    h = live
    response, variants = trip(h)
    shown = for_profile("demo")
    assert {r["ref_id"] for r in shown if r["kind"] == "variant"} == {v.id for v in variants}
    assert all(r["source"] == "shown" for r in shown)
    assert all(r["at"] == h.pinned_now.isoformat() for r in shown)
    variant = variants[0]
    assert any(r["kind"] == "hotel" and r["ref_id"] == variant.hotel.hotel_id for r in shown)
    h.act("choose_variant", variant_id=variant.id)
    views = for_profile("demo")
    assert {r["source"] for r in views if r["kind"] == "variant" and r["ref_id"] == variant.id} == {
        "shown", "opened", "carted"}
    assert len([r for r in views if r["source"] == "shown"]) == len(shown)  # cart is not a new selection
    result = h.act("checkout", travellers=[{"name": "Иван Петров", "birth_date": "1990-01-01"}],
                   contact={"email": "a@b.c"})
    assert not h.engine.profiles.get("demo").past_trips
    order_id = result["order"]["order_id"]
    result = h.act("pay", order_id=order_id)
    assert not result.get("error")
    history = h.engine.profiles.get("demo").past_trips
    assert len(history) == 1
    saved = history[0]
    assert saved.order_id == order_id and saved.price_rub == result["order"]["total_rub"]
    assert saved.nights == 3 and saved.party["adults"] == 1 and saved.transport == "train"
    assert saved.destination_id == "kazan" and saved.destination_kind is not None
    assert saved.stars == variant.hotel.stars and saved.paid_at == h.pinned_now
    h.act("pay", order_id=order_id)
    assert h.engine.profiles.get("demo").past_trips == history
    assert all(hook["verdict"] == "ok" for hook in h.session().trace[-1]["hooks"]
               if hook["name"] == "profile_history")


def test_failed_and_anonymous_turns_are_not_recorded(live):
    h = live
    h.act("choose_variant", variant_id="custom-999")
    assert for_profile("demo") == []
    h.new(profile_id=None)
    trip(h)
    assert for_profile("demo") == [] and for_profile(None) == []


def test_destination_choice_and_hotel_question_are_opened_events(live):
    h = live
    h.understand(u(set={"origin.city": "Москва", "dates.month": 12, "dates.nights": 3,
                        "party.adults": 1, "traits.add": ["city"]}))
    result = h.turn("Куда-нибудь в город на три ночи в декабре, я один")
    assert result["cards"]["destinations"]
    dest = result["cards"]["destinations"][0]["id"]
    assert any(r["kind"] == "destination" and r["ref_id"] == dest and r["source"] == "shown"
               for r in for_profile("demo"))
    result = h.act("choose_destination", destination_id=dest)
    assert not result.get("error")
    assert any(r["ref_id"] == dest and r["source"] == "opened" for r in for_profile("demo"))
    hotel_id = h.session().variants[0].hotel.hotel_id
    h.understand(u(intent="info", refs=[{"kind": "variant", "ref": h.session().variants[0].id}],
                    info="Что есть в этом отеле?"))
    h.turn("Что есть в этом отеле?")
    assert any(r["kind"] == "hotel" and r["ref_id"] == hotel_id and r["source"] == "opened"
               for r in for_profile("demo"))
