"""HTTP fetching with polite rate limits, retries and a stored copy of every response.

Only the standard library is used, so the data layer has no third-party runtime dependencies.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

RETRY_STATUSES = {429, 500, 502, 503, 504}


def _reason(e: urllib.error.HTTPError) -> str:
    """A short excerpt of an error response body (e.g. an API's "invalid token" message). Headers are never shown."""
    try:
        body = e.read(300).decode("utf-8", "replace") if e.fp is not None else ""
    except (OSError, ValueError):
        return ""
    text = " ".join(body.split())
    return f": {text[:200]}" if text else ""


class FetchError(RuntimeError):
    """A source could not be reached or refused the request. Never treated as an empty result."""


@dataclass(frozen=True)
class Response:
    url: str
    body: bytes
    sha256: str
    retrieved_at: str
    content_type: str


class Fetcher:
    """Fetches URLs. ``min_interval`` is per host, in seconds (SEC allows at most 10 requests a second)."""

    def __init__(
        self,
        user_agent: str,
        min_interval: dict[str, float] | None = None,
        retries: int = 4,
        timeout: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[[urllib.request.Request, float], bytes] | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("A User-Agent is required")
        self.user_agent = user_agent
        self.min_interval = min_interval or {
            "www.sec.gov": 0.125,
            "data.sec.gov": 0.125,
            "query1.finance.yahoo.com": 0.5,  # unofficial endpoint: stay well clear of its throttling
        }
        self.retries = retries
        self.timeout = timeout
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._opener = opener

    def _wait_turn(self, host: str) -> None:
        gap = self.min_interval.get(host, 0)
        last = self._last.get(host)
        if gap and last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = time.monotonic()

    def _open(self, req: urllib.request.Request) -> tuple[bytes, str]:
        if self._opener is not None:
            return self._opener(req, self.timeout), ""
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (https URLs built in code)
            return resp.read(), resp.headers.get("Content-Type", "")

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        """Fetch ``url``. Extra ``headers`` (e.g. an API token) are sent but never stored."""
        host = urlparse(url).netloc
        req = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity", **(headers or {})}
        )
        delay = 1.0
        last_error = ""
        for attempt in range(self.retries + 1):
            self._wait_turn(host)
            try:
                body, ctype = self._open(req)
                return Response(
                    url=url,
                    body=body,
                    sha256=hashlib.sha256(body).hexdigest(),
                    retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    content_type=ctype,
                )
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}{_reason(e)}"
                if e.code not in RETRY_STATUSES:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = str(getattr(e, "reason", e))
            if attempt < self.retries:
                self._sleep(delay)
                delay *= 2
        raise FetchError(f"{url}: {last_error}")
