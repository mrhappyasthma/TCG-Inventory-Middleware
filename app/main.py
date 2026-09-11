import os
import sys
import io
import csv
import hashlib
import json
import mimetypes
# The OAuth callback renders eBay's own error text into a small HTML page.
# That text arrives in a query string, so it is attacker-controlled for anyone
# who can get a person to click a link -- it must be escaped.
from html import escape as escape_html
import shutil
import tempfile
import zipfile
from datetime import datetime
from typing import Optional, Dict, Any, List, Literal

# Ensure project root is in sys.path when running as direct script
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load .env file into os.environ BEFORE any other imports that read env vars at import time
# (auth.py reads GOOGLE_CLIENT_ID at module level and refuses to import without it,
# so this must run first)
_env_path = os.path.join(project_root, ".env")
if os.path.isfile(_env_path):
    with open(_env_path, encoding="utf-8") as _ef:
        for _line in _ef:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            if _key and _key not in os.environ:  # don't override real env vars (e.g. Docker)
                os.environ[_key] = _val

from fastapi import (
    FastAPI,
    BackgroundTasks,
    UploadFile,
    File,
    Form,
    Request,
    Response,
    HTTPException,
    status,
    Depends,
)
from fastapi.responses import (
    HTMLResponse,
    StreamingResponse,
    JSONResponse,
    RedirectResponse,
    FileResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from tcg_engine.csvtools import decode_csv_bytes
from tcg_engine.db import (
    Database,
    apply_pricing_rules,
    apply_condition_multiplier,
)
from tcg_engine.orders import (
    process_orders_csv,
    build_deduction_csv,
    deduction_row,
)
from tcg_engine.batches import (
    process_batch_csv,
    build_cover_photo_revise_csv,
    build_cover_photo_revise_rows,
    QUANTITY_MODE_SET,
    QUANTITY_MODES,
)
from tcg_engine.sync import sync_active_listings_csv
from tcg_engine.pricing_feed import (
    refresh_market_prices,
    build_reprice_csv,
    PriceFeedError,
)
from tcg_engine.plan_exports import build_plan_exports
from tcg_engine.push import PushError, push_plan
from tcg_engine.plans import (
    PlanError,
    approve_plan,
    build_plan,
    plan_blockers,
    revalidate_item,
)

# Submodule imports, not the package root: the project root is on sys.path and
# the outer ebay_client/ directory shadows the installed package there as an
# empty namespace package. See AGENTS.md section 2.
#
# Guarded, because the eBay integration is optional and must degrade rather
# than take the dashboard with it. This is not hypothetical: the package was
# listed in requirements.txt as an editable install, the Dockerfile strips
# every '-e' line before installing, and the resulting ImportError stopped the
# container from starting at all -- so a one-line packaging omission cost the
# entire site, including endpoints with nothing to do with eBay. An
# unimportable library is the same situation as an unconfigured one, and now
# behaves the same way.
try:
    from ebay_client.client import EbayClient
    from ebay_client.config import EbayConfig
    from ebay_client.errors import EbayError, SignatureError
    from ebay_client.notifications import (
        SIGNATURE_HEADER,
        challenge_response,
        payload_topic,
        verify_signature,
    )
    from ebay_client.oauth import TokenStore
    from ebay_client.feed import (
        FeedError,
        download_active_inventory_report,
        report_outline,
    )
    from ebay_client.account import (
        create_inventory_location,
        get_inventory_locations,
        get_policies,
        suggest_policy_ids,
    )
    from app.ebay_push import InventoryApiAdapter

    EBAY_CLIENT_AVAILABLE = True
except ImportError as _ebay_import_error:  # pragma: no cover - packaging fault
    print(
        "[ebay] ebay_client could not be imported, so every eBay feature is "
        f"disabled. The rest of the app is unaffected: {_ebay_import_error}",
        flush=True,
    )
    EBAY_CLIENT_AVAILABLE = False

    class _EbayUnavailable(Exception):
        """
        Stands in for the library's exception types.

        The endpoints below catch EbayError and SignatureError by name, and an
        `except None` is a TypeError at handling time -- which would turn a
        missing library into a 500 at exactly the moment we are trying to
        degrade gracefully.
        """

    EbayClient = None
    EbayConfig = None
    EbayError = SignatureError = _EbayUnavailable
    SIGNATURE_HEADER = "x-ebay-signature"
    challenge_response = payload_topic = verify_signature = None
    TokenStore = object
    FeedError = _EbayUnavailable
    download_active_inventory_report = None
    report_outline = None

try:
    from app.user_db import UserDatabase
    from app.auth import (
        AuthManager,
        create_jwt_token,
        decode_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )
except ImportError:
    from .user_db import UserDatabase
    from .auth import (
        AuthManager,
        create_jwt_token,
        decode_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )

# App Configuration
DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")
USER_DATABASE_URL = os.environ.get("USER_DATABASE_URL", "data/users.db")
PORT = int(os.environ.get("PORT", 8080))

# The eBay integration is optional: with these unset the app is exactly the
# CSV tool it has always been, and every eBay control is simply absent. The
# client is built lazily and cached, because its PublicKeyCache must outlive a
# single request -- refetching eBay's verification key per notification is
# what their documentation warns will exhaust the call quota.
EBAY_NOTIFICATION_ENDPOINT = os.environ.get("EBAY_NOTIFICATION_ENDPOINT", "").strip()
# Read independently of the rest of the eBay configuration, because the
# endpoint challenge needs only this and the endpoint URL. That ordering is
# not hypothetical: eBay disables a new keyset until the deletion endpoint
# validates, and the RuName is registered later still -- so requiring a
# complete credential set to answer the challenge would deadlock the very
# bootstrap the challenge exists to unblock.
EBAY_VERIFICATION_TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "").strip()
_ebay_client = None


def get_ebay_client():
    """
    The shared eBay client, or None when the integration is not configured.

    Returns None rather than raising so that an unconfigured deployment keeps
    working. Callers that genuinely need eBay must check and answer 503
    themselves, which reads better than a stack trace about a missing key.
    """
    global _ebay_client
    if not EBAY_CLIENT_AVAILABLE:
        return None
    if _ebay_client is None and EbayConfig.is_configured():
        _ebay_client = EbayClient(
            EbayConfig.from_env(), store=_UserDbTokenStore()
        )
    return _ebay_client


class _UserDbTokenStore(TokenStore):
    """
    Persists the eBay refresh token in the users database.

    The library takes a store rather than touching SQLite itself, which is what
    keeps it free of any opinion about where credentials live. ``actor`` is set
    by the OAuth callback just before the code exchange, so the connection can
    record who authorised it; a refresh does not write, so it never clears it.
    """

    def __init__(self):
        self.actor = None

    def load(self):
        return user_db.get_ebay_token()

    def save(self, token):
        user_db.save_ebay_token(token, connected_by=self.actor)

# Initialize databases & auth
db = Database(db_path=DATABASE_URL)
user_db = UserDatabase(db_path=USER_DATABASE_URL)
auth_manager = AuthManager(user_db=user_db)

app = FastAPI(
    title="TCG Card Inventory Middleware",
    description="Bridge between SortSwift and eBay Seller Hub Reports",
    version="1.0.0",
)

# Mount static assets. Register woff2 explicitly: the vendored fonts would
# otherwise be served as application/octet-stream on some platforms.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/x-icon", ".ico")

static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# An upload is read into memory before parsing, so without a ceiling a single
# request can exhaust the container's RAM. Generous enough for any real
# SortSwift or eBay export; a 50,000-row dump is a few megabytes.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024


