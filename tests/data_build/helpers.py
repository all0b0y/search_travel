"""Offline helpers for data_build tests: fixtures and a scripted fake HTTP client."""
from __future__ import annotations

import json
from pathlib import Path

from data_build.common import HttpResponse

FIX = Path(__file__).resolve().parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


class FakeHttp:
    """Routes requests by URL substring to (status, body) or a callable(url, params|data) -> (status, body)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None]] = []

    def _answer(self, method, url, payload):
        self.calls.append((method, url, payload))
        for key, ans in self.routes.items():
            if key in url:
                if callable(ans):
                    ans = ans(url, payload)
                status, body = ans
                if isinstance(body, Exception):
                    raise body
                return HttpResponse(status, body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
        raise AssertionError(f"unexpected request {method} {url}")

    def get(self, url, params=None, headers=None, timeout=60):
        return self._answer("GET", url, params)

    def post(self, url, data=None, headers=None, timeout=60):
        return self._answer("POST", url, data)
