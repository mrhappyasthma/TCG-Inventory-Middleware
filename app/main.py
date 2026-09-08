import os
import sys
import io
import csv
import mimetypes
import shutil
import tempfile
import zipfile
from datetime import datetime
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
from pydantic import BaseModel

from tcg_engine.csvtools import decode_csv_bytes
from tcg_engine.db import Database
from tcg_engine.orders import (
    process_orders_csv,
    build_deduction_csv,
    deduction_row,
)
from tcg_engine.batches import (
    process_batch_csv,
    build_cover_photo_revise_csv,
    QUANTITY_MODE_SET,
    QUANTITY_MODES,
)
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
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/x-icon", ".ico")

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
    content_bytes = await file.read()
    csv_text = decode_csv_bytes(content_bytes)
    result = process_orders_csv(csv_text, db)
    return result


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

    content_bytes = await file.read()
    csv_text = decode_csv_bytes(content_bytes)
    result = process_batch_csv(
        csv_text,
        db,
        source_name=file.filename or "upload.csv",
        force=force,
        dry_run=dry_run,
        user_id=user["id"],
        quantity_mode=quantity_mode,
    )
    return result


@app.post("/api/process/sync")
async def process_sync_endpoint(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module B: Ingest eBay Active Listings report CSV & sync live store mirror state.
    """
    content_bytes = await file.read()
    csv_text = decode_csv_bytes(content_bytes)
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


@app.post("/api/pricing-rules/preview")
def preview_pricing_endpoint(
    req: PricePreviewRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Test and preview what an eBay price would be for a given TCG price."""
    calculated_price, rule = db.calculate_price(req.price, user_id=user["id"])
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
        with open(staged, "wb") as out:
            while chunk := await file.read(1024 * 1024):
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
