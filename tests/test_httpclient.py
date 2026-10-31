"""HTTP etiquette: timeouts, bounded retries, rate limiting, error handling."""

from __future__ import annotations

import time

import pytest
import requests

from iris_cre41.httpclient import HttpError, PoliteSession, RateLimiter


class FakeResponse:
    def __init__(self, status_code=200, text="ok", headers=None, url="https://x/"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers or {"Content-Type": "text/html"}
        self.url = url


def session(max_retries=3, sleeps=None):
    recorded = sleeps if sleeps is not None else []
    instance = PoliteSession(
        user_agent="IRIS-CRE41-tests/1.0",
        timeout=1.0,
        max_retries=max_retries,
        min_interval=0.0,
        sleeper=recorded.append,
    )
    instance.recorded_sleeps = recorded
    return instance


# -- rate limiter -----------------------------------------------------------

def test_rate_limiter_enforces_minimum_interval():
    limiter = RateLimiter()
    limiter.wait("host", 0.0)
    started = time.monotonic()
    limiter.wait("host", 0.05)
    limiter.wait("host", 0.05)
    assert time.monotonic() - started >= 0.05


def test_rate_limiter_is_per_host():
    limiter = RateLimiter()
    assert limiter.wait("a", 1.0) == 0.0
    assert limiter.wait("b", 1.0) == 0.0


def test_zero_interval_does_not_sleep():
    assert RateLimiter().wait("h", 0.0) == 0.0


# -- retries ----------------------------------------------------------------

def test_retryable_status_is_retried_then_succeeds(monkeypatch):
    instance = session()
    responses = [FakeResponse(503), FakeResponse(503), FakeResponse(200, "done")]
    monkeypatch.setattr(
        instance.session, "request", lambda *a, **k: responses.pop(0)
    )
    result = instance.get("https://example.test/x")
    assert result.status_code == 200
    assert result.attempts == 3
    assert instance.retry_count == 2
    assert len(instance.recorded_sleeps) == 2


def test_retries_are_bounded_and_then_raise(monkeypatch):
    instance = session(max_retries=2)
    monkeypatch.setattr(
        instance.session, "request", lambda *a, **k: FakeResponse(503)
    )
    with pytest.raises(HttpError):
        instance.get("https://example.test/x")
    # max_retries=2 means three attempts in total.
    assert instance.request_count == 3


def test_retry_after_header_is_respected(monkeypatch):
    instance = session()
    responses = [
        FakeResponse(429, headers={"Retry-After": "7", "Content-Type": "text/html"}),
        FakeResponse(200),
    ]
    monkeypatch.setattr(instance.session, "request", lambda *a, **k: responses.pop(0))
    instance.get("https://example.test/x")
    assert instance.recorded_sleeps == [7.0]


def test_backoff_is_capped(monkeypatch):
    instance = session(max_retries=8)
    monkeypatch.setattr(instance.session, "request", lambda *a, **k: FakeResponse(503))
    with pytest.raises(HttpError):
        instance.get("https://example.test/x")
    assert max(instance.recorded_sleeps) <= 30.0


def test_connection_errors_are_retried_then_raise(monkeypatch):
    instance = session(max_retries=1)

    def boom(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(instance.session, "request", boom)
    with pytest.raises(HttpError, match="request failed after 2 attempts"):
        instance.get("https://example.test/x")


def test_client_errors_are_not_retried(monkeypatch):
    instance = session()
    calls = []

    def respond(*args, **kwargs):
        calls.append(1)
        return FakeResponse(404)

    monkeypatch.setattr(instance.session, "request", respond)
    with pytest.raises(HttpError, match="HTTP 404"):
        instance.get("https://example.test/missing")
    assert len(calls) == 1


# -- headers / timeout ------------------------------------------------------

def test_user_agent_is_set_and_identifying():
    instance = session()
    assert instance.session.headers["User-Agent"] == "IRIS-CRE41-tests/1.0"
    assert "python-requests" not in instance.session.headers["User-Agent"]


def test_timeout_is_always_passed(monkeypatch):
    instance = session()
    seen = {}

    def respond(method, url, **kwargs):
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(instance.session, "request", respond)
    instance.get("https://example.test/x")
    assert seen["timeout"] == 1.0


def test_stats_are_reported(monkeypatch):
    instance = session()
    monkeypatch.setattr(instance.session, "request", lambda *a, **k: FakeResponse())
    instance.get("https://example.test/a")
    instance.get("https://example.test/b")
    assert instance.stats() == {"requests": 2, "retries": 0}


def test_response_hash_is_stable(monkeypatch):
    instance = session()
    monkeypatch.setattr(
        instance.session, "request", lambda *a, **k: FakeResponse(200, "payload")
    )
    first = instance.get("https://example.test/a").sha256
    second = instance.get("https://example.test/a").sha256
    assert first == second and len(first) == 64
