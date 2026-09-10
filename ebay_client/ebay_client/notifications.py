"""
Receiving eBay's push notifications: the endpoint challenge, and verifying
that a payload really came from eBay.

Two separate mechanisms, both required.

**The challenge** runs once, when eBay validates a newly configured endpoint.
It sends a GET with a ``challenge_code`` query parameter and expects the
SHA-256 of the challenge code, our verification token and the endpoint URL,
concatenated in that order, as hex. Getting the order or the URL wrong is the
single most common reason endpoint validation fails, and eBay's error does not
say which part was wrong -- so :func:`challenge_response` takes the URL
explicitly rather than trying to reconstruct it from the request, because
behind a reverse proxy the URL the app sees is not the URL eBay called.

**The signature** runs on every notification. Anyone who learns the endpoint
URL can POST to it, so an unverified payload is an anonymous request that
merely looks like eBay. A failure must answer 412 and must not be acted on.
"""

import base64
import hashlib
import json
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import SignatureError

SIGNATURE_HEADER = "x-ebay-signature"

# eBay's own guidance: cache the verification key for a reasonable period,
# about an hour. Fetching per notification would spend the API quota on
# something that changes rarely, and a burst of notifications would then be
# rate-limited into failing verification.
PUBLIC_KEY_TTL_SECONDS = 3600


def challenge_response(
    challenge_code: str, verification_token: str, endpoint_url: str
) -> str:
    """
    The hex digest eBay expects when it validates the endpoint.

    Order matters and is not negotiable: challenge code, then verification
    token, then the endpoint URL exactly as configured in eBay's console --
    including scheme and any path, with no trailing slash unless the console
    has one.
    """
    digest = hashlib.sha256()
    digest.update(challenge_code.encode("utf-8"))
    digest.update(verification_token.encode("utf-8"))
    digest.update(endpoint_url.encode("utf-8"))
    return digest.hexdigest()


def parse_signature_header(raw: str) -> Dict[str, Any]:
    """
    Decode the base64-packed JSON in ``X-EBAY-SIGNATURE``.

    Shape is ``{"alg": "ECDSA", "kid": "...", "signature": "...",
    "digest": "SHA1"}``.
    """
    if not raw:
        raise SignatureError("the notification carried no signature header")
    try:
        decoded = base64.b64decode(raw, validate=False)
        parsed = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise SignatureError("the signature header was not base64 JSON") from exc
    if not isinstance(parsed, dict):
        raise SignatureError("the signature header did not decode to an object")
    for field in ("kid", "signature"):
        if not parsed.get(field):
            raise SignatureError(f"the signature header is missing {field!r}")
    return parsed


class PublicKeyCache:
    """
    Fetches and caches eBay's notification verification keys by key id.

    Thread safe because notifications arrive concurrently on the web server
    and the whole point of the cache is that a burst does not turn into a
    burst of API calls.
    """

    def __init__(
        self,
        fetch: Callable[[str], str],
        ttl: float = PUBLIC_KEY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: Dict[str, Tuple[str, float]] = {}

    def get(self, key_id: str) -> str:
        with self._lock:
            cached = self._keys.get(key_id)
            if cached and cached[1] > self._clock():
                return cached[0]
        # Fetched outside the lock: a slow network call should not block every
        # other notification, and a duplicate fetch is harmless.
        key = self._fetch(key_id)
        with self._lock:
            self._keys[key_id] = (key, self._clock() + self._ttl)
        return key

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()


def _normalise_pem(key: str) -> bytes:
    """
    Accept the key with or without PEM armour.

    eBay has returned both forms, and a bare base64 body will not load.
    """
    text = (key or "").strip()
    if not text:
        raise SignatureError("eBay returned an empty verification key")
    if "-----BEGIN" in text:
        return text.encode("ascii")
    body = "\n".join(text[i : i + 64] for i in range(0, len(text), 64))
    return (
        f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----\n"
    ).encode("ascii")


def verify_signature(body: bytes, signature_header: str, keys: PublicKeyCache) -> None:
    """
    Raise :class:`SignatureError` unless ``body`` was signed by eBay.

    ``body`` must be the raw request bytes, not a re-serialised copy of the
    parsed JSON. Any reformatting -- key order, whitespace, unicode escaping --
    changes the bytes and invalidates the signature, so the endpoint must read
    the body before anything parses it.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SignatureError(
            "verifying notifications requires the 'cryptography' package "
            "(install ebay-client[notifications])"
        ) from exc

    header = parse_signature_header(signature_header)
    try:
        signature = base64.b64decode(header["signature"], validate=False)
    except Exception as exc:
        raise SignatureError("the signature value was not valid base64") from exc

    pem = _normalise_pem(keys.get(header["kid"]))
    try:
        public_key = load_pem_public_key(pem)
    except Exception as exc:
        raise SignatureError("eBay's verification key could not be loaded") from exc

    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise SignatureError("eBay's verification key was not an EC public key")

    # SHA-1 is eBay's choice, declared in the header's "digest" field. It is
    # weak in general but the signature is over a payload we already treat as
    # untrusted until verified, and there is no alternative on offer.
    algorithm = ec.ECDSA(hashes.SHA1())
    try:
        public_key.verify(signature, body, algorithm)
    except InvalidSignature as exc:
        raise SignatureError("the notification signature did not verify") from exc


def payload_topic(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """The topic of a decoded notification, if it declares one."""
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("topic"):
        return metadata["topic"]
    return payload.get("topic")
