"""Eval persona: plays the traveller in level-3 scenarios.

    persona = ClaudeCodePersona()                    # Sonnet 5.5 through the Claude Code CLI
    reply = persona.reply(scenario, transcript, response)
    # {"type": "text", "text": ...} | {"type": "action", "action": {"type", "payload"}, "label": ...}
    # | {"type": "stop", "outcome": "done"|"failed", "reason": ...};  reply["meta"] = {model, duration_ms, ...}

The persona model sees the agent response rendered like the UI (evals.render_ui) with clickable
references and answers with evals/schemas/persona_reply.schema.json; clicks are resolved here
into request actions (checkout gets the travellers and contact from the scenario profile, like a
filled-in form).

ClaudeCodePersona runs exactly the command of AGENT_PLAN.md section 2 with a working directory
OUTSIDE the repository (no project CLAUDE.md is picked up), never `--bare`, never another HOME.
"""
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Protocol

import jsonschema

from .common import ROOT, SKILLS_DIR, load_schema
from .render_ui import build_refs, render

PERSONA_MODEL = "claude-sonnet-5-5"
PERSONA_SKILL = SKILLS_DIR / "persona" / "SKILL.md"
REPLY_SCHEMA_FILE = "persona_reply.schema.json"
SCENARIO_PLACEHOLDER = "{{scenario}}"


class PersonaError(Exception):
    def __init__(self, message, raw=None, meta=None):
        super().__init__(message)
        self.raw = raw
        self.meta = meta or {}


class Persona(Protocol):
    name: str

    def reply(self, scenario: dict, transcript: list, response: dict) -> dict: ...


# ---------------------------------------------------------------- shared

def reply_schema():
    return load_schema(REPLY_SCHEMA_FILE)


def scenario_for_persona(scenario):
    """What the persona may know: everything about the traveller, nothing about the checks."""
    p = scenario["persona"]
    return {
        "id": scenario["id"],
        "start_message": p["start_message"],
        "has_photo": bool(p.get("image")),
        "profile": p["profile"],
        "hidden_facts": p["hidden"]["facts"],
        "wishes": p["hidden"].get("nice", []),
        "behaviour": p["behaviour"],
        "max_turns": scenario["max_turns"],
    }


def render_system_prompt(scenario, skill_path=PERSONA_SKILL):
    text = Path(skill_path).read_text(encoding="utf-8")
    block = "```json\n" + json.dumps(scenario_for_persona(scenario), ensure_ascii=False, indent=2) + "\n```"
    if SCENARIO_PLACEHOLDER in text:
        return text.replace(SCENARIO_PLACEHOLDER, block)
    return text + "\n\n## Your scenario\n\n" + block + "\n"


def render_turn_prompt(transcript, response, turn, max_turns):
    screen, _ = render(response, with_refs=True)
    history = "\n\n".join(t["text"] for t in transcript[:-1]) if len(transcript) > 1 else "(это первый ответ агента)"
    return (f"Переписка до этого момента:\n\n{history}\n\n"
            f"Сейчас на экране (ответ агента, ход {turn} из {max_turns}):\n\n{screen}\n\n"
            "Ответь одним JSON-объектом по схеме ответа: type=click с ref, type=text с text или type=stop.")


