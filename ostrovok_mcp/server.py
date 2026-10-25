"""MCP server (stdio) for the Ostrovok mock: exactly contract.TOOL_NAMES, each delegating to
service.call. Built on the SDK's MCPServer (the mcp 2.x successor of FastMCP).

Each tool's parameters are the fields of its contract input model (aliases kept, e.g. `from`
for search_transfers) and its structured output is the contract output model.
"""
from __future__ import annotations

import inspect
import logging
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from . import service
from .contract import TOOL_NAMES, TOOLS
from .db import ServiceError

DESCRIPTIONS = {
    "quote_destinations": "Cheapest round-trip flight + 3-4 star stay estimate per destination for the party (RUB).",
    "search_hotels": "Hotel offers in a destination for the stay and party, with filters; up to 40, best first.",
    "get_hotel": "Hotel details: policies, room types, real photo links only.",
    "search_flights": "Flight offers (direct or 1 stop) for the date +- flex_days and the party (RUB).",
    "search_trains": "Train offers for Russian domestic routes up to ~2500 km (over 1000 km: 1-2 long-distance sleepers a day, up to two days on the road); empty with a note otherwise.",
    "search_transfers": "Transfer offers between an airport/station/point and a hotel (reverse=true: back).",
    "cart_create": "Get or create the open cart of a session.",
    "cart_add": "Add an offer to the session's cart by offer id (same slot is replaced); returns the cart.",
    "cart_remove": "Remove an offer from the session's open cart; returns the cart.",
    "cart_get": "The session's cart with totals and compatibility issues.",
    "checkout": "Validate travellers against the cart and create an order pending payment, with issues.",
    "pay": "Pay an order with the mock card; returns booking numbers.",
    "order_get": "An order with items, travellers and events.",
    "order_request_early_checkin": "Request early check-in for a paid order: granted, on_request or refused.",
    "order_change_dates": "Quote (and with confirm=true apply) new stay dates for a paid order.",
    "order_add_transfer": "Add a transfer offer to an order; returns the order.",
    "insights_popular_destinations": "Anonymous popular destinations for origin, month, segment (groups n >= 20).",
    "insights_segment_preferences": "Anonymous preference shares for a segment (groups n >= 20).",
    "insights_log_event": "Log an anonymous analytics event (no personal data).",
}

log = logging.getLogger("ostrovok_mcp")


def _tool_fn(name: str):
    model_in, model_out = TOOLS[name]

    def fn(**kwargs: Any):
        try:
            payload = model_in.model_validate(kwargs).model_dump(mode="json", by_alias=True)
            return model_out.model_validate(service.call(name, payload))
        except ServiceError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        except ValidationError as exc:
            raise ToolError(f"invalid_input: {exc}") from exc

    params = [inspect.Parameter(fname, inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[f.annotation, f])
              for fname, f in model_in.model_fields.items()]
    fn.__signature__ = inspect.Signature(params, return_annotation=model_out)  # type: ignore[attr-defined]
    fn.__name__ = name
    fn.__doc__ = DESCRIPTIONS[name]
    return fn


def build_server() -> MCPServer:
    server = MCPServer("ostrovok-mock", instructions="Ostrovok mock: synthetic prices, availability and orders "
                       "over real places. All prices RUB, times ISO 8601 with local UTC offset.", log_level="WARNING")
    for name in TOOL_NAMES:
        server.add_tool(_tool_fn(name), name=name, description=DESCRIPTIONS[name])
    return server


def main() -> None:
    build_server().run("stdio")
