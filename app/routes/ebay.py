"""
The eBay account: consent, connection, seller setup and notifications.

One application keyset serves every account -- that is how eBay OAuth is
designed -- so what differs per account is only the refresh token. Which
account a consent flow belongs to comes from the signed OAuth ``state`` and
never from a session or a query parameter, which is what stops somebody
delivering an authorization code that links *their* store to another account.

Two things here are addressed to the **application** rather than to a seller
and so are not per-account: the marketplace account-deletion notification
endpoint, which eBay posts to directly, and eBay's call limits, which are
per-application and therefore shared.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

# The OAuth callback renders eBay's own error text into a small HTML page.
# That text arrives in a query string, so it is attacker-controlled for anyone
# who can get a person to click a link -- it must be escaped.
from html import escape as escape_html

from tcg_engine.sync import sync_active_listings_csv

try:
    from app import deps
    from app.auth import create_jwt_token, decode_jwt_token
    from app.deps import (
        EBAY_CLIENT_AVAILABLE,
        EbayConfig,
        EbayError,
        FeedError,
        SIGNATURE_HEADER,
        SignatureError,
        inventory_for,
        require_active_user,
        user_db,
    )
except ImportError:
    from . import deps
    from .auth import create_jwt_token, decode_jwt_token
    from .deps import (
        EBAY_CLIENT_AVAILABLE,
        EbayConfig,
        EbayError,
        FeedError,
        SIGNATURE_HEADER,
        SignatureError,
        inventory_for,
        require_active_user,
        user_db,
    )

router = APIRouter()

# Read here rather than imported, so a test that sets it after import still
# sees the change -- the endpoint challenge is answered from the environment.
EBAY_NOTIFICATION_ENDPOINT = os.environ.get(
    "EBAY_NOTIFICATION_ENDPOINT", ""
).strip()
EBAY_VERIFICATION_TOKEN = os.environ.get(
    "EBAY_VERIFICATION_TOKEN", ""
).strip()
EBAY_OAUTH_STATE_TTL_SECONDS = 600


def _require_ebay_client(user_id: int):
    client = deps.get_ebay_client(user_id)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "eBay is not configured. Set EBAY_CLIENT_ID, "
                "EBAY_CLIENT_SECRET and EBAY_REDIRECT_URI."
            ),
        )
    return client

@router.get("/api/ebay/status")
def ebay_status(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Whether eBay is configured and connected, for the dashboard.

    Carries no token material. The refresh expiry is included because an eBay
    refresh token dies after roughly eighteen months and the only cure is an
    interactive re-consent, so it is worth showing before it lapses rather
    than after.
    """
    client = deps.get_ebay_client(user["id"])
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

@router.post("/api/ebay/connect")
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

@router.get("/api/ebay/callback")
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

    client = deps.get_ebay_client(connecting_user)
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

@router.post("/api/ebay/sync")
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
        report = deps.download_active_inventory_report(client)
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
            result["report"]["outline"] = deps.report_outline(report["content"])
        return result

    try:
        return await run_in_threadpool(run)
    except FeedError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except EbayError as exc:
        print(f"[ebay] report sync failed: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"eBay refused the request: {exc}")

@router.post("/api/ebay/disconnect")
def ebay_disconnect(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Forget the stored refresh token.

    Reconnecting requires the consent screen again, so this is not a toggle to
    flip idly -- but it is the correct response to a credential you no longer
    trust.
    """
    client = deps.get_ebay_client(user["id"])
    if client is not None:
        client.oauth.disconnect()
    else:
        # Still clear the row: the token outlives a configuration change, and
        # leaving it behind would silently reconnect if the keys came back.
        user_db.save_ebay_token(None)
    print(f"[ebay] account disconnected by user {user['id']}", flush=True)
    return {"success": True, "connected": False}

@router.get("/api/ebay/account-setup")
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
    client = deps.get_ebay_client(user["id"])
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
        policies = deps.get_policies(client.seller, marketplace_id)
        return policies, deps.get_inventory_locations(client.seller)

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
        "suggested": deps.suggest_policy_ids(
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

@router.post("/api/ebay/inventory-location")
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
    client = deps.get_ebay_client(user["id"])
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
        return deps.create_inventory_location(
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

@router.get("/api/ebay/notifications")
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
        "challengeResponse": deps.challenge_response(
            challenge_code,
            EBAY_VERIFICATION_TOKEN,
            EBAY_NOTIFICATION_ENDPOINT,
        )
    }

@router.post("/api/ebay/notifications")
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
    client = deps.get_ebay_client(deps._owner_scope())
    if client is None:
        raise HTTPException(
            status_code=503, detail="eBay notifications are not configured."
        )

    body = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    try:
        await run_in_threadpool(
            deps.verify_signature, body, signature, client.public_keys
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
        f"{deps.payload_topic(payload) or 'unknown'}",
        flush=True,
    )
    return {"status": "acknowledged"}