def validate_reply(data):
    errors = sorted(jsonschema.Draft7Validator(reply_schema()).iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        raise PersonaError(f"persona reply invalid: {errors[0].message}", raw=data)
    t = data["type"]
    if t == "text" and not (data.get("text") or "").strip():
        raise PersonaError("persona reply type=text without text", raw=data)
    if t == "click" and not data.get("ref"):
        raise PersonaError("persona reply type=click without ref", raw=data)
    return data


def resolve_reply(data, response, scenario):
    """Model reply -> runner reply: clicks become request actions."""
    t = data["type"]
    if t == "text":
        return {"type": "text", "text": data["text"].strip()}
    if t == "stop":
        return {"type": "stop", "outcome": data.get("outcome") or "done", "reason": data.get("reason") or ""}
    refs = build_refs(response)
    ref = data["ref"]
    if ref not in refs:
        raise PersonaError(f"persona clicked an unknown reference {ref!r}; available {sorted(refs)}", raw=data)
    action = {"type": refs[ref]["type"], "payload": json.loads(json.dumps(refs[ref]["payload"]))}
    fill_action(action, response, scenario)
    return {"type": "action", "action": action, "label": refs[ref]["label"], "ref": ref}


def fill_action(action, response, scenario):
    """Complete what a UI form would add: travellers/contact for checkout, order id for pay."""
    profile = scenario["persona"]["profile"]
    if action["type"] == "checkout":
        p = action["payload"]
        if not p.get("travellers"):
            p["travellers"] = [dict(t) for t in profile["travellers"]]
        if not p.get("contact"):
            p["contact"] = dict(profile["contact"])
    if action["type"] in ("pay", "order_action") and not action["payload"].get("order_id"):
        order = response.get("order") or {}
        if order.get("order_id"):
            action["payload"]["order_id"] = order["order_id"]
    if action["type"] == "order_action":
        action["payload"].setdefault("params", {})
    return action


# ---------------------------------------------------------------- Claude Code persona

def default_cwd():
    """A scratch directory outside the repository (TMPDIR points to ~/.hermes/cache/scratch here)."""
    base = Path(tempfile.gettempdir()) / "persona-cwd"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=base))


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def parse_claude_output(stdout):
    """Claude Code `--output-format json` stdout -> (reply dict, meta).

    Handles a single result object or a list of events (the last `type: result` wins); the reply is
    `structured_output` (with --json-schema) or the JSON text in `result` (fences stripped).
    """
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PersonaError(f"claude output is not JSON: {exc}", raw=stdout) from None
    if isinstance(obj, list):
        results = [e for e in obj if isinstance(e, dict) and e.get("type") == "result"]
        if not results:
            raise PersonaError("claude output has no result event", raw=stdout)
        obj = results[-1]
    if not isinstance(obj, dict):
        raise PersonaError("claude output is not an object", raw=stdout)
    models = list((obj.get("modelUsage") or {}).keys())
    meta = {"model": models[0] if len(models) == 1 else (models or [obj.get("model")])[0],
            "models": models, "duration_ms": obj.get("duration_ms"), "cost_usd": obj.get("total_cost_usd"),
            "session_id": obj.get("session_id"), "num_turns": obj.get("num_turns")}
    if obj.get("is_error") or (obj.get("subtype") not in (None, "success")):
        raise PersonaError(f"claude reported an error: {obj.get('subtype')}: {str(obj.get('result'))[:200]}",
                           raw=stdout, meta=meta)
    data = obj.get("structured_output")
    if data is None:
        text = obj.get("result")
        if not isinstance(text, str) or not text.strip():
            raise PersonaError("claude output has neither structured_output nor result text", raw=stdout, meta=meta)
        from agent.llm import strip_fences
        try:
            data = json.loads(strip_fences(text))
        except json.JSONDecodeError as exc:
            raise PersonaError(f"claude result is not JSON: {exc}", raw=stdout, meta=meta) from None
    if not isinstance(data, dict):
        raise PersonaError("persona reply is not an object", raw=stdout, meta=meta)
    return data, meta


class ClaudeCodePersona:
    name = "claude-code"

    def __init__(self, claude_bin="claude", model=PERSONA_MODEL, cwd=None, timeout_s=240, skill_path=PERSONA_SKILL,
                 runner=subprocess.run, env=None):
        self.claude_bin = claude_bin
        self.model = model
        self.cwd = Path(cwd) if cwd else default_cwd()
        if _inside(self.cwd, ROOT):
            raise ValueError(f"persona cwd must be outside the repository: {self.cwd}")
        self.timeout_s = timeout_s
        self.skill_path = skill_path
        self.runner = runner
        self.env = env  # None = inherit (same HOME: subscription auth)
        self._prompt_files = {}

    def system_prompt_file(self, scenario):
        key = scenario["id"]
        if key not in self._prompt_files:
            path = self.cwd / f"persona-{key}.md"
            path.write_text(render_system_prompt(scenario, self.skill_path), encoding="utf-8")
            self._prompt_files[key] = path
        return self._prompt_files[key]

    def command(self, system_prompt_file):
        return [self.claude_bin, "-p", "--model", self.model,
                "--system-prompt-file", str(system_prompt_file),
                "--tools", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "",
                "--no-session-persistence",
                "--output-format", "json",
                "--json-schema", json.dumps(reply_schema(), ensure_ascii=False, separators=(",", ":"))]

    def reply(self, scenario, transcript, response):
        turn = sum(1 for t in transcript if t.get("role") == "agent")
        prompt = render_turn_prompt(transcript, response, turn, scenario["max_turns"])
        cmd = self.command(self.system_prompt_file(scenario))
        assert "--bare" not in cmd
        started = time.monotonic()
        try:
            proc = self.runner(cmd, input=prompt, capture_output=True, text=True, cwd=str(self.cwd),
                               timeout=self.timeout_s, env=self.env)
        except subprocess.TimeoutExpired:
            raise PersonaError(f"claude timed out after {self.timeout_s}s") from None
        except OSError as exc:
            raise PersonaError(f"cannot run {self.claude_bin}: {exc}") from None
        wall_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0 and not (proc.stdout or "").strip():
            raise PersonaError(f"claude exited {proc.returncode}: {(proc.stderr or '')[:300]}",
                               meta={"duration_ms": wall_ms})
        data, meta = parse_claude_output(proc.stdout)
        meta["wall_ms"] = wall_ms
        validate_reply(data)
        out = resolve_reply(data, response, scenario)
        out["meta"] = dict(meta, raw=data)
        return out


