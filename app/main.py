import os
import sys
import io
import csv
import mimetypes
from typing import Optional, Dict, Any, List

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
    UploadFile,
    File,
    Form,
    Request,
    Response,
    HTTPException,
    status,
    Depends,
)
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tcg_engine.db import Database
from tcg_engine.orders import process_orders_csv
from tcg_engine.batches import process_batch_csv
from tcg_engine.sync import sync_active_listings_csv

try:
    from app.user_db import UserDatabase
    from app.auth import (
        AuthManager,
        create_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )
except ImportError:
    from .user_db import UserDatabase
    from .auth import (
        AuthManager,
        create_jwt_token,
        set_session_cookie,
        verify_google_id_token,
        GOOGLE_CLIENT_ID,
    )

# App Configuration
DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")
USER_DATABASE_URL = os.environ.get("USER_DATABASE_URL", "data/users.db")
PORT = int(os.environ.get("PORT", 8080))

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

static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Pydantic Schemas
class GoogleAuthRequest(BaseModel):
    id_token: str


class StatusUpdateRequest(BaseModel):
    status: str


class RoleUpdateRequest(BaseModel):
    role: str


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
    Module A: Process raw eBay orders CSV & convert to SortSwift Orders Import CSV.
    """
    content_bytes = await file.read()
    csv_text = content_bytes.decode("utf-8", errors="replace")
    result = process_orders_csv(csv_text, db)
    return result


@app.post("/api/process/batch")
async def process_batch_endpoint(
    file: UploadFile = File(...),
    force: bool = Form(False),
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module B: Ingest SortSwift scan batch, auto-catalog cards, route to Add vs Revise CSVs.

    Quantities are additive, so re-processing the same export would inflate live
    eBay stock. Uploads are fingerprinted and a repeat of an already-processed
    file is refused unless the caller explicitly passes force=true.

    Pass dry_run=true to rebuild the CSVs from current settings without writing
    anything to the catalogue or store mirror.
    """
    content_bytes = await file.read()
    csv_text = content_bytes.decode("utf-8", errors="replace")
    result = process_batch_csv(
        csv_text,
        db,
        source_name=file.filename or "upload.csv",
        force=force,
        dry_run=dry_run,
    )
    return result


@app.post("/api/process/sync")
async def process_sync_endpoint(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module C: Ingest eBay Active Listings report CSV & sync live store mirror state.
    """
    content_bytes = await file.read()
    csv_text = content_bytes.decode("utf-8", errors="replace")
    result = sync_active_listings_csv(csv_text, db)
    return result


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


class PricePreviewRequest(BaseModel):
    price: float


@app.get("/api/pricing-rules")
def get_pricing_rules_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Fetch all active pricing rules."""
    return {"rules": db.get_pricing_rules()}


@app.post("/api/pricing-rules")
def update_pricing_rules_endpoint(
    req: PricingRulesUpdateRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Update pricing rules (Admin only)."""
    rules_data = [r.model_dump() for r in req.rules]
    db.set_pricing_rules(rules_data)
    return {"success": True, "rules": db.get_pricing_rules()}


@app.post("/api/pricing-rules/reset")
def reset_pricing_rules_endpoint(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Reset pricing rules to system defaults (Admin only)."""
    rules = db.reset_default_pricing_rules()
    return {"success": True, "rules": rules}


@app.post("/api/pricing-rules/preview")
def preview_pricing_endpoint(
    req: PricePreviewRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Test and preview what an eBay price would be for a given TCG price."""
    calculated_price, rule = db.calculate_price(req.price)
    return {
        "input_price": req.price,
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
    """Fetch active listing and variation grouping settings."""
    return {"settings": db.get_listing_settings()}


@app.post("/api/listing-settings")
def update_listing_settings_endpoint(
    req: ListingSettingsUpdateRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Update listing and variation grouping settings (Admin only)."""
    db.set_listing_settings(req.settings)
    return {"success": True, "settings": db.get_listing_settings()}


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
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Fetch paginated, filtered, and sorted inventory list."""
    items = db.get_inventory(
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    total = db.get_inventory_count(search=search)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
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
    writer.writerows(items)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=master_catalog_export.csv"},
    )


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
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "database": "unreachable", "detail": str(exc)},
        )


# ---------------------------------------------------------
# FRONTEND HTML ROUTE
# ---------------------------------------------------------

@app.get("/")
def serve_dashboard():
    """Serve the single-page dashboard."""
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>TCG Inventory Middleware API is running.</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
