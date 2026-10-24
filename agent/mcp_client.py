"""Clients of the Ostrovok mock (AGENT_PLAN.md sections 5 "Module interfaces" and 7).

    client = InProcessOstrovokClient()          # calls ostrovok_mcp.service.call directly (evals, tests)
    client = StdioOstrovokClient()              # one long-lived `python -m ostrovok_mcp` over MCP stdio
    out = client.call("search_hotels", {...})   # dict in, dict out; both validated by ostrovok_mcp.contract.TOOLS

Every failure of the mock surfaces as ToolError (never a bare exception), so the engine can answer
honestly ("поиск сейчас не отвечает") instead of crashing. Input that does not satisfy the tool's
input model is a ToolError with code "bad_request" raised before anything is sent.
"""
import asyncio
import json
import os
import sys
import threading
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ToolError(Exception):
    """A tool call failed. code: bad_request | unavailable | error | bad_output."""

    def __init__(self, tool, message, code="error", retryable=None):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message
        self.code = code
        self.retryable = (code in ("unavailable", "error")) if retryable is None else retryable


@runtime_checkable
class OstrovokClient(Protocol):
    def call(self, tool: str, payload: dict) -> dict: ...


def _tools():
    from ostrovok_mcp.contract import TOOLS
    return TOOLS


def _first_error(exc):
    try:
        err = exc.errors()[0]
        where = ".".join(str(p) for p in err.get("loc", ()))
        return f"{where}: {err.get('msg')}" if where else str(err.get("msg"))
    except Exception:  # pragma: no cover - defensive
        return str(exc)


def validate_input(tool, payload):
    """-> JSON-ready dict of the tool's input model (aliases, ISO dates). ToolError(bad_request) if invalid."""
    tools = _tools()
    if tool not in tools:
        raise ToolError(tool, "unknown tool", code="bad_request", retryable=False)
    in_model, _ = tools[tool]
    try:
        return in_model.model_validate(payload).model_dump(mode="json", by_alias=True, exclude_none=True)
    except ValidationError as exc:
        raise ToolError(tool, f"invalid input: {_first_error(exc)}", code="bad_request", retryable=False) from None


def validate_output(tool, data):
    """-> JSON-ready dict of the tool's output model. ToolError(bad_output) if the mock broke the contract."""
    _, out_model = _tools()[tool]
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json", by_alias=True)
    try:
        return out_model.model_validate(data).model_dump(mode="json", by_alias=True)
    except ValidationError as exc:
        raise ToolError(tool, f"output violates the contract: {_first_error(exc)}", code="bad_output",
                        retryable=False) from None


class InProcessOstrovokClient:
    """Calls `ostrovok_mcp.service.call(tool, payload)` in this process (imported lazily)."""

    def __init__(self, service=None):
        self._service = service
        self._lock = threading.Lock()

    @property
    def service(self):
        if self._service is None:
            with self._lock:
                if self._service is None:
                    try:
                        from ostrovok_mcp import service
                    except ImportError as exc:
                        raise ToolError("*", f"ostrovok_mcp.service is not available: {exc}",
                                        code="unavailable") from None
                    self._service = service
        return self._service

    def call(self, tool, payload):
        clean = validate_input(tool, payload)
        try:
            data = self.service.call(tool, clean)
        except ToolError:
            raise
        except ValidationError as exc:
            raise ToolError(tool, f"invalid input: {_first_error(exc)}", code="bad_request", retryable=False) from None
        except Exception as exc:
            if type(exc).__name__ == "ServiceError" or isinstance(exc, (ValueError, KeyError, LookupError)):
                # a problem the caller can fix (unknown id, wrong state): not retryable
                code = getattr(exc, "code", None)
                message = getattr(exc, "message", None) or str(exc)
                raise ToolError(tool, f"{code}: {message}" if code else message, code="error",
                                retryable=False) from None
            raise ToolError(tool, f"{type(exc).__name__}: {exc}", code="unavailable") from None
        return validate_output(tool, data)

    def close(self):
        pass


def _result_payload(tool, result):
    """CallToolResult -> plain dict (structured content preferred, else the JSON text block)."""
    if getattr(result, "is_error", False):
        text = " ".join(getattr(c, "text", "") or "" for c in (result.content or []))
        raise ToolError(tool, text or "tool reported an error", code="error", retryable=False)
    data = getattr(result, "structured_content", None)
    if data is None:
        texts = [getattr(c, "text", None) for c in (result.content or [])]
        texts = [t for t in texts if t]
        if not texts:
            raise ToolError(tool, "empty tool result", code="bad_output", retryable=False)
        try:
            data = json.loads(texts[0])
        except json.JSONDecodeError:
            raise ToolError(tool, "tool result is not JSON", code="bad_output", retryable=False) from None
    _, out_model = _tools()[tool]
    if isinstance(data, dict) and set(data) == {"result"} and "result" not in out_model.model_fields:
        data = data["result"]   # MCPServer wraps non-object return types as {"result": ...}
    return data


