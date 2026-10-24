"""Cart -> checkout issues -> pay -> order operations over the runtime state DB."""
from datetime import datetime, timedelta

import pytest

from ostrovok_mcp import contract as c
from ostrovok_mcp import service as s
from ostrovok_mcp.db import ServiceError

TODAY = "2026-10-25"
NOW = "2026-10-25T13:05:00+03:00"
ADULTS = [{"name": "Иван Петров", "birth_date": "1988-04-02"}, {"name": "Мария Петрова", "birth_date": "1990-07-19"}]
CONTACT = {"email": "ivan@example.com"}


def flight(o, d, day, tier_kg: int | None = 23, refundable=False, **party):
    payload = {"origin_iata": [o], "dest_iata": [d], "date": day, "adults": 2, "today": TODAY, **party}
    offers = s.search_flights(payload)["offers"]
    return next(x for x in offers if x["baggage"]["checked_kg"] == tier_kg and x["fare"]["refundable"] == refundable)


def hotel(hotel_id="h_sochi_1", ci="2026-11-14", co="2026-11-21", adults=2, ages=()):
    offers = s.search_hotels({"destination_id": "sochi", "check_in": ci, "check_out": co, "adults": adults,
                              "children_ages": list(ages), "today": TODAY})["offers"]
    return next(x for x in offers if x["hotel_id"] == hotel_id)


def transfer(at, reverse=False, pax=2, kind="taxi"):
    offers = s.search_transfers({"from": {"iata": "AER"}, "to": {"hotel_id": "h_sochi_1"}, "at": at, "pax": pax,
                                 "reverse": reverse, "kinds": [kind], "today": TODAY})["offers"]
    return offers[0]


def add(session, offer, kind):
    cart = s.cart_add({"session_id": session, "offer_id": offer["offer_id"], "kind": kind, "now": NOW})
    c.Cart.model_validate(cart)
    return cart


def codes(issues):
    return {i["code"] for i in issues}


def iso_shift(value, minutes):
    return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


@pytest.fixture
def trip(mock_env):
    out = flight("SVO", "AER", "2026-11-14")
    back = flight("AER", "SVO", "2026-11-21")
    stay = hotel()
    return {"out": out, "back": back, "stay": stay}


def test_cart_create_is_idempotent_per_session(mock_env):
    a = s.cart_create({"session_id": "s1", "now": NOW})
    b = s.cart_create({"session_id": "s1", "now": "2026-10-25T14:00:00+03:00"})
    assert a["cart_id"] == b["cart_id"] and a["items"] == [] and a["total_rub"] == 0
    assert s.cart_create({"session_id": "s2", "now": NOW})["cart_id"] != a["cart_id"]
    with pytest.raises(ServiceError):
        s.cart_get({"session_id": "nobody"})


def test_cart_totals_replace_and_remove(trip):
    add("s1", trip["out"], "flight")
    add("s1", trip["stay"], "hotel")
    cart = add("s1", trip["back"], "flight")
    assert len(cart["items"]) == 3
    assert cart["total_rub"] == trip["out"]["price_rub"] + trip["back"]["price_rub"] + trip["stay"]["price_total_rub"]
    # same flight on the flex tier replaces the standard one (flexible_fare upsell)
    same_slot_flex = next(x for x in s.search_flights({"origin_iata": ["SVO"], "dest_iata": ["AER"],
                          "date": "2026-11-14", "adults": 2, "today": TODAY})["offers"]
                          if x["flight_no"] == trip["out"]["flight_no"] and x["fare"]["refundable"])
    cart = add("s1", same_slot_flex, "flight")
    assert len(cart["items"]) == 3
    assert same_slot_flex["offer_id"] in {i["offer_id"] for i in cart["items"]}
    assert trip["out"]["offer_id"] not in {i["offer_id"] for i in cart["items"]}
    # meal upgrade of the same hotel and dates replaces the stay
    upgraded = next(x for x in s.search_hotels({"destination_id": "sochi", "check_in": "2026-11-14",
                    "check_out": "2026-11-21", "adults": 2, "filters": {"meals": "half_board"},
                    "today": TODAY})["offers"] if x["hotel_id"] == "h_sochi_1")
    cart = add("s1", upgraded, "hotel")
    hotels_in = [i for i in cart["items"] if i["kind"] == "hotel"]
    assert len(hotels_in) == 1 and hotels_in[0]["offer_id"] == upgraded["offer_id"]
    assert add("s1", upgraded, "hotel") == cart  # idempotent
    cart = s.cart_remove({"session_id": "s1", "offer_id": upgraded["offer_id"], "kind": "hotel"})
    assert len(cart["items"]) == 2 and s.cart_get({"session_id": "s1"}) == cart


