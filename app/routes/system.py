"""
The application shell: the dashboard page, the health probe and the log.

Three things with nothing in common except that they are about the
deployment rather than about cards.

**The dashboard** is sent with `no-store`. The HTML is the index of which
asset versions belong together, so a cached copy pairs old markup with a new
script -- and the assets it points at carry a content hash in their URL, so
there is nothing to gain by caching the page itself.

**The health probe** reports *degraded* rather than merely "the process is
up", because a container with an unreachable or unwritable data volume
answers every request and serves nothing useful. It is unauthenticated, so
it says nothing an attacker could use.

**The operational log** is the only record of what an unattended job did.
Until it was persisted server-side the only copy was whatever a browser tab
happened to have witnessed, which meant the nightly repricer and the price
refresh ran unobserved. Paging is by id rather than offset, because new
lines keep arriving at the other end while you read.
"""

import hashlib
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from app.deps import (
        db,
        inventory_for,
        require_active_user,
        require_admin_user,
        static_dir,
        user_db,
    )
except ImportError:
    from .deps import (
        db,
        inventory_for,
        require_active_user,
        require_admin_user,
        static_dir,
        user_db,
    )

router = APIRouter()


CONSOLE_RECENT_LINES = 200

CONSOLE_PAGE_LINES = 500

@router.get("/api/logs")
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

@router.post("/api/logs/clear")
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

@router.get("/api/health")
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

@router.get("/")
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
