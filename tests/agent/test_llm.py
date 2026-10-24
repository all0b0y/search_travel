import json

import httpx2
import pytest

from agent.llm import (DEFAULT_MODEL, DEFAULT_PROVIDER, Backend, FakeBackend, LLMError, LLMRequest,
                       OpenRouterBackend, reasoning_param)

SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object", "required": ["intent"], "additionalProperties": False,
    "properties": {"intent": {"enum": ["new_trip", "refine"]}},
}


def req(**kw):
    base = dict(card="understand", system="SYS", user='{"message": "хочу в Сочи"}', schema=SCHEMA,
                thinking="off", max_tokens=300)
    base.update(kw)
    return LLMRequest(**base)


def completion(content, model="deepseek/deepseek-v4.1-flash-1017", provider="DeepSeek", cost=0.0002):
    return {
        "id": "gen-1", "object": "chat.completion", "created": 1, "model": model, "provider": provider,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 64},
                  "completion_tokens_details": {"reasoning_tokens": 7}},
    }


class Transport:
    """Fake OpenRouter: replays bodies and records request JSON."""

    def __init__(self, *contents, status=200):
        self.contents = list(contents)
        self.status = status
        self.requests = []

    def __call__(self, request):
        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "body": json.loads(request.content)})
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "boom", "code": self.status}})
        return httpx2.Response(200, json=completion(self.contents.pop(0)))


def backend(transport, **kw):
    return OpenRouterBackend(api_key="sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
                             **kw)


def test_request_shape_provider_pin_reasoning_and_format():
    t = Transport('{"intent": "new_trip"}')
    result = backend(t).complete(req())
    sent = t.requests[0]
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer sk-test"
    body = sent["body"]
    assert body["model"] == DEFAULT_MODEL == "deepseek/deepseek-v4.1-flash-1017"
    assert body["provider"] == {"order": ["DeepSeek"], "allow_fallbacks": False} == DEFAULT_PROVIDER
    assert body["reasoning"] == {"enabled": False}
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 300
    assert body["messages"] == [{"role": "system", "content": "SYS"},
                                {"role": "user", "content": '{"message": "хочу в Сочи"}'}]
    assert result.data == {"intent": "new_trip"} and result.retries == 0
    assert result.model == "deepseek/deepseek-v4.1-flash-1017" and result.provider == "DeepSeek"
    assert result.usage == {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 64,
                            "reasoning_tokens": 7, "cost_usd": 0.0002}
    assert isinstance(result.latency_ms, int)
    assert isinstance(backend(Transport()), Backend)


@pytest.mark.parametrize("thinking, expected", [("off", {"enabled": False}), ("low", {"effort": "low"}),
                                                 ("high", {"effort": "high"})])
def test_reasoning_mapping(thinking, expected):
    assert reasoning_param(thinking) == expected
    t = Transport('{"intent": "refine"}')
    backend(t).complete(req(thinking=thinking))
    assert t.requests[0]["body"]["reasoning"] == expected


def test_bad_thinking_rejected():
    with pytest.raises(ValueError):
        req(thinking="medium")


def test_provider_configurable():
    t = Transport('{"intent": "refine"}')
    backend(t, provider={"order": ["Other"], "allow_fallbacks": True}, model="qwen/qwen3.5-flash-02-23").complete(req())
    body = t.requests[0]["body"]
    assert body["provider"] == {"order": ["Other"], "allow_fallbacks": True}
    assert body["model"] == "qwen/qwen3.5-flash-02-23"


def test_images_as_content_parts():
    t = Transport('{"intent": "new_trip"}')
    url = "data:image/png;base64,iVBORw0KGgo="
    backend(t).complete(req(images=[url]))
    content = t.requests[0]["body"]["messages"][1]["content"]
    assert content == [{"type": "text", "text": '{"message": "хочу в Сочи"}'},
                       {"type": "image_url", "image_url": {"url": url}}]


