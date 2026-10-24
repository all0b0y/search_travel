"""Ostrovok mock service: `call(tool, payload)` plus one plain function per tool.

Every call validates the payload with the contract's input model and the result with the
output model (ostrovok_mcp.contract.TOOLS) and returns JSON-ready dicts (aliases applied).
Problems the caller can fix raise `ServiceError(code, message)`; invalid payloads raise
pydantic's `ValidationError`. The mock never reads the wall clock: `today` / `now` come in
every payload that needs them.
"""
from __future__ import annotations

from typing import Any, Callable

from . import flights, hotels, insights, orders, quotes, trains, transfers
from .contract import TOOL_NAMES, TOOLS
from .db import ServiceError, reset_caches  # noqa: F401  (re-exported for callers and tests)

__all__ = ["call", "ServiceError", "reset_caches", *TOOL_NAMES]


def _quote_destinations(r) -> dict:
    return {"quotes": quotes.quote(r.destination_ids, r.origin_city, r.month,
                                   r.dates.model_dump() if r.dates else None, r.nights, r.party.adults,
                                   list(r.party.children_ages), r.today), "synthetic": True}


def _search_hotels(r) -> dict:
    offers, total = hotels.search(r.destination_id, r.check_in, r.check_out, r.adults, list(r.children_ages),
                                  r.filters.model_dump(), r.limit, r.today,
                                  hotel_ids={r.hotel_id} if r.hotel_id else None, all_rooms=r.all_rooms)
    return {"offers": offers, "total_found": total, "synthetic": True}


def _get_hotel(r) -> dict:
    return hotels.details(r.hotel_id)


def _search_flights(r) -> dict:
    return {"offers": flights.search(r.origin_iata, r.dest_iata, r.date, r.adults, r.children, r.infants,
                                     r.flex_days, r.today), "synthetic": True}


def _search_trains(r) -> dict:
    offers, note = trains.search(r.origin_city, r.dest_city, r.date, r.passengers, r.today)
    return {"offers": offers, "note": note, "synthetic": True}


def _search_transfers(r) -> dict:
    offers = transfers.search(r.from_.model_dump(), r.to.hotel_id, r.at, r.pax, r.kinds, r.reverse, r.today)
    return {"offers": [{k: v for k, v in o.items() if not k.startswith("_")} for o in offers], "synthetic": True}


def _cart_create(r) -> dict:
    return orders.cart_create(r.session_id, r.now)


def _cart_add(r) -> dict:
    return orders.cart_add(r.session_id, r.offer_id, r.kind, r.now)


def _cart_remove(r) -> dict:
    return orders.cart_remove(r.session_id, r.offer_id, r.kind)


def _cart_get(r) -> dict:
    return orders.cart_get(r.session_id)


def _checkout(r) -> dict:
    return orders.checkout(r.cart_id, [t.model_dump(mode="json") for t in r.travellers],
                           r.contact.model_dump(mode="json", exclude_none=True), r.now)


def _pay(r) -> dict:
    return orders.pay(r.order_id, r.method, r.now)


def _order_get(r) -> dict:
    return orders.order_get(r.order_id)


def _order_request_early_checkin(r) -> dict:
    return orders.request_early_checkin(r.order_id, r.time, r.now)


def _order_change_dates(r) -> dict:
    return orders.change_dates(r.order_id, r.new_dates.check_in, r.new_dates.check_out, r.confirm, r.now)


def _order_add_transfer(r) -> dict:
    return orders.add_transfer(r.order_id, r.offer_id, r.now)


def _insights_popular_destinations(r) -> dict:
    return {"items": insights.popular_destinations(r.origin_city, r.month, r.segment), "synthetic": True}


def _insights_segment_preferences(r) -> dict:
    return insights.segment_preferences(r.segment, r.destination_kind)


def _insights_log_event(r) -> dict:
    return insights.log_event(r.event.model_dump(mode="json"))


IMPL: dict[str, Callable[[Any], dict]] = {
    "quote_destinations": _quote_destinations,
    "search_hotels": _search_hotels,
    "get_hotel": _get_hotel,
    "search_flights": _search_flights,
    "search_trains": _search_trains,
    "search_transfers": _search_transfers,
    "cart_create": _cart_create,
    "cart_add": _cart_add,
    "cart_remove": _cart_remove,
    "cart_get": _cart_get,
    "checkout": _checkout,
    "pay": _pay,
    "order_get": _order_get,
    "order_request_early_checkin": _order_request_early_checkin,
    "order_change_dates": _order_change_dates,
    "order_add_transfer": _order_add_transfer,
    "insights_popular_destinations": _insights_popular_destinations,
    "insights_segment_preferences": _insights_segment_preferences,
    "insights_log_event": _insights_log_event,
}
assert tuple(IMPL) == TOOL_NAMES, "service must implement exactly the contract tools"


def call(tool: str, payload: dict) -> dict:
    """Validate `payload` with the tool's input model, run it, validate and return the output."""
    if tool not in TOOLS:
        raise ServiceError("unknown_tool", f"unknown tool {tool!r}")
    model_in, model_out = TOOLS[tool]
    req = model_in.model_validate(payload)
    result = IMPL[tool](req)
    return model_out.model_validate(result).model_dump(mode="json", by_alias=True)


def _plain(name: str) -> Callable[[dict], dict]:
    def fn(payload: dict) -> dict:
        return call(name, payload)
    fn.__name__ = name
    fn.__qualname__ = name
    fn.__doc__ = f"Tool `{name}`: validated call (see ostrovok_mcp.contract.TOOLS[{name!r}])."
    return fn


quote_destinations = _plain("quote_destinations")
search_hotels = _plain("search_hotels")
get_hotel = _plain("get_hotel")
search_flights = _plain("search_flights")
search_trains = _plain("search_trains")
search_transfers = _plain("search_transfers")
cart_create = _plain("cart_create")
cart_add = _plain("cart_add")
cart_remove = _plain("cart_remove")
cart_get = _plain("cart_get")
checkout = _plain("checkout")
pay = _plain("pay")
order_get = _plain("order_get")
order_request_early_checkin = _plain("order_request_early_checkin")
order_change_dates = _plain("order_change_dates")
order_add_transfer = _plain("order_add_transfer")
insights_popular_destinations = _plain("insights_popular_destinations")
insights_segment_preferences = _plain("insights_segment_preferences")
insights_log_event = _plain("insights_log_event")
