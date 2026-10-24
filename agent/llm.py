"""Model backends (AGENT_PLAN.md section 2).

    backend.complete(LLMRequest(...)) -> LLMResult      # data is parsed and schema-valid

Invalid JSON or a schema violation -> exactly one retry with the validation error
appended as a user message; still invalid -> LLMError. Transport/API errors are
not retried here (the engine degrades honestly).

OpenRouterBackend talks to OpenRouter through the `openai` SDK; its HTTP layer is
injectable (`client=` an OpenAI-like object, or `http_client=` an httpx2.Client
with a MockTransport) so tests never touch the network. FakeBackend replays
scripted responses per card and is the only backend used in unit tests.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

import jsonschema

from .env import get_openrouter_key

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_PROVIDER = {"order": ["DeepSeek"], "allow_fallbacks": False}
THINKING_LEVELS = ("off", "low", "high")
MAX_INVALID_RETRIES = 1


@dataclass
class LLMRequest:
    card: str                       # e.g. "understand", "present.variants"
    system: str                     # full system prompt text (SkillPackage.system_prompt)
    user: str                       # JSON payload as text
    schema: dict                    # JSON schema (draft-07) of the card's output
    images: list = field(default_factory=list)   # data URLs
    thinking: str = "off"           # off | low | high
    max_tokens: int = 1000

    def __post_init__(self):
        if self.thinking not in THINKING_LEVELS:
            raise ValueError(f"thinking must be one of {THINKING_LEVELS}: {self.thinking!r}")


@dataclass
class LLMResult:
    data: dict
    raw_text: str
    model: Optional[str]            # served model id reported by the API
    provider: Optional[str]         # served provider reported by the API
    usage: dict                     # input_tokens, output_tokens, cached_tokens, reasoning_tokens, cost_usd (None if not reported)
    latency_ms: int
    retries: int                    # 0 or 1
    validation_errors: list = field(default_factory=list)  # errors of the rejected first attempt, if any


class LLMError(Exception):
    """The backend could not produce a schema-valid answer (or the API failed)."""

    def __init__(self, message, card=None, raw_text=None, errors=None, retries=0, usage=None, latency_ms=None):
        super().__init__(message)
        self.card = card
        self.raw_text = raw_text
        self.errors = list(errors or [])
        self.retries = retries
        self.usage = usage
        self.latency_ms = latency_ms


@runtime_checkable
class Backend(Protocol):
    name: str

    def complete(self, req: LLMRequest) -> LLMResult: ...


# ---------------------------------------------------------------- shared validation / retry

def strip_fences(text):
    """Remove a ```json ... ``` wrapper that some models add despite json_object mode."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def validate_output(text, schema):
    """-> (data or None, [error strings]). Data must be a JSON object valid against `schema`."""
    try:
        data = json.loads(strip_fences(text))
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [f"invalid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["the reply must be a JSON object"]
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        return None, [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}" for e in errors[:5]]
    return data, []


def retry_message(errors):
    return ("Your previous reply was rejected: " + "; ".join(errors)
            + ". Reply again with ONE JSON object that satisfies the schema. No prose, no code fences.")


def build_messages(req):
    """OpenAI chat messages: system prompt + user payload (+ images as content parts)."""
    if req.images:
        content = [{"type": "text", "text": req.user}]
        content += [{"type": "image_url", "image_url": {"url": url}} for url in req.images]
    else:
        content = req.user
    return [{"role": "system", "content": req.system}, {"role": "user", "content": content}]


def empty_usage():
    return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "cost_usd": None}


def add_usage(total, part):
    for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
        total[key] += int(part.get(key) or 0)
    if part.get("cost_usd") is not None:
        total["cost_usd"] = (total["cost_usd"] or 0.0) + float(part["cost_usd"])
    return total


@dataclass
class RawReply:
    text: str
    model: Optional[str] = None
    provider: Optional[str] = None
    usage: dict = field(default_factory=empty_usage)


class JsonBackend:
    """Validation + exactly-one-retry loop shared by real and fake backends."""
    name = "json"

    def _call(self, req: LLMRequest, messages: list) -> RawReply:  # pragma: no cover - abstract
        raise NotImplementedError

    def _clock(self):
        return time.monotonic()

    def complete(self, req: LLMRequest) -> LLMResult:
        messages = build_messages(req)
        usage = empty_usage()
        started = self._clock()
        first_errors: list = []
        errors: list = []
        reply = None
        for attempt in range(MAX_INVALID_RETRIES + 1):
            try:
                reply = self._call(req, messages)
            except LLMError as exc:
                exc.card = exc.card or req.card
                exc.retries = attempt
                raise
            except Exception as exc:  # transport / API failure: no retry here
                raise LLMError(f"{self.name} call failed: {type(exc).__name__}: {exc}", card=req.card,
                               retries=attempt, usage=usage) from exc
            add_usage(usage, reply.usage)
            data, errors = validate_output(reply.text, req.schema)
            if data is not None:
                return LLMResult(data=data, raw_text=reply.text, model=reply.model, provider=reply.provider,
                                 usage=usage, latency_ms=int((self._clock() - started) * 1000),
                                 retries=attempt, validation_errors=first_errors)
            if attempt == 0:
                first_errors = errors
            messages = messages + [{"role": "assistant", "content": reply.text or ""},
                                   {"role": "user", "content": retry_message(errors)}]
        raise LLMError(f"invalid output for card {req.card} after retry: {'; '.join(errors)}", card=req.card,
                       raw_text=reply.text if reply else None, errors=errors, retries=MAX_INVALID_RETRIES,
                       usage=usage, latency_ms=int((self._clock() - started) * 1000))


