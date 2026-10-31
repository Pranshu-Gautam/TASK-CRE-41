"""One polite HTTP client for every outbound call.

Responsibilities kept in a single place so that *all* external access shares
the same guarantees: explicit User-Agent, hard timeouts, bounded retries with
exponential backoff + jitter, and a per-host minimum request interval.
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from .logging_setup import get_logger

log = get_logger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504, 509}


class HttpError(RuntimeError):
    """Raised when a request definitively fails after exhausting retries."""


@dataclass
class HttpResponse:
    url: str
    status_code: int
    text: str
    content: bytes
    headers: Mapping[str, str]
    elapsed_s: float
    attempts: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class RateLimiter:
    """Thread-safe per-key minimum interval between calls."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, min_interval: float) -> float:
        if min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key)
            sleep_for = 0.0
            if last is not None:
                sleep_for = max(0.0, min_interval - (now - last))
            self._last[key] = now + sleep_for
        if sleep_for:
            time.sleep(sleep_for)
        return sleep_for


_GLOBAL_LIMITER = RateLimiter()


class PoliteSession:
    """``requests`` wrapper enforcing the project's access etiquette."""

    def __init__(
        self,
        user_agent: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        min_interval: float = 1.0,
        backoff_base: float = 1.5,
        limiter: RateLimiter | None = None,
        sleeper=time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.backoff_base = backoff_base
        self.limiter = limiter or _GLOBAL_LIMITER
        self._sleep = sleeper
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.9,*/*;q=0.8",
            }
        )
        self.request_count = 0
        self.retry_count = 0

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _host(url: str) -> str:
        return urlsplit(url).netloc.lower()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(60.0, float(retry_after))
            except ValueError:
                pass
        return min(30.0, (self.backoff_base ** attempt) + random.uniform(0, 0.4))

    # -- public API --------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | str | None = None,
        min_interval: float | None = None,
        expect_json: bool = False,
    ) -> HttpResponse:
        host = self._host(url)
        interval = self.min_interval if min_interval is None else min_interval
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 2):  # n retries => n+1 tries
            waited = self.limiter.wait(host, interval)
            started = time.monotonic()
            try:
                self.request_count += 1
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "http_request_exception",
                    extra={"url": url, "attempt": attempt, "error": last_error},
                )
            else:
                elapsed = time.monotonic() - started
                if resp.status_code in RETRY_STATUS and attempt <= self.max_retries:
                    self.retry_count += 1
                    delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                    log.warning(
                        "http_retryable_status",
                        extra={
                            "url": url,
                            "status": resp.status_code,
                            "attempt": attempt,
                            "sleep_s": round(delay, 2),
                        },
                    )
                    self._sleep(delay)
                    continue
                if resp.status_code >= 400:
                    raise HttpError(
                        f"HTTP {resp.status_code} for {resp.url}"
                    )
                if expect_json and "json" not in resp.headers.get(
                    "Content-Type", ""
                ).lower():
                    log.warning(
                        "http_unexpected_content_type",
                        extra={
                            "url": url,
                            "content_type": resp.headers.get("Content-Type"),
                        },
                    )
                log.debug(
                    "http_ok",
                    extra={
                        "url": resp.url,
                        "status": resp.status_code,
                        "bytes": len(resp.content),
                        "elapsed_s": round(elapsed, 3),
                        "rate_limit_wait_s": round(waited, 3),
                        "attempt": attempt,
                    },
                )
                return HttpResponse(
                    url=str(resp.url),
                    status_code=resp.status_code,
                    text=resp.text,
                    content=resp.content,
                    headers=dict(resp.headers),
                    elapsed_s=elapsed,
                    attempts=attempt,
                )

            if attempt <= self.max_retries:
                self.retry_count += 1
                self._sleep(self._backoff(attempt, None))

        raise HttpError(f"request failed after {self.max_retries + 1} attempts: "
                        f"{url} ({last_error})")

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", url, **kwargs)

    def stats(self) -> dict[str, int]:
        return {"requests": self.request_count, "retries": self.retry_count}