class StdioOstrovokClient:
    """MCP client over stdio to ONE long-lived `python -m ostrovok_mcp` process.

    The MCP session lives in a private asyncio loop on a daemon thread; `call` is thread-safe
    (requests are multiplexed by the SDK). Arguments are sent flat (the input model's fields);
    if the server's tool declares a single wrapper parameter instead (e.g. `{"payload": {...}}`),
    the client detects it from the tool listing and wraps. `close()` ends the process cleanly.
    """

    def __init__(self, command=None, args=None, env=None, cwd=None, timeout_s=30.0, start_timeout_s=30.0):
        self.command = command or sys.executable
        self.args = list(args) if args is not None else ["-m", "ostrovok_mcp"]
        self.env = env
        self.cwd = cwd or REPO_ROOT
        self.timeout_s = timeout_s
        self.start_timeout_s = start_timeout_s
        self._loop = None
        self._thread = None
        self._client = None
        self._wrappers = {}
        self._ready = threading.Event()
        self._start_error = None
        self._stop = None
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------ lifecycle
    def _ensure_started(self):
        if self._ready.is_set() and self._client is not None:
            return
        with self._lock:
            if self._closed:
                raise ToolError("*", "client is closed", code="unavailable", retryable=False)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="ostrovok-mcp-stdio", daemon=True)
                self._thread.start()
        if not self._ready.wait(self.start_timeout_s):
            raise ToolError("*", "MCP server did not start in time", code="unavailable")
        if self._start_error is not None:
            raise ToolError("*", f"MCP server failed to start: {self._start_error}", code="unavailable")

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001 - report start failures to the caller
            if not self._ready.is_set():
                self._start_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._client = None
            self._ready.set()
            try:
                loop.close()
            except Exception:  # pragma: no cover
                pass

    async def _main(self):
        from mcp import Client, StdioServerParameters
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env, cwd=self.cwd)
        self._stop = asyncio.Event()
        try:
            async with Client(params) as client:
                listing = await client.list_tools()
                self._wrappers = self._detect_wrappers(listing)
                self._client = client
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            if not self._ready.is_set():
                self._start_error = f"{type(exc).__name__}: {exc}"
                self._ready.set()
            raise

    @staticmethod
    def _detect_wrappers(listing):
        wrappers = {}
        try:
            tools = _tools()
        except Exception:  # pragma: no cover
            return wrappers
        for tool in getattr(listing, "tools", []) or []:
            name = getattr(tool, "name", None)
            if name not in tools:
                continue
            props = list(((getattr(tool, "input_schema", None) or {}).get("properties") or {}).keys())
            fields = set()
            for fname, info in tools[name][0].model_fields.items():
                fields.add(info.alias or fname)
                fields.add(fname)
            if len(props) == 1 and props[0] not in fields:
                wrappers[name] = props[0]
        return wrappers

    def close(self):
        with self._lock:
            self._closed = True
            loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ calls
    def call(self, tool, payload):
        clean = validate_input(tool, payload)
        self._ensure_started()
        client, loop = self._client, self._loop
        if client is None or loop is None:
            raise ToolError(tool, "MCP server is not running", code="unavailable")
        args = {self._wrappers[tool]: clean} if tool in self._wrappers else clean
        future = asyncio.run_coroutine_threadsafe(client.call_tool(tool, args), loop)
        try:
            result = future.result(self.timeout_s)
        except TimeoutError:
            future.cancel()
            raise ToolError(tool, "timeout", code="unavailable") from None
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(tool, f"{type(exc).__name__}: {exc}", code="unavailable") from None
        return validate_output(tool, _result_payload(tool, result))


def default_client() -> Any:
    """The engine default: in-process (fast, same data); set OSTROVOK_TRANSPORT=stdio for the MCP process."""
    if os.environ.get("OSTROVOK_TRANSPORT", "").lower() == "stdio":
        return StdioOstrovokClient()
    return InProcessOstrovokClient()


__all__ = ["OstrovokClient", "InProcessOstrovokClient", "StdioOstrovokClient", "ToolError", "validate_input",
           "validate_output", "default_client"]