# ---------------------------------------------------------------- OpenRouter

def reasoning_param(thinking):
    if thinking == "off":
        return {"enabled": False}
    if thinking in ("low", "high"):
        return {"effort": thinking}
    raise ValueError(f"unknown thinking level: {thinking!r}")


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(response):
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    raise TypeError(f"unexpected response type: {type(response).__name__}")


def parse_usage(usage):
    """OpenRouter usage -> {input_tokens, output_tokens, cached_tokens, reasoning_tokens, cost_usd}."""
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    out_details = usage.get("completion_tokens_details") or {}
    cost = usage.get("cost")
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "reasoning_tokens": int(out_details.get("reasoning_tokens") or 0),
        "cost_usd": float(cost) if cost is not None else None,
    }


class OpenRouterBackend(JsonBackend):
    """DeepSeek V4.1 Flash (or another model) through OpenRouter chat completions."""
    name = "openrouter"

    def __init__(self, model=DEFAULT_MODEL, provider=None, api_key=None, client=None, http_client=None,
                 base_url=OPENROUTER_BASE_URL, timeout_s=90.0, temperature=None, clock=None):
        self.model = model
        # provider pin: explicit dict replaces the default; pass {} to send none (log it!)
        self.provider = dict(DEFAULT_PROVIDER) if provider is None else dict(provider)
        self.temperature = temperature
        self._clock_fn = clock
        if client is None:
            key = api_key or get_openrouter_key()
            if not key:
                raise LLMError("OPENROUTER_API_KEY is not set (load .env with agent.env.load_env)")
            from openai import OpenAI
            client = OpenAI(api_key=key, base_url=base_url, timeout=timeout_s, max_retries=0,
                            http_client=http_client)
        self.client = client

    def _clock(self):
        return self._clock_fn() if self._clock_fn else time.monotonic()

    def request_params(self, req, messages):
        """Keyword arguments for client.chat.completions.create (inspectable in tests)."""
        extra = {"reasoning": reasoning_param(req.thinking)}
        if self.provider:
            extra["provider"] = self.provider
        params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "response_format": {"type": "json_object"},
            "extra_body": extra,
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature
        return params

    def _call(self, req, messages):
        response = _as_dict(self.client.chat.completions.create(**self.request_params(req, messages)))
        choices = response.get("choices") or []
        message = _get(choices[0], "message") if choices else None
        text = _get(message, "content") or ""
        return RawReply(text=text, model=response.get("model"), provider=response.get("provider"),
                        usage=parse_usage(response.get("usage")))


# ---------------------------------------------------------------- fake

class FakeBackend(JsonBackend):
    """Scripted backend for tests.

    scripts: {card: [item, ...] | callable(req) -> item}
      item: dict (sent as JSON text), str (raw text, may be invalid on purpose),
            or an Exception instance (raised as a transport failure).
    Each attempt (including the validation retry) consumes one list item.
    Every LLMRequest is recorded in `requests`; every attempt's messages in `calls`.
    """
    name = "fake"

    def __init__(self, scripts=None, model="fake/model", provider="fake", usage=None):
        self.scripts = dict(scripts or {})
        self.model = model
        self.provider = provider
        self.usage = usage or {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 0, "reasoning_tokens": 0,
                                "cost_usd": 0.0}
        self.requests: list = []
        self.calls: list = []

    def add(self, card, *items):
        self.scripts.setdefault(card, [])
        self.scripts[card].extend(items)
        return self

    def complete(self, req):
        self.requests.append(req)
        return super().complete(req)

    def _call(self, req, messages):
        self.calls.append({"card": req.card, "messages": messages})
        script = self.scripts.get(req.card)
        if callable(script):
            item = script(req)
        elif script:
            item = script.pop(0)
        else:
            raise LLMError(f"FakeBackend: no scripted response for card {req.card!r}", card=req.card)
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return RawReply(text=text, model=self.model, provider=self.provider, usage=dict(self.usage))

    def count(self, card=None):
        return sum(1 for r in self.requests if card is None or r.card == card)
