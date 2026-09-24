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
from typing import Any, Dict, List, Optional, Sequence

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
    from ebay_client import pictures as ebay_pictures
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
    ebay_pictures = None
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

# One eBay client per account, because each account links its own store.
#
# eBay's model is what makes this cheap: the **application** holds one set of
# credentials -- App ID, Cert ID and RuName -- and each seller grants that
# application access to their own account, which yields a refresh token per
# seller. So there is nothing extra to register with eBay and no new keys to
# obtain; the same `EbayConfig.from_env()` serves everybody. What differs per
# account is only the token, and therefore only the store.
#
# Each is built lazily and then kept, because its PublicKeyCache must outlive
# a single request -- refetching eBay's verification key per notification is
# what their documentation warns will exhaust the call quota.
_ebay_clients: Dict[int, Any] = {}

_ebay_client_lock = threading.Lock()


def check_picture(url: str) -> Dict[str, Any]:
    """
    Judge one image URL against eBay's picture policy.

    Fetches the image's header, so it is a network call and belongs off the
    event loop in an async endpoint.

    ``ok`` is three-valued -- True, False, or None for "not established" --
    and None must not be read as a pass. A missing eBay library produces None
    for the same reason an unreachable URL does: nothing was checked, and
    saying otherwise is the failure this exists to prevent.
    """
    if ebay_pictures is None:
        return {
            "ok": None, "width": None, "height": None, "longest": None,
            "reason": (
                "The eBay library is not available, so the picture could not "
                "be checked against eBay's 500-pixel minimum."
            ),
        }
    return ebay_pictures.check(url)

def ebay_hosted_cover_problem(url) -> str:
    """
    Why this cover photo cannot be used, or "" if it can.

    eBay refuses a listing whose pictures mix its own hosted copies with
    self-hosted ones. Every card picture this application sends is
    self-hosted, so an eBay-hosted cover fails the *whole listing* at
    publish -- with an error that names neither the cover nor the listing
    usefully. Six listings and 211 cards were rejected that way before
    this check existed.

    The obvious way to acquire one is to copy the image address out of an
    existing listing, which is exactly what a person does when looking for
    a cover photo.
    """
    if ebay_pictures is None or not str(url or "").strip():
        return ""
    if not ebay_pictures.is_ebay_hosted(url):
        return ""
    return (
        "That picture is hosted by eBay. eBay refuses a listing whose "
        "pictures mix its own copies with self-hosted ones, and every card "
        "picture here is self-hosted -- so this cover would fail the whole "
        "listing at publish. Use the image's original address rather than "
        "eBay's copy of it, or upload it somewhere of your own."
    )


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

# Replacement card pictures this deployment serves to eBay itself.
#
# Beside the databases, so they are inside the same bind mount and survive a
# container rebuild. They are *not* inside either .db, so the backup and
# restore in the Database dialog do not cover them -- which matters, because
# a card whose image_override points at a file that is gone sends eBay a
# dead URL, and eBay refuses the whole listing on the next revision rather
# than just that picture.
CARD_IMAGE_DIR = os.environ.get(
    "CARD_IMAGE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE_URL)), "card-images"),
)

# The public origin eBay should fetch those pictures from.
#
# Configuration rather than anything read off a request, for exactly the
# reason EBAY_NOTIFICATION_ENDPOINT is: behind the DSM reverse proxy the URL
# this process sees is not the URL the outside world uses, so a URL built
# from a request would be unreachable from eBay and the failure would arrive
# much later, as a refused listing.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

# Where the dashboard's assets live. Needed by `main` to mount /static
# and by the route that serves index.html, so it has one definition
# here rather than one each.
static_dir = os.path.join(os.path.dirname(__file__), "static")

# An upload is read into memory before parsing, so without a ceiling a single
# request can exhaust the container's RAM. Generous enough for any real
# SortSwift or eBay export; a 50,000-row dump is a few megabytes.
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


def record_logs(
    inv: Database, logs: Sequence[Dict[str, str]], source: str
) -> None:
    """
    Persist a pipeline's console lines, never at the cost of the work.

    The console is the only record of what an unattended job did, and until
    this existed it lived in the browser tab that happened to be open. But a
    log that can fail the operation it describes is worse than no log, so
    every error here is swallowed after being printed.
    """
    if not logs:
        return
    try:
        with inv.session():
            inv.record_log_entries(logs, source=source)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[log] could not record the {source} log: {exc}", flush=True)

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
