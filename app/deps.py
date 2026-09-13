"""
The application's shared runtime: databases, per-account resolution, and the
dependencies every route needs.

This exists so the routes can be split into modules without a circular
import. Every router needs the same handful of things -- which account is
signed in, which database is theirs, how to read an upload safely -- and if
those live in ``main`` then any module ``main`` imports cannot reach them.
They live here instead, and ``main`` re-exports them so that
``app.main.<name>`` keeps working for anything that patches it.

Imported before anything that reads configuration, because it loads ``.env``
itself. That is deliberate rather than tidy: ``auth`` reads
``GOOGLE_CLIENT_ID`` at module level and refuses to import without it, so the
file has to be in ``os.environ`` before it is imported, whichever module
happens to be imported first.
"""

import os
import sys
import threading
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, UploadFile

# Ensure the project root is importable when running as a direct script.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Real environment variables win: a value set by Docker or the shell must not
# be overridden by a stale .env left in the checkout.
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
            if _key and _key not in os.environ:
                os.environ[_key] = _val

from tcg_engine.db import Database  # noqa: E402

try:  # noqa: E402
    from app.user_db import UserDatabase
    from app.auth import AuthManager
except ImportError:
    from .user_db import UserDatabase
    from .auth import AuthManager


DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")
USER_DATABASE_URL = os.environ.get("USER_DATABASE_URL", "data/users.db")

# Every CSV endpoint reads the body into memory, so an unbounded upload is a
# way to exhaust the container's RAM.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

# `db` is the **deployment owner's** inventory. It keeps the historic path,
# because that file holds the live catalogue whose manifest ids are the SKUs
# of live eBay listings -- and a variation's SKU cannot be renamed, so the
# file can never be rebuilt or re-keyed. Every other account gets its own
# file beside it. See `inventory_for`.
db = Database(db_path=DATABASE_URL)
user_db = UserDatabase(db_path=USER_DATABASE_URL)
auth_manager = AuthManager(user_db=user_db)

# One inventory database per account, opened on demand and kept.
#
# Isolation by file rather than by a `user_id` column on sixty-four query
# methods. The two differ in the kind of mistake they permit: a forgotten
# scope filter silently shows one account another's cards -- or pushes them
# to the wrong eBay store -- while a mis-resolved database is loud and
# harmless. There is no legitimate view that spans accounts, so nothing is
# lost by making the join impossible to write.
_inventories: Dict[int, Database] = {}
_inventory_lock = threading.Lock()


def inventory_path_for(user_id: int) -> str:
    """
    Where one account's inventory lives.

    The owner keeps `data/inventory.db`. Anyone else gets a sibling named
    after their user id, in the same directory, so the existing data volume,
    backup routine and NAS bind mount all keep working untouched.
    """
    if int(user_id) == _owner_scope():
        return DATABASE_URL
    root, ext = os.path.splitext(DATABASE_URL)
    return f"{root}-user-{int(user_id)}{ext or '.db'}"


def _owner_scope() -> int:
    """
    The account whose inventory is the original file.

    The oldest active admin, which is the same account background jobs act
    as. Before anyone has signed up there is none, and the owner path is
    reserved rather than handed to whoever arrives first -- the first admin
    to be created inherits it.
    """
    owner = user_db.get_owner_user_id()
    return int(owner) if owner is not None else 0


def inventory_for(user: Any) -> Database:
    """
    The inventory database belonging to one account.

    Accepts a user dict (as the endpoints hold) or a bare id (as background
    jobs resolve). Instances are cached because `Database.__init__` runs the
    schema migration, which should happen once per file and not per request.
    """
    user_id = int(user["id"] if isinstance(user, dict) else user)
    with _inventory_lock:
        existing = _inventories.get(user_id)
        if existing is not None:
            return existing
        path = inventory_path_for(user_id)
        # The owner's database is already open as `db`; reuse that instance
        # rather than opening a second connection pool onto the same file.
        instance = db if path == DATABASE_URL else Database(db_path=path)
        _inventories[user_id] = instance
        return instance


def owner_inventory() -> Database:
    """
    The database an unattended job should act on.

    Falls back to the owner's file when there is no admin yet, which is the
    same thing it has always been: on a deployment with no users there is
    nothing to reprice and nothing to poll, so the fallback only has to be
    harmless.
    """
    owner = user_db.get_owner_user_id()
    return db if owner is None else inventory_for(owner)


# -- request dependencies ---------------------------------------------------

def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    return auth_manager.get_current_user_from_request(request)


def require_active_user(request: Request) -> Dict[str, Any]:
    return auth_manager.require_user(request)


def require_admin_user(request: Request) -> Dict[str, Any]:
    return auth_manager.require_admin(request)


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
