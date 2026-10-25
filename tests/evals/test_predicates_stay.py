"""The stay predicates: the chosen step (a past-midnight arrival counts from the night before), else the booking."""
from types import SimpleNamespace

from evals import predicates as P


def _state(chosen=None, cart=None, order=None):
    return SimpleNamespace(chosen=chosen, cart=cart, order=order, brief={})


def test_booking_dates_without_a_chosen_variant():
    cart = {"items": [{"kind": "hotel", "start": "2026-12-05T15:00:00+03:00", "end": "2026-12-08T12:00:00+03:00"}]}
    st = _state(cart=cart)
    assert P.stay_starts_on(st, "2026-12-05")[0] and P.nights_between(st, 3, 3)[0]


def test_a_step_starting_after_midnight_counts_from_the_night_before():
    chosen = {"steps": [{"type": "stay", "start": "2026-12-06T02:13:00+03:00", "end": "2026-12-08T05:40:00+03:00"}]}
    st = _state(chosen=chosen)
    assert P.stay_starts_on(st, "2026-12-05")[0] and P.nights_between(st, 3, 3)[0]
    day = _state(chosen={"steps": [{"type": "stay", "start": "2026-12-05T22:18:00+03:00", "end": "2026-12-08T10:00:00+03:00"}]})
    assert P.stay_starts_on(day, "2026-12-05")[0] and P.nights_between(day, 3, 3)[0]


def test_a_changed_order_wins_over_the_chosen_variant():
    chosen = {"steps": [{"type": "stay", "start": "2027-03-03T14:00:00+03:00", "end": "2027-03-06T12:00:00+03:00"}]}
    order = {"status": "changed", "items": [{"kind": "hotel", "start": "2027-03-10T14:00:00+03:00",
                                             "end": "2027-03-13T12:00:00+03:00"}]}
    st = _state(chosen=chosen, order=order)
    assert P.stay_starts_on(st, "2027-03-10")[0] and P.nights_between(st, 3, 3)[0]
    assert P.dates_within(st, "2027-03-09", "2027-03-14")[0]
    paid = _state(chosen=chosen, order=dict(order, status="paid"))
    assert P.stay_starts_on(paid, "2027-03-03")[0]
