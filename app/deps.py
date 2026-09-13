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
    # These five had no stub before, and did not need one: the import and
    # every use of them sat inside the same try block, so an unimportable
    # library simply skipped both. Now that the names are imported from
    # here by other modules, a missing one is an ImportError at start-up --
    # which is exactly the whole-dashboard outage this block exists to
    # prevent. Stubbed to None like the rest; the endpoints that use them
    # check EBAY_CLIENT_AVAILABLE first and answer 503.
    InventoryApiAdapter = None
    create_inventory_location = None
    get_inventory_locations = None
    get_policies = None
    suggest_policy_ids = None

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