def test_retry_once_with_validation_error_then_success():
    t = Transport('{"intent": "fly"}', '```json\n{"intent": "refine"}\n```')
    result = backend(t).complete(req())
    assert result.data == {"intent": "refine"} and result.retries == 1
    assert len(t.requests) == 2
    retry_messages = t.requests[1]["body"]["messages"]
    assert retry_messages[2] == {"role": "assistant", "content": '{"intent": "fly"}'}
    assert retry_messages[3]["role"] == "user" and "intent" in retry_messages[3]["content"]
    assert "rejected" in retry_messages[3]["content"]
    assert result.validation_errors and result.usage["input_tokens"] == 200
    assert result.usage["cost_usd"] == pytest.approx(0.0004)


def test_still_invalid_after_retry_raises():
    t = Transport("not json", '{"intent": "nope"}')
    with pytest.raises(LLMError) as err:
        backend(t).complete(req())
    assert len(t.requests) == 2
    assert err.value.retries == 1 and err.value.card == "understand" and err.value.errors


def test_server_error_is_retried_then_llm_error(monkeypatch):
    import agent.llm as llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    t = Transport(status=500)
    with pytest.raises(LLMError):
        backend(t).complete(req())
    assert len(t.requests) == 4          # a 5xx is transient: three more tries, then an honest error


def test_client_error_is_llm_error_without_retry(monkeypatch):
    import agent.llm as llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    t = Transport(status=400)
    with pytest.raises(LLMError):
        backend(t).complete(req())
    assert len(t.requests) == 1


def test_missing_key_raises():
    with pytest.raises(LLMError):
        OpenRouterBackend()


def test_injected_client_object():
    class Completions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return completion('{"intent": "new_trip"}')

    class Client:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": Completions()})()

    client = Client()
    result = OpenRouterBackend(client=client).complete(req(thinking="low"))
    assert result.data == {"intent": "new_trip"}
    assert client.chat.completions.kwargs["extra_body"] == {"reasoning": {"effort": "low"},
                                                            "provider": DEFAULT_PROVIDER}


# ---------------------------------------------------------------- FakeBackend

def test_fake_backend_scripted_list_and_records():
    fb = FakeBackend({"understand": [{"intent": "new_trip"}, {"intent": "refine"}]})
    assert fb.complete(req()).data == {"intent": "new_trip"}
    assert fb.complete(req()).data == {"intent": "refine"}
    assert fb.count("understand") == 2 and fb.requests[0].card == "understand"
    with pytest.raises(LLMError):
        fb.complete(req())


def test_fake_backend_callable_and_retry():
    fb = FakeBackend({"understand": lambda r: {"intent": "new_trip" if "Сочи" in r.user else "refine"}})
    assert fb.complete(req()).data == {"intent": "new_trip"}
    fb2 = FakeBackend().add("understand", "garbage", {"intent": "refine"})
    result = fb2.complete(req())
    assert result.retries == 1 and result.data == {"intent": "refine"} and len(fb2.calls) == 2
    assert result.model == "fake/model" and result.provider == "fake"


def test_fake_backend_exception_item():
    fb = FakeBackend({"understand": [TimeoutError("slow")]})
    with pytest.raises(LLMError):
        fb.complete(req())


def test_fake_backend_unknown_card():
    with pytest.raises(LLMError):
        FakeBackend().complete(req(card="present.variants"))


def test_transient_transport_errors_are_retried_then_succeed(monkeypatch):
    import agent.llm as llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class APIConnectionError(Exception):
        pass

    class Flaky(llm.JsonBackend):
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def _call(self, req, messages):
            self.calls += 1
            if self.calls < 3:
                raise APIConnectionError("Connection error.")
            return llm.RawReply(text='{"ok": true}', model="m", provider="p")

    b = Flaky()
    req = llm.LLMRequest(card="x", system="s", user="{}", schema={"type": "object"})
    out = b.complete(req)
    assert out.data == {"ok": True} and b.calls == 3
    assert out.usage.get("transport_retries") == 2


def test_non_transient_errors_are_not_retried(monkeypatch):
    import agent.llm as llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class AuthenticationError(Exception):
        status_code = 401

    class Bad(llm.JsonBackend):
        name = "bad"
        calls = 0

        def _call(self, req, messages):
            Bad.calls += 1
            raise AuthenticationError("no")

    with __import__("pytest").raises(llm.LLMError):
        Bad().complete(llm.LLMRequest(card="x", system="s", user="{}", schema={"type": "object"}))
    assert Bad.calls == 1