def test_cart_compatibility_issues(trip):
    add("s1", trip["out"], "flight")
    add("s1", trip["stay"], "hotel")
    cart = add("s1", trip["back"], "flight")
    arr = datetime.fromisoformat(trip["out"]["arr"])
    check_in = datetime.fromisoformat(trip["stay"]["check_in"])
    if check_in - arr >= timedelta(hours=3):
        assert "early_arrival" in codes(cart["issues"])
    assert not {i["severity"] for i in cart["issues"]} & {"blocking", "warning"}
    early = transfer(iso_shift(trip["out"]["arr"], -20))
    cart = add("s1", early, "transfer")
    assert any(i["code"] == "transfer_too_early" and i["severity"] == "blocking" for i in cart["issues"])
    s.cart_remove({"session_id": "s1", "offer_id": early["offer_id"], "kind": "transfer"})
    cart = add("s1", transfer(iso_shift(trip["out"]["arr"], 50)), "transfer")
    assert "transfer_too_early" not in codes(cart["issues"])
    late = transfer(iso_shift(trip["back"]["dep"], -60), reverse=True)
    cart = add("s1", late, "transfer")
    assert "transfer_too_late" in codes(cart["issues"])
    # party mismatch and a stay that does not match the flights
    three = flight("SVO", "AER", "2026-11-14", children=1)
    add("s2", three, "flight")
    cart = add("s2", hotel(ci="2026-11-15", co="2026-11-21"), "hotel")
    assert {"party_mismatch", "night_without_hotel"} <= codes(cart["issues"])


def test_checkout_pay_and_order_ops(trip):
    add("s1", trip["out"], "flight")
    add("s1", trip["stay"], "hotel")
    cart = add("s1", trip["back"], "flight")
    total = cart["total_rub"]

    bad = s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS[:1], "contact": CONTACT, "now": NOW})
    c.CheckoutOut.model_validate(bad)
    assert "travellers_count" in codes(bad["issues"])
    with pytest.raises(ServiceError) as err:
        s.pay({"order_id": bad["order_id"], "method": "mock_card", "now": NOW})
    assert err.value.code == "blocking_issues"

    good = s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS, "contact": CONTACT, "now": NOW})
    assert good["order_id"] == bad["order_id"] and good["total_rub"] == total
    assert not [i for i in good["issues"] if i["severity"] == "blocking"]
    paid = s.pay({"order_id": good["order_id"], "method": "mock_card", "now": NOW})
    c.PayOut.model_validate(paid)
    assert len(paid["bookings"]) == 3 and all(b["booking_number"] for b in paid["bookings"])
    assert s.pay({"order_id": good["order_id"], "method": "mock_card", "now": NOW}) == paid  # idempotent

    order = s.order_get({"order_id": good["order_id"]})
    c.Order.model_validate(order)
    assert order["status"] == "paid" and order["total_rub"] == total
    assert [e["kind"] for e in order["events"]] == ["created", "travellers_updated", "paid"]
    assert all(i["status"] == "confirmed" for i in order["items"])
    assert s.cart_get({"session_id": "s1"})["status"] == "checked_out"
    with pytest.raises(ServiceError):
        s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS, "contact": CONTACT, "now": NOW})

    # early check-in: deterministic answer; regular time is always granted for free
    e1 = s.order_request_early_checkin({"order_id": good["order_id"], "time": "11:00", "now": NOW})
    c.OrderRequestEarlyCheckinOut.model_validate(e1)
    assert e1["status"] in ("granted", "on_request", "refused")
    after = s.order_get({"order_id": good["order_id"]})
    charged = e1["price_rub"] if e1["status"] == "granted" else 0
    assert after["total_rub"] == total + charged
    e2 = s.order_request_early_checkin({"order_id": good["order_id"], "time": "15:00", "now": NOW})
    assert e2 == {"order_id": good["order_id"], "status": "granted", "price_rub": 0, "synthetic": True}
    assert s.order_get({"order_id": good["order_id"]})["total_rub"] == total
    total = s.order_get({"order_id": good["order_id"]})["total_rub"]

    # change dates: quote first, nothing applied
    q = s.order_change_dates({"order_id": good["order_id"], "new_dates": {"check_in": "2026-11-15",
                              "check_out": "2026-11-22"}, "confirm": False, "now": NOW})
    c.OrderChangeDatesOut.model_validate(q)
    assert q["available"] and not q["applied"] and q["order"] is None
    assert q["quote"]["old_total_rub"] == total
    assert q["quote"]["penalty_rub"] == 2 * 2500 * 2  # two standard fares x 2 pax; hotel change is still free
    assert q["quote"]["difference_rub"] == q["quote"]["new_total_rub"] - total
    assert s.order_get({"order_id": good["order_id"]})["total_rub"] == total
    applied = s.order_change_dates({"order_id": good["order_id"], "new_dates": {"check_in": "2026-11-15",
                                    "check_out": "2026-11-22"}, "confirm": True, "now": NOW})
    assert applied["applied"] and applied["order"]["status"] == "changed"
    order = applied["order"]
    assert order["total_rub"] == q["quote"]["new_total_rub"]
    starts = sorted(i["start"][:10] for i in order["items"])
    assert starts == ["2026-11-15", "2026-11-15", "2026-11-22"]
    assert all(i["status"] == "changed" for i in order["items"])
    assert order["events"][-1]["kind"] == "dates_changed"

    # add a transfer after purchase
    flight_out = next(i for i in order["items"] if i["kind"] == "flight" and i["start"][:10] == "2026-11-15")
    tr = transfer(iso_shift(flight_out["end"], 45))
    with_tr = s.order_add_transfer({"order_id": good["order_id"], "offer_id": tr["offer_id"], "now": NOW})
    c.Order.model_validate(with_tr)
    added = [i for i in with_tr["items"] if i["kind"] == "transfer"]
    assert len(added) == 1 and added[0]["booking_number"].startswith("TRF-") and added[0]["status"] == "confirmed"
    assert with_tr["total_rub"] == order["total_rub"] + tr["price_rub"]
    again = s.order_add_transfer({"order_id": good["order_id"], "offer_id": tr["offer_id"], "now": NOW})
    assert again["total_rub"] == with_tr["total_rub"]