async def read_upload_limited(
    file: UploadFile, limit: int = MAX_UPLOAD_BYTES
) -> bytes:
    """
    Read an upload, refusing anything over the limit.

    Streams in chunks and stops at the ceiling rather than calling read() with
    no argument, which would materialise the whole body first and so defeat
    the check it is meant to enforce.
    """
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"That file is larger than the {limit // (1024 * 1024)} MB "
                    f"upload limit. Raise MAX_UPLOAD_MB if you really need to "
                    f"process a file this big."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """
    Baseline response headers.

    The CSP is sent Report-Only deliberately. An enforcing policy has to allow
    Google Identity Services and the vendored Tailwind build, which compiles
    classes in the browser, and getting either wrong renders a blank page. In
    report-only mode violations are visible in the browser console without any
    risk of breaking the dashboard, so the policy can be tightened against
    real evidence and then switched to enforcing.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # The dashboard has no reason to be framed, and framing it invites
    # clickjacking against the admin controls.
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy-Report-Only",
        "; ".join([
            "default-src 'self'",
            # 'unsafe-inline' covers the inline Tailwind config block.
            "script-src 'self' 'unsafe-inline' https://accounts.google.com",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "connect-src 'self' https://accounts.google.com",
            "frame-src https://accounts.google.com",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ]),
    )
    return response


# Pydantic Schemas
class GoogleAuthRequest(BaseModel):
    id_token: str


class StatusUpdateRequest(BaseModel):
    # A closed set, not a free string. An arbitrary value would be stored
    # verbatim and then compared against "active" on every request, so a typo
    # locks the account out in a way the UI cannot express or undo -- and the
    # value is rendered back into the admin table, which made it an injection
    # sink as well.
    status: Literal["active", "pending", "disabled"]


class RoleUpdateRequest(BaseModel):
    role: Literal["admin", "user"]


class CoverImageRequest(BaseModel):
    cover_image_url: str


class QuantityUpdateRequest(BaseModel):
    quantity: int
    # When the quantity drops, optionally emit a SortSwift deduction file for
    # the difference so the correction can be pushed back to SortSwift.
    generate_deduction: bool = False
    order_number: Optional[str] = None


class ManualCardAddRequest(BaseModel):
    product_name: str
    set_name: str
    condition: str = "Near Mint"
    printing: str = "Normal"
    quantity: int = 0
    ebay_parent_id: Optional[str] = None


# Dependencies
def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    return auth_manager.get_current_user_from_request(request)


def require_active_user(request: Request) -> Dict[str, Any]:
    return auth_manager.require_user(request)


def require_admin_user(request: Request) -> Dict[str, Any]:
    return auth_manager.require_admin(request)


# ---------------------------------------------------------
# AUTHENTICATION ENDPOINTS
# ---------------------------------------------------------

@app.get("/api/auth/me")
def get_auth_status(request: Request):
    """Check current authentication status and configuration."""
    user = auth_manager.get_current_user_from_request(request)
    total_users = user_db.get_user_count()
    return {
        "google_client_id": GOOGLE_CLIENT_ID,
        "is_authenticated": user is not None and user.get("status") == "active",
        "is_pending": user is not None and user.get("status") == "pending",
        "user": user,
        "has_users": total_users > 0,
    }


@app.post("/api/auth/google")
def login_google(req: GoogleAuthRequest, response: Response):
    """
    Sign in (or sign up) with a Google ID token.

    This is the only authentication endpoint. Accounts are keyed on the Google
    'sub' claim so that a user changing their email address on the Google side
    keeps the same local account and approval state.
    """
    claims = verify_google_id_token(req.id_token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Google sign-in token.",
        )

    google_sub = str(claims["sub"]).strip()
    email = claims["email"].strip()
    display_name = (claims.get("name") or email.split("@")[0]).strip()

    user = user_db.get_user_by_google_sub(google_sub)

    if not user:
        # A record already holding this email but no matching Google subject is a
        # stale leftover. Refuse rather than silently creating a second account,
        # which would split approval state across two rows.
        existing_by_email = user_db.get_user_by_email(email)
        if existing_by_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "An account already exists for this email address but is not "
                    "linked to this Google account. Ask an administrator to remove "
                    "the stale account and sign in again."
                ),
            )

        candidate_username = display_name
        counter = 1
        while user_db.get_user_by_username(candidate_username):
            candidate_username = f"{display_name}_{counter}"
            counter += 1

        user = user_db.create_user(
            username=candidate_username,
            google_sub=google_sub,
            email=email,
            auth_provider="google",
        )
    else:
        user_db.update_profile_from_google(user["id"], email=email)

    if user.get("status") == "disabled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated.",
        )

    user_db.update_last_login(user["id"])
    user = user_db.get_user_by_id(user["id"])

    token = create_jwt_token({"user_id": user["id"], "username": user["username"]})
    set_session_cookie(response, token)

    return {
        "success": True,
        "user": user,
        "is_pending": user.get("status") == "pending",
        "message": (
            "Signed in with Google. Your account is pending admin approval."
            if user.get("status") == "pending"
            else "Signed in successfully."
        ),
    }


@app.post("/api/auth/logout")
def logout_user(response: Response):
    """Clear session token cookie."""
    response.delete_cookie(key="session_token")
    return {"success": True, "message": "Logged out successfully."}


# ---------------------------------------------------------
# ADMIN USER MANAGEMENT ENDPOINTS
# ---------------------------------------------------------

@app.get("/api/admin/users")
def get_all_users(admin: Dict[str, Any] = Depends(require_admin_user)):
    """List all registered users (Admin only)."""
    return {"users": user_db.list_all_users()}


@app.post("/api/admin/users/{user_id}/status")
def set_user_status(
    user_id: int,
    req: StatusUpdateRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Approve or disable user account (Admin only)."""
    target = user_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target["id"] == admin["id"] and req.status != "active":
        raise HTTPException(status_code=400, detail="Cannot deactivate your own admin account.")
    user_db.set_user_status(user_id, req.status)
    return {"success": True, "user_id": user_id, "new_status": req.status}


@app.post("/api/admin/users/{user_id}/role")
def set_user_role(
    user_id: int,
    req: RoleUpdateRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Promote or demote user (Admin only)."""
    target = user_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target["id"] == admin["id"] and req.role != "admin":
        raise HTTPException(status_code=400, detail="Cannot demote your own admin account.")
    user_db.set_user_role(user_id, req.role)
    return {"success": True, "user_id": user_id, "new_role": req.role}


@app.delete("/api/admin/users/{user_id}")
def delete_user(
    user_id: int,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Delete a user account (Admin only)."""
    target = user_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target["id"] == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own admin account.")
    user_db.delete_user(user_id)
    return {"success": True, "message": "User deleted."}


# ---------------------------------------------------------
# CORE BUSINESS LOGIC / PROCESSING ENDPOINTS
# ---------------------------------------------------------

@app.post("/api/process/orders")
async def process_orders_endpoint(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module C: Process raw eBay orders CSV & convert to SortSwift Orders Import CSV.
    """
    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    def run():
        with db.session():
            return process_orders_csv(csv_text, db)

    return await run_in_threadpool(run)


@app.post("/api/process/batch")
async def process_batch_endpoint(
    file: UploadFile = File(...),
    force: bool = Form(False),
    dry_run: bool = Form(False),
    quantity_mode: str = Form(QUANTITY_MODE_SET),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module A: Ingest SortSwift scan batch, auto-catalog cards, route to Add vs Revise CSVs.

    Quantities are additive, so re-processing the same export would inflate live
    eBay stock. Uploads are fingerprinted and a repeat of an already-processed
    file is refused unless the caller explicitly passes force=true.

    Pass dry_run=true to rebuild the CSVs from current settings without writing
    anything to the catalogue or store mirror.

    Prices and listing settings come from the signed-in user's own rules, so
    two sellers processing the same export each get their own output.

    quantity_mode="set" (the default) treats the upload as a full inventory
    dump and replaces quantities; "add" treats it as a delta of newly scanned
    cards. The wrong one silently doubles live eBay stock on every upload, so
    an unrecognised value is rejected rather than guessed at.
    """
    if str(quantity_mode).strip().lower() not in QUANTITY_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"quantity_mode must be one of {', '.join(QUANTITY_MODES)}; "
                f"got {quantity_mode!r}"
            ),
        )

    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    # A few thousand card rows is seconds of synchronous SQLite work. Run it in
    # a worker thread: doing it inline blocks uvicorn's event loop, which makes
    # the whole dashboard unresponsive rather than just this request. The
    # session holds one connection open for the run instead of opening and
    # closing several per card.
    def run():
        with db.session():
            return process_batch_csv(
                csv_text,
                db,
                source_name=file.filename or "upload.csv",
                force=force,
                dry_run=dry_run,
                user_id=user["id"],
                quantity_mode=quantity_mode,
            )

    return await run_in_threadpool(run)


