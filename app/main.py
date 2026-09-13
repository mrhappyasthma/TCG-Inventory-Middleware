import os
import sys
import io
import csv
import mimetypes
# The OAuth callback renders eBay's own error text into a small HTML page.
# That text arrives in a query string, so it is attacker-controlled for anyone
# who can get a person to click a link -- it must be escaped.
import time
from datetime import datetime
from typing import List

# Importing app.deps first is load-bearing, not stylistic: it puts the
# project root on sys.path and loads .env, and `auth` reads GOOGLE_CLIENT_ID
# at module level and refuses to import without it.
try:
    from app.deps import project_root  # noqa: F401
except ImportError:
    from .deps import project_root  # noqa: F401

from fastapi import (
    FastAPI,
    File,
    Request,
    Response,
    status,
)
from fastapi.staticfiles import StaticFiles

# Every group of endpoints lives in its own router. Paths are unchanged
# from when they all lived here, which is what let the existing tests
# verify each move rather than be rewritten for it.
try:
    from app.routes import (
        accounts as accounts_routes,
        database as database_routes,
        ebay as ebay_routes,
        inventory as inventory_routes,
        listings as listings_routes,
        orders as orders_routes,
        plans as plans_routes,
        pricing as pricing_routes,
        settings as settings_routes,
        system as system_routes,
    )
except ImportError:
    from .routes import (
        accounts as accounts_routes,
        database as database_routes,
        ebay as ebay_routes,
        inventory as inventory_routes,
        listings as listings_routes,
        orders as orders_routes,
        plans as plans_routes,
        pricing as pricing_routes,
        settings as settings_routes,
        system as system_routes,
    )




try:
    from app import deps
    from app.auth import (
        create_jwt_token,
        decode_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )
    from app.deps import (
        DATABASE_URL,
        static_dir,
        record_logs,
        EBAY_CLIENT_AVAILABLE,
        EbayClient,
        EbayConfig,
        EbayError,
        FeedError,
        InventoryApiAdapter,
        ORDER_HISTORY_DAYS,
        OrderPageError,
        SIGNATURE_HEADER,
        SignatureError,
        TokenStore,
        _ebay_clients,
        challenge_response,
        create_inventory_location,
        download_active_inventory_report,
        get_ebay_client,
        get_inventory_locations,
        get_orders,
        get_policies,
        payload_topic,
        project_order_lines,
        report_outline,
        suggest_policy_ids,
        verify_signature,
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
    from . import deps
    from .auth import (
        create_jwt_token,
        decode_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )
    from .deps import (
        DATABASE_URL,
        static_dir,
        record_logs,
        EBAY_CLIENT_AVAILABLE,
        EbayClient,
        EbayConfig,
        EbayError,
        FeedError,
        InventoryApiAdapter,
        ORDER_HISTORY_DAYS,
        OrderPageError,
        SIGNATURE_HEADER,
        SignatureError,
        TokenStore,
        _ebay_clients,
        challenge_response,
        create_inventory_location,
        download_active_inventory_report,
        get_ebay_client,
        get_inventory_locations,
        get_orders,
        get_policies,
        payload_topic,
        project_order_lines,
        report_outline,
        suggest_policy_ids,
        verify_signature,
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
# Read independently of the rest of the eBay configuration, because the
# endpoint challenge needs only this and the endpoint URL. That ordering is
# not hypothetical: eBay disables a new keyset until the deletion endpoint
# validates, and the RuName is registered later still -- so requiring a
# complete credential set to answer the challenge would deadlock the very
# bootstrap the challenge exists to unblock.
# One eBay client per account, because each account links its own store.
#
# eBay's model is what makes this cheap: the **application** holds one set of
# credentials -- App ID, Cert ID and RuName -- and each seller grants that
# application access to their own account, which yields a refresh token per
# seller. So there is nothing extra to register with eBay and no new keys to
# obtain; the same `EbayConfig.from_env()` serves everybody. What differs per
# account is only the token, and therefore only the store.
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

if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
# Routes that act on whole databases rather than on cards: backup and
# restore. The first group split out of this module -- their paths are
# unchanged, so nothing about the API moved with them.
app.include_router(database_routes.router)

# The eBay account: consent, connection, seller setup and the
# notification endpoint eBay posts to.
app.include_router(ebay_routes.router)

# Orders: the poller that deducts what sold, and the pick list.
app.include_router(orders_routes.router)


# Registered here rather than on the router: APIRouter.on_event is
# deprecated, and a background loop that silently stops starting is a
# poller that looks exactly like a shop with no sales.
@app.on_event("startup")
async def start_order_poll_loop():
    await orders_routes.order_poll_loop()

# Draft plans and the push that acts on them: the only code here that
# changes a live eBay listing.
app.include_router(plans_routes.router)

# Accounts: Google sign-in, and administering who may sign in.
app.include_router(accounts_routes.router)

# Pricing: the tiered rules, the market feed and the repricer.
app.include_router(pricing_routes.router)


# Registered here for the same reason as the order poller: the loop has
# to start, and APIRouter.on_event is deprecated.
@app.on_event("startup")
async def start_price_refresh_loop():
    await pricing_routes.price_refresh_loop()

# Listing rules: the eBay-facing defaults a push reads.
app.include_router(settings_routes.router)

# The catalogue: what we hold, and the upload that grows it.
app.include_router(inventory_routes.router)

# The eBay listings mirror, and the report that reconciles it.
app.include_router(listings_routes.router)

# The shell: the dashboard page, the health probe and the log.
app.include_router(system_routes.router)

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
# The longest bin/remark accepted from the dashboard. A shelf label, not a
# field for prose: an unbounded string here would reach the inventory table
# and the packing-slip column and wreck both.
# ---------------------------------------------------------
# AUTHENTICATION ENDPOINTS
# ---------------------------------------------------------

# ---------------------------------------------------------
# ADMIN USER MANAGEMENT ENDPOINTS
# ---------------------------------------------------------

# ---------------------------------------------------------
# CORE BUSINESS LOGIC / PROCESSING ENDPOINTS
# ---------------------------------------------------------

# ---------------------------------------------------------
# PRICING RULES API ENDPOINTS
# ---------------------------------------------------------

# How often the background refresh runs, and whether it runs at all. TCGCSV
# publishes once a day and asks for at most one sync per 24 hours, so anything
# under that is wasted requests against a service that asks us not to.
# The console shows this many lines; the dialog pages back through the rest.
# ---------------------------------------------------------
# LISTING & VARIATION SETTINGS API ENDPOINTS
# ---------------------------------------------------------

# ---------------------------------------------------------
# INVENTORY & CATALOG API ENDPOINTS
# ---------------------------------------------------------

# ---------------------------------------------------------
# DATABASE BACKUP / RESTORE
# ---------------------------------------------------------

# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

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


# -------------------------------------------------------------------
# Draft plans: staging for every eBay-bound change
# -------------------------------------------------------------------


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
# Finished jobs are kept long enough for a reload to collect the result.
# Assets whose URLs get a content hash appended, so the browser is forced to
# fetch the version that belongs with the HTML it just received.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