def test_basic_fare_cannot_change_and_unpaid_orders(mock_env):
    out = flight("SVO", "AER", "2026-11-14", tier_kg=None)
    add("s9", out, "flight")
    cart = add("s9", hotel(), "hotel")
    order = s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS, "contact": CONTACT, "now": NOW})
    with pytest.raises(ServiceError) as err:
        s.order_request_early_checkin({"order_id": order["order_id"], "time": "10:00", "now": NOW})
    assert err.value.code == "order_not_paid"
    s.pay({"order_id": order["order_id"], "method": "mock_card", "now": NOW})
    q = s.order_change_dates({"order_id": order["order_id"], "new_dates": {"check_in": "2026-11-16",
                              "check_out": "2026-11-21"}, "confirm": False, "now": NOW})
    assert "flight_change_not_allowed" in codes(q["issues"])
    late = s.order_change_dates({"order_id": order["order_id"], "new_dates": {"check_in": "2026-11-16",
                                 "check_out": "2026-11-21"}, "confirm": False, "now": "2026-11-13T10:00:00+03:00"})
    assert late["quote"]["penalty_rub"] > 0 and "change_penalty" in codes(late["issues"])
    with pytest.raises(ServiceError):
        s.order_get({"order_id": "o_missing"})


def test_checkout_age_policies(mock_env):
    # flight bought for 2 adults + 1 child, but the third traveller is an infant
    add("k1", flight("SVO", "AER", "2026-11-14", children=1), "flight")
    cart = s.cart_get({"session_id": "k1"})
    baby = {"name": "Малыш", "birth_date": "2026-03-01"}
    out = s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS + [baby], "contact": CONTACT, "now": NOW})
    assert any(i["code"] == "infant_fare" and i["severity"] == "blocking" for i in out["issues"])

    # one adult with two infants: every infant needs an adult
    add("k2", flight("SVO", "AER", "2026-11-14", adults=1, infants=2), "flight")
    cart = s.cart_get({"session_id": "k2"})
    out = s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS[:1] + [baby, baby], "contact": CONTACT,
                      "now": NOW})
    assert "infant_without_adult" in codes(out["issues"])

    # adults-only hotel booked for 2 adults, one traveller is 10
    cart = add("k3", hotel("h_sochi_3"), "hotel")
    kid = {"name": "Петя", "birth_date": "2016-05-05"}
    out = s.checkout({"cart_id": cart["cart_id"], "travellers": [ADULTS[0], kid], "contact": CONTACT, "now": NOW})
    assert "hotel_age_limit" in codes(out["issues"])

    # hotel without cots, infant in the party
    cart = add("k4", hotel("h_sochi_2", ages=[1]), "hotel")
    out = s.checkout({"cart_id": cart["cart_id"], "travellers": ADULTS + [baby], "contact": CONTACT, "now": NOW})
    assert any(i["code"] == "child_bed" and "кроват" in i["message"] for i in out["issues"])

    # nobody is an adult, and a birth date after the trip
    cart = add("k5", hotel(adults=1), "hotel")
    out = s.checkout({"cart_id": cart["cart_id"], "travellers": [kid], "contact": CONTACT, "now": NOW})
    assert "no_adult" in codes(out["issues"])
    cart = add("k6", hotel(adults=1), "hotel")
    out = s.checkout({"cart_id": cart["cart_id"], "travellers": [{"name": "X", "birth_date": "2027-01-01"}],
                      "contact": CONTACT, "now": NOW})
    assert "invalid_birth_date" in codes(out["issues"])


def test_state_is_never_written_to_catalog(trip, mock_env):
    import sqlite3
    add("s1", trip["out"], "flight")
    with sqlite3.connect(mock_env["catalog"]) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "carts" not in tables
    assert (mock_env["tmp"] / "state.sqlite").exists()
