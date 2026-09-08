import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional, Dict, Any

from fastapi import Request, HTTPException, status
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

try:
    from app.user_db import UserDatabase
except ImportError:
    from .user_db import UserDatabase

# ---------------------------------------------------------------------------
# Configuration
#
# Authentication is Google Sign-In only. There is deliberately no local
# username/password path and no "disabled auth" development bypass, so that
# there is exactly one way to become an authenticated user.
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()

# Accepted issuers for a Google-issued ID token.
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Session cookie lifetime (7 days).
SESSION_TTL_SECONDS = 86400 * 7

SESSION_SECRET_FILE = os.environ.get("SESSION_SECRET_FILE", "data/.session_secret")

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)


class ConfigurationError(RuntimeError):
    """Raised when the app is not configured well enough to authenticate anyone."""


if not GOOGLE_CLIENT_ID:
    raise ConfigurationError(
        "GOOGLE_CLIENT_ID is not set.\n"
        "\n"
        "This application authenticates exclusively through Google Sign-In, so a\n"
        "Google OAuth 2.0 Web application Client ID is required to start.\n"
        "\n"
        "Create one at https://console.cloud.google.com under\n"
        "  APIs & Services > Credentials > Create Credentials > OAuth client ID\n"
        "and add it to your .env file as:\n"
        "  GOOGLE_CLIENT_ID=<your-id>.apps.googleusercontent.com\n"
        "\n"
        "Remember to register your Authorized JavaScript origins. Google rejects\n"
        "raw IP addresses and plain HTTP, so a bare LAN address such as\n"
        "http://192.168.1.50:8080 can never be authorized. Use an HTTPS domain\n"
        "(e.g. https://yourname.synology.me) or http://localhost:8080 for dev.\n"
        "See the README for the full setup walkthrough."
    )


def _load_or_create_session_secret() -> str:
    """
    Resolve the secret used to sign session cookies.

    An explicit JWT_SECRET environment variable always wins. Otherwise a random
    secret is generated once and persisted alongside the databases, so sessions
    survive restarts without the code ever shipping a guessable default. A
    hardcoded fallback would be public in the repository and would let anyone
    forge an admin session cookie.
    """
    env_secret = os.environ.get("JWT_SECRET", "").strip()
    if env_secret:
        return env_secret

    if os.path.isfile(SESSION_SECRET_FILE):
        with open(SESSION_SECRET_FILE, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            return stored

    parent_dir = os.path.dirname(os.path.abspath(SESSION_SECRET_FILE))
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    generated = secrets.token_urlsafe(32)
    with open(SESSION_SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(generated)
    try:
        os.chmod(SESSION_SECRET_FILE, 0o600)
    except OSError:
        # Windows does not honour POSIX modes; the file still lives in the
        # non-public data directory.
        pass
    return generated


JWT_SECRET = _load_or_create_session_secret()


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

def create_jwt_token(
    payload: Dict[str, Any], expires_in_seconds: int = SESSION_TTL_SECONDS
) -> str:
    """Create a signed JWT-style session token using HMAC-SHA256."""
    header = {"alg": "HS256", "typ": "JWT"}
    token_payload = dict(payload)
    token_payload["exp"] = int(time.time()) + expires_in_seconds
    token_payload["iat"] = int(time.time())

    def b64_encode(data: dict) -> str:
        dumped = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(dumped).decode("utf-8").rstrip("=")

    header_b64 = b64_encode(header)
    payload_b64 = b64_encode(token_payload)
    message = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = hmac.new(JWT_SECRET.encode("utf-8"), message, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a session token, returning None if it is not valid."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        message = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig = hmac.new(
            JWT_SECRET.encode("utf-8"), message, hashlib.sha256
        ).digest()
        expected_sig_b64 = (
            base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")
        )

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))

        if "exp" in payload and payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


def set_session_cookie(response, token: str) -> None:
    """Attach the session cookie using consistent, hardened flags."""
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=SESSION_TTL_SECONDS,
    )


# ---------------------------------------------------------------------------
# Google ID token verification
# ---------------------------------------------------------------------------

def verify_google_id_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify a Google ID token locally against Google's published signing certs.

    ``verify_oauth2_token`` checks the signature, expiry and — because the
    audience is passed explicitly — that the token was actually minted for this
    application. That audience check is mandatory: accepting a token without it
    would let an ID token issued for any other Google app authenticate here.

    Returns the claims dict on success, or None if the token is unusable.
    """
    if not token:
        return None

    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except (ValueError, GoogleAuthError):
        return None

    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None

    if not claims.get("sub"):
        return None

    if not claims.get("email"):
        return None

    # A Google account can carry an unverified email; treating one as an
    # identity would let someone claim an address they do not control.
    if claims.get("email_verified") not in (True, "true", "True"):
        return None

    return claims


class AuthManager:
    def __init__(self, user_db: UserDatabase):
        self.user_db = user_db

    def get_current_user_from_request(
        self, request: Request
    ) -> Optional[Dict[str, Any]]:
        """Resolve the current user from the session cookie or bearer header."""
        token = request.cookies.get("session_token")
        if not token:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()

        if not token:
            return None

        payload = decode_jwt_token(token)
        if not payload or "user_id" not in payload:
            return None

        return self.user_db.get_user_by_id(payload["user_id"])

    def require_user(self, request: Request) -> Dict[str, Any]:
        """Require an authenticated, approved user."""
        user = self.get_current_user_from_request(request)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Please sign in with Google.",
            )
        if user.get("status") == "pending":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is pending approval by an administrator.",
            )
        if user.get("status") == "disabled":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has been deactivated.",
            )
        return user

    def require_admin(self, request: Request) -> Dict[str, Any]:
        """Require an approved user holding the admin role."""
        user = self.require_user(request)
        if user.get("role") != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required.",
            )
        return user
