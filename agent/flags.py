"""Feature flags and activation stages (AGENT_PLAN.md "Ступени включения", section 9).

    TRIP_AGENT_FLAGS=stage3                    # a preset
    TRIP_AGENT_FLAGS=stage3,-jev_guard,+prefetch
    TRIP_AGENT_FLAGS=sensitivity,jev_routing   # plain names enable flags on top of stage1
    TRIP_AGENT_FLAGS=stage3,guide              # guide mode (decisions 23-26) on top of a preset
Unset or empty -> stage1. Unknown tokens raise ValueError. Opt-in flags (`guide`) are in no preset; they are
reported by as_dict() only when on, so a flag-off engine answers exactly as before.
"""
import os
from dataclasses import asdict, dataclass

FLAG_NAMES = ("sensitivity", "jev_routing", "jev_ask", "jev_guard", "prefetch", "upsell", "insights")
OPT_IN_FLAGS = ("guide",)   # never in a preset: --flags guide / TRIP_AGENT_FLAGS=guide
ALL_FLAGS = FLAG_NAMES + OPT_IN_FLAGS

_S2 = ("sensitivity",)
_S3 = _S2 + ("jev_routing", "jev_ask", "jev_guard")
_S4 = _S3 + ("prefetch",)
_S5 = _S4 + ("upsell", "insights")
PRESETS = {"stage1": (), "stage2": _S2, "stage3": _S3, "stage4": _S4, "stage5": _S5}
DEFAULT_PRESET = "stage1"
ENV_NAME = "TRIP_AGENT_FLAGS"


@dataclass(frozen=True)
class Flags:
    sensitivity: bool = False
    jev_routing: bool = False
    jev_ask: bool = False
    jev_guard: bool = False
    prefetch: bool = False
    upsell: bool = False
    insights: bool = False
    guide: bool = False            # guide mode: a reasoning model decides each free-text turn (agent/guide.py)
    preset: str = DEFAULT_PRESET   # the preset the spec started from (for reports)

    def as_dict(self):
        out = asdict(self)
        for name in OPT_IN_FLAGS:
            if not out[name]:
                out.pop(name)
        return out

    def enabled(self):
        return tuple(name for name in ALL_FLAGS if getattr(self, name))


def preset(name):
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name}")
    return Flags(preset=name, **{f: True for f in PRESETS[name]})


def parse_flags(spec=None, environ=None):
    """Parse a flag spec (default: $TRIP_AGENT_FLAGS)."""
    if spec is None:
        spec = (os.environ if environ is None else environ).get(ENV_NAME, "")
    tokens = [t.strip() for t in spec.split(",") if t.strip()]
    base = DEFAULT_PRESET
    if tokens and tokens[0] in PRESETS:
        base = tokens.pop(0)
    values = {f: f in PRESETS[base] for f in ALL_FLAGS}
    for token in tokens:
        if token in PRESETS:
            raise ValueError(f"preset must come first: {token}")
        sign, name = (token[0], token[1:]) if token[0] in "+-" else ("+", token)
        if name not in ALL_FLAGS:
            raise ValueError(f"unknown flag: {name}")
        values[name] = sign == "+"
    return Flags(preset=base, **values)