# ---------------------------------------------------------------- fakes

class FakePersona:
    """Scripted replies for tests: each item is a model-level reply ({"type": "click", "ref": "A1"},
    {"type": "text", "text": ...}, {"type": "stop", ...}) or a callable(response) -> such a dict."""
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def reply(self, scenario, transcript, response):
        self.seen.append(response)
        if not self.replies:
            return {"type": "stop", "outcome": "failed", "reason": "сценарий закончился", "meta": {"model": "fake"}}
        item = self.replies.pop(0)
        data = item(response) if callable(item) else item
        validate_reply(data)
        out = resolve_reply(data, response, scenario)
        out["meta"] = {"model": "fake/persona", "duration_ms": 0}
        return out


class ButtonPersona:
    """Deterministic offline persona for dry runs: always clicks the obvious next button
    (pay > checkout > recommended variant > first destination > recommended answer), and stops
    when the order is paid."""
    name = "buttons"

    def reply(self, scenario, transcript, response):
        data = self.decide(response, scenario)
        validate_reply(data)
        out = resolve_reply(data, response, scenario)
        out["meta"] = {"model": "fake/buttons", "duration_ms": 0}
        return out

    @staticmethod
    def decide(response, scenario):
        order = response.get("order") or {}
        if response.get("stage") == "aftercare":
            return {"type": "stop", "outcome": "done", "reason": "вопрос после покупки решён"}
        if order.get("status") == "paid":
            if scenario.get("stop_on_paid", True) is False:
                for i, a in enumerate(response.get("actions") or [], 1):
                    if a["type"] == "order_action":
                        return {"type": "click", "ref": f"A{i}"}
            return {"type": "stop", "outcome": "done", "reason": "поездка оплачена"}
        actions = response.get("actions") or []
        for wanted in ("pay", "checkout"):
            for i, a in enumerate(actions, 1):
                if a["type"] == wanted:
                    return {"type": "click", "ref": f"A{i}"}
        variants = (response.get("cards") or {}).get("variants") or []
        if variants:
            idx = next((i for i, v in enumerate(variants, 1) if v.get("recommended")), 1)
            return {"type": "click", "ref": f"V{idx}"}
        if (response.get("cards") or {}).get("destinations"):
            return {"type": "click", "ref": "D1"}
        for qi, q in enumerate(response.get("questions") or [], 1):
            ci = next((i for i, c in enumerate(q["choices"], 1) if c.get("recommended")), 1)
            return {"type": "click", "ref": f"Q{qi}.{ci}"}
        return {"type": "stop", "outcome": "failed", "reason": "непонятно, что нажать дальше"}


def make_persona(kind, claude_bin="claude", cwd: Optional[str] = None):
    if kind == "claude":
        return ClaudeCodePersona(claude_bin=claude_bin, cwd=cwd)
    if kind == "buttons":
        return ButtonPersona()
    raise ValueError(f"unknown persona kind: {kind}")


__all__ = ["Persona", "PersonaError", "ClaudeCodePersona", "FakePersona", "ButtonPersona", "make_persona",
           "parse_claude_output", "render_system_prompt", "render_turn_prompt", "resolve_reply"]
