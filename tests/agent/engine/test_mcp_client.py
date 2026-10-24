"""MCP clients: in-process validation/error mapping and the stdio client against `python -m ostrovok_mcp`."""
import os
import threading

import pytest

from agent.mcp_client import InProcessOstrovokClient, StdioOstrovokClient, ToolError

EVENT = {"event": {"kind": "turn", "stage": "inspiration", "data": {"route": "x"}, "at": "2026-10-24T12:00:00+03:00"}}


class FakeService:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def call(self, tool, payload):
        self.calls.append((tool, payload))
        if self.exc:
            raise self.exc
        return self.result


class ServiceError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def test_inprocess_validates_both_ways():
    svc = FakeService(result={"ok": True})
    client = InProcessOstrovokClient(service=svc)
    assert client.call("insights_log_event", EVENT) == {"ok": True}
    with pytest.raises(ToolError) as bad_in:
        client.call("insights_log_event", {"event": {"kind": "x"}})
    assert bad_in.value.code == "bad_request" and len(svc.calls) == 1
    with pytest.raises(ToolError) as unknown:
        client.call("no_such_tool", {})
    assert unknown.value.code == "bad_request"
    svc.result = {"ok": "maybe"}
    with pytest.raises(ToolError) as bad_out:
        client.call("insights_log_event", EVENT)
    assert bad_out.value.code == "bad_output"


def test_inprocess_maps_service_errors():
    client = InProcessOstrovokClient(service=FakeService(exc=ServiceError("unknown_hotel", "нет такого")))
    with pytest.raises(ToolError) as err:
        client.call("get_hotel", {"hotel_id": "h"})
    assert err.value.code == "error" and not err.value.retryable and "unknown_hotel" in err.value.message
    client = InProcessOstrovokClient(service=FakeService(exc=OSError("disk")))
    with pytest.raises(ToolError) as err:
        client.call("get_hotel", {"hotel_id": "h"})
    assert err.value.code == "unavailable" and err.value.retryable


def test_stdio_client_real_mock_process(tmp_path):
    try:
        import ostrovok_mcp.server  # noqa: F401
    except ImportError:
        pytest.skip("ostrovok_mcp.server not available")
    env = {**os.environ, "OSTROVOK_STATE_DB": str(tmp_path / "state.sqlite")}
    client = StdioOstrovokClient(env=env, timeout_s=60, start_timeout_s=60)
    try:
        assert client.call("insights_log_event", EVENT) == {"ok": True}
        results, errors = [], []

        def worker():
            try:
                results.append(client.call("cart_create", {"session_id": f"s_t{threading.get_ident()}",
                                                           "now": "2026-10-24T12:00:00+03:00"}))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors and len(results) == 4 and all(r["status"] == "open" for r in results)
        with pytest.raises(ToolError) as err:
            client.call("order_get", {"order_id": "o_missing"})
        assert err.value.code == "error"
    finally:
        client.close()
    assert not client._thread.is_alive()
    with pytest.raises(ToolError):
        client.call("insights_log_event", EVENT)
