"""CodexBackend against the scripted fake app-server (never the real codex binary)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent.llm import LLMError, LLMRequest
from agent.llm_codex import EFFORT_MAP, CodexAppServer, CodexBackend, decline_result, find_codex

FAKE = Path(__file__).parent / "fakes" / "fake_codex_app_server.py"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["message", "n"],
          "properties": {"message": {"type": "string"}, "n": {"type": "integer"}}}
GOOD = json.dumps({"message": "Привет", "n": 3}, ensure_ascii=False)


def make_backend(tmp_path, monkeypatch, replies=(GOOD,), modes=(), **kwargs):
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_CODEX_REPLIES", json.dumps(list(replies)))
    monkeypatch.setenv("FAKE_CODEX_MODE", ",".join(modes))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    backend = CodexBackend(command=[sys.executable, str(FAKE)], codex_home=tmp_path / "codex",
                           user_home=tmp_path / "home", workdir=tmp_path / "work",
                           request_timeout_s=10, turn_timeout_s=20, **kwargs)
    return backend, log


def read_log(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def req(thinking="off", images=()):
    return LLMRequest(card="present.variants", system="SYSTEM PROMPT", user='{"x": 1}', schema=SCHEMA,
                      thinking=thinking, images=list(images))


def test_happy_path_sends_isolated_profile_thread_and_output_schema(tmp_path, monkeypatch):
    backend, log = make_backend(tmp_path, monkeypatch)
    with backend:
        result = backend.complete(req(thinking="high", images=["data:image/png;base64,AAAA"]))
    assert result.data == {"message": "Привет", "n": 3}
    assert result.model == "gpt-6-luna" and result.provider == "codex" and result.retries == 0
    assert result.usage["input_tokens"] == 120 and result.usage["output_tokens"] == 30
    assert result.usage["cached_tokens"] == 20 and result.usage["reasoning_tokens"] == 7
    assert result.usage["cost_usd"] is None
    records = read_log(log)
    env = records[0]["env"]
    assert env["CODEX_HOME"] == str((tmp_path / "codex").resolve())
    assert env["HOME"] == str((tmp_path / "home").resolve())
    assert env["OPENROUTER_API_KEY"] is False
    by_method = {r.get("method"): r for r in records if "method" in r}
    assert by_method["initialize"]["params"]["capabilities"] == {"experimentalApi": True}
    assert "initialized" in by_method and "account/read" in by_method
    thread = by_method["thread/start"]["params"]
    assert thread["model"] == "gpt-6-luna" and thread["ephemeral"] is True
    assert thread["sandbox"] == "read-only" and thread["approvalPolicy"] == "never"
    assert thread["environments"] == [] and thread["config"] == {"web_search": "disabled"}
    assert thread["developerInstructions"] == "SYSTEM PROMPT"
    turn = by_method["turn/start"]["params"]
    assert turn["effort"] == "high" and turn["outputSchema"] == SCHEMA
    assert turn["input"][0] == {"type": "text", "text": '{"x": 1}'}
    assert turn["input"][1] == {"type": "image", "url": "data:image/png;base64,AAAA"}


def test_effort_mapping_and_override(tmp_path, monkeypatch):
    assert EFFORT_MAP == {"off": "low", "low": "low", "high": "high"}
    backend, log = make_backend(tmp_path, monkeypatch, effort="high")
    with backend:
        backend.complete(req(thinking="off"))
    turn = next(r for r in read_log(log) if r.get("method") == "turn/start")
    assert turn["params"]["effort"] == "high"


def test_one_ephemeral_thread_per_call_on_a_warm_process(tmp_path, monkeypatch):
    backend, log = make_backend(tmp_path, monkeypatch)
    with backend:
        backend.complete(req())
        first = backend.server
        backend.complete(req())
        assert backend.server is first           # process kept warm
    records = read_log(log)
    assert sum(1 for r in records if r.get("method") == "thread/start") == 2
    assert sum(1 for r in records if r.get("method") == "initialize") == 1


def test_burst_of_400_deltas_is_not_dropped(tmp_path, monkeypatch):
    long_message = "б" * 4000
    reply = json.dumps({"message": long_message, "n": 1}, ensure_ascii=False)
    backend, _ = make_backend(tmp_path, monkeypatch, replies=[reply], modes=["burst", "no_completed_item"],
                              put_timeout_s=2)
    with backend:
        result = backend.complete(req())
    assert result.data["message"] == long_message


def test_server_initiated_requests_are_declined(tmp_path, monkeypatch):
    backend, log = make_backend(tmp_path, monkeypatch, modes=["approval", "unknown_request"])
    with backend:
        result = backend.complete(req())
        assert "item/commandExecution/requestApproval" in backend.server.declined
    assert result.data["n"] == 3
    records = read_log(log)
    approval = next(r["approval_response"] for r in records if "approval_response" in r)
    assert approval["result"] == {"decision": "decline"}
    unknown = next(r["unknown_response"] for r in records if "unknown_response" in r)
    assert unknown["error"]["code"] == -32601


def test_decline_shapes_follow_the_protocol():
    assert decline_result("item/fileChange/requestApproval", {})[0] == {"decision": "decline"}
    assert decline_result("execCommandApproval", {})[0]["decision"]["denied"]["rejection"]
    assert decline_result("mcpServer/elicitation/request", {})[0] == {"action": "decline"}
    assert decline_result("item/tool/call", {})[0]["success"] is False
    assert decline_result("item/tool/requestUserInput", {})[0] == {"answers": {}}
    assert decline_result("account/chatgptAuthTokens/refresh", {})[1]["code"] == -32601


def test_signed_out_raises_clear_error(tmp_path, monkeypatch):
    backend, _ = make_backend(tmp_path, monkeypatch, modes=["signed_out"])
    with backend, pytest.raises(LLMError) as err:
        backend.complete(req())
    assert "signed out" in str(err.value) and "codex login" in str(err.value)


def test_error_notification_fails_only_without_retry(tmp_path, monkeypatch):
    backend, _ = make_backend(tmp_path, monkeypatch, modes=["error_retry"])
    with backend:
        assert backend.complete(req()).data["n"] == 3
    backend, _ = make_backend(tmp_path, monkeypatch, modes=["error_fatal"])
    with backend, pytest.raises(LLMError) as err:
        backend.complete(req())
    assert "usage limit reached" in str(err.value)


def test_failed_turn_status_raises(tmp_path, monkeypatch):
    backend, _ = make_backend(tmp_path, monkeypatch, modes=["turn_failed"])
    with backend, pytest.raises(LLMError, match="failed"):
        backend.complete(req())


def test_retry_once_on_invalid_json_then_valid(tmp_path, monkeypatch):
    backend, log = make_backend(tmp_path, monkeypatch, replies=["not json at all", GOOD])
    with backend:
        result = backend.complete(req())
    assert result.retries == 1 and result.data["n"] == 3
    assert result.validation_errors and "invalid JSON" in result.validation_errors[0]
    turns = [r for r in read_log(log) if r.get("method") == "turn/start"]
    assert len(turns) == 2
    retry_text = turns[1]["params"]["input"][0]["text"]
    assert "not json at all" in retry_text and "rejected" in retry_text
    assert result.usage["input_tokens"] == 240   # both attempts counted


def test_invalid_twice_raises_after_one_retry(tmp_path, monkeypatch):
    backend, _ = make_backend(tmp_path, monkeypatch, replies=['{"message": "x"}'])
    with backend, pytest.raises(LLMError) as err:
        backend.complete(req())
    assert err.value.retries == 1 and err.value.card == "present.variants"


def test_preamble_message_is_ignored_last_message_wins(tmp_path, monkeypatch):
    backend, _ = make_backend(tmp_path, monkeypatch, modes=["preamble"])
    with backend:
        assert backend.complete(req()).data["n"] == 3


def test_process_death_mid_turn_fails_and_next_call_restarts(tmp_path, monkeypatch):
    backend, log = make_backend(tmp_path, monkeypatch, modes=["die_mid_turn"])
    with backend:
        with pytest.raises(LLMError, match="exited"):
            backend.complete(req())
        monkeypatch.setenv("FAKE_CODEX_MODE", "")
        assert backend.complete(req()).data["n"] == 3   # a fresh process
    assert sum(1 for r in read_log(log) if r.get("method") == "initialize") == 2


def test_missing_binary_and_real_profile_are_refused(tmp_path, monkeypatch):
    assert find_codex(path_env=str(tmp_path), search_dirs=[tmp_path]) is None
    backend = CodexBackend(codex_home=tmp_path / "c", user_home=tmp_path / "h", workdir=tmp_path / "w",
                           codex_path=None, search_dirs=[tmp_path])
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(LLMError, match="not found"):
        backend.complete(req())
    backend.close()
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(LLMError, match="refusing"):
        CodexBackend(codex_home=tmp_path / ".codex")


def test_unread_events_count_as_flood_not_a_hang():
    async def scenario():
        server = CodexAppServer(["unused"], {}, put_timeout_s=0.05)
        turn = server.subscribe("thr_x")
        turn.queue = asyncio.Queue(maxsize=2)
        for i in range(3):
            await server._dispatch({"method": "item/agentMessage/delta",
                                    "params": {"threadId": "thr_x", "itemId": "m", "delta": str(i)}})
        return turn
    turn = asyncio.run(scenario())
    assert turn.failed is not None and "flood" in str(turn.failed)
