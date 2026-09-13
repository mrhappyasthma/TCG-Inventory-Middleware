import asyncio
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
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Literal, Sequence

# Importing app.deps first is load-bearing, not stylistic: it puts the
# project root on sys.path and loads .env, and `auth` reads GOOGLE_CLIENT_ID
# at module level and refuses to import without it.
try:
    from app.deps import project_root  # noqa: F401
except ImportError:
    from .deps import project_root  # noqa: F401

from fastapi import (
    FastAPI,
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
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from tcg_engine.csvtools import decode_csv_bytes

try:
    from app.routes import database as database_routes
except ImportError:
    from .routes import database as database_routes

from tcg_engine.db import (
    SHARED_SCOPE,
    Database,
    apply_pricing_rules,
    apply_condition_multiplier,
)
from tcg_engine.batches import (
    process_batch_csv,
)
from tcg_engine.sync import sync_active_listings_csv
from tcg_engine.pricing_feed import (
    refresh_market_prices,
    PriceFeedError,
)
from tcg_engine.push import PushError, push_plan, refresh_listing
from tcg_engine.order_sync import OrderSyncError, sync_orders
from tcg_engine.repricer import (
    RepriceError,
    auto_reprice_enabled,
    format_timestamp,
    parse_timestamp,
    plan_reprice,
    run_reprice,
)
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
    from app.ebay_orders import project_order_lines
    from ebay_client.orders import (
        ORDER_HISTORY_DAYS,
        OrderPageError,
        get_orders,
    )

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
    project_order_lines = get_orders = None
    OrderPageError = _EbayUnavailable
    ORDER_HISTORY_DAYS = 90
    EbayConfig = None
    EbayError = SignatureError = _EbayUnavailable
    SIGNATURE_HEADER = "x-ebay-signature"
    challenge_response = payload_topic = verify_signature = None
    TokenStore = object
    FeedError = _EbayUnavailable
    download_active_inventory_report = None
    report_outline = None

try:
    from app.auth import (
        create_jwt_token,
        decode_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )
    from app.deps import (
        DATABASE_URL,
        MAX_UPLOAD_BYTES,
        USER_DATABASE_URL,
        _inventories,
        _owner_scope,
        auth_manager,
        db,
        get_current_user,
        inventory_for,
        inventory_path_for,
        owner_inventory,
        read_upload_limited,
        require_active_user,
        require_admin_user,
        user_db,
    )
except ImportError:
    from .auth import (
        create_jwt_token,
        decode_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )
    from .deps import (
        DATABASE_URL,
        MAX_UPLOAD_BYTES,
        USER_DATABASE_URL,
        _inventories,
        _owner_scope,
        auth_manager,
        db,
        get_current_user,
        inventory_for,
        inventory_path_for,
        owner_inventory,
        read_upload_limited,
        require_active_user,
        require_admin_user,
        user_db,
    )

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
# One eBay client per account, because each account links its own store.
#
# eBay's model is what makes this cheap: the **application** holds one set of
# credentials -- App ID, Cert ID and RuName -- and each seller grants that
# application access to their own account, which yields a refresh token per
# seller. So there is nothing extra to register with eBay and no new keys to
# obtain; the same `EbayConfig.from_env()` serves everybody. What differs per
# account is only the token, and therefore only the store.
_ebay_clients: Dict[int, Any] = {}
_ebay_client_lock = threading.Lock()


def get_ebay_client(user_id: int):
    """
    One account's eBay client, or None when the integration is unconfigured.

    Returns None rather than raising so that an unconfigured deployment keeps
    working. Callers that genuinely need eBay must check and answer 503
    themselves, which reads better than a stack trace about a missing key.

    Takes the account explicitly and has no default, deliberately. A default
    would be the single most expensive mistake available here -- writing to
    somebody else's live eBay store -- so every caller is made to say who it
    is acting as.
    """
    if not EBAY_CLIENT_AVAILABLE or not EbayConfig.is_configured():
        return None
    key = int(user_id)
    with _ebay_client_lock:
        existing = _ebay_clients.get(key)
        if existing is not None:
            return existing
        client = EbayClient(
            EbayConfig.from_env(), store=_UserDbTokenStore(key)
        )
        _ebay_clients[key] = client
        return client


class _UserDbTokenStore(TokenStore):
    """
    Persists one account's eBay refresh token in the users database.

    The library takes a store rather than touching SQLite itself, which is what
    keeps it free of any opinion about where credentials live. ``actor`` is set
    by the OAuth callback just before the code exchange, so the connection can
    record who authorised it; a refresh does not write, so it never clears it.

    The account is fixed when the store is built and never read from ambient
    state, so a token refresh triggered by a background job cannot end up
    written against whoever happens to be signed in.
    """

    def __init__(self, user_id: int):
        self.user_id = int(user_id)
        self.actor = None

    def load(self):
        return user_db.get_ebay_token(self.user_id)

    def save(self, token):
        user_db.save_ebay_token(
            self.user_id, token, connected_by=self.actor
        )


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
# Routes that act on whole databases rather than on cards: backup and
# restore. The first group split out of this module -- their paths are
# unchanged, so nothing about the API moved with them.
app.include_router(database_routes.router)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


# An upload is read into memory before parsing, so without a ceiling a single
# request can exhaust the container's RAM. Generous enough for any real
# SortSwift or eBay export; a 50,000-row dump is a few megabytes.


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


# The longest bin/remark accepted from the dashboard. A shelf label, not a
# field for prose: an unbounded string here would reach the inventory table
# and the packing-slip column and wreck both.
REMARK_MAX_LENGTH = 60


class QuantityUpdateRequest(BaseModel):
    quantity: int


class PickRequest(BaseModel):
    # True when a card has been pulled off the shelf, False when it has been
    # put back. Unticking is deliberately allowed: unlike a deduction, which
    # a machine claims exactly once, this records what a person did in a room
    # and people change their minds.
    picked: bool = True
    # One card, or the whole order when absent. Most orders are a single
    # card, where those are the same action.
    line_item_id: Optional[str] = None


class TargetQuantityRequest(BaseModel):
    # How many copies of this card to aim to hold. None clears the override
    # so the card follows the account default again -- which is not the same
    # as zero, a deliberate "never restock this one".
    target_quantity: Optional[int] = Field(default=None, ge=0, le=999)


class RemarkUpdateRequest(BaseModel):
    # The bin or note a card is stored under. Capped because it is a shelf
    # label, not a field for prose, and an unbounded string here would end up
    # in the inventory table and the packing-slip column.
    remarks: str = Field(default="", max_length=REMARK_MAX_LENGTH)


class ManualCardAddRequest(BaseModel):
    product_name: str
    set_name: str
    condition: str = "Near Mint"
    printing: str = "Normal"
    quantity: int = 0
    ebay_parent_id: Optional[str] = None


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

@app.post("/api/process/batch")
async def process_batch_endpoint(
    file: UploadFile = File(...),
    force: bool = Form(False),
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module A: ingest a SortSwift export, catalogue the cards, stage a draft.

    It no longer produces anything to download. The catalogue is updated
    from the file and a draft plan is staged from the catalogue, which the
    drafts page reviews and the API push applies -- the only route to eBay
    there is.

    An upload is a **delta of newly scanned cards**: its quantities are added
    to what is already held, and a card absent from it means nothing at all.
    So re-processing the same file double-counts stock, which makes the
    fingerprint load-bearing -- a repeat is refused unless the caller passes
    force=true.

    Pass dry_run=true to see what a file would change without writing
    anything to the catalogue, the store mirror or a draft.

    Prices and listing settings come from the signed-in user's own rules, so
    two sellers processing the same export each get their own output.

    """
    inv = inventory_for(user)
    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    # A few thousand card rows is seconds of synchronous SQLite work. Run it in
    # a worker thread: doing it inline blocks uvicorn's event loop, which makes
    # the whole dashboard unresponsive rather than just this request. The
    # session holds one connection open for the run instead of opening and
    # closing several per card.
    def run():
        with inv.session():
            result = process_batch_csv(
                csv_text,
                inv,
                source_name=file.filename or "upload.csv",
                force=force,
                dry_run=dry_run,
                user_id=user["id"],
            )
            # Stage the draft in the same breath as the ingest. Module A used
            # to finish by handing over two files; now it finishes by leaving
            # a reviewable draft of what eBay needs, which is the only route
            # to eBay there is. Doing it here rather than inside
            # process_batch_csv keeps the engine's ingest free of any opinion
            # about plans, and avoids an import cycle.
            #
            # Skipped for a dry run, which must write nothing, and for a
            # refused duplicate, which changed nothing to re-plan against.
            if not dry_run and not result.get("duplicate"):
                try:
                    result["plan"] = build_plan(
                        inv, user["id"], source="batch",
                        source_ref=file.filename or None,
                    )
                except PlanError as exc:
                    # An ingest that succeeded must not report failure because
                    # the draft could not be built; the Rebuild button is
                    # still there.
                    result["plan"] = None
                    result["logs"].append({
                        "level": "WARN",
                        "message": (
                            f"The catalogue was updated, but the draft could "
                            f"not be staged: {exc}. Press Rebuild draft."
                        ),
                    })
            return result

    return await run_in_threadpool(run)


@app.post("/api/process/sync")
async def process_sync_endpoint(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module B: Ingest eBay Active Listings report CSV & sync live store mirror state.
    """
    inv = inventory_for(user)
    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    def run():
        with inv.session():
            return sync_active_listings_csv(csv_text, inv)

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
    inv = inventory_for(user)
    return {
        "rules": inv.get_pricing_rules(user_id=user["id"]),
        "is_own": inv.has_own_pricing_rules(user["id"]),
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
    inv = inventory_for(user)
    rules_data = [r.model_dump() for r in req.rules]
    inv.set_pricing_rules(rules_data, user_id=user["id"])
    return {
        "success": True,
        "rules": inv.get_pricing_rules(user_id=user["id"]),
        "is_own": inv.has_own_pricing_rules(user["id"]),
    }


@app.post("/api/pricing-rules/reset")
def reset_pricing_rules_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Discard the caller's own rules and inherit the shared defaults again."""
    inv = inventory_for(user)
    rules = inv.reset_default_pricing_rules(user_id=user["id"])
    return {
        "success": True,
        "rules": rules,
        "is_own": inv.has_own_pricing_rules(user["id"]),
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
    inv = inventory_for(user)
    def run():
        with inv.session():
            return refresh_market_prices(inv, force=force)

    try:
        result = await run_in_threadpool(run)
    except PriceFeedError as exc:
        record_logs(inv, [{"level": "ERROR", "message": str(exc)}], "prices")
        raise HTTPException(status_code=502, detail=str(exc))
    record_logs(inv, result.get("logs") or [], "prices")
    return result


@app.get("/api/pricing/history/{manifest_id}")
def price_history_endpoint(
    manifest_id: str,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Recent market prices for one card, so a surprising reprice is traceable."""
    inv = inventory_for(user)
    return {"manifest_id": manifest_id,
            "history": inv.get_price_history(manifest_id)}


def record_logs(
    inv: Database, logs: Sequence[Dict[str, str]], source: str
) -> None:
    """
    Persist a pipeline's console lines, never at the cost of the work.

    The console is the only record of what an unattended job did, and until
    this existed it lived in the browser tab that happened to be open. But a
    log that can fail the operation it describes is worse than no log, so
    every error here is swallowed after being printed.
    """
    if not logs:
        return
    try:
        with inv.session():
            inv.record_log_entries(logs, source=source)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[log] could not record the {source} log: {exc}", flush=True)


def _reprice_now(user_id: int, dry_run: bool = False) -> Dict[str, Any]:
    """
    One repricing round, against the listings the Inventory API can reach.

    Shared by the manual endpoint and the nightly job so there is exactly one
    code path: a preview the operator ran by hand and the run that happens at
    three in the morning must reach the same verdicts, or the preview is
    worthless.

    Its lines are printed as they happen -- the container log is still the
    first place to look when something is wrong -- and persisted in one write
    at the end, from a ``finally`` so that a run which raises part-way through
    still leaves the account of how far it got.
    """
    inv = inventory_for(user_id)
    client = get_ebay_client(user_id)
    adapter = None
    if client is not None and client.oauth.is_connected():
        adapter = InventoryApiAdapter(client)
    elif not dry_run:
        raise RepriceError("Connect the eBay account first.")

    collected: List[Dict[str, str]] = []

    def log(level: str, message: str) -> None:
        print(f"[reprice] {level}: {message}", flush=True)
        collected.append({"level": level, "message": message})

    try:
        with inv.session():
            return run_reprice(
                inv, adapter, user_id=user_id, dry_run=dry_run, log=log,
            )
    finally:
        record_logs(inv, collected, "reprice")


@app.post("/api/pricing/auto-reprice")
async def auto_reprice_endpoint(
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Run the repricer now, or preview what it would do.

    The nightly job does this by itself; this exists so the first run can be
    inspected before being trusted, and so a hold that has just expired can be
    applied without waiting for the next cycle.

    Runs in a worker thread: it makes outbound calls to eBay in batches of 25,
    and doing that on the event loop would freeze the dashboard.
    """
    try:
        result = await run_in_threadpool(
            lambda: _reprice_now(user["id"], dry_run=dry_run)
        )
    except RepriceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result


@app.get("/api/pricing/auto-reprice/preview")
def auto_reprice_preview_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    What the repricer would change and what it is holding, without eBay.

    Reads nothing but our own database, so the dashboard can show the pending
    changes and the yellow-flagged holds without a network call or the risk of
    a stray write.
    """
    inv = inventory_for(user)
    planned = plan_reprice(inv, user_id=user["id"])
    return {
        "enabled": auto_reprice_enabled(inv, user_id=user["id"]),
        "considered": planned["considered"],
        "eligible": planned["eligible"],
        "change_count": len(planned["changes"]),
        "hold_count": len(planned["holds"]),
        "over_cap": planned["over_cap"],
        "change_share": planned["change_share"],
        "hold_days": planned["config"]["hold_days"],
        "changes": [
            {
                "manifest_id": d["manifest_id"],
                "label": d["label"],
                "verdict": d["verdict"],
                "market_price": d["market_price"],
                "current_price": d["current_price"],
                "target_price": d["target_price"],
                "reason": d["reason"],
            }
            for d in planned["changes"]
        ],
        "holds": [
            {
                "manifest_id": d["manifest_id"],
                "label": d["label"],
                "market_price": d["market_price"],
                "current_price": d["current_price"],
                "target_price": d["target_price"],
                "days_remaining": d["days_remaining"],
            }
            for d in planned["holds"]
        ],
    }


@app.get("/api/pricing/reprice-log")
def reprice_log_endpoint(
    limit: int = 200,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    What the repricer has actually done.

    The nightly job logs to the terminal, but a container restart takes that
    with it, and on a Synology nobody is watching the console anyway. This is
    the durable record of every verdict, including the holds.
    """
    inv = inventory_for(user)
    return {"entries": inv.get_reprice_history(limit=max(1, min(1000, limit)))}


# The console shows this many lines; the dialog pages back through the rest.
CONSOLE_RECENT_LINES = 200
CONSOLE_PAGE_LINES = 500


@app.get("/api/logs")
def get_logs_endpoint(
    limit: int = CONSOLE_RECENT_LINES,
    before_id: Optional[int] = None,
    level: Optional[str] = None,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    The operational log, newest first.

    The console used to be whatever this browser tab had seen since it was
    opened, which meant everything that runs unattended -- the nightly price
    refresh, the repricer, a push that outlived the page -- was invisible to
    it, and a reload threw away the rest.

    ``before_id`` pages backwards from a line the caller already holds, rather
    than by offset, so the window stays stable while new lines arrive at the
    other end.
    """
    inv = inventory_for(user)
    levels = [part for part in (level or "").split(",") if part.strip()]
    entries = inv.get_log_entries(
        limit=max(1, min(2000, limit)),
        before_id=before_id,
        levels=levels or None,
    )
    return {
        "entries": entries,
        "total": inv.count_log_entries(),
        "oldest_id": entries[-1]["id"] if entries else None,
    }


@app.post("/api/logs/clear")
def clear_logs_endpoint(user: Dict[str, Any] = Depends(require_admin_user)):
    """
    Discard the stored console history.

    Admin-only and separate from the console's own Clear button, which only
    empties the view. This is the record of what the unattended jobs did to
    live listings, so throwing it away is a deliberate act rather than a side
    effect of tidying the screen.
    """
    inv = inventory_for(user)
    removed = inv.clear_log_entries()
    return {"success": True, "removed": removed}


# -- Module C: orders -> stock deductions ---------------------------------
#
# The only thing that takes a sold card off the shelf. Module A used to do it
# as a side effect of a full inventory dump; that inference is gone, so
# without this the catalogue drifts upward and the next draft offers stock
# that has already sold.

ORDER_POLL_ENABLED = os.environ.get(
    "ORDER_POLL_ENABLED", "true"
).strip().lower() not in ("0", "false", "no", "off")
ORDER_POLL_INTERVAL_MINUTES = max(
    1, int(os.environ.get("ORDER_POLL_INTERVAL_MINUTES", "15"))
)

# How far back each poll reaches beyond the last one. Deliberate overlap:
# clock skew between us and eBay is real, an order's modification can land
# after the window that should have contained it, and re-reading an order
# costs nothing because ebay_order_line makes a second deduction impossible.
# A missed sale, by contrast, is invisible -- nothing in the system notices an
# order it never read.
ORDER_POLL_OVERLAP_MINUTES = 30

# Where the watermark lives. A setting rather than a column because it is one
# value for the whole deployment, and it is read and written on the same
# schedule as everything else in there.
ORDER_WATERMARK_SETTING = "orders_last_polled_at"

# Past this, the watermark is old enough that eBay's ninety-day window may
# have swallowed sales we never read. Worth saying loudly rather than
# reporting a quiet poll.
ORDER_WATERMARK_STALE_DAYS = 60


def _ebay_timestamp(moment: datetime) -> str:
    """eBay's filter wants ISO 8601 in UTC with milliseconds and a Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _poll_orders_now(
    inv: Database, user_id: int, dry_run: bool = False
) -> Dict[str, Any]:
    """
    One round of reading orders and deducting what sold.

    Shared by the manual button and the background loop so there is exactly
    one code path: a poll somebody ran by hand and the one that happens every
    fifteen minutes must reach the same decisions.

    The watermark only advances on success. A poll that raises leaves it
    where it was, so the next attempt re-reads the same window rather than
    stepping over sales nobody has seen.
    """
    client = get_ebay_client(user_id)
    if client is None or not client.oauth.is_connected():
        raise OrderSyncError("Connect the eBay account first.")

    collected: List[Dict[str, str]] = []

    def log(level: str, message: str) -> None:
        print(f"[orders] {level}: {message}", flush=True)
        collected.append({"level": level, "message": message})

    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    stored = inv.get_listing_setting(ORDER_WATERMARK_SETTING, "")
    watermark = parse_timestamp(stored)

    # No watermark means this deployment has never polled. eBay will hand
    # over ninety days of history, all of it already accounted for, so the
    # first run adopts rather than deducts.
    first_run = watermark is None
    if first_run:
        since = now - timedelta(days=ORDER_HISTORY_DAYS)
    else:
        since = watermark - timedelta(minutes=ORDER_POLL_OVERLAP_MINUTES)
        stale = (now - watermark).days
        if stale >= ORDER_WATERMARK_STALE_DAYS:
            log("WARN", (
                f"The last successful poll was {stale} days ago, and eBay only "
                f"serves {ORDER_HISTORY_DAYS} days of orders. Any sale older "
                f"than that window cannot be read now and will have to be "
                f"corrected by hand."
            ))

    try:
        raw = get_orders(client.seller, modified_since=_ebay_timestamp(since))
    except OrderPageError as exc:
        # A partial read looks like a quiet week, which is the one thing this
        # must never report. The watermark stays put.
        log("ERROR", f"Refusing a partial read of orders: {exc}")
        record_logs(inv, collected, "orders")
        inv.record_order_poll(outcome="failed", detail=str(exc)[:200])
        raise OrderSyncError(str(exc))

    lines = project_order_lines(raw)

    if dry_run:
        log("INFO", (
            f"Preview only: {len(raw)} order(s) and {len(lines)} line item(s) "
            f"since {_ebay_timestamp(since)}. Nothing was deducted and the "
            f"watermark was not moved."
        ))
        record_logs(inv, collected, "orders")
        inv.record_order_poll(
            orders=len(raw), seen=len(lines),
            outcome="preview",
        )
        return {
            "attempted": False, "reason": "preview", "orders": len(raw),
            "seen": len(lines), "deducted": 0, "logs": collected,
        }

    def run():
        with inv.session():
            return sync_orders(inv, lines, log=log, adopt=first_run)

    try:
        result = run()
    finally:
        record_logs(inv, collected, "orders")

    # Only now, and only on success. Recorded as the moment the poll started
    # rather than finished: an order modified during the call belongs to the
    # next window, not to neither.
    inv.set_listing_settings(
        {ORDER_WATERMARK_SETTING: format_timestamp(now)}, user_id=SHARED_SCOPE
    )

    inv.record_order_poll(
        orders=len(raw),
        seen=result.get("seen", 0),
        deducted=result.get("deducted", 0),
        cards=result.get("deducted_cards", 0),
        foreign_sales=result.get("foreign", 0),
        unmatched=result.get("unmatched", 0),
        outcome="adopted" if first_run else "ok",
    )

    result["attempted"] = True
    result["orders"] = len(raw)
    result["first_run"] = first_run
    result["logs"] = collected
    return result


@app.post("/api/orders/poll")
async def poll_orders_endpoint(
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Read orders now and deduct what sold.

    The background loop does this every fifteen minutes; this exists so the
    first run can be inspected before being trusted, and so a sale can be
    accounted for without waiting.
    """
    inv = inventory_for(user)
    try:
        return await run_in_threadpool(
            lambda: _poll_orders_now(inv, user["id"], dry_run=dry_run)
        )
    except OrderSyncError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/orders/recent")
def recent_orders_endpoint(
    limit: int = 100,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    What the poller has seen, and what it deducted.

    Includes the lines it could not act on -- a SKU matching no card, a
    cancellation after a deduction -- because those are the ones needing a
    person, and a list that showed only successes would hide them.
    """
    inv = inventory_for(user)
    return {
        "last_polled_at": inv.get_listing_setting(ORDER_WATERMARK_SETTING, ""),
        "poll_interval_minutes": ORDER_POLL_INTERVAL_MINUTES,
        "enabled": ORDER_POLL_ENABLED,
        # The polls themselves. A row of quiet ones is how you know the
        # poller is alive, which nothing else on the page can tell you.
        "polls": inv.get_order_polls(limit=8),
        # Only sales that resolved to a catalogued card. The rest are
        # overwhelmingly ordinary business -- sales from listings this
        # application does not manage -- and listing them read like a page
        # of problems, which is exactly what it was not.
        "lines": inv.get_recent_order_lines(
            limit=max(1, min(500, limit)), matched_only=True
        ),
        "unmatched_count": inv.count_order_lines(matched=False),
        # How many cards are waiting to be pulled. Returned here as well as
        # from the pick endpoint so the To Pick badge is right without the
        # tab having been opened -- this is the call the dashboard already
        # makes on a timer, and a queue nobody knows about is not a queue.
        "outstanding_cards": inv.count_outstanding_pick_lines(),
    }


@app.get("/api/orders/pick")
def pick_list_endpoint(
    scope: str = "outstanding",
    limit: int = 50,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    The cards to pull, grouped by order.

    ``scope=outstanding`` is the work queue: orders with at least one live
    line not yet picked. ``scope=all`` includes the packed ones, so a
    finished order can be checked or un-ticked.

    Carries **no buyer information**, because none is stored -- not a name,
    not an address, not a username. The address is eBay's to print. This
    answers the other half: which cards, and out of which box.
    """
    inv = inventory_for(user)
    outstanding = str(scope or "outstanding").lower() != "all"
    return {
        "scope": "outstanding" if outstanding else "all",
        "orders": inv.get_pick_orders(
            outstanding_only=outstanding, limit=max(1, min(200, limit))
        ),
        "outstanding_cards": inv.count_outstanding_pick_lines(),
    }


@app.post("/api/orders/pick/{order_id}")
def set_pick_endpoint(
    order_id: str,
    req: PickRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Tick a card as pulled, or a whole order at once.

    Touches nothing but this record: it does not move stock, and it does not
    tell eBay anything. The poller already took the card off the catalogue
    when the sale was seen -- this is only the note that the physical card is
    now in an envelope rather than on a shelf.
    """
    inv = inventory_for(user)
    if req.line_item_id:
        moved = inv.set_order_line_picked(
            order_id, req.line_item_id, req.picked
        )
        if not moved:
            raise HTTPException(
                status_code=404, detail="That order line is not on record."
            )
        changed = 1
    else:
        changed = inv.set_order_picked(order_id, req.picked)
        if not changed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Order {order_id} has no live lines on record, so there "
                    f"is nothing to mark."
                ),
            )
    return {
        "success": True,
        "order_id": order_id,
        "picked": req.picked,
        "lines_changed": changed,
        "outstanding_cards": inv.count_outstanding_pick_lines(),
    }


async def _scheduled_order_poll() -> None:
    """
    Read orders on a schedule.

    Nothing in here may be fatal: it shares a task with nothing, but an
    exception would end the loop and the symptom would be silence -- which is
    indistinguishable from a shop with no sales. So every path returns.
    """
    inv = owner_inventory()
    try:
        client = get_ebay_client(_owner_scope())
        if client is None or not client.oauth.is_connected():
            print("[orders] eBay is not connected, skipping", flush=True)
            return
        # Acts as the owner throughout: the same account whose store is
        # read and whose stock is deducted.
        result = await run_in_threadpool(
            lambda: _poll_orders_now(inv, _owner_scope())
        )
        print(
            f"[orders] {result.get('seen', 0)} line(s) seen, "
            f"{result.get('deducted', 0)} deducted, "
            f"{result.get('unmatched', 0)} matching no card",
            flush=True,
        )
    except (OrderSyncError, EbayError) as exc:
        print(f"[orders] poll failed, no stock moved: {exc}", flush=True)
        record_logs(inv, 
            [{"level": "WARN", "message": f"Order poll failed: {exc}"}],
            "orders",
        )
    except Exception as exc:
        print(f"[orders] unexpected error, no stock moved: {exc}", flush=True)
        record_logs(inv, 
            [{"level": "ERROR",
              "message": f"Unexpected order poll error: {exc}"}],
            "orders",
        )


@app.on_event("startup")
async def start_order_poll_loop():
    """
    Poll for orders on its own timer, separate from the price loop.

    Separate because the two have nothing to do with each other and very
    different cadences: prices change once a day, a sale can happen at any
    moment and leaves stock on the shelf that is gone until it is read.
    """
    if not ORDER_POLL_ENABLED:
        print("[orders] background poll disabled", flush=True)
        return

    async def loop():
        # A little after the price loop's own delay, so a cold start does not
        # fire two outbound bursts at once.
        await asyncio.sleep(150)
        while True:
            await _scheduled_order_poll()
            await asyncio.sleep(ORDER_POLL_INTERVAL_MINUTES * 60)

    asyncio.create_task(loop())


async def _nightly_reprice() -> None:
    """
    Reprice the API-managed listings, straight after the market refresh.

    Deliberately part of the same loop iteration rather than a second timer.
    Repricing is only as good as the prices it reads, so the ordering is not
    incidental -- a separate schedule would drift and eventually reprice
    against yesterday's market for no reason anybody could see.

    It runs even when the refresh fetched nothing. A refresh is skipped when
    TCGCSV has not published since the last one, but the hold windows are
    measured on the calendar: a fall that has now waited out its window has to
    be applied on a day with no new data just the same.

    Nothing in here is allowed to be fatal. This runs inside the loop that
    also keeps market prices current, and an exception would take that with
    it.
    """
    inv = owner_inventory()
    def skip(reason: str) -> None:
        print(f"[reprice] {reason}, skipping", flush=True)
        record_logs(inv, [{
            "level": "INFO",
            "message": f"Nightly repricing skipped: {reason}.",
        }], "reprice")

    try:
        if not auto_reprice_enabled(inv):
            skip("disabled in Listing Rules")
            return

        owner = user_db.get_owner_user_id()
        if owner is None:
            skip("no admin account to act as")
            return

        client = get_ebay_client(_owner_scope())
        if client is None or not client.oauth.is_connected():
            skip("eBay is not connected")
            return

        result = await run_in_threadpool(lambda: _reprice_now(owner))
        if not result.get("attempted"):
            print(f"[reprice] nothing applied: {result.get('reason')}",
                  flush=True)
        else:
            print(
                f"[reprice] applied {result['applied']}, "
                f"held {result['held']}, failed {result['failed']}, "
                f"skipped {result['skipped']} of {result['considered']}",
                flush=True,
            )
    except RepriceError as exc:
        print(f"[reprice] refused, no price changed: {exc}", flush=True)
    except Exception as exc:
        print(f"[reprice] unexpected error, prices left alone: {exc}",
              flush=True)


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
    inv = owner_inventory()
    if not PRICE_REFRESH_ENABLED:
        print("[prices] background refresh disabled", flush=True)
        return

    async def loop():
        await asyncio.sleep(120)
        while True:
            try:
                def run():
                    with inv.session():
                        return refresh_market_prices(inv)

                result = await run_in_threadpool(run)
                record_logs(inv, result.get("logs") or [], "prices")
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
                record_logs(inv, [{
                    "level": "WARN",
                    "message": f"Price refresh failed, prices unchanged: {exc}",
                }], "prices")
            except Exception as exc:
                print(f"[prices] unexpected refresh error: {exc}", flush=True)
                record_logs(inv, [{
                    "level": "ERROR",
                    "message": f"Unexpected price refresh error: {exc}",
                }], "prices")

            await _nightly_reprice()
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
    inv = inventory_for(user)
    return {
        "multipliers": inv.get_condition_multipliers(user_id=user["id"]),
        "is_own": inv.has_own_condition_multipliers(user["id"]),
    }


@app.post("/api/condition-multipliers")
def update_condition_multipliers_endpoint(
    req: ConditionMultipliersUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Save the signed-in user's own grade discounts."""
    inv = inventory_for(user)
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
    inv.set_condition_multipliers(
        [m.model_dump() for m in req.multipliers], user_id=user["id"]
    )
    return {
        "success": True,
        "multipliers": inv.get_condition_multipliers(user_id=user["id"]),
        "is_own": inv.has_own_condition_multipliers(user["id"]),
    }


@app.post("/api/condition-multipliers/reset")
def reset_condition_multipliers_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Discard the caller's own grade discounts and inherit the shared set."""
    inv = inventory_for(user)
    multipliers = inv.reset_condition_multipliers(user_id=user["id"])
    return {
        "success": True,
        "multipliers": multipliers,
        "is_own": inv.has_own_condition_multipliers(user["id"]),
    }


@app.post("/api/pricing-rules/preview")
def preview_pricing_endpoint(
    req: PricePreviewRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Test and preview what an eBay price would be for a given TCG price."""
    inv = inventory_for(user)
    multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in inv.get_condition_multipliers(user_id=user["id"])
    }
    adjusted, factor = apply_condition_multiplier(
        req.price, req.condition, multipliers
    )
    calculated_price, rule = apply_pricing_rules(
        inv.get_pricing_rules(user_id=user["id"]), adjusted
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
    inv = inventory_for(user)
    return {
        "settings": inv.get_listing_settings(user_id=user["id"]),
        "own_keys": inv.get_own_listing_setting_keys(user["id"]),
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
    inv = inventory_for(user)
    inv.set_listing_settings(req.settings, user_id=user["id"])
    return {
        "success": True,
        "settings": inv.get_listing_settings(user_id=user["id"]),
        "own_keys": inv.get_own_listing_setting_keys(user["id"]),
    }


@app.post("/api/listing-settings/reset")
def reset_listing_settings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Discard the caller's own settings and inherit the shared defaults again."""
    inv = inventory_for(user)
    settings = inv.reset_listing_settings(user_id=user["id"])
    return {
        "success": True,
        "settings": settings,
        "own_keys": inv.get_own_listing_setting_keys(user["id"]),
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
    below_target: bool = False,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Fetch paginated, filtered, and sorted inventory list.

    ``below_target`` narrows it to cards held below the depth they aim for,
    which is the restock list: combined with the set filter it answers "which
    cards in this set do I still need".
    """
    # Read once and passed to both calls, so the rows and the total cannot
    # be computed against different targets.
    inv = inventory_for(user)
    target_default = inv.get_target_quantity_default(user_id=user["id"])
    items = inv.get_inventory(
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
        set_name=set_name,
        below_target=below_target,
        target_default=target_default,
    )
    total = inv.get_inventory_count(
        search=search, set_name=set_name,
        below_target=below_target, target_default=target_default,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "target_default": target_default,
        # Always the full shortfall under the current search and set, not
        # just this page's. The question is how much there is left to buy,
        # which a page cannot answer.
        "restock": inv.get_restock_summary(
            set_name=set_name, search=search, target_default=target_default
        ),
    }


@app.post("/api/inventory/{manifest_id}/target-quantity")
def set_card_target_quantity(
    manifest_id: str,
    req: TargetQuantityRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set how many copies of one card to aim to hold.

    A target, not a ceiling: nothing refuses stock above it and no listing is
    delisted down to it. Holding more than the target simply means nothing is
    needed. Its whole purpose is the restock question.

    Sending no value clears the override, so the card follows the account
    default again.
    """
    inv = inventory_for(user)
    result = inv.set_manifest_target_quantity(manifest_id, req.target_quantity)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found.")
    target_default = inv.get_target_quantity_default(user_id=user["id"])
    effective = (
        result["current"] if result["current"] is not None else target_default
    )
    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "target_quantity": result["current"],
        "effective_target": effective,
        "quantity": result["quantity"],
        "needed": max(0, effective - result["quantity"]),
    }


@app.get("/api/ebay-listings")
def get_ebay_listings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Live eBay listings, rolled up from the store mirror.

    Derived rather than stored: the mirror is keyed by card, so this groups by
    eBay item number to show the store the way eBay presents it.
    """
    # Which path manages each listing, so the page can offer the right
    # action. A listing created through this API is corrected in place; a File
    # Exchange one needs a Revise file uploaded, and the two are not
    # interchangeable -- offering the wrong one hands out a file that silently
    # does nothing, or an API call eBay refuses.
    inv = inventory_for(user)
    managed = {
        row["ebay_parent_id"]
        for row in inv.get_managed_listings()
        if row.get("ebay_parent_id")
    }
    return {
        "listings": [
            {**listing, "managed": listing["ebay_parent_id"] in managed}
            for listing in inv.get_ebay_listings()
        ]
    }


@app.post("/api/ebay-listings/{item_id}/cover")
async def set_listing_cover(
    item_id: str,
    req: CoverImageRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Record a listing's cover photo, and apply it however that listing allows.

    Saving locally is never enough on its own: the listing lives on eBay. How
    the change gets there depends on which path created the listing, and the
    two are not interchangeable -- File Exchange cannot revise a listing the
    Inventory API manages, so handing out a CSV for one of those would be
    handing out something that silently does nothing.

    A listing we created through the API is therefore updated immediately, and
    a legacy one still gets the Revise file to upload.
    """
    inv = inventory_for(user)
    url = (req.cover_image_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="A cover photo URL is required.")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="The cover photo must be a full http:// or https:// URL that eBay can fetch.",
        )

    known = {l["ebay_parent_id"] for l in inv.get_ebay_listings()}
    if item_id not in known:
        raise HTTPException(
            status_code=404,
            detail="No linked listing with that eBay item number.",
        )

    saved = inv.set_listing_cover_image(item_id, url)

    # The choice is recorded first, so that a failure to apply it still leaves
    # it stored and retryable with the listing's Refresh button.
    if inv.get_managed_listing_by_parent(item_id) is None:
        return {
            "success": True,
            "ebay_parent_id": item_id,
            "cover_image_url": saved,
            "applied": False,
            "reason": (
                "Saved, but this listing is not managed through the eBay API, "
                "so there is no way to apply it. Sync from eBay, then press "
                "Refresh on the listing."
            ),
        }

    client = get_ebay_client(user["id"])
    if client is None or not client.oauth.is_connected():
        return {
            "success": True,
            "ebay_parent_id": item_id,
            "cover_image_url": saved,
            "applied": False,
            "reason": (
                "Saved, but eBay is not connected, so it has not been applied "
                "yet. Connect the account and press Refresh on the listing."
            ),
        }

    adapter = InventoryApiAdapter(client)

    def run():
        with inv.session():
            return refresh_listing(inv, adapter, item_id, user_id=user["id"])

    try:
        result = await run_in_threadpool(run)
    except (PushError, EbayError) as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"The cover was saved but eBay refused the update: {exc}. "
                f"Press Refresh on the listing to try again."
            ),
        )
    return {
        "success": True,
        "ebay_parent_id": item_id,
        "cover_image_url": saved,
        "applied": True,
        "refreshed": result.get("refreshed", 0),
    }


@app.get("/api/inventory/sets")
def get_inventory_sets(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Expansion sets present in the catalog, for the dashboard filter.

    Derived from the catalog rather than a fixed list, so the filter can only
    ever offer a set that actually has cards behind it.
    """
    inv = inventory_for(user)
    return {"sets": inv.get_distinct_set_names()}


@app.post("/api/inventory/{manifest_id}/quantity")
def set_card_quantity(
    manifest_id: str,
    req: QuantityUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set a card's catalogued quantity to an absolute value.

    This is the manual correction path, so the figure supplied is the figure
    stored -- unlike batch intake, which accumulates.

    Nothing is written back to SortSwift. Its numbers drift upward from
    reality by design; this catalogue is the authoritative one, and eBay's
    orders API is the only other thing that moves a quantity.
    """
    inv = inventory_for(user)
    if req.quantity < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative.")

    # One existence check, not two: set_manifest_quantity already reports
    # a missing card. The card itself was only read to build the
    # deduction row that no longer exists.
    result = inv.set_manifest_quantity(manifest_id, req.quantity)
    if not result:
        raise HTTPException(status_code=404, detail="Card not found.")

    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "current": result["current"],
    }


@app.post("/api/inventory/{manifest_id}/remark")
def set_card_remark(
    manifest_id: str,
    req: RemarkUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set a card's bin/remark by hand.

    A **local label only**. It cannot be pushed to eBay: the bin reaches eBay
    only encoded in a variation's SKU, which is set when the listing is
    created and cannot be renamed afterwards -- eBay returns Success and
    changes nothing. So this changes where the dashboard says a card is, and
    nothing else.

    Editing it marks the value as owned here, which stops the next SortSwift
    upload from overwriting it. Clearing it hands ownership back to the
    export.
    """
    inv = inventory_for(user)
    result = inv.set_manifest_remarks(manifest_id, req.remarks)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found.")
    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "current": result["current"],
    }


@app.get("/api/stats")
def get_stats_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Get catalog and stock statistics."""
    inv = inventory_for(user)
    return inv.get_stats()


@app.post("/api/inventory/add")
def add_card_manually(
    req: ManualCardAddRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Manually add or update a card in the master catalog."""
    inv = inventory_for(user)
    manifest_id, is_new, card_data = inv.get_or_create_manifest(
        req.product_name, req.set_name, req.condition, req.printing
    )
    if req.ebay_parent_id:
        inv.upsert_variation(manifest_id, req.ebay_parent_id, req.quantity)
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
    inv = inventory_for(user)
    success = inv.delete_manifest(manifest_id)
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
    inv = inventory_for(user)
    items = inv.export_all_manifest()
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
    inv = db
    try:
        inv.get_stats()
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


def _require_ebay_client(user_id: int):
    client = get_ebay_client(user_id)
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
    client = get_ebay_client(user["id"])
    if client is None:
        return {
            "available": EBAY_CLIENT_AVAILABLE,
            "configured": False,
            "connected": False,
        }
    status_payload = dict(client.status())
    status_payload["available"] = True
    meta = user_db.get_ebay_connection_meta(user["id"]) or {}
    status_payload["connected_by"] = meta.get("username")
    status_payload["connected_at"] = meta.get("updated_at")
    return status_payload


@app.post("/api/ebay/connect")
def ebay_connect(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Begin the consent flow: returns the eBay URL to send the seller to.

    Any approved account may link **its own** eBay store. This was admin-only
    while a single token served the whole deployment, when connecting was
    infrastructure rather than a preference; now each account has its own
    connection and can only ever affect its own.

    The ``state`` is a signed, short-lived token rather than a random string
    held in memory. Signing it makes the callback self-contained -- it survives
    a container restart mid-consent -- and verifying it on return is what stops
    an attacker delivering an authorization code of their choosing to the
    callback, which would connect *their* eBay account to this deployment.
    """
    client = _require_ebay_client(user["id"])
    state = create_jwt_token(
        {"purpose": "ebay_oauth", "user_id": user["id"]},
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

    # The account comes from the signed state, never from a session or a
    # query parameter. That is what stops somebody delivering an
    # authorization code that links *their* eBay store to another account.
    connecting_user = claims.get("user_id")
    if not connecting_user:
        return page(
            "That link is no longer valid",
            "Start the connection again from the dashboard.",
            False,
        )

    client = get_ebay_client(connecting_user)
    if client is None:
        return page(
            "eBay is not configured",
            "Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET and EBAY_REDIRECT_URI, "
            "then try again.",
            False,
        )

    client.oauth.store.actor = connecting_user
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
    inv = inventory_for(user)
    client = _require_ebay_client(user["id"])
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
        with inv.session():
            result = sync_active_listings_csv(csv_text, inv)
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


@app.post("/api/ebay-listings/{item_id}/refresh")
async def refresh_listing_endpoint(
    item_id: str, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    Re-send a live listing's pictures, specifics, title and description.

    These live on the inventory items rather than on a plan, so once a listing
    is up there is no route to them through the drafts page: a plan is a diff
    of quantities and prices, and a picture change produces no diff at all.
    The first listing this application created went live carrying the card
    backs, and nothing could reach in to correct it.

    Deliberately cannot move stock or money: quantity is re-sent as what eBay
    is already known to hold, and no price is sent at all.
    """
    inv = inventory_for(user)
    client = get_ebay_client(user["id"])
    if client is None or not client.oauth.is_connected():
        raise HTTPException(
            status_code=409, detail="Connect the eBay account first."
        )

    adapter = InventoryApiAdapter(client)

    def run():
        with inv.session():
            return refresh_listing(inv, adapter, item_id, user_id=user["id"])

    try:
        result = await run_in_threadpool(run)
    except PushError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=f"eBay refused: {exc}")

    for entry in result.get("logs", []):
        print(f"[refresh] {entry['level']}: {entry['message']}", flush=True)
    return {"success": True, **result}


@app.post("/api/ebay/disconnect")
def ebay_disconnect(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Forget the stored refresh token.

    Reconnecting requires the consent screen again, so this is not a toggle to
    flip idly -- but it is the correct response to a credential you no longer
    trust.
    """
    client = get_ebay_client(user["id"])
    if client is not None:
        client.oauth.disconnect()
    else:
        # Still clear the row: the token outlives a configuration change, and
        # leaving it behind would silently reconnect if the keys came back.
        user_db.save_ebay_token(None)
    print(f"[ebay] account disconnected by user {user['id']}", flush=True)
    return {"success": True, "connected": False}


@app.get("/api/ebay/account-setup")
async def ebay_account_setup(user: Dict[str, Any] = Depends(require_active_user)):
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
    inv = inventory_for(user)
    client = get_ebay_client(user["id"])
    if client is None:
        raise HTTPException(
            status_code=503, detail="eBay is not configured."
        )
    if not client.oauth.is_connected():
        raise HTTPException(
            status_code=409,
            detail="Connect the eBay account first: this reads its policies.",
        )

    settings = inv.get_listing_settings(user_id=user["id"])
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
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Create the inventory location eBay requires before publishing an offer.

    A warehouse location needs only a name and a postal code and country --
    no street address -- which is the right shape for shipping from home
    without publishing an address. The key is permanent once set, so it is
    validated before the call rather than after.
    """
    inv = inventory_for(user)
    client = get_ebay_client(user["id"])
    if client is None or not client.oauth.is_connected():
        raise HTTPException(
            status_code=409, detail="Connect the eBay account first."
        )

    settings = inv.get_listing_settings(user_id=user["id"])
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
    inv.set_listing_settings(
        {"merchant_location_key": key}, user_id=user["id"]
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
    client = get_ebay_client(_owner_scope())
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
    inv = inventory_for(user)
    return {"plans": inv.get_plans(user_id=user["id"])}


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
    inv = inventory_for(user)
    try:
        summary = await run_in_threadpool(
            build_plan,
            inv,
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
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")

    try:
        blockers = plan_blockers(inv, plan_id)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "plan": plan,
        "groups": inv.get_plan_groups(plan_id),
        "items": inv.get_plan_items(plan_id),
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
    inv = inventory_for(user)
    item = inv.get_plan_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plan item not found.")
    plan = inv.get_plan(item["plan_id"])
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

    inv.update_plan_item(item_id, **changes)
    # Re-validate straight away so the page never shows a blocker for a
    # problem the user has just fixed.
    problems = revalidate_item(inv, item_id)
    return {
        "success": True,
        "item": inv.get_plan_item(item_id),
        "problems": problems,
        "blockers": plan_blockers(inv, item["plan_id"]),
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
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
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

    inv.set_plan_group_cover(plan_id, req.group_key, url)
    return {
        "success": True,
        "groups": inv.get_plan_groups(plan_id),
    }


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
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    try:
        result = approve_plan(inv, plan_id, approved_by=user["id"])
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": True, **result}


# -------------------------------------------------------------------
# Push jobs
# -------------------------------------------------------------------
#
# A push is long: creating a hundred-card listing is a dozen eBay calls, and a
# whole plan is several listings of that. Held open as one HTTP request it
# outlasts the reverse proxy, which answers the browser with its own error page
# while the push carries on regardless -- and the page, having lost its
# request, also loses any idea that a push is happening. Switching tabs and
# back showed nothing in progress.
#
# So the request starts a job and returns immediately, and the page follows it
# by polling. That makes the two problems the same problem, and the fix is that
# progress lives on the server rather than in one browser request.
#
# Deliberately in memory. A job is a few minutes of transient state, and the
# durable record of what happened is the plan itself -- each card's status and
# reason are written as eBay answers, which is what a restart or a closed
# laptop has to be able to rely on. A vanished job is reported as unknown
# rather than as a failure, because the push it described may well have
# finished.
_push_jobs: Dict[str, Dict[str, Any]] = {}
_push_jobs_lock = threading.Lock()

# Finished jobs are kept long enough for a reload to collect the result.
PUSH_JOB_RETENTION_SECONDS = 3600


def _prune_push_jobs() -> None:
    cutoff = time.time() - PUSH_JOB_RETENTION_SECONDS
    for job_id, job in list(_push_jobs.items()):
        if job["status"] != "running" and job["finished_at"] < cutoff:
            _push_jobs.pop(job_id, None)


def _running_push_job(plan_id: int) -> Optional[str]:
    """The id of a push already running for this plan, if there is one."""
    with _push_jobs_lock:
        for job_id, job in _push_jobs.items():
            if job["plan_id"] == plan_id and job["status"] == "running":
                return job_id
    return None


def _push_job_snapshot(job_id: str, since: int = 0) -> Optional[Dict[str, Any]]:
    """
    A job's state, with only the log lines the caller has not seen.

    ``since`` is an index rather than a timestamp so that a poller cannot miss
    a line or replay one, however irregular its own timing is.
    """
    with _push_jobs_lock:
        job = _push_jobs.get(job_id)
        if job is None:
            return None
        logs = job["logs"][since:]
        return {
            "job_id": job_id,
            "plan_id": job["plan_id"],
            "group_key": job["group_key"],
            "status": job["status"],
            "started_at": job["started_at"],
            "logs": list(logs),
            "log_count": len(job["logs"]),
            "result": job["result"],
            "error": job["error"],
        }


def _run_push_job(job_id: str, plan_id: int, user_id: int, group_keys) -> None:
    """Run one push to completion, recording progress as it goes."""
    inv = inventory_for(user_id)
    def note(level: str, message: str) -> None:
        with _push_jobs_lock:
            job = _push_jobs.get(job_id)
            if job is not None:
                job["logs"].append({"level": level, "message": message})
        print(f"[push] {level}: {message}", flush=True)

    client = get_ebay_client(user_id)
    adapter = InventoryApiAdapter(client)
    try:
        with inv.session():
            result = push_plan(
                inv, adapter, plan_id, user_id=user_id,
                group_keys=group_keys, log=note,
            )
        outcome, error = result, None
    except (PushError, EbayError) as exc:
        outcome, error = None, str(exc)
        note("ERROR", str(exc))
    except Exception as exc:  # noqa: BLE001 - a job must not die silently
        outcome, error = None, f"unexpected failure: {exc}"
        note("ERROR", f"unexpected failure: {exc}")

    with _push_jobs_lock:
        job = _push_jobs.get(job_id)
        if job is not None:
            job["status"] = "failed" if error else "done"
            job["result"] = outcome
            job["error"] = error
            job["finished_at"] = time.time()
        _prune_push_jobs()

    # A push outlives the page that started it by design, and the job registry
    # is memory that a restart clears. This is the copy that is still there
    # tomorrow.
    with _push_jobs_lock:
        job = _push_jobs.get(job_id)
        lines = list(job["logs"]) if job else []
    record_logs(inv, lines, "push")


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
    Start pushing an approved plan to eBay, and return the job that is doing it.

    The only endpoint in this application that changes a live listing. It acts
    on a *stored* approval rather than on a claim made by this request, so the
    record of who authorised the change exists before the change does.

    Returns immediately rather than holding the request open for the minutes a
    large push takes. Held open, it outlasted the reverse proxy, which answered
    the browser with an error page while the push carried on -- and the page,
    having lost its request, lost any sign that a push was running.

    Only listings created through this API are touched. Anything made through
    File Exchange is left for the CSV path and reported as deferred, since
    pushing it would create a duplicate rather than update the live one.
    """
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] == "draft":
        raise HTTPException(
            status_code=409, detail="Approve the draft before pushing it."
        )

    client = get_ebay_client(user["id"])
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

    # One push per plan at a time. Two running together would race on the same
    # items and the same listing, and could publish the same group twice.
    existing = _running_push_job(plan_id)
    if existing is not None:
        return {"success": True, "job_id": existing, "already_running": True}

    group_keys = None
    if req is not None and req.group_key is not None:
        group_keys = [req.group_key]

    job_id = uuid.uuid4().hex
    with _push_jobs_lock:
        _push_jobs[job_id] = {
            "plan_id": plan_id,
            "group_key": group_keys[0] if group_keys else None,
            "status": "running",
            "logs": [],
            "result": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": 0.0,
        }

    asyncio.create_task(run_in_threadpool(
        _run_push_job, job_id, plan_id, user["id"], group_keys
    ))
    return {"success": True, "job_id": job_id, "already_running": False}


@app.get("/api/push-jobs/{job_id}")
def get_push_job(
    job_id: str,
    since: int = 0,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    How a push is going, and the log lines not yet collected.

    404 means the job is unknown -- most often because the server restarted,
    which says nothing about whether the push finished. The plan's own item
    statuses are the durable answer to that, so the page sends the reader
    there rather than declaring a failure it cannot know about.
    """
    inv = inventory_for(user)
    snapshot = _push_job_snapshot(job_id, since=max(0, since))
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "That push is no longer being tracked, which usually means "
                "the server restarted. Check the eBay Listings tab: the "
                "push may well have finished."
            ),
        )
    if snapshot["plan_id"] is not None:
        plan = inv.get_plan(snapshot["plan_id"])
        if plan is not None and plan["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="Job not found.")
    return snapshot


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
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
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
    inv.delete_plan(plan_id)
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
