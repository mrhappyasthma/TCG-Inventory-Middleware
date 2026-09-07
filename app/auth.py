import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, Tuple
from fastapi import Request, HTTPException, status, Depends
try:
    from app.user_db import UserDatabase
except ImportError:
    from .user_db import UserDatabase

# Environment configurations
AUTH_METHOD = os.environ.get("AUTH_METHOD", "local").lower()  # "local", "google", "none"
JWT_SECRET = os.environ.get("JWT_SECRET", "tcg-middleware-default-secret-key-change-me")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a unique salt."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    )
    return f"{salt}${key.hex()}"


def verify_password(stored_password_hash: str, provided_password: str) -> bool:
    """Verify a stored password hash against a provided password."""
    try:
        salt, key_hex = stored_password_hash.split("$", 1)
        key = hashlib.pbkdf2_hmac(
            "sha256", provided_password.encode("utf-8"), salt.encode("utf-8"), 100000
        )
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def create_jwt_token(payload: Dict[str, Any], expires_in_seconds: int = 86400 * 7) -> str:
    """Create a signed JWT-style token using HMAC-SHA256."""
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
    """Verify and decode a JWT token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        message = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig = hmac.new(JWT_SECRET.encode("utf-8"), message, hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        # Fix base64 padding
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))

        if "exp" in payload and payload["exp"] < time.time():
            return None  # Expired
        return payload
    except Exception:
        return None


def verify_google_id_token(id_token: str) -> Optional[Dict[str, Any]]:
    """
    Verify Google OAuth ID Token via Google's tokeninfo endpoint.
    Zero external heavy Google client library dependency.
    """
    try:
        url = f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
        req = urllib.request.Request(url, headers={"User-Agent": "TCG-Middleware"})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                # Optionally check audience if configured
                if GOOGLE_CLIENT_ID and data.get("aud") != GOOGLE_CLIENT_ID:
                    return None
                return data
    except Exception:
        return None
    return None


class AuthManager:
    def __init__(self, user_db: UserDatabase):
        self.user_db = user_db

    def get_current_user_from_request(self, request: Request) -> Optional[Dict[str, Any]]:
        """Extract and validate current user from session cookie or Authorization header."""
        if AUTH_METHOD == "none":
            # In offline/dev mode, return default active admin
            return {
                "id": 0,
                "username": "local_admin",
                "email": "admin@local",
                "role": "admin",
                "status": "active",
            }

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

        user = self.user_db.get_user_by_id(payload["user_id"])
        return user

    def require_user(self, request: Request) -> Dict[str, Any]:
        """Require authenticated and active user."""
        user = self.get_current_user_from_request(request)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Please log in.",
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
        """Require user with active status and admin role."""
        user = self.require_user(request)
        if user.get("role") != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required.",
            )
        return user
