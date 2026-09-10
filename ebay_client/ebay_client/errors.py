"""
Typed failures, so a caller can tell the difference between "eBay said no",
"eBay is busy" and "our credentials are wrong".

Every one of those needs a different response and conflating them is how a
retry loop ends up hammering an endpoint that will never succeed. A push
worker should retry a RateLimited, surface an ApiError against the specific
listing item that caused it, and stop the whole run on an AuthError.
"""

from typing import Any, Dict, List, Optional


class EbayError(RuntimeError):
    """Base class for everything this library raises."""


class ConfigError(EbayError):
    """Required configuration is missing or contradictory."""


class TransportError(EbayError):
    """The request never produced an HTTP response: DNS, TLS, timeout."""


class AuthError(EbayError):
    """
    Credentials were rejected (HTTP 401), or a refresh token has expired.

    Distinct from ApiError because it is never worth retrying and never the
    fault of the particular record being pushed. eBay refresh tokens have a
    finite life and require the user to re-consent, so this is the signal that
    the dashboard must ask Mark to reconnect his eBay account.
    """


class RateLimited(EbayError):
    """
    HTTP 429, or a rate-limit error id.

    ``retry_after`` carries the server's own advice in seconds when it sent
    any; honour it rather than guessing, because eBay's daily quotas are per
    application and burning through one affects every future call today.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ApiError(EbayError):
    """
    eBay accepted the request and refused the operation.

    The ``errors`` list is eBay's own payload, preserved verbatim rather than
    flattened into a string: ``errorId`` is the only stable, machine-readable
    part of an eBay failure, and the caller needs it to decide whether a
    failure is retryable, belongs to one card, or invalidates a whole group.
    """

    def __init__(
        self,
        message: str,
        status_code: int,
        errors: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []

    @property
    def error_ids(self) -> List[int]:
        ids = []
        for entry in self.errors:
            raw = entry.get("errorId")
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        return ids

    def __str__(self) -> str:
        base = super().__str__()
        if not self.errors:
            return base
        details = "; ".join(
            str(e.get("longMessage") or e.get("message") or e)
            for e in self.errors
        )
        return f"{base}: {details}"


class SignatureError(EbayError):
    """
    A notification's signature could not be verified.

    The receiving endpoint must answer 412 and must not act on the payload. An
    unverified notification is an unauthenticated request that happens to be
    shaped like eBay -- anyone who learns the URL can send one.
    """
