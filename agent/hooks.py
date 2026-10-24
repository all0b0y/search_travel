"""Hook registry (AGENT_PLAN.md section 9).

    hooks = Hooks()
    hooks.register("after_variants", fn, "validate")   # fn(ctx) -> list[Issue]; blocking
    hooks.register("after_present", fn, "prefetch")    # fn(ctx) -> None; starts background work
    hooks.register("turn_end", fn, "analytics")        # fn(ctx) -> None; fire-and-forget
    issues = hooks.run("after_variants", ctx)

Every hook call leaves a verdict in `ctx.trace["hooks"]` (and so in `debug.hooks`):
{event, name, kind, verdict: ok|warn|block|error, issues, ms}. A validate hook that raises is a
programming error and propagates; prefetch/analytics hooks never break a turn.
"""
import time
from dataclasses import dataclass
from typing import Callable

EVENTS = ("after_understand", "after_search", "after_variants", "before_present", "after_present",
          "after_travellers", "turn_end")
KINDS = ("validate", "prefetch", "analytics")
SEVERITIES = ("block", "warn")


def issue(code, message, severity="block", **data):
    """Issue = {code, severity: block|warn, message, data} (AGENT_PLAN.md "Module interfaces")."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    return {"code": code, "severity": severity, "message": message, "data": data}


def as_issue(obj):
    """Normalise an issue from any producer (dict, pydantic model, dataclass) to the dict form."""
    if isinstance(obj, dict):
        d = dict(obj)
    elif hasattr(obj, "model_dump"):
        d = obj.model_dump(mode="json")
    elif hasattr(obj, "__dict__"):
        d = dict(vars(obj))
    else:
        d = {"code": "unknown", "message": str(obj)}
    sev = d.get("severity", "block")
    sev = {"blocking": "block", "warning": "warn", "info": "warn", "error": "block"}.get(sev, sev)
    return {"code": str(d.get("code") or "unknown"), "severity": sev if sev in SEVERITIES else "block",
            "message": str(d.get("message") or ""), "data": dict(d.get("data") or {})}


def blocking(issues):
    return [i for i in issues if i["severity"] == "block"]


@dataclass
class Hook:
    event: str
    fn: Callable
    kind: str
    name: str


class Hooks:
    def __init__(self):
        self._hooks: list[Hook] = []

    def register(self, event, fn, kind, name=None):
        if event not in EVENTS:
            raise ValueError(f"unknown hook event: {event}")
        if kind not in KINDS:
            raise ValueError(f"unknown hook kind: {kind}")
        self._hooks.append(Hook(event, fn, kind, name or getattr(fn, "__name__", "hook")))
        return fn

    def unregister(self, name):
        self._hooks = [h for h in self._hooks if h.name != name]

    def registered(self, event=None, kind=None):
        return [h for h in self._hooks if (event is None or h.event == event) and (kind is None or h.kind == kind)]

    def copy(self):
        other = Hooks()
        other._hooks = list(self._hooks)
        return other

    def run(self, event, ctx, kinds=KINDS):
        """Run the hooks of `event`; returns the issues of the validate hooks (normalised)."""
        found = []
        for hook in self.registered(event):
            if hook.kind not in kinds:
                continue
            started = time.perf_counter()
            record = {"event": event, "name": hook.name, "kind": hook.kind, "verdict": "ok", "issues": []}
            if hook.kind == "validate":
                issues = [as_issue(i) for i in (hook.fn(ctx) or [])]
                record["issues"] = issues
                record["verdict"] = "block" if blocking(issues) else ("warn" if issues else "ok")
                found.extend(issues)
            else:
                try:
                    hook.fn(ctx)
                except Exception as exc:  # background / analytics hooks never break a turn
                    record["verdict"] = "error"
                    record["error"] = f"{type(exc).__name__}: {exc}"
            record["ms"] = round((time.perf_counter() - started) * 1000, 2)
            trace = getattr(ctx, "trace", None)
            if isinstance(trace, dict):
                trace.setdefault("hooks", []).append(record)
        return found


DEFAULT_HOOKS = Hooks()


def register(event, fn, kind, name=None):
    """Register on the module-level default registry (copied into every new Engine)."""
    return DEFAULT_HOOKS.register(event, fn, kind, name)
