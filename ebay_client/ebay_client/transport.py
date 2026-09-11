"""
One place where HTTP happens.

Written on ``urllib`` from the standard library for the same reason
``tcg_engine.pricing_feed`` is: this library has no third-party dependencies,
so it installs and its tests run on a bare interpreter. The container already
carries ``requests`` transitively, but depending on it here would make the
library's dependency footprint a function of what the web app happens to need.

Everything network-shaped is funnelled through :meth:`Transport.request` so
that retries, rate-limit handling, request spacing and error translation are
written once. A resource module should never call ``urlopen`` itself.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from .errors import ApiError, AuthError, RateLimited, TransportError

USER_AGENT = (
    "TCG-Inventory-Middleware/1.0 "
    "(+https://github.com/mrhappyasthma/TCG-Inventory-Middleware)"
)

DEFAULT_TIMEOUT_SECONDS = 30

# Retried with backoff. 429 is the explicit rate limit; the 5xx family is
# eBay having a bad moment, which is common enough at their scale that a
# single-attempt push would fail runs for no real reason.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 4

# Minimum gap between two calls from this process. eBay's quotas are daily
# rather than per-second, so this is politeness and burst protection rather
# than a documented requirement.
REQUEST_SPACING_SECONDS = 0.05


class Response:
    """An HTTP response, already decoded."""

    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


def _urlopen_adapter(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> Response:
    """
    The real network call.

    Kept as a module-level function rather than a method so that tests can
    replace it wholesale by passing ``opener=``, and so that no test can reach
    the network by accident.
    """
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as raw:
            return Response(raw.status, dict(raw.headers), raw.read())
    except urllib.error.HTTPError as exc:
        # An HTTPError *is* a response, and eBay puts its error payload in the
        # body, so this is not an exceptional path -- it is how a refusal
        # arrives.
        return Response(exc.code, dict(exc.headers or {}), exc.read())
    except urllib.error.URLError as exc:
        raise TransportError(f"{method} {url} failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TransportError(f"{method} {url} timed out") from exc


class Transport:
    """
    Issues authenticated JSON requests against one eBay environment.

    ``token_provider`` is a callable rather than a string so that a token can
    be refreshed underneath a long-running push without the caller having to
    thread a new Transport through. It is consulted per attempt, which is also
    what lets a 401 be retried once after a forced refresh.
    """

    def __init__(
        self,
        base_url: str,
        token_provider: Optional[Callable[[], str]] = None,
        marketplace_id: Optional[str] = None,
        opener: Callable[..., Response] = _urlopen_adapter,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        spacing: float = REQUEST_SPACING_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self.marketplace_id = marketplace_id
        self._opener = opener
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._spacing = spacing
        self._last_call_at = 0.0

    # -- public ---------------------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, **kw):
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, payload: Any = None, **kw):
        return self.request("POST", path, payload=payload, **kw)

    def put(self, path: str, payload: Any = None, **kw):
        return self.request("PUT", path, payload=payload, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Any = None,
        extra_headers: Optional[Dict[str, str]] = None,
        authenticated: bool = True,
        raw: bool = False,
    ) -> Any:
        """
        Make one call and return its decoded body, or raise a typed error.

        Returns ``None`` for the empty bodies eBay sends on 204, which several
        of its write calls do on success.

        ``raw=True`` returns the :class:`Response` instead. Two Feed API calls
        need it: creating a task puts the new task's id in the ``Location``
        header rather than the body, and downloading a result file returns
        gzipped bytes rather than JSON.
        """
        url = self._url(path, params)
        body = None
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.marketplace_id:
            headers["X-EBAY-C-MARKETPLACE-ID"] = self.marketplace_id
        # Several Inventory API calls reject a request without a language
        # header, and the error they return does not say so.
        headers["Content-Language"] = "en-US"
        headers.update(extra_headers or {})

        forced_refresh_used = False
        for attempt in range(1, self._max_attempts + 1):
            if authenticated:
                if not self.token_provider:
                    raise AuthError(
                        "an authenticated request was made with no token provider"
                    )
                headers["Authorization"] = f"Bearer {self.token_provider()}"

            self._space_requests()
            response = self._opener(method, url, headers, body, self._timeout)

            if 200 <= response.status < 300:
                return response if raw else response.json()

            # A 401 on a token we believed was valid usually means it was
            # revoked or eBay expired it early. Worth exactly one forced
            # refresh; a second 401 is a real credential problem and looping
            # on it would spend the refresh quota for nothing.
            if response.status == 401 and authenticated and not forced_refresh_used:
                forced_refresh_used = True
                refresh = getattr(self.token_provider, "force_refresh", None)
                if callable(refresh):
                    refresh()
                    continue

            if response.status in RETRY_STATUS and attempt < self._max_attempts:
                self._sleep(self._backoff(response, attempt))
                continue

            raise self._to_error(response, method, url)

        # Unreachable: the loop either returns or raises.
        raise TransportError(f"{method} {url} exhausted retries")

    # -- internals ------------------------------------------------------

    def _url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        if params:
            # eBay's filter syntax contains brackets and colons that must
            # survive; quote_via=quote_plus would mangle them into something
            # eBay rejects with an unhelpful message.
            cleaned = {k: v for k, v in params.items() if v is not None}
            if cleaned:
                url = f"{url}?{urllib.parse.urlencode(cleaned, safe='[]:.,')}"
        return url

    def _space_requests(self) -> None:
        if self._spacing <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self._spacing:
            self._sleep(self._spacing - elapsed)
        self._last_call_at = time.monotonic()

    def _backoff(self, response: Response, attempt: int) -> float:
        """Honour Retry-After when eBay sends it; otherwise back off."""
        advice = response.headers.get("retry-after")
        if advice:
            try:
                return max(0.0, float(advice))
            except ValueError:
                pass
        return min(30.0, 0.5 * (2 ** (attempt - 1)))

    def _to_error(self, response: Response, method: str, url: str):
        try:
            payload = response.json() or {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        errors = payload.get("errors") if isinstance(payload, dict) else None
        summary = f"{method} {url} returned {response.status}"

        # Carried through even when the payload parsed, because a body that
        # parsed as JSON without an "errors" key is exactly the case where
        # the status code alone told us nothing.
        try:
            body = (response.body or b"").decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            body = ""

        if response.status in (401, 403):
            return AuthError(summary)
        if response.status == 429:
            return RateLimited(summary, self._backoff(response, 1))
        return ApiError(summary, response.status, errors, body=body)
