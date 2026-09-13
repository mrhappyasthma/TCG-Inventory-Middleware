"""
Accounts: signing in, and administering who may.

Google Sign-In is the only mechanism. There is no password to store, no
registration form and no auth-disabled mode -- tests stub
``verify_google_id_token`` rather than bypassing it.

Accounts are keyed on Google's ``sub`` claim rather than on the email
address, so somebody changing their email at Google keeps the same local
account and the same approval state. The first account ever created is
granted admin and active automatically, because a deployment with no
administrator has nobody who could approve one; every later account lands in
`pending`.

An admin cannot demote or suspend themselves, and cannot delete their own
account. That is not politeness -- it is what stops a deployment being left
with no one able to approve anybody.
"""

from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

try:
    from app import auth
    from app.auth import (
        GOOGLE_CLIENT_ID,
        create_jwt_token,
        set_session_cookie,
    )
    from app.deps import (
        auth_manager,
        get_current_user,
        require_active_user,
        require_admin_user,
        user_db,
    )
except ImportError:
    from . import auth
    from .auth import (
        GOOGLE_CLIENT_ID,
        create_jwt_token,
        set_session_cookie,
    )
    from .deps import (
        auth_manager,
        get_current_user,
        require_active_user,
        require_admin_user,
        user_db,
    )

router = APIRouter()


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

@router.get("/api/auth/me")
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

@router.post("/api/auth/google")
def login_google(req: GoogleAuthRequest, response: Response):
    """
    Sign in (or sign up) with a Google ID token.

    This is the only authentication endpoint. Accounts are keyed on the Google
    'sub' claim so that a user changing their email address on the Google side
    keeps the same local account and approval state.
    """
    claims = auth.verify_google_id_token(req.id_token)
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

@router.post("/api/auth/logout")
def logout_user(response: Response):
    """Clear session token cookie."""
    response.delete_cookie(key="session_token")
    return {"success": True, "message": "Logged out successfully."}

@router.get("/api/admin/users")
def get_all_users(admin: Dict[str, Any] = Depends(require_admin_user)):
    """List all registered users (Admin only)."""
    return {"users": user_db.list_all_users()}

@router.post("/api/admin/users/{user_id}/status")
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

@router.post("/api/admin/users/{user_id}/role")
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

@router.delete("/api/admin/users/{user_id}")
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
