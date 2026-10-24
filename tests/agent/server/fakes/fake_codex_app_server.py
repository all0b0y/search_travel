"""A scripted stand-in for `codex app-server` (JSON-RPC over stdio, one JSON object per line).

Behaviour is driven by environment variables:
  FAKE_CODEX_REPLIES   JSON list of reply texts, one per turn (cycled); default ['{"ok": true}']
  FAKE_CODEX_MODE      comma list of: signed_out, burst, approval, error_fatal, error_retry, preamble,
                       turn_failed, die_mid_turn, no_completed_item, unknown_request
  FAKE_CODEX_LOG       file to append every received message to (one JSON per line), plus an
                       {"env": {...}} record at start with CODEX_HOME and HOME
Never contacts a network or a model.
"""
import json
import os
import sys

MODES = set(filter(None, os.environ.get("FAKE_CODEX_MODE", "").split(",")))
REPLIES = json.loads(os.environ.get("FAKE_CODEX_REPLIES") or '["{\\"ok\\": true}"]')
LOG = os.environ.get("FAKE_CODEX_LOG")
state = {"threads": 0, "turns": 0, "server_req": 1000}


def log(record):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def send(obj, flush=True):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    if flush:
        sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    msg = json.loads(line)
    log(msg)
    return msg


def wait_response(req_id):
    """Read until the client's response to our server-initiated request arrives (log the rest)."""
    pending = []
    while True:
        msg = read()
        if msg.get("id") == req_id and "method" not in msg:
            return msg, pending
        pending.append(msg)


def chunks(text, n):
    size = max(1, len(text) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


def run_turn(thread_id, turn_id):
    reply = REPLIES[state["turns"] % len(REPLIES)]
    state["turns"] += 1
    note = lambda method, params: send({"method": method, "params": dict(params, threadId=thread_id)})
    note("turn/started", {"turn": {"id": turn_id, "items": [], "status": "inProgress"}})
    if "approval" in MODES:
        state["server_req"] += 1
        rid = state["server_req"]
        send({"id": rid, "method": "item/commandExecution/requestApproval",
              "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "cmd1", "command": "ls"}})
        resp, _ = wait_response(rid)
        log({"approval_response": resp})
        if (resp.get("result") or {}).get("decision") != "decline":
            sys.exit(3)
    if "unknown_request" in MODES:
        state["server_req"] += 1
        rid = state["server_req"]
        send({"id": rid, "method": "some/futureRequest", "params": {"threadId": thread_id}})
        resp, _ = wait_response(rid)
        log({"unknown_response": resp})
    if "error_retry" in MODES:
        note("error", {"turnId": turn_id, "willRetry": True, "error": {"message": "transient, retrying"}})
    if "error_fatal" in MODES:
        note("error", {"turnId": turn_id, "willRetry": False, "error": {"message": "usage limit reached"}})
        note("turn/completed", {"turn": {"id": turn_id, "items": [], "status": "failed",
                                         "error": {"message": "usage limit reached"}}})
        return
    if "preamble" in MODES:
        note("item/agentMessage/delta", {"turnId": turn_id, "itemId": "pre", "delta": "Let me think. "})
        note("item/completed", {"turnId": turn_id, "item": {"type": "agentMessage", "id": "pre",
                                                             "text": "Let me think."}})
    if "die_mid_turn" in MODES:
        note("item/agentMessage/delta", {"turnId": turn_id, "itemId": "m1", "delta": reply[:3]})
        sys.exit(1)
    parts = chunks(reply, 400) if "burst" in MODES else chunks(reply, 5)
    burst = "burst" in MODES
    for part in parts:  # a burst is written in one flush
        send({"method": "item/agentMessage/delta",
              "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "m1", "delta": part}},
             flush=not burst)
    sys.stdout.flush()
    if "no_completed_item" not in MODES:
        note("item/completed", {"turnId": turn_id, "item": {"type": "agentMessage", "id": "m1", "text": reply}})
    note("thread/tokenUsage/updated", {"turnId": turn_id, "tokenUsage": {
        "last": {"inputTokens": 120, "outputTokens": 30, "cachedInputTokens": 20, "reasoningOutputTokens": 7,
                 "totalTokens": 150},
        "total": {"inputTokens": 120, "outputTokens": 30, "cachedInputTokens": 20, "reasoningOutputTokens": 7,
                  "totalTokens": 150}}})
    status = "failed" if "turn_failed" in MODES else "completed"
    note("turn/completed", {"turn": {"id": turn_id, "items": [], "status": status,
                                     "error": {"message": "model refused"} if status == "failed" else None}})


def main():
    log({"env": {"CODEX_HOME": os.environ.get("CODEX_HOME"), "HOME": os.environ.get("HOME"),
                 "OPENROUTER_API_KEY": "OPENROUTER_API_KEY" in os.environ}, "argv": sys.argv[1:]})
    while True:
        msg = read()
        method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if req_id is None:
            continue  # notifications (initialized) need no answer
        if method == "initialize":
            send({"id": req_id, "result": {"userAgent": "fake-codex/0.0"}})
        elif method == "account/read":
            account = None if "signed_out" in MODES else {"type": "chatgpt", "email": None, "planType": "pro"}
            send({"id": req_id, "result": {"account": account, "requiresOpenaiAuth": True}})
        elif method == "thread/start":
            state["threads"] += 1
            tid = f"thr_{state['threads']}"
            send({"id": req_id, "result": {"thread": {"id": tid}, "model": params.get("model"),
                                           "modelProvider": "openai", "cwd": params.get("cwd") or "/",
                                           "approvalPolicy": "never", "approvalsReviewer": "user",
                                           "sandbox": {"type": "readOnly"}}})
        elif method == "turn/start":
            tid = params["threadId"]
            turn_id = f"turn_{state['turns'] + 1}"
            send({"id": req_id, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
            run_turn(tid, turn_id)
        else:
            send({"id": req_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
