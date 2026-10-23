"""HTTP fetching with polite rate limits, retries and a stored copy of every response.

Only the standard library is used, so the data layer has no third-party runtime dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

RETRY_STATUSES = {429, 500, 502, 503, 504}
TLS_HINT = (
    "this Python cannot check HTTPS certificates: run pip install certifi, or on a Mac run "
    "'Install Certificates.command' in the Python folder under Applications"
)


def tls_context() -> ssl.SSLContext:
    """Certificates to trust for HTTPS.

    Python from python.org on macOS ships without system certificates until "Install Certificates.command" is
    run, so every fetch fails with CERTIFICATE_VERIFY_FAILED. If the certifi package is installed (it comes with
    the workbench extras) its bundle is used. Setting SSL_CERT_FILE or SSL_CERT_DIR keeps the system's own setup.
    Verification is never turned off.
    """
    if not (os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")):
        try:
            import certifi  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            pass
        else:
            return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


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
            "api.openfigi.com": 12.5,  # OpenFIGI search/filter without a key: 5 requests a minute
            "filings.xbrl.org": 1.0,  # run by XBRL International as a free public service: one a second
            "api.gleif.org": 0.5,
            "data-api.ecb.europa.eu": 0.5,
        }
        self.retries = retries
        self.timeout = timeout
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._opener = opener
        self._tls: ssl.SSLContext | None = None

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
        if self._tls is None:
            self._tls = tls_context()
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._tls) as resp:  # noqa: S310 (https URLs built in code)
            return resp.read(), resp.headers.get("Content-Type", "")

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        """Fetch ``url``. Extra ``headers`` (e.g. an API token) are sent but never stored."""
        return self._request(url, headers, None)

    def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None) -> Response:
        """POST a JSON body. The returned ``Response.url`` is ``url#<request JSON>`` so the stored copy says
        exactly what was asked for. Extra ``headers`` (e.g. an API key) are sent but never stored."""
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        resp = self._request(url, {"Content-Type": "application/json", **(headers or {})}, body.encode())
        return Response(f"{url}#{body}", resp.body, resp.sha256, resp.retrieved_at, resp.content_type)

    def _request(self, url: str, headers: dict[str, str] | None, data: bytes | None) -> Response:
        host = urlparse(url).netloc
        req = urllib.request.Request(  # noqa: S310
            url,
            data=data,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity", **(headers or {})},
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
                reason = getattr(e, "reason", e)
                last_error = str(reason)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    last_error += f" ({TLS_HINT})"
                    break  # trying again cannot fix a certificate problem
            if attempt < self.retries:
                self._sleep(delay)
                delay *= 2
        raise FetchError(f"{url}: {last_error}")
