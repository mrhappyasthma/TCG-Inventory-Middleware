"""
Listing rules: the per-account defaults every eBay-bound write reads.

Not pricing -- these describe the *seller*, which is why they are per-user:
the postal code, the business policy names and ids, the inventory location,
the title and variation templates, the condition-descriptor style. There is
no sensible single value for any of them once more than one person is
listing.

They merge key by key against a shared baseline rather than being copied on
sign-up, so a new setting added by an upgrade reaches an account that has
only ever changed its postal code. The reset endpoint drops the account's own
overrides and lets the baseline show through again rather than writing the
defaults back as overrides -- which would freeze them.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tcg_engine.db import SHARED_SCOPE
from tcg_engine.batches import build_variation_option_name, generate_variation_title

try:
    from app.deps import inventory_for, require_active_user
except ImportError:
    from .deps import inventory_for, require_active_user

router = APIRouter()


class ListingSettingsUpdateRequest(BaseModel):
    settings: Dict[str, str]

class TitlePreviewRequest(BaseModel):
    set_name: str
    condition: str = ""
    template: Optional[str] = "{set_name}: Pick Your Card - {condition} - Complete Your Set"

@router.get("/api/listing-settings")
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

@router.post("/api/listing-settings")
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

@router.post("/api/listing-settings/reset")
def reset_listing_settings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Discard the caller's own settings and inherit the shared defaults again."""
    inv = inventory_for(user)
    settings = inv.reset_listing_settings(user_id=user["id"])
    return {
        "success": True,
        "settings": settings,
        "own_keys": inv.get_own_listing_setting_keys(user["id"]),
    }

@router.post("/api/listing-settings/preview-title")
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