@app.post("/api/process/sync")
async def process_sync_endpoint(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module B: Ingest eBay Active Listings report CSV & sync live store mirror state.
    """
    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    def run():
        with db.session():
            return sync_active_listings_csv(csv_text, db)

    return await run_in_threadpool(run)


# ---------------------------------------------------------
# PRICING RULES API ENDPOINTS
# ---------------------------------------------------------

class PricingRuleItem(BaseModel):
    min_price: float = 0.0
    max_price: Optional[float] = None
    rule_type: str  # 'fixed', 'markup_fixed', 'markup_percent'
    rule_value: float
    sort_order: Optional[int] = 1


class PricingRulesUpdateRequest(BaseModel):
    rules: List[PricingRuleItem]


class ConditionMultiplierItem(BaseModel):
    condition_key: str
    multiplier: float
    label: Optional[str] = ""


class ConditionMultipliersUpdateRequest(BaseModel):
    multipliers: List[ConditionMultiplierItem]


class PricePreviewRequest(BaseModel):
    price: float
    # Optional so existing callers keep working; without it the preview is the
    # mint price, which is what it always was.
    condition: Optional[str] = None


@app.get("/api/pricing-rules")
def get_pricing_rules_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    The pricing rules that apply to the signed-in user.

    Rules are per-user, so this returns the caller's own set if they have saved
    one and the shared baseline otherwise. ``is_own`` lets the UI say which,
    since an inherited set looks identical but resetting it does nothing.
    """
    return {
        "rules": db.get_pricing_rules(user_id=user["id"]),
        "is_own": db.has_own_pricing_rules(user["id"]),
    }


@app.post("/api/pricing-rules")
def update_pricing_rules_endpoint(
    req: PricingRulesUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Save the signed-in user's own pricing rules.

    No longer admin-only: the rules belong to the user, and saving them cannot
    affect anyone else's prices. The write is scoped to the caller, so a user
    editing theirs for the first time creates their own set rather than
    changing the baseline others still inherit.
    """
    rules_data = [r.model_dump() for r in req.rules]
    db.set_pricing_rules(rules_data, user_id=user["id"])
    return {
        "success": True,
        "rules": db.get_pricing_rules(user_id=user["id"]),
        "is_own": db.has_own_pricing_rules(user["id"]),
    }


@app.post("/api/pricing-rules/reset")
def reset_pricing_rules_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Discard the caller's own rules and inherit the shared defaults again."""
    rules = db.reset_default_pricing_rules(user_id=user["id"])
    return {
        "success": True,
        "rules": rules,
        "is_own": db.has_own_pricing_rules(user["id"]),
    }


# How often the background refresh runs, and whether it runs at all. TCGCSV
# publishes once a day and asks for at most one sync per 24 hours, so anything
# under that is wasted requests against a service that asks us not to.
PRICE_REFRESH_ENABLED = os.environ.get(
    "PRICE_REFRESH_ENABLED", "true"
).strip().lower() not in ("0", "false", "no", "off")
PRICE_REFRESH_INTERVAL_HOURS = max(
    1, int(os.environ.get("PRICE_REFRESH_INTERVAL_HOURS", "24"))
)


@app.post("/api/pricing/refresh")
async def refresh_prices_endpoint(
    force: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Fetch current market prices from TCGCSV.

    Not admin-only. It writes the shared catalogue's market_price, but the
    value is objective external data rather than a preference, the write is
    idempotent, and every previous value is kept in price_history -- so no
    information can be lost and there is nothing for one user to impose on
    another.

    Runs in a worker thread: it makes outbound HTTP calls with deliberate
    spacing between them, and doing that on the event loop would freeze the
    dashboard exactly as the CSV pipelines used to.
    """
    def run():
        with db.session():
            return refresh_market_prices(db, force=force)

    try:
        result = await run_in_threadpool(run)
    except PriceFeedError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result


@app.get("/api/pricing/reprice")
def reprice_csv_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Summarise the eBay Revise file that current prices would produce.

    Separate from the download so the dashboard can say how many listings
    would change before anyone commits to a file, and so the indicator can
    appear without triggering a browser download.
    """
    result = build_reprice_csv(db, user_id=user["id"])
    return {
        "reprice_count": result["reprice_count"],
        "unchanged_count": result["unchanged_count"],
        "missing_price_count": result["missing_price_count"],
        "logs": result["logs"],
    }


@app.get("/api/pricing/reprice.csv")
def reprice_csv_download(user: Dict[str, Any] = Depends(require_active_user)):
    """The Revise file itself, priced by the caller's own rules."""
    result = build_reprice_csv(db, user_id=user["id"])
    return StreamingResponse(
        iter([result["csv_content"]]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                "attachment; filename=ebay_reprice_updates.csv"
        },
    )


@app.get("/api/pricing/history/{manifest_id}")
def price_history_endpoint(
    manifest_id: str,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Recent market prices for one card, so a surprising reprice is traceable."""
    return {"manifest_id": manifest_id,
            "history": db.get_price_history(manifest_id)}


@app.on_event("startup")
async def start_price_refresh_loop():
    """
    Refresh prices on a schedule.

    An in-process task rather than a host cron, so a Synology deployment needs
    no extra setup. The first run is delayed: a container restart should not
    fire an outbound fetch before the app is even serving. Every run is gated
    on TCGCSV's own last-updated timestamp, so a loop that wakes more often
    than they publish costs one request and changes nothing.
    """
    if not PRICE_REFRESH_ENABLED:
        print("[prices] background refresh disabled", flush=True)
        return

    import asyncio

    async def loop():
        await asyncio.sleep(120)
        while True:
            try:
                def run():
                    with db.session():
                        return refresh_market_prices(db)

                result = await run_in_threadpool(run)
                if result.get("skipped"):
                    print(f"[prices] already current ({result.get('snapshot')})",
                          flush=True)
                else:
                    print(f"[prices] updated {result.get('updated')} card(s) "
                          f"from snapshot {result.get('snapshot')}", flush=True)
            except PriceFeedError as exc:
                # Never fatal: a refresh that cannot reach TCGCSV leaves every
                # stored price exactly as it was.
                print(f"[prices] refresh failed, prices unchanged: {exc}",
                      flush=True)
            except Exception as exc:
                print(f"[prices] unexpected refresh error: {exc}", flush=True)
            await asyncio.sleep(PRICE_REFRESH_INTERVAL_HOURS * 3600)

    asyncio.create_task(loop())


@app.get("/api/condition-multipliers")
def get_condition_multipliers_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    The grade discounts that apply to the signed-in user.

    The market price we can obtain is product-level -- neither TCGplayer's
    public price data nor the SortSwift export it was relayed through breaks
    down by condition -- so the grade adjustment is policy, configured here.
    """
    return {
        "multipliers": db.get_condition_multipliers(user_id=user["id"]),
        "is_own": db.has_own_condition_multipliers(user["id"]),
    }


@app.post("/api/condition-multipliers")
def update_condition_multipliers_endpoint(
    req: ConditionMultipliersUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Save the signed-in user's own grade discounts."""
    for item in req.multipliers:
        if item.multiplier < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The multiplier for {item.condition_key} is negative. "
                    f"A grade discount cannot invert a price."
                ),
            )
        if item.multiplier > 10:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The multiplier for {item.condition_key} is {item.multiplier}, "
                    f"which would multiply the price rather than discount it. "
                    f"Use a markup rule for that."
                ),
            )
    db.set_condition_multipliers(
        [m.model_dump() for m in req.multipliers], user_id=user["id"]
    )
    return {
        "success": True,
        "multipliers": db.get_condition_multipliers(user_id=user["id"]),
        "is_own": db.has_own_condition_multipliers(user["id"]),
    }


@app.post("/api/condition-multipliers/reset")
def reset_condition_multipliers_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Discard the caller's own grade discounts and inherit the shared set."""
    multipliers = db.reset_condition_multipliers(user_id=user["id"])
    return {
        "success": True,
        "multipliers": multipliers,
        "is_own": db.has_own_condition_multipliers(user["id"]),
    }


@app.post("/api/pricing-rules/preview")
def preview_pricing_endpoint(
    req: PricePreviewRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Test and preview what an eBay price would be for a given TCG price."""
    multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in db.get_condition_multipliers(user_id=user["id"])
    }
    adjusted, factor = apply_condition_multiplier(
        req.price, req.condition, multipliers
    )
    calculated_price, rule = apply_pricing_rules(
        db.get_pricing_rules(user_id=user["id"]), adjusted
    )
    return {
        "input_price": req.price,
        "condition": req.condition,
        "condition_multiplier": factor,
        "adjusted_price": round(adjusted, 2),
        "calculated_price": calculated_price,
        "matched_rule": rule,
    }


# ---------------------------------------------------------
# LISTING & VARIATION SETTINGS API ENDPOINTS
# ---------------------------------------------------------

class ListingSettingsUpdateRequest(BaseModel):
    settings: Dict[str, str]


class TitlePreviewRequest(BaseModel):
    set_name: str
    condition: str = ""
    template: Optional[str] = "{set_name}: Pick Your Card - {condition} - Complete Your Set"


@app.get("/api/listing-settings")
def get_listing_settings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    The listing settings that apply to the signed-in user.

    These merge key by key: the shared baseline with the caller's own overrides
    on top. ``own_keys`` names the ones the caller has actually set, so the UI
    can distinguish an inherited value from a chosen one.
    """
    return {
        "settings": db.get_listing_settings(user_id=user["id"]),
        "own_keys": db.get_own_listing_setting_keys(user["id"]),
    }


@app.post("/api/listing-settings")
def update_listing_settings_endpoint(
    req: ListingSettingsUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Save the signed-in user's own listing settings.

    No longer admin-only, for the same reason as pricing rules: the title
    template, business policy names and postal code describe the caller's own
    eBay account, so they cannot sensibly be shared.
    """
    db.set_listing_settings(req.settings, user_id=user["id"])
    return {
        "success": True,
        "settings": db.get_listing_settings(user_id=user["id"]),
        "own_keys": db.get_own_listing_setting_keys(user["id"]),
    }


@app.post("/api/listing-settings/reset")
def reset_listing_settings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Discard the caller's own settings and inherit the shared defaults again."""
    settings = db.reset_listing_settings(user_id=user["id"])
    return {
        "success": True,
        "settings": settings,
        "own_keys": db.get_own_listing_setting_keys(user["id"]),
    }


@app.post("/api/listing-settings/preview-title")
def preview_title_endpoint(
    req: TitlePreviewRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Preview a variation title, including the 80-character fallback."""
    from tcg_engine.batches import (
        generate_variation_title,
        DEFAULT_VARIATION_TITLE_TEMPLATE,
    )
    generated_title = generate_variation_title(
        set_name=req.set_name,
        condition=req.condition,
        template=req.template or DEFAULT_VARIATION_TITLE_TEMPLATE,
    )
    return {
        "set_name": req.set_name,
        "condition": req.condition,
        "generated_title": generated_title,
        "char_count": len(generated_title),
        "is_valid": len(generated_title) <= 80,
    }


# ---------------------------------------------------------
# INVENTORY & CATALOG API ENDPOINTS
# ---------------------------------------------------------

@app.get("/api/inventory")
def get_inventory_endpoint(
    search: Optional[str] = None,
    sort_by: str = "manifest_id",
    sort_dir: str = "ASC",
    limit: int = 50,
    offset: int = 0,
    set_name: Optional[str] = None,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Fetch paginated, filtered, and sorted inventory list."""
    items = db.get_inventory(
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
        set_name=set_name,
    )
    total = db.get_inventory_count(search=search, set_name=set_name)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/ebay-listings")
def get_ebay_listings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Live eBay listings, rolled up from the store mirror.

    Derived rather than stored: the mirror is keyed by card, so this groups by
    eBay item number to show the store the way eBay presents it.
    """
    return {"listings": db.get_ebay_listings()}


@app.post("/api/ebay-listings/{item_id}/cover")
def set_listing_cover(
    item_id: str,
    req: CoverImageRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Record a listing's cover photo and return a Revise file that applies it.

    Saving locally is not enough on its own: the listing lives on eBay, so the
    change only takes effect once the returned CSV is uploaded there.
    """
    url = (req.cover_image_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="A cover photo URL is required.")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="The cover photo must be a full http:// or https:// URL that eBay can fetch.",
        )

    known = {l["ebay_parent_id"] for l in db.get_ebay_listings()}
    if item_id not in known:
        raise HTTPException(
            status_code=404,
            detail="No linked listing with that eBay item number.",
        )

    saved = db.set_listing_cover_image(item_id, url)
    return {
        "success": True,
        "ebay_parent_id": item_id,
        "cover_image_url": saved,
        "csv_content": build_cover_photo_revise_csv(item_id, saved),
    }


@app.get("/api/inventory/sets")
def get_inventory_sets(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Expansion sets present in the catalog, for the dashboard filter.

    Derived from the catalog rather than a fixed list, so the filter can only
    ever offer a set that actually has cards behind it.
    """
    return {"sets": db.get_distinct_set_names()}


@app.post("/api/inventory/{manifest_id}/quantity")
def set_card_quantity(
    manifest_id: str,
    req: QuantityUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set a card's catalogued quantity to an absolute value.

    This is the manual correction path, so the figure supplied is the figure
    stored -- unlike batch intake, which accumulates. When the quantity drops
    and generate_deduction is set, a SortSwift deduction CSV for the difference
    is returned so the same correction can be applied there.
    """
    if req.quantity < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative.")

    card = db.get_manifest_by_id(manifest_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found.")

    result = db.set_manifest_quantity(manifest_id, req.quantity)
    if not result:
        raise HTTPException(status_code=404, detail="Card not found.")

    delta = result["previous"] - result["current"]
    csv_content = None
    if req.generate_deduction and delta > 0:
        order_number = (req.order_number or "").strip() or f"MANUAL-{manifest_id}"
        csv_content = build_deduction_csv(
            [deduction_row(card, delta, order_number)]
        )

    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "current": result["current"],
        "deducted": delta if delta > 0 else 0,
        "csv_content": csv_content,
    }


@app.get("/api/stats")
def get_stats_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Get catalog and stock statistics."""
    return db.get_stats()


@app.post("/api/inventory/add")
def add_card_manually(
    req: ManualCardAddRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Manually add or update a card in the master catalog."""
    manifest_id, is_new, card_data = db.get_or_create_manifest(
        req.product_name, req.set_name, req.condition, req.printing
    )
    if req.ebay_parent_id:
        db.upsert_variation(manifest_id, req.ebay_parent_id, req.quantity)
    return {
        "success": True,
        "manifest_id": manifest_id,
        "is_new": is_new,
        "card": card_data,
    }


@app.delete("/api/inventory/{manifest_id}")
def delete_card_endpoint(
    manifest_id: str,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Delete a card from the master catalog."""
    success = db.delete_manifest(manifest_id)
    if not success:
        raise HTTPException(status_code=404, detail="Card not found.")
    return {"success": True, "manifest_id": manifest_id}


def _csv_safe(value: Any) -> Any:
    """
    Neutralise spreadsheet formula injection for a human-facing export.

    A cell beginning =, +, - or @ is evaluated as a formula by Excel and
    Sheets, so a card name or bin note carrying one becomes code in whoever
    opens the file. Prefixing with an apostrophe makes it literal text.

    Applied ONLY to this export, which exists to be opened in a spreadsheet.
    The eBay Add/Revise files must never be touched this way: eBay parses them
    as data, and an apostrophe would corrupt a title or a price.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@app.get("/api/export/manifest")
def export_manifest_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Export complete Master Catalog & Live Mirror as CSV download."""
    items = db.export_all_manifest()
    fieldnames = [
        "manifest_id",
        "product_name",
        "card_number",
        "set_name",
        "condition",
        "printing",
        "quantity",
        "ebay_parent_id",
        "last_known_qty",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    # This file is meant to be opened in a spreadsheet, so cells that would
    # otherwise be read as formulas are made literal first.
    writer.writerows(
        {key: _csv_safe(row.get(key)) for key in fieldnames} for row in items
    )
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=master_catalog_export.csv"},
    )


# ---------------------------------------------------------
# DATABASE BACKUP / RESTORE
# ---------------------------------------------------------

# Every SQLite file the deployment owns, so "download the database" can mean
# all of it rather than just the inventory. Each entry knows how to take its
# own consistent snapshot; a plain file copy is not safe while the app is
# running, because both databases are in WAL mode.
DATABASE_FILES = {
    "inventory": {
        "label": "Inventory",
        "stem": "tcg-inventory",
        "description": (
            "Card catalogue, eBay links, catalogued quantities, per-user "
            "pricing rules and listing settings, and cover photo overrides."
        ),
        "path": lambda: DATABASE_URL,
        "export": lambda dest: db.export_snapshot(dest),
        "summary": lambda: {
            "cards": db.get_stats()["total_cards"],
            "linked_to_ebay": db.get_stats()["active_listings"],
        },
    },
    "users": {
        "label": "Users",
        "stem": "tcg-users",
        "description": (
            "Google accounts, roles and approval status. Contains no passwords "
            "and no OAuth secrets -- sign-in is delegated to Google."
        ),
        "path": lambda: USER_DATABASE_URL,
        "export": lambda dest: user_db.export_snapshot(dest),
        "summary": lambda: {"accounts": user_db.count_users()},
    },
}


@app.get("/api/database/files")
def list_database_files(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    What is available to download, so the UI does not hardcode the list.

    Admin only, matching the downloads themselves.
    """
    entries = []
    for name, spec in DATABASE_FILES.items():
        path = os.path.abspath(spec["path"]())
        try:
            summary = spec["summary"]()
        except Exception:
            # A summary is cosmetic; never let it block a backup.
            summary = {}
        entries.append(
            {
                "name": name,
                "label": spec["label"],
                "description": spec["description"],
                "filename": os.path.basename(path),
                "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                "summary": summary,
            }
        )
    return {"files": entries}


@app.get("/api/database/download/{name}")
def download_database_file(
    name: str,
    background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Download one database as a consistent snapshot. Admin only."""
    spec = DATABASE_FILES.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No such database: {name}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="tcg-snapshot-")
    dest = os.path.join(tmp_dir, f"{spec['stem']}-{stamp}.db")

    try:
        spec["export"](dest)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Could not create a snapshot: {exc}"
        )

    background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        dest,
        media_type="application/vnd.sqlite3",
        filename=os.path.basename(dest),
        background=background,
    )


@app.get("/api/database/bundle")
def download_database_bundle(
    background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Download every database in one zip. Admin only.

    Each member is a VACUUM INTO snapshot rather than a copied file, so the
    archive is internally consistent and restorable without sidecars. A short
    README is included because a bare pair of .db files is not self-describing
    six months later.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="tcg-bundle-")
    archive = os.path.join(tmp_dir, f"tcg-databases-{stamp}.zip")
    notes = [
        f"TCG Inventory Middleware -- database backup taken {stamp}",
        "",
        "Each .db is a VACUUM INTO snapshot: complete on its own, with no",
        "-wal or -shm sidecar needed.",
        "",
    ]

    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, spec in DATABASE_FILES.items():
                member = f"{spec['stem']}-{stamp}.db"
                staged = os.path.join(tmp_dir, member)
                spec["export"](staged)
                bundle.write(staged, arcname=member)
                os.remove(staged)
                notes.append(f"{member}")
                notes.append(f"    {spec['description']}")
                notes.append("")
            bundle.writestr("README.txt", chr(10).join(notes))
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Could not build the backup archive: {exc}"
        )

    background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=os.path.basename(archive),
        background=background,
    )


@app.get("/api/inventory/database")
def download_inventory_database(
    background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Download a consistent snapshot of the inventory database. Admin only.

    Both halves of backup/restore are administrative: the file is the whole
    shared catalog plus every listing setting, which is more than an ordinary
    user needs in order to work. The endpoint is restricted rather than merely
    hidden, so the permission does not depend on the UI.

    Taken with VACUUM INTO so the write-ahead log is checkpointed into the file.
    A hand-copied .db can otherwise be missing its most recent commits.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="tcg-snapshot-")
    dest = os.path.join(tmp_dir, f"tcg-inventory-{stamp}.db")

    try:
        db.export_snapshot(dest)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Could not create a snapshot: {exc}"
        )

    # Remove the temp copy once the response has been sent.
    background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)

    return FileResponse(
        dest,
        media_type="application/vnd.sqlite3",
        filename=os.path.basename(dest),
        background=background,
    )


@app.post("/api/inventory/database")
async def import_inventory_database(
    file: UploadFile = File(...),
    confirm: bool = Form(False),
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Replace the inventory database with an uploaded snapshot. Admin only.

    Restricted to admins because the catalog is shared: a restore replaces
    everyone's data, not just the uploader's.

    Without confirm=true the upload is only validated and summarised, so an
    operator can see what a restore would bring in before committing to it. The
    current database is copied aside first either way, so a restore is
    reversible.
    """
    tmp_dir = tempfile.mkdtemp(prefix="tcg-import-")
    staged = os.path.join(tmp_dir, "upload.db")
    try:
        written = 0
        with open(staged, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"That database is larger than the "
                            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload "
                            f"limit. Raise MAX_UPLOAD_MB to restore it."
                        ),
                    )
                out.write(chunk)

        check = db.inspect_snapshot(staged)
        if not check["ok"]:
            raise HTTPException(status_code=400, detail=check["error"])

        current = db.get_stats()

        if not confirm:
            return {
                "applied": False,
                "filename": file.filename,
                "incoming": check["counts"],
                "current": {
                    "manifest": current["total_cards"],
                    "ebay_variations": current["active_listings"],
                },
                "message": (
                    "Validated but not applied. Re-send with confirm=true to "
                    "replace the current inventory database."
                ),
            }

        result = db.replace_with_snapshot(staged)
        return {
            "applied": True,
            "filename": file.filename,
            "incoming": result["counts"],
            "backup_path": result["backup_path"],
            "message": "Inventory database replaced. A backup of the previous one was kept.",
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

@app.get("/api/health")
def health_check():
    """
    Unauthenticated liveness/readiness probe for Docker and Container Manager.

    Reports degraded rather than merely 'process is up' so that a container with
    an unreachable or unwritable data volume is surfaced as unhealthy.
    """
    try:
        db.get_stats()
        return {"status": "ok", "database": "reachable"}
    except Exception as exc:
        # This endpoint is unauthenticated, so the exception text stays in the
        # server log rather than going to whoever asked. A SQLite error
        # discloses absolute paths, which is free reconnaissance.
        print(f"[health] database unreachable: {exc}", flush=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "database": "unreachable"},
        )


# ---------------------------------------------------------
# FRONTEND HTML ROUTE
# ---------------------------------------------------------

# -------------------------------------------------------------------
# Connecting the eBay account
# -------------------------------------------------------------------
#
# One connection for the whole deployment, and an administrative one. There is
# a single eBay store behind this application and the inventory it manages is
# shared rather than per-user, so letting two users each connect a different
# account would make it ambiguous which store a push targets. Pricing rules
# are per-user because they are preferences; the store connection is
# infrastructure, like backup and restore.

# How long a consent attempt may sit unfinished. Long enough to read eBay's
# screen, short enough that a stale link in someone's history is useless.
EBAY_OAUTH_STATE_TTL_SECONDS = 600


def _require_ebay_client():
    client = get_ebay_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "eBay is not configured. Set EBAY_CLIENT_ID, "
                "EBAY_CLIENT_SECRET and EBAY_REDIRECT_URI."
            ),
        )
    return client


@app.get("/api/ebay/status")
def ebay_status(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Whether eBay is configured and connected, for the dashboard.

    Carries no token material. The refresh expiry is included because an eBay
    refresh token dies after roughly eighteen months and the only cure is an
    interactive re-consent, so it is worth showing before it lapses rather
    than after.
    """
    client = get_ebay_client()
    if client is None:
        return {
            "available": EBAY_CLIENT_AVAILABLE,
            "configured": False,
            "connected": False,
        }
    status_payload = dict(client.status())
    status_payload["available"] = True
    meta = user_db.get_ebay_connection_meta() or {}
    status_payload["connected_by"] = meta.get("username")
    status_payload["connected_at"] = meta.get("updated_at")
    return status_payload


@app.post("/api/ebay/connect")
def ebay_connect(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    Begin the consent flow: returns the eBay URL to send the seller to.

    The ``state`` is a signed, short-lived token rather than a random string
    held in memory. Signing it makes the callback self-contained -- it survives
    a container restart mid-consent -- and verifying it on return is what stops
    an attacker delivering an authorization code of their choosing to the
    callback, which would connect *their* eBay account to this deployment.
    """
    client = _require_ebay_client()
    state = create_jwt_token(
        {"purpose": "ebay_oauth", "user_id": admin["id"]},
        expires_in_seconds=EBAY_OAUTH_STATE_TTL_SECONDS,
    )
    return {"authorization_url": client.oauth.authorization_url(state)}


@app.get("/api/ebay/callback")
def ebay_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    declined: str = "",
):
    """
    Finish the consent flow and store the refresh token.

    Deliberately not behind a session dependency: the signed ``state`` is the
    authorisation, and it carries the user id itself. Requiring a live session
    as well would throw away a valid authorization code whenever a session
    happened to lapse during consent -- and the code cannot be replayed, so
    that costs a whole re-consent for no security gain.

    Answers HTML rather than JSON because a person's browser lands here, not a
    program.
    """
    def page(title: str, message: str, ok: bool) -> HTMLResponse:
        colour = "#10B981" if ok else "#F43F5E"
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{escape_html(title)}</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;background:#0f172a;"
            "color:#e2e8f0;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0\">"
            "<div style='text-align:center;max-width:34rem;padding:2rem'>"
            f"<h1 style='color:{colour};font-size:1.25rem'>"
            f"{escape_html(title)}</h1>"
            f"<p style='color:#94a3b8;font-size:0.9rem;line-height:1.6'>"
            f"{escape_html(message)}</p>"
            "<p><a href='/' style='color:#818CF8;font-size:0.9rem'>"
            "Back to the dashboard</a></p></div></body></html>",
            status_code=200 if ok else 400,
        )

    if declined or error:
        # eBay's own description is shown: it is the only thing that
        # distinguishes "I changed my mind" from a misconfigured RuName.
        return page(
            "eBay access was not granted",
            error_description or error or "The consent screen was declined.",
            False,
        )

    claims = decode_jwt_token(state) if state else None
    if not claims or claims.get("purpose") != "ebay_oauth":
        print("[ebay] oauth callback with a missing or invalid state", flush=True)
        return page(
            "That link is no longer valid",
            "Start the connection again from the dashboard. Consent links "
            "expire after ten minutes.",
            False,
        )
    if not code:
        return page(
            "eBay returned no authorization code",
            "Start the connection again from the dashboard.",
            False,
        )

    client = get_ebay_client()
    if client is None:
        return page(
            "eBay is not configured",
            "Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET and EBAY_REDIRECT_URI, "
            "then try again.",
            False,
        )

    client.oauth.store.actor = claims.get("user_id")
    try:
        client.oauth.exchange_code(code)
    except EbayError as exc:
        # eBay's own detail, plus whatever we can add. Its 401 for the token
        # endpoint says only "client authentication failed", which names
        # neither the credential nor the environment -- so the hints below are
        # usually more useful than the error itself.
        print(f"[ebay] authorization code exchange failed: {exc}", flush=True)
        hint = client.config.environment_mismatch() or (
            "Check EBAY_CLIENT_SECRET against the Cert ID on the Application "
            "Keysets page, and that EBAY_CLIENT_ID and EBAY_CLIENT_SECRET come "
            "from the same keyset. A .env saved with Windows line endings can "
            "also leave a stray carriage return on the value."
        )
        return page("eBay refused the authorization", f"{exc}. {hint}", False)

    print("[ebay] account connected", flush=True)
    return page(
        "eBay account connected",
        "This deployment can now read and manage your listings. Nothing has "
        "been sent to eBay: a draft still has to be approved before anything "
        "is pushed.",
        True,
    )


@app.post("/api/ebay/sync")
async def ebay_sync_from_api(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Module B without the manual download: fetch the report from eBay and sync.

    Read-only as far as eBay is concerned. It asks for a report, waits, and
    downloads it -- the same data the Seller Hub Active Listings report
    carries, obtained through the Feed API instead of a browser.

    The report is handed to the *existing* CSV parser rather than a new one.
    Every rule in that parser -- skipping variation parent rows, the manifest
    lookup, and above all zeroing cards absent from the report -- would
    otherwise have to be reimplemented and could drift from the upload path.

    Diagnostics come back alongside the result because the exact column names
    in the Feed report are not something this code has yet seen against a real
    store; if a column is missing, the headers in the response say which.
    """
    client = _require_ebay_client()
    if not client.oauth.is_connected():
        raise HTTPException(
            status_code=409,
            detail="No eBay account is connected. Connect one first.",
        )

    def run():
        # The library returns the report already normalised to CSV. It arrives
        # as eBay XML, and reading that as a CSV does not fail -- every line
        # becomes a row, none carries a SKU column, and the result is
        # indistinguishable from a store that ended every listing.
        report = download_active_inventory_report(client)
        csv_text = report["csv_text"]
        # Captured before parsing: if the sync finds nothing, the headers are
        # the first thing worth looking at, and by then the reader is spent.
        first_line = next(
            (line for line in csv_text.splitlines() if line.strip()), ""
        )
        with db.session():
            result = sync_active_listings_csv(csv_text, db)
        result["report"] = {
            "task_id": report["task_id"],
            "status": report["status"],
            "was_xml": report.get("was_xml", False),
            "bytes": len(report["content"]),
            "row_count": max(
                0, len([l for l in csv_text.splitlines() if l.strip()]) - 1
            ),
            "headers": [h.strip() for h in first_line.split(",")][:40],
        }
        # Only when nothing matched, and only element names -- never values.
        # Inferring the report's structure from row counts cost two rounds of
        # guessing; an outline settles it in one look and is safe to paste.
        if result.get("synced_count", 0) == 0 and report.get("was_xml"):
            result["report"]["outline"] = report_outline(report["content"])
        return result

    try:
        return await run_in_threadpool(run)
    except FeedError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except EbayError as exc:
        print(f"[ebay] report sync failed: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"eBay refused the request: {exc}")


@app.post("/api/ebay/disconnect")
def ebay_disconnect(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    Forget the stored refresh token.

    Reconnecting requires the consent screen again, so this is not a toggle to
    flip idly -- but it is the correct response to a credential you no longer
    trust.
    """
    client = get_ebay_client()
    if client is not None:
        client.oauth.disconnect()
    else:
        # Still clear the row: the token outlives a configuration change, and
        # leaving it behind would silently reconnect if the keys came back.
        user_db.save_ebay_token(None)
    print(f"[ebay] account disconnected by user {admin['id']}", flush=True)
    return {"success": True, "connected": False}


@app.get("/api/ebay/account-setup")
async def ebay_account_setup(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    The ids a push needs, read from the seller's own eBay account.

    Business policy ids appear nowhere in Seller Hub -- the API is the only
    way to learn them -- and the Inventory API addresses policies by id while
    File Exchange addressed them by name. So the dashboard asks eBay, matches
    the names already configured for the CSV path, and lets the answer be
    confirmed rather than hunted for.

    Also reports whether an inventory location exists. An account that has
    only ever listed through File Exchange has none, and eBay will not publish
    an offer without one, so an empty list here is the normal state and a
    thing to fix rather than a fault.
    """
    client = get_ebay_client()
    if client is None:
        raise HTTPException(
            status_code=503, detail="eBay is not configured."
        )
    if not client.oauth.is_connected():
        raise HTTPException(
            status_code=409,
            detail="Connect the eBay account first: this reads its policies.",
        )

    settings = db.get_listing_settings(user_id=admin["id"])
    marketplace_id = str(settings.get("marketplace_id") or "EBAY_US")

    def run():
        policies = get_policies(client.seller, marketplace_id)
        return policies, get_inventory_locations(client.seller)

    try:
        policies, locations = await run_in_threadpool(run)
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=f"eBay refused: {exc}")

    return {
        "marketplace_id": marketplace_id,
        "policies": policies,
        "locations": locations,
        # What the CSV path has been using, so the names can be shown beside
        # the matches and checked by eye.
        "configured_names": {
            "fulfillment": settings.get("shipping_profile_name") or "",
            "return": settings.get("return_profile_name") or "",
            "payment": settings.get("payment_profile_name") or "",
        },
        "suggested": suggest_policy_ids(
            policies,
            shipping_name=settings.get("shipping_profile_name") or "",
            return_name=settings.get("return_profile_name") or "",
            payment_name=settings.get("payment_profile_name") or "",
        ),
        "current": {
            "shipping_policy_id": settings.get("shipping_policy_id") or "",
            "return_policy_id": settings.get("return_policy_id") or "",
            "payment_policy_id": settings.get("payment_policy_id") or "",
            "merchant_location_key": settings.get("merchant_location_key") or "",
        },
        "postal_code": settings.get("seller_postal_code") or "",
    }


class InventoryLocationRequest(BaseModel):
    merchant_location_key: str = "home"
    name: str = "Home"
    postal_code: str = ""


@app.post("/api/ebay/inventory-location")
async def ebay_create_inventory_location(
    req: InventoryLocationRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Create the inventory location eBay requires before publishing an offer.

    A warehouse location needs only a name and a postal code and country --
    no street address -- which is the right shape for shipping from home
    without publishing an address. The key is permanent once set, so it is
    validated before the call rather than after.
    """
    client = get_ebay_client()
    if client is None or not client.oauth.is_connected():
        raise HTTPException(
            status_code=409, detail="Connect the eBay account first."
        )

    settings = db.get_listing_settings(user_id=admin["id"])
    postal = (req.postal_code or settings.get("seller_postal_code") or "").strip()
    if not postal:
        raise HTTPException(
            status_code=400,
            detail="A postal code is required. Set one in Listing Rules.",
        )

    def run():
        return create_inventory_location(
            client.seller, req.merchant_location_key,
            name=req.name, postal_code=postal,
        )

    try:
        key = await run_in_threadpool(run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=f"eBay refused: {exc}")

    # Recorded immediately: the key cannot be changed on eBay's side, so
    # losing track of it would mean a location nothing can reference.
    db.set_listing_settings(
        {"merchant_location_key": key}, user_id=admin["id"]
    )
    print(f"[ebay] inventory location {key} created", flush=True)
    return {"success": True, "merchant_location_key": key}


# -------------------------------------------------------------------
# eBay marketplace account deletion notifications
# -------------------------------------------------------------------
#
# Required by eBay before a keyset will function at all: a developer must
# either receive these notifications or hold an exemption. Two distinct
# mechanisms live on the one URL, which is eBay's design, not ours.
#
#   GET  - a one-time challenge when eBay validates the endpoint. Answer with
#          the SHA-256 of the challenge code, our verification token and the
#          endpoint URL, in that order.
#   POST - a real notification, signed. Verify it or answer 412.
#
# Both are necessarily unauthenticated: eBay has no session with us. The GET
# discloses only a hash, and the POST is refused unless eBay signed it.


@app.get("/api/ebay/notifications")
def ebay_notification_challenge(challenge_code: str = ""):
    """
    Answer eBay's endpoint-validation challenge.

    The endpoint URL is taken from configuration rather than reconstructed
    from the request, because behind the DSM reverse proxy the URL this
    process sees is not the URL eBay called -- and the URL is hashed, so a
    mismatch fails validation with an error that never says why. This is the
    single most common cause of failed endpoint validation.

    Needs only the verification token and the endpoint URL: no client id, no
    secret, no RuName. eBay disables a new keyset until this endpoint
    validates, so demanding a complete credential set here would deadlock the
    bootstrap it exists to unblock.
    """
    missing = [
        name
        for name, value in (
            ("the ebay_client library", EBAY_CLIENT_AVAILABLE),
            ("EBAY_VERIFICATION_TOKEN", EBAY_VERIFICATION_TOKEN),
            ("EBAY_NOTIFICATION_ENDPOINT", EBAY_NOTIFICATION_ENDPOINT),
        )
        if not value
    ]
    if missing:
        # Deliberately vague to an unauthenticated caller; the detail goes to
        # the log, matching how /api/health handles its failures.
        print(
            "[ebay] notification challenge received but missing: "
            + ", ".join(missing)
            + ". The endpoint must be the public URL exactly as entered in "
            "eBay's console, because the challenge hashes that string.",
            flush=True,
        )
        raise HTTPException(
            status_code=503, detail="eBay notifications are not configured."
        )
    if not challenge_code:
        raise HTTPException(status_code=400, detail="challenge_code is required.")

    return {
        "challengeResponse": challenge_response(
            challenge_code,
            EBAY_VERIFICATION_TOKEN,
            EBAY_NOTIFICATION_ENDPOINT,
        )
    }


@app.post("/api/ebay/notifications")
async def ebay_notification_receive(request: Request):
    """
    Receive a signed eBay notification.

    The raw request body is verified, never a re-serialised copy: any
    reformatting -- key order, whitespace, unicode escaping -- changes the
    signed bytes and invalidates the signature.

    Nothing is deleted in response to an account-deletion notification because
    this application stores no eBay user personal data. Module C reads an order
    export in memory and persists nothing from it; the store mirror holds only
    our own listings' item numbers, labels, quantities and prices. Should that
    ever change, this is the handler that has to grow a deletion path.
    """
    client = get_ebay_client()
    if client is None:
        raise HTTPException(
            status_code=503, detail="eBay notifications are not configured."
        )

    body = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    try:
        await run_in_threadpool(
            verify_signature, body, signature, client.public_keys
        )
    except SignatureError as exc:
        # 412 is what eBay's own SDKs answer, and it must not be a 200: an
        # unverified payload is an anonymous request that merely looks like
        # eBay, since anyone who learns this URL can post to it.
        print(f"[ebay] notification signature rejected: {exc}", flush=True)
        return JSONResponse(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            content={"status": "signature not verified"},
        )
    except EbayError as exc:
        # Could not reach eBay for the verification key. Answering 500 makes
        # eBay retry, which is right -- silently accepting would defeat the
        # verification entirely.
        print(f"[ebay] could not verify notification: {exc}", flush=True)
        raise HTTPException(status_code=500, detail="Verification unavailable.")

    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        payload = None

    # The topic only. An account-deletion payload carries the closing user's
    # username and user id, and logging those would create a durable record of
    # exactly the personal data this application is attesting it does not keep.
    print(
        f"[ebay] verified notification received: topic="
        f"{payload_topic(payload) or 'unknown'}",
        flush=True,
    )
    return {"status": "acknowledged"}


# -------------------------------------------------------------------
# Draft plans: staging for every eBay-bound change
# -------------------------------------------------------------------


class PlanItemUpdateRequest(BaseModel):
    """
    One edit to one planned change.

    Every field is optional so the drafts page can send just what changed.
    status is a Literal because it is stored verbatim, compared on later
    requests and rendered back into the page -- the same reasoning that made
    role and status closed sets on the user model.
    """

    proposed_qty: Optional[int] = None
    proposed_price: Optional[float] = None
    group_key: Optional[str] = None
    status: Optional[Literal["pending", "excluded"]] = None


class PlanBuildRequest(BaseModel):
    source: Literal["manual", "batch", "reprice", "photo", "grouping"] = "manual"
    note: Optional[str] = None


@app.get("/api/plans")
def list_plans(user: Dict[str, Any] = Depends(require_active_user)):
    """
    This user's recent plans, newest first.

    Scoped to the caller. Plans carry a user_id because approval is an
    authorisation record -- whose plan it was and who approved it -- so
    showing another user's drafts would let one person approve another's
    intent.
    """
    return {"plans": db.get_plans(user_id=user["id"])}


@app.post("/api/plans/build")
async def build_draft_plan(
    req: PlanBuildRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Compute a fresh draft plan from the current catalogue.

    Replaces any open draft: a draft is a snapshot of a diff, and once the
    catalogue moves underneath it the old draft describes a change that no
    longer applies. Runs in a threadpool because it walks the whole catalogue
    and would otherwise block the event loop and freeze the dashboard.
    """
    try:
        summary = await run_in_threadpool(
            build_plan,
            db,
            user["id"],
            source=req.source,
            note=req.note,
        )
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": True, **summary}


@app.get("/api/plans/{plan_id}")
def get_plan_detail(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    A plan with its listings, its items and everything blocking approval.

    One response rather than three round trips, because the page cannot render
    a meaningful row without all of them: an item's blockers decide how it is
    drawn.
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")

    try:
        blockers = plan_blockers(db, plan_id)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "plan": plan,
        "groups": db.get_plan_groups(plan_id),
        "items": db.get_plan_items(plan_id),
        "blockers": blockers,
    }


@app.patch("/api/plans/items/{item_id}")
def update_plan_item_endpoint(
    item_id: int,
    req: PlanItemUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Edit one planned change: its quantity, price, listing or inclusion.

    Only a draft may be edited. Editing an approved plan would change what is
    about to be pushed after the approval that authorised it, which makes the
    approval a record of something that never happened.
    """
    item = db.get_plan_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plan item not found.")
    plan = db.get_plan(item["plan_id"])
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan item not found.")
    if plan["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"This plan is {plan['status']} and can no longer be edited.",
        )

    changes = req.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    if "proposed_qty" in changes and changes["proposed_qty"] < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative.")
    if "proposed_price" in changes and changes["proposed_price"] <= 0:
        raise HTTPException(
            status_code=400,
            detail="Price must be above zero; eBay rejects a zero-price listing.",
        )

    db.update_plan_item(item_id, **changes)
    # Re-validate straight away so the page never shows a blocker for a
    # problem the user has just fixed.
    problems = revalidate_item(db, item_id)
    return {
        "success": True,
        "item": db.get_plan_item(item_id),
        "problems": problems,
        "blockers": plan_blockers(db, item["plan_id"]),
    }


class PlanCoverRequest(BaseModel):
    group_key: str
    cover_image_url: str = ""


@app.post("/api/plans/{plan_id}/cover")
def set_plan_cover(
    plan_id: int,
    req: PlanCoverRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Stage a cover photo for one listing in a draft.

    Stored against the plan rather than written to ebay_listing_overrides,
    because that table is what the store currently has -- writing there would
    apply the change before it was approved. An empty URL clears the staged
    choice and the page falls back to showing the live listing's own picture.
    """
    plan = db.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"This plan is {plan['status']} and can no longer be edited.",
        )

    url = req.cover_image_url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="A cover photo must be an http:// or https:// URL that eBay can fetch.",
        )

    db.set_plan_group_cover(plan_id, req.group_key, url)
    return {
        "success": True,
        "groups": db.get_plan_groups(plan_id),
    }


def _plan_for_download(plan_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    plan = db.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] == "draft":
        raise HTTPException(
            status_code=409,
            detail="Approve the draft before downloading its files.",
        )
    return plan


@app.get("/api/plans/{plan_id}/files")
def get_plan_file_summary(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    What an approved plan's files contain, without downloading them.

    Lets the dashboard offer only the files that have rows in them, and show
    the listing count so it can be checked against what was on screen.
    """
    _plan_for_download(plan_id, user)
    try:
        built = build_plan_exports(db, plan_id, user_id=user["id"])
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "listing_count": built["listing_count"],
        "variation_listing_count": built["variation_listing_count"],
        "single_listing_count": built["single_listing_count"],
        "add_card_count": built["add_card_count"],
        "revise_count": built["revise_count"],
        "unlistable": built["unlistable"],
        "cover_count": len(db.get_plan_cover_revisions(plan_id)),
        # A cover staged for a listing that does not exist yet cannot be
        # revised onto anything, so it rides in the Add file instead. Reported
        # separately because otherwise it looks like the choice was dropped.
        "add_cover_count": built["add_cover_count"],
        # One row per listing, so the page can offer a single listing to be
        # pushed rather than only all of them. "managed" is the distinction
        # that decides whether the API can touch it at all: a listing made
        # through File Exchange is invisible to the Inventory API and a push
        # would duplicate it rather than update it.
        "groups": [
            {
                "group_key": group["group_key"],
                "set_name": group.get("set_name") or "",
                "condition": group.get("condition") or "",
                "item_count": group.get("item_count") or 0,
                "excluded_count": group.get("excluded_count") or 0,
                "ebay_parent_id": group.get("ebay_parent_id") or "",
                "managed": db.get_managed_listing(group["group_key"]) is not None,
            }
            for group in db.get_plan_groups(plan_id)
        ],
    }


@app.get("/api/plans/{plan_id}/add.csv")
def download_plan_add_csv(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    The Add file an approved plan authorised: one listing per group.

    Built from the plan rather than from the original upload, so a regrouped
    card, an edited price and an excluded card all land in the file. Module A's
    own download predates every one of those edits.
    """
    _plan_for_download(plan_id, user)
    try:
        built = build_plan_exports(db, plan_id, user_id=user["id"])
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not built["add_card_count"]:
        raise HTTPException(
            status_code=404, detail="This plan creates no new listings."
        )
    return StreamingResponse(
        io.StringIO(built["add_csv"]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="ebay_new_additions_plan_{plan_id}.csv"'
            )
        },
    )


@app.get("/api/plans/{plan_id}/revise.csv")
def download_plan_revise_csv(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """The Revise file: quantity and price changes to existing listings."""
    _plan_for_download(plan_id, user)
    try:
        built = build_plan_exports(db, plan_id, user_id=user["id"])
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not built["revise_count"]:
        raise HTTPException(
            status_code=404, detail="This plan revises no existing listings."
        )
    return StreamingResponse(
        io.StringIO(built["revise_csv"]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="ebay_inventory_updates_plan_{plan_id}.csv"'
            )
        },
    )


@app.get("/api/plans/{plan_id}/cover-revise.csv")
def download_plan_cover_revise(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    The cover-photo Revise file for an approved plan.

    Approved only: the file exists to be uploaded, and generating it from a
    draft would hand out something that had not been authorised.

    Note that revising PicURL replaces a listing's whole picture set rather
    than adding to it, which is why this is a deliberate, separate file rather
    than something folded silently into another export.
    """
    plan = db.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] == "draft":
        raise HTTPException(
            status_code=409,
            detail="Approve the draft before downloading its files.",
        )

    rows = db.get_plan_cover_revisions(plan_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                "No cover photo changes in this plan apply to an existing "
                "listing. A cover for a listing that does not exist yet is "
                "carried into its creation instead."
            ),
        )
    csv_content = build_cover_photo_revise_rows(rows)
    return StreamingResponse(
        io.StringIO(csv_content),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="ebay_cover_photos_plan_{plan_id}.csv"'
            )
        },
    )


@app.post("/api/plans/{plan_id}/approve")
def approve_plan_endpoint(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    Approve a plan, which is the only thing that authorises an eBay write.

    Nothing is pushed here. Approval and push are separate so that the push
    worker's authorisation check is a stored fact rather than a claim made by
    whichever request happens to be running.
    """
    plan = db.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    try:
        result = approve_plan(db, plan_id, approved_by=user["id"])
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": True, **result}


class PlanPushRequest(BaseModel):
    """
    Which of a plan's listings to push.

    ``group_key`` restricts the push to one listing, which is what makes a
    first push testable: a create cannot be undone by pressing the button
    again, so one small listing goes up and is checked in Seller Hub before
    several hundred cards go live in a single call. Omitted means the whole
    plan.
    """

    group_key: Optional[str] = None


@app.post("/api/plans/{plan_id}/push")
async def push_plan_endpoint(
    plan_id: int,
    req: Optional[PlanPushRequest] = None,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Apply an approved plan to eBay.

    The only endpoint in this application that changes a live listing. It acts
    on a *stored* approval rather than on a claim made by this request, so the
    record of who authorised the change exists before the change does.

    Runs in a worker thread: a push walks several eBay calls per listing, each
    with its own retry budget, and holding the event loop for that would stall
    every other request including the health check.

    Only listings created through this API are touched. Anything made through
    File Exchange is left for the CSV path and reported as deferred -- pushing
    it would create a duplicate listing rather than update the live one.
    """
    plan = db.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] == "draft":
        raise HTTPException(
            status_code=409, detail="Approve the draft before pushing it."
        )

    client = get_ebay_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="eBay is not configured, so nothing can be pushed.",
        )
    if not client.oauth.is_connected():
        raise HTTPException(
            status_code=409,
            detail=(
                "Connect the eBay account first: a push acts as the seller "
                "and needs their consent."
            ),
        )

    adapter = InventoryApiAdapter(client)

    group_keys = None
    if req is not None and req.group_key is not None:
        group_keys = [req.group_key]

    def run():
        with db.session():
            return push_plan(
                db, adapter, plan_id, user_id=user["id"],
                group_keys=group_keys,
            )

    try:
        result = await run_in_threadpool(run)
    except PushError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        # A failure that belongs to the whole push rather than to one card:
        # credentials, or eBay being unreachable. Reported as such, because
        # the per-card verdicts inside the result mean something different.
        raise HTTPException(status_code=502, detail=f"eBay refused: {exc}")

    for entry in result.get("logs", []):
        print(f"[push] {entry['level']}: {entry['message']}", flush=True)
    return {"success": True, **result}


@app.delete("/api/plans/{plan_id}")
def discard_plan_endpoint(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    Throw a plan away: a draft, or an approved plan nothing acted on.

    The line is drawn at whether the plan reached eBay, not at whether it was
    approved. An approval that was never pushed is a decision the user changed
    their mind about, and the drafts page fills up with them; a plan that was
    pushed is the only record of who authorised a live change and what it did,
    so it stays.
    """
    plan = db.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] not in ("draft", "approved"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Plan {plan_id} is {plan['status']} -- it reached eBay, so it "
                f"is the record of that change and cannot be deleted."
            ),
        )
    db.delete_plan(plan_id)
    return {"success": True}


# Assets whose URLs get a content hash appended, so the browser is forced to
# fetch the version that belongs with the HTML it just received.
VERSIONED_ASSETS = ("/static/app.js", "/static/style.css")


def _asset_version(url_path: str) -> str:
    """Short content hash for a static file, or an empty string if absent."""
    relative = url_path.replace("/static/", "", 1)
    path = os.path.join(static_dir, *relative.split("/"))
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:12]
    except OSError:
        return ""


def _version_asset_urls(html: str) -> str:
    """
    Append a content hash to the app's own asset URLs.

    Without this a browser can hold a cached app.js from a previous deploy and
    pair it with fresh HTML, or vice versa. That combination is not a slow
    page, it is a broken one: renaming a single element id makes the old script
    dereference null on an element the new markup no longer has.
    """
    for asset in VERSIONED_ASSETS:
        version = _asset_version(asset)
        if version:
            html = html.replace(asset + '"', f'{asset}?v={version}"')
    return html


@app.get("/")
def serve_dashboard():
    """
    Serve the single-page dashboard.

    Sent with no-store: the HTML is the index of which asset versions belong
    together, so a stale copy pairs old markup with a new script. It is a few
    tens of kilobytes and the assets it points at are hashed, so there is
    nothing to gain by caching it.
    """
    index_file = os.path.join(static_dir, "index.html")
    headers = {
        "Cache-Control": "no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(
                content=_version_asset_urls(f.read()), headers=headers
            )
    return HTMLResponse(
        "<h1>TCG Inventory Middleware API is running.</h1>", headers=headers
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
