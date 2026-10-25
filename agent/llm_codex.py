"""CodexBackend: the strong-model check (`gpt-6-luna`) over `codex app-server` (AGENT_PLAN.md section 2).

    backend = CodexBackend(effort="high")          # decision 22: the strong-model check runs at `high`
    result = backend.complete(LLMRequest(...))     # same contract as OpenRouterBackend
    backend.close()

Protocol: JSON-RPC 2.0 over the child's stdio, one JSON object per line (the app-server omits the
"jsonrpc" member; we send it anyway, the server ignores it). Generated schemas: runtime/codex-schema/.

Process and profile
- The child is `codex app-server` started with CODEX_HOME=<repo>/.codex-profile/codex and
  HOME=<repo>/.codex-profile/home (override with the constructor or TRIP_AGENT_CODEX_HOME /
  TRIP_AGENT_CODEX_USER_HOME). A CODEX_HOME that resolves to the user's real ~/.codex is refused.
- The process is started lazily on the first call and kept warm; `close()` stops it.
- Handshake: `initialize {clientInfo, capabilities: {experimentalApi: true}}` (experimental because
  `environments` is an experimental thread/start field), the `initialized` notification, then
  `account/read {}`. `account == null` with `requiresOpenaiAuth` -> LLMError (never a silent fallback).

One call = one fresh ephemeral thread
- `thread/start {model, ephemeral: true, sandbox: "read-only", approvalPolicy: "never", environments: [],
  developerInstructions: <system prompt>, config: {web_search: "disabled"}, cwd: <workdir>}`.
- `turn/start {threadId, effort, input: [text, image...], outputSchema: req.schema}`.
  Images go as `{"type": "image", "url": <data URL>}` (UrlUserInput); not yet verified live.
- Thinking -> Codex effort: off -> "low", low -> "low", high -> "high" (Codex has no "off"; `low` is the
  cheapest effort it advertises). `CodexBackend(effort=...)` forces one effort for every card.
- Text: `item/agentMessage/delta` collected per itemId; the answer is the LAST agent message of the turn
  (its `item/completed` text when present, else its concatenated deltas). Commentary messages before it
  are ignored, so a preamble cannot break the JSON.
- The turn ends on `turn/completed` (status completed; failed/interrupted -> LLMError).
- `error` notifications fail the turn only when `willRetry` is false.
- Every server-initiated request (it has both `id` and `method`) is answered at once with a decline
  (approvals declined, dynamic tool calls fail, user-input requests get no answers, unknown methods get
  a JSON-RPC error), so a turn never stalls on an unanswered request.
- Backpressure: the reader awaits `queue.put` with a timeout (never put_nowait/drop); only events nobody
  reads within that timeout fail the turn as a flood.
- Usage from `thread/tokenUsage/updated` (`last` breakdown); cost_usd is None (subscription).
- JSON parsing, schema validation and exactly one retry come from `agent.llm.JsonBackend`. The retry runs
  on a new thread whose input carries the rejected reply and the validation errors.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .llm import JsonBackend, LLMError, LLMRequest, RawReply, empty_usage

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gpt-6-luna"
PROVIDER = "codex"
DEFAULT_CODEX_HOME = ROOT / ".codex-profile" / "codex"
DEFAULT_USER_HOME = ROOT / ".codex-profile" / "home"
DEFAULT_WORKDIR = ROOT / ".codex-profile" / "work"
EFFORT_MAP = {"off": "low", "low": "low", "high": "high"}
INSTALL_HINT = "install it with `brew install --cask codex` or `npm install -g @openai/codex`"
CLIENT_INFO = {"name": "trip-agent", "title": "Trip agent (strong-model check)", "version": "0.1.0"}
STREAM_LIMIT = 16 * 1024 * 1024      # max bytes of one stdio line (a whole item/completed frame)
QUEUE_SIZE = 256


def default_search_dirs(home: Optional[Path] = None):
    """Folders where a `codex` binary usually lives besides PATH (injectable in tests)."""
    home = Path(home or Path.home())
    dirs = [home / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".npm-global/bin",
            home / "bin", home / ".volta/bin", home / "Library/pnpm", home / ".bun/bin"]
    nvm = home / ".nvm/versions/node"
    if nvm.is_dir():
        def version_key(p):
            try:
                return tuple(int(x) for x in p.name.lstrip("v").split("."))
            except ValueError:
                return (0,)
        dirs += [p / "bin" for p in sorted(nvm.iterdir(), key=version_key, reverse=True)]
    return dirs


def find_codex(path_env: Optional[str] = None, search_dirs=None) -> Optional[str]:
    found = shutil.which("codex", path=path_env if path_env is not None else os.environ.get("PATH", ""))
    if found:
        return found
    for d in (default_search_dirs() if search_dirs is None else search_dirs):
        candidate = Path(d) / "codex"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def decline_result(method: str, params: Any):
    """-> (result, error) answering a server-initiated request with a refusal."""
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "decline"}, None
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"decision": {"denied": {"rejection": "Tools are disabled for this client."}}}, None
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}, None
    if method == "item/tool/requestUserInput":
        return {"answers": {}}, None
    if method == "mcpServer/elicitation/request":
        return {"action": "decline"}, None
    if method == "item/tool/call":
        return {"success": False, "contentItems": [{"type": "inputText",
                                                    "text": "Tools are not available in this client."}]}, None
    return None, {"code": -32601, "message": f"{method} is not supported by this client"}


@dataclass
class _Turn:
    thread_id: str
    queue: asyncio.Queue
    failed: Optional[BaseException] = None


class CodexAppServer:
    """Async JSON-RPC client over one `codex app-server` child. Runs on a private event loop thread."""

    def __init__(self, command, env, cwd=None, request_timeout_s=30.0, put_timeout_s=10.0, stderr_path=None):
        self.command = list(command)
        self.env = env
        self.cwd = cwd
        self.request_timeout_s = request_timeout_s
        self.put_timeout_s = put_timeout_s
        self.stderr_path = stderr_path
        self.proc = None
        self.account = None
        self.declined: list = []          # server-initiated requests we refused (method names), for tests/debug
        self._ids = itertools.count(1)
        self._pending: dict = {}
        self._turns: dict = {}
        self._reader = None
        self._write_lock = None
        self._dead: Optional[BaseException] = None

    # -------------------------------------------------------------- lifecycle
    async def start(self):
        self._write_lock = asyncio.Lock()
        stderr = open(self.stderr_path, "ab") if self.stderr_path else asyncio.subprocess.DEVNULL
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                env=self.env, cwd=self.cwd, limit=STREAM_LIMIT)
        except FileNotFoundError as exc:
            raise LLMError(f"codex binary not found ({self.command[0]}): {INSTALL_HINT}") from exc
        finally:
            if self.stderr_path:
                stderr.close()
        self._reader = asyncio.ensure_future(self._read_loop())
        await self.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}})
        await self.notify("initialized")
        account = await self.request("account/read", {})
        self.account = (account or {}).get("account")
        if self.account is None and (account or {}).get("requiresOpenaiAuth", True):
            codex_home = self.env.get("CODEX_HOME", "?")
            raise LLMError("codex app-server is signed out (no account in CODEX_HOME="
                           f"{codex_home}). Sign in once: CODEX_HOME={codex_home} codex login")

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            self._reader.cancel()
        self._fail_all(LLMError("codex app-server stopped"))

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None and self._dead is None

    # -------------------------------------------------------------- wire
    async def _send(self, message):
        if self._dead is not None:
            raise LLMError(f"codex app-server is not running: {self._dead}")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise LLMError("codex app-server closed its input") from exc

    async def request(self, method, params=None, timeout=None):
        req_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            return await asyncio.wait_for(future, timeout or self.request_timeout_s)
        except asyncio.TimeoutError as exc:
            raise LLMError(f"codex app-server did not answer {method} in {timeout or self.request_timeout_s:.0f} s") \
                from exc
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    def subscribe(self, thread_id) -> _Turn:
        turn = _Turn(thread_id=thread_id, queue=asyncio.Queue(maxsize=QUEUE_SIZE))
        self._turns[thread_id] = turn
        if self._dead is not None:
            turn.failed = self._dead
        return turn

    def unsubscribe(self, thread_id):
        self._turns.pop(thread_id, None)

    async def _read_loop(self):
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError as exc:  # line longer than STREAM_LIMIT
                    raise LLMError(f"codex app-server sent a frame over {STREAM_LIMIT} bytes") from exc
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # not protocol output; ignore
                await self._dispatch(message)
            code = await self.proc.wait()
            self._fail_all(LLMError(f"codex app-server exited (code {code})"))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the waiters
            self._fail_all(exc if isinstance(exc, LLMError) else LLMError(f"codex reader failed: {exc!r}"))

    async def _dispatch(self, message):
        has_id, method = "id" in message, message.get("method")
        if has_id and method:  # server-initiated request: always answer
            result, error = decline_result(method, message.get("params"))
            self.declined.append(method)
            reply = {"jsonrpc": "2.0", "id": message["id"]}
            reply.update({"result": result} if error is None else {"error": error})
            try:
                await self._send(reply)
            except LLMError:
                pass
            return
        if has_id:  # response
            future = self._pending.get(message["id"])
            if future is None or future.done():
                return
            if "error" in message and message["error"] is not None:
                err = message["error"] or {}
                future.set_exception(LLMError(f"codex RPC error {err.get('code')}: {err.get('message')}"))
            else:
                future.set_result(message.get("result"))
            return
        if not method:
            return
        params = message.get("params") or {}
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        turn = self._turns.get(thread_id) if thread_id else None
        if turn is None or turn.failed is not None:
            return
        try:
            await asyncio.wait_for(turn.queue.put(message), self.put_timeout_s)
        except asyncio.TimeoutError:
            turn.failed = LLMError(f"codex events not consumed within {self.put_timeout_s:.0f} s (flood)")

    def _fail_all(self, exc):
        if self._dead is None:
            self._dead = exc
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        for turn in list(self._turns.values()):
            if turn.failed is None:
                turn.failed = exc
            try:
                turn.queue.put_nowait({"method": "__dead__"})  # wake a waiting consumer; a full queue is fine
            except asyncio.QueueFull:
                pass

    # -------------------------------------------------------------- one turn
    async def run_turn(self, thread_params, turn_params, timeout_s):
        """Start a thread, run one turn on it -> (text, served_model, usage)."""
        started = await self.request("thread/start", thread_params)
        thread_id = ((started or {}).get("thread") or {}).get("id")
        if not thread_id:
            raise LLMError("codex thread/start returned no thread id")
        served_model = (started or {}).get("model")
        turn = self.subscribe(thread_id)
        try:
            await self.request("turn/start", dict(turn_params, threadId=thread_id))
            text, usage = await self._collect(turn, timeout_s)
            return text, served_model, usage
        finally:
            self.unsubscribe(thread_id)

    async def _collect(self, turn: _Turn, timeout_s):
        deadline = time.monotonic() + timeout_s
        texts: dict = {}        # itemId -> text so far
        order: list = []        # agent message item ids in arrival order
        usage = empty_usage()
        while True:
            if turn.failed is not None:
                raise turn.failed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(f"codex turn did not finish in {timeout_s:.0f} s")
            try:
                message = await asyncio.wait_for(turn.queue.get(), remaining)
            except asyncio.TimeoutError:
                continue
            method, params = message.get("method"), message.get("params") or {}
            if method == "item/agentMessage/delta":
                item_id = params.get("itemId") or "_"
                if item_id not in texts:
                    texts[item_id] = ""
                    order.append(item_id)
                texts[item_id] += params.get("delta") or ""
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    item_id = item.get("id") or "_"
                    if item_id not in texts:
                        order.append(item_id)
                    texts[item_id] = item["text"]
            elif method == "thread/tokenUsage/updated":
                last = (params.get("tokenUsage") or {}).get("last") or {}
                usage = {"input_tokens": int(last.get("inputTokens") or 0),
                         "output_tokens": int(last.get("outputTokens") or 0),
                         "cached_tokens": int(last.get("cachedInputTokens") or 0),
                         "reasoning_tokens": int(last.get("reasoningOutputTokens") or 0),
                         "cost_usd": None}
            elif method == "error":
                if not params.get("willRetry", False):
                    err = params.get("error") or {}
                    raise LLMError(f"codex turn error: {err.get('message') or err}")
            elif method == "turn/completed":
                status = (params.get("turn") or {}).get("status")
                if status != "completed":
                    err = (params.get("turn") or {}).get("error") or {}
                    raise LLMError(f"codex turn {status}: {err.get('message') or 'no details'}")
                return (texts[order[-1]] if order else ""), usage


def strict_compatible(schema) -> bool:
    """True if OpenAI strict structured output accepts the schema: every object lists all its properties
    in `required`, forbids additional properties, and no unsupported keywords are used."""
    if isinstance(schema, list):
        return all(strict_compatible(s) for s in schema)
    if not isinstance(schema, dict):
        return True
    if any(k in schema for k in ("patternProperties", "propertyNames", "dependencies", "if", "not")):
        return False
    props = schema.get("properties")
    if isinstance(props, dict):
        if set(schema.get("required") or []) != set(props) or schema.get("additionalProperties") is not False:
            return False
    return all(strict_compatible(v) for k, v in schema.items()
               if k in ("properties", "items", "anyOf", "allOf", "definitions", "$defs") or isinstance(v, (dict, list))
               and k not in ("enum", "const", "required", "examples", "default"))

class CodexBackend(JsonBackend):
    """agent.llm.Backend over `codex app-server` (subscription; strong-model check)."""
    name = "codex"

    def __init__(self, model=DEFAULT_MODEL, effort=None, codex_home=None, user_home=None, workdir=None,
                 command=None, codex_path=None, search_dirs=None, extra_env=None,
                 request_timeout_s=60.0, turn_timeout_s=300.0, put_timeout_s=10.0, stderr_path=None):
        if effort is not None and not isinstance(effort, str):
            raise ValueError("effort must be a Codex effort name such as 'low' or 'high'")
        self.model = model
        self.effort = effort
        self.codex_home = Path(codex_home or os.environ.get("TRIP_AGENT_CODEX_HOME") or DEFAULT_CODEX_HOME).resolve()
        self.user_home = Path(user_home or os.environ.get("TRIP_AGENT_CODEX_USER_HOME") or DEFAULT_USER_HOME).resolve()
        self.workdir = Path(workdir or DEFAULT_WORKDIR).resolve()
        self._guard_profile()
        self._command = list(command) if command else None
        self._codex_path = codex_path
        self._search_dirs = search_dirs
        self.extra_env = dict(extra_env or {})
        self.request_timeout_s = request_timeout_s
        self.turn_timeout_s = turn_timeout_s
        self.put_timeout_s = put_timeout_s
        self.stderr_path = stderr_path
        self._loop = None
        self._loop_thread = None
        self._server: Optional[CodexAppServer] = None
        self._start_lock = threading.Lock()

    # -------------------------------------------------------------- setup
    def _guard_profile(self):
        real = (Path(os.path.expanduser("~")) / ".codex").resolve()
        if self.codex_home == real:
            raise LLMError(f"refusing to run Codex on the user's own profile ({real}); use .codex-profile/codex")

    def command(self):
        if self._command:
            return self._command
        path = self._codex_path or find_codex(search_dirs=self._search_dirs)
        if not path:
            raise LLMError(f"codex binary not found on PATH or in the usual folders: {INSTALL_HINT}")
        return [path, "app-server"]

    def child_env(self, command):
        env = dict(os.environ)
        env.pop("OPENROUTER_API_KEY", None)          # the child never needs our key
        env["CODEX_HOME"] = str(self.codex_home)
        env["HOME"] = str(self.user_home)
        folder = str(Path(command[0]).parent) if os.sep in command[0] else None
        if folder:  # under nvm the `node` the shim needs sits beside codex
            env["PATH"] = folder + os.pathsep + env.get("PATH", "")
        env.update(self.extra_env)
        return env

    def effort_for(self, thinking):
        return self.effort or EFFORT_MAP[thinking]

    def thread_params(self, req: LLMRequest):
        return {"model": self.model, "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
                "environments": [], "developerInstructions": req.system, "cwd": str(self.workdir),
                "config": {"web_search": "disabled"}}

    def turn_params(self, req: LLMRequest, text: str):
        items = [{"type": "text", "text": text}]
        items += [{"type": "image", "url": url} for url in (req.images or [])]
        params = {"effort": self.effort_for(req.thinking), "input": items}
        if strict_compatible(req.schema):
            params["outputSchema"] = req.schema
        # else: OpenAI strict structured output would reject the schema (optional properties); the prompt asks
        # for JSON only and agent.llm.JsonBackend validates the reply against the full schema with one retry
        return params

    # -------------------------------------------------------------- loop thread
    def _ensure_loop(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, name="codex-app-server",
                                                 daemon=True)
            self._loop_thread.start()

    def _run(self, coro, timeout):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:  # concurrent.futures.TimeoutError is TimeoutError on 3.11
            future.cancel()
            raise LLMError("codex call timed out") from exc

    def _ensure_server(self) -> CodexAppServer:
        with self._start_lock:
            self._ensure_loop()
            if self._server is not None and self._server.alive:
                return self._server
            if self._server is not None:
                self._run(self._server.stop(), 10)
            command = self.command()
            self.workdir.mkdir(parents=True, exist_ok=True)
            self.user_home.mkdir(parents=True, exist_ok=True)
            server = CodexAppServer(command, self.child_env(command), cwd=str(self.workdir),
                                    request_timeout_s=self.request_timeout_s, put_timeout_s=self.put_timeout_s,
                                    stderr_path=self.stderr_path)
            try:
                self._run(server.start(), self.request_timeout_s * 3 + 5)
            except BaseException:
                self._run(server.stop(), 10)
                raise
            self._server = server
            return server

    @property
    def server(self):
        return self._server

    def close(self):
        if self._loop is None:
            return
        if self._server is not None:
            try:
                self._run(self._server.stop(), 10)
            except Exception:
                pass
            self._server = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(5)
        self._loop.close()
        self._loop = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -------------------------------------------------------------- JsonBackend hook
    @staticmethod
    def turn_text(messages):
        """Chat messages (system, user[, assistant, user]) -> the turn's input text."""
        first = messages[1]["content"]
        if isinstance(first, list):  # text part + image parts; images travel separately
            first = next((p.get("text", "") for p in first if p.get("type") == "text"), "")
        if len(messages) <= 2:
            return first
        rejected = messages[-2].get("content") or ""
        feedback = messages[-1].get("content") or ""
        return f"{first}\n\n[Your previous reply]\n{rejected}\n\n[Feedback]\n{feedback}"

    def _call(self, req: LLMRequest, messages: list) -> RawReply:
        server = self._ensure_server()
        text = self.turn_text(messages)
        coro = server.run_turn(self.thread_params(req), self.turn_params(req, text), self.turn_timeout_s)
        reply_text, served_model, usage = self._run(coro, self.turn_timeout_s + self.request_timeout_s * 2 + 5)
        return RawReply(text=reply_text, model=served_model or self.model, provider=PROVIDER, usage=usage)
