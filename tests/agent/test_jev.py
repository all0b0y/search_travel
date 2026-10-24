import json

import httpx
import pytest

from agent.jev import JEV_MODEL, JEV_URL, FakeJev, choice, choice_answer, decide, noul, noul_answer, score

QUESTIONS = {
    "is_answer": noul("Is the message an answer to the pending question?",
                      "It names one of the offered choices or an equivalent.",
                      "It asks something new or changes the topic."),
    "segment": choice("Who travels?", {"solo": "alone", "couple": "two adults", "family": "with kids",
                                       "none": "cannot tell"}),
    "certainty": score("How sure is the user about dates?", ["no idea", "roughly", "exact dates"]),
}
ANSWERS = {
    "is_answer": {"type": "noul", "noul": 0.97},
    "segment": {"type": "choice", "choice": "couple", "probabilities": {"couple": 0.9, "solo": 0.1},
                "confidence": 0.85},
    "certainty": {"type": "score", "score": 1.9, "legend": {"0": "no idea", "1": "roughly", "2": "exact dates"},
                  "probabilities": {"0": 0, "1": 0.1, "2": 0.9}, "confidence": 0.8},
}


def client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_handler(seen):
    def handler(request):
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                     "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917",
                                         "provider": "TypeSafe", "answers": ANSWERS,
                                         "usage": {"input_tokens": 400, "output_tokens": 60, "cost": 1.9e-5}})
    return handler


def test_builders_shape():
    assert QUESTIONS["is_answer"] == {"type": "noul", "instructions": "Is the message an answer to the pending question?",
                                      "criteria": {"true": "It names one of the offered choices or an equivalent.",
                                                   "false": "It asks something new or changes the topic."}}
    assert QUESTIONS["segment"]["criteria"]["couple"] == "two adults"
    assert QUESTIONS["certainty"]["criteria"] == ["no idea", "roughly", "exact dates"]
    assert noul("x") == {"type": "noul", "instructions": "x"}
    with pytest.raises(ValueError):
        noul("x", "only true")
    with pytest.raises(ValueError):
        choice("x", {})
    with pytest.raises(ValueError):
        score("x", ["one"])


def test_decide_request_and_parsing():
    seen = []
    result = decide({"message": "вдвоём"}, QUESTIONS, client=client_with(ok_handler(seen)), api_key="sk-test")
    assert seen[0]["url"] == JEV_URL == "https://openrouter.ai/api/alpha/decisions"
    assert seen[0]["auth"] == "Bearer sk-test"
    assert seen[0]["body"] == {"model": JEV_MODEL, "state": {"message": "вдвоём"}, "questions": QUESTIONS}
    assert result["answers"] == ANSWERS
    assert result["meta"]["model"] == "typesafe/jev-1.13-20260917"
    assert result["meta"]["provider"] == "TypeSafe"
    assert result["meta"]["cost_usd"] == pytest.approx(1.9e-5)
    assert isinstance(result["meta"]["latency_ms"], int)
    assert choice_answer(result, "segment") == ("couple", 0.85)
    assert noul_answer(result, "is_answer") == pytest.approx(0.97)


def test_missing_key_returns_none_without_call():
    seen = []
    assert decide({"m": 1}, QUESTIONS, client=client_with(ok_handler(seen))) is None
    assert seen == []


def test_http_error_returns_none():
    client = client_with(lambda r: httpx.Response(500, json={"error": "x"}))
    assert decide({}, QUESTIONS, client=client, api_key="k") is None


def test_timeout_returns_none():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    assert decide({}, QUESTIONS, client=client_with(handler), api_key="k", timeout_s=0.01) is None


def test_malformed_body_returns_none():
    bad_json = client_with(lambda r: httpx.Response(200, content=b"<html>"))
    assert decide({}, QUESTIONS, client=bad_json, api_key="k") is None
    wrong_choice = dict(ANSWERS, segment={"type": "choice", "choice": "martians", "confidence": 1.0})
    client = client_with(lambda r: httpx.Response(200, json={"answers": wrong_choice}))
    assert decide({}, QUESTIONS, client=client, api_key="k") is None
    missing = client_with(lambda r: httpx.Response(200, json={"answers": {"is_answer": ANSWERS["is_answer"]}}))
    assert decide({}, QUESTIONS, client=missing, api_key="k") is None


def test_default_client_without_key_never_connects():
    assert decide({}, QUESTIONS) is None


def test_fail_open_helpers():
    assert choice_answer(None, "segment") == (None, 0.0)
    assert noul_answer(None, "x") is None


def test_fake_jev():
    fj = FakeJev(ANSWERS)
    result = fj.decide({"m": 1}, QUESTIONS)
    assert result["answers"] == ANSWERS and fj.calls[0]["timeout_s"] == 1.5
    assert FakeJev(fail=True)({}, QUESTIONS) is None
    dyn = FakeJev(lambda state, qs: {"segment": {"type": "choice", "choice": state["s"], "confidence": 1.0}})
    assert choice_answer(dyn.decide({"s": "solo"}, QUESTIONS), "segment") == ("solo", 1.0)
