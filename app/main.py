import os
import mimetypes

# Importing app.deps first is load-bearing, not stylistic: it puts the
# project root on sys.path and loads .env, and `auth` reads GOOGLE_CLIENT_ID
# at module level and refuses to import without it.
try:
    from app.deps import project_root  # noqa: F401
except ImportError:
    from .deps import project_root  # noqa: F401

from fastapi import (
    FastAPI,
    Request,
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




# `main` imports only what `main` uses. The shared runtime is not
# re-exported here: a second name for one object is what let a mock
# applied to this module miss the code actually under test, twice, while
# the suite reported success. Reach it through `app.deps`.
try:
    from app.deps import static_dir
except ImportError:
    from .deps import static_dir


PORT = int(os.environ.get("PORT", 8080))

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
