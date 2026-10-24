"""Jev decisions (typesafe/jev-1.13 through OpenRouter's Decisions API), fail-open.

    decide(state, questions, timeout_s=1.5) -> {"answers": {...}, "meta": {...}} | None

Request:  POST https://openrouter.ai/api/alpha/decisions {model, state, questions}
Question specs (build them with noul / choice / score):
    {"type": "noul",   "instructions": str, "criteria": {"true": str, "false": str}}
    {"type": "choice", "instructions": str, "criteria": {option: description}}
    {"type": "score",  "instructions": str, "criteria": [level0, level1, ...]}
Answers (as returned):
    noul   -> {"type": "noul", "noul": p_true}                 (0.5 = cannot tell)
    choice -> {"type": "choice", "choice": opt, "probabilities": {...}, "confidence": c}
    score  -> {"type": "score", "score": x, "legend": {...}, "probabilities": {...}, "confidence": c}
Any error, timeout, missing key or malformed body -> None; callers fall back to rules or Flash.
"""
import json
import time

import httpx

from .env import get_openrouter_key

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
QUESTION_TYPES = ("noul", "choice", "score")


def noul(instructions, true_criteria=None, false_criteria=None):
    spec = {"type": "noul", "instructions": instructions}
    if true_criteria is not None or false_criteria is not None:
        if not (true_criteria and false_criteria):
            raise ValueError("noul criteria need both true and false")
        spec["criteria"] = {"true": true_criteria, "false": false_criteria}
    return spec


def choice(instructions, options):
    """options: {option: criteria}. Give one a no-match option: Jev cannot pick what it was not offered."""
    if not options or not isinstance(options, dict):
        raise ValueError("choice needs a non-empty {option: criteria} dict")
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions, levels):
    """levels: ordered list, lowest first."""
    if not levels or len(levels) < 2:
        raise ValueError("score needs at least two levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def _valid_answers(answers, questions):
    if not isinstance(answers, dict):
        return False
    for name, spec in questions.items():
        ans = answers.get(name)
        if not isinstance(ans, dict):
            return False
        kind = spec.get("type")
        if kind == "noul" and not isinstance(ans.get("noul"), (int, float)):
            return False
        if kind == "choice" and ans.get("choice") not in spec.get("criteria", {}):
            return False
        if kind == "score" and not isinstance(ans.get("score"), (int, float)):
            return False
    return True


def decide(state, questions, timeout_s=1.5, client=None, api_key=None, url=JEV_URL, model=JEV_MODEL):
    """Ask Jev; return {"answers", "meta": {model, provider, latency_ms, cost_usd, input_tokens}} or None.

    `client`: an httpx.Client (tests pass one with a MockTransport). Never raises.
    """
    started = time.monotonic()
    try:
        key = api_key or get_openrouter_key()
        if not key or not questions:
            return None
        body = {"model": model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        own = client is None
        http = client or httpx.Client()
        try:
            response = http.post(url, content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, timeout=timeout_s)
        finally:
            if own:
                http.close()
        if response.status_code != 200:
            return None
        payload = response.json()
        answers = payload.get("answers")
        if not _valid_answers(answers, questions):
            return None
        usage = payload.get("usage") or {}
        cost = usage.get("cost")
        return {
            "answers": answers,
            "meta": {
                "model": payload.get("model"),
                "provider": payload.get("provider"),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "cost_usd": float(cost) if cost is not None else None,
                "input_tokens": usage.get("input_tokens"),
            },
        }
    except Exception:  # fail-open by contract: timeouts, transport, JSON, anything
        return None


def choice_answer(result, name):
    """(choice, confidence) from a decide() result, or (None, 0.0)."""
    if not result:
        return None, 0.0
    ans = result["answers"].get(name) or {}
    return ans.get("choice"), float(ans.get("confidence") or 0.0)


def noul_answer(result, name):
    """P(true) from a decide() result, or None."""
    if not result:
        return None
    value = (result["answers"].get(name) or {}).get("noul")
    return float(value) if value is not None else None


class FakeJev:
    """Test double with decide()'s signature.

    answers: dict returned as "answers" for every call, or callable(state, questions) -> dict | None.
    fail=True makes every call return None (as on timeout).
    """

    def __init__(self, answers=None, fail=False, model="typesafe/jev-1.13-fake"):
        self.answers = answers
        self.fail = fail
        self.model = model
        self.calls = []

    def decide(self, state, questions, timeout_s=1.5, **_):
        self.calls.append({"state": state, "questions": questions, "timeout_s": timeout_s})
        if self.fail:
            return None
        answers = self.answers(state, questions) if callable(self.answers) else self.answers
        if answers is None:
            return None
        return {"answers": answers, "meta": {"model": self.model, "provider": "fake", "latency_ms": 0,
                                             "cost_usd": 0.0, "input_tokens": 0}}

    __call__ = decide
