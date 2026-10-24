"""MCP round trips: in-memory (SDK Client over the MCPServer) and `python -m ostrovok_mcp` over stdio."""
import os
import sys

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ostrovok_mcp import contract as c
from ostrovok_mcp import service as s
from ostrovok_mcp.server import build_server

TODAY = "2026-10-25"
NOW = "2026-10-25T13:05:00+03:00"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_in_memory_round_trip(mock_env):
    async def run():
        async with Client(build_server()) as client:
            tools = (await client.list_tools()).tools
            assert tuple(sorted(t.name for t in tools)) == tuple(sorted(c.TOOL_NAMES))
            assert all(t.description for t in tools)
            transfer_tool = next(t for t in tools if t.name == "search_transfers")
            assert "from" in transfer_tool.input_schema["properties"]

            payload = {"destination_id": "sochi", "check_in": "2026-11-14", "check_out": "2026-11-21", "adults": 2,
                       "today": TODAY}
            res = await client.call_tool("search_hotels", payload)
            assert not res.is_error
            out = c.SearchHotelsOut.model_validate(res.structured_content)
            assert out.model_dump(mode="json") == s.search_hotels(payload)

            res = await client.call_tool("search_transfers", {"from": {"iata": "AER"}, "to": {"hotel_id": "h_sochi_1"},
                                                              "at": "2026-11-14T12:00:00+03:00", "pax": 2,
                                                              "today": TODAY})
            assert not res.is_error and res.structured_content["offers"]

            offer_id = out.offers[0].offer_id
            res = await client.call_tool("cart_add", {"session_id": "mcp", "offer_id": offer_id, "kind": "hotel",
                                                      "now": NOW})
            assert not res.is_error and res.structured_content["total_rub"] == out.offers[0].price_total_rub

            bad = await client.call_tool("cart_add", {"session_id": "mcp", "offer_id": offer_id[:-1] + "x",
                                                      "kind": "hotel", "now": NOW})
            assert bad.is_error and "invalid_offer_id" in bad.content[0].text
            bad = await client.call_tool("get_hotel", {"hotel_id": "nope"})
            assert bad.is_error and "hotel_not_found" in bad.content[0].text
    anyio.run(run)


def test_stdio_subprocess_lists_tools(mock_env):
    env = {**os.environ, "PYTHONPATH": ROOT}
    params = StdioServerParameters(command=sys.executable, args=["-m", "ostrovok_mcp"], env=env, cwd=ROOT)

    async def run():
        with anyio.fail_after(30):
            async with Client(params) as client:
                names = sorted(t.name for t in (await client.list_tools()).tools)
                assert names == sorted(c.TOOL_NAMES)
                res = await client.call_tool("search_trains", {"origin_city": "Москва", "dest_city": "Казань",
                                                               "date": "2026-11-14", "passengers": 1, "today": TODAY})
                assert not res.is_error and res.structured_content["offers"]
    anyio.run(run)
