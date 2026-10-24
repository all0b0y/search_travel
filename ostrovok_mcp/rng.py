"""Deterministic randomness: every draw is sha256(entity | field | date | ...).

Never Python's global `random` state, never the wall clock. The same parts always give the
same number, so every generator is a pure function of its inputs.
"""
from __future__ import annotations

import hashlib
from typing import Any, Sequence, TypeVar

T = TypeVar("T")


def digest(*parts: Any) -> bytes:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()


def unit(*parts: Any) -> float:
    """Uniform float in [0, 1)."""
    return int.from_bytes(digest(*parts)[:8], "big") / 2 ** 64


def between(lo: float, hi: float, *parts: Any) -> float:
    return lo + (hi - lo) * unit(*parts)


def rint(lo: int, hi: int, *parts: Any) -> int:
    """Uniform int in [lo, hi]."""
    return min(hi, lo + int(unit(*parts) * (hi - lo + 1)))


def pick(seq: Sequence[T], *parts: Any) -> T:
    return seq[min(len(seq) - 1, int(unit(*parts) * len(seq)))]


def hexid(*parts: Any, n: int = 12) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:n]


def code(alphabet: str, length: int, *parts: Any) -> str:
    raw = digest(*parts)
    return "".join(alphabet[b % len(alphabet)] for b in raw[:length])
