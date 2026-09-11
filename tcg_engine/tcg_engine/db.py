import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Tuple


# Default seller details used when generating eBay Add files. The policy names
# must match the seller's eBay business policies exactly, including case.
# All of these are editable at runtime under Listing Rules.
DEFAULT_SELLER_POSTAL_CODE = "94305"
# eBay requires the "Game" item specific on card listings. Used only when the
# uploaded export does not supply one.
DEFAULT_GAME = "Pokémon TCG"
# Dropdown label for each card in a variation listing. The card number keeps
# reprints distinguishable and gives the list a natural order.
DEFAULT_VARIATION_OPTION_TEMPLATE = "{name} ({card_number})"
DEFAULT_SHIPPING_PROFILE = "Free Shipping Cards"
DEFAULT_RETURN_PROFILE = "No Returns"
DEFAULT_PAYMENT_PROFILE = "Immediate Payment"

# The automatic repricer's policy, as settings so it is adjustable without a
# redeploy. Stored as strings because listing_settings holds strings; the
# repricer parses them and falls back to these on anything unreadable.
#
# It is on by default, and pushes to eBay by itself. That is a deliberate
# choice and the reason the other three numbers exist: the two thresholds
# below are what make an unattended price change safe enough to leave alone.
DEFAULT_AUTO_REPRICE_ENABLED = "true"
# How far past a pricing-rule boundary the market has to move before the card
# changes tier. The tiers are cliffs -- at the shipped rules a card at $0.249
# prices to $1.99 and one at $0.251 to $2.49 -- so without a margin a one-cent
# move on TCGplayer produces a 25% price change, and a card sitting on a
# boundary is rewritten every single day. 10% of the boundary is enough that
# ordinary daily noise cannot cross it.
DEFAULT_PRICE_BOUNDARY_MARGIN_PERCENT = "10"
# How long a lower computed price must keep being true before it is accepted.
# Raising a price costs nothing if the market recovers; lowering one gives
# away margin that only a sale at the higher price could have earned, so the
# two directions are deliberately not symmetric.
DEFAULT_PRICE_HOLD_DAYS = "14"
# The share of live cards that may change price in one run before the run is
# refused outright. A repricer is downstream of a third-party price feed, and
# the signature of bad feed data is that it moves everything at once. Same
# reasoning as the delisting cap on the sync path.
DEFAULT_REPRICE_MAX_CHANGE_PERCENT = "25"

# Pricing rules and listing settings are per-user, so every row in those two
# tables carries the id of the user who owns it. Scope 0 is the shared baseline
# that a user inherits until they save a change of their own; no real user can
# ever have id 0, because SQLite AUTOINCREMENT starts at 1. A literal 0 is used
# rather than NULL so the tables can carry a real composite primary key --
# NULLs compare as distinct in SQLite, which would let duplicates through.
SHARED_SCOPE = 0

# A card's grade is a discount off the market price, and the market price we
# can obtain is product-level -- TCGplayer's public price data does not break
# down by condition, and neither did the SortSwift export it was relayed
# through. So the adjustment is policy, set here, rather than data fetched from
# anywhere. Keys are the canonical grades; the aliases each export uses are
# folded onto them by normalize_condition_key.
SHIPPED_CONDITION_MULTIPLIERS = [
    ("NM", 1.00, "Near mint or better"),
    ("LP", 0.85, "Lightly played / Excellent"),
    ("MP", 0.70, "Moderately played / Very good"),
    ("HP", 0.50, "Heavily played / Poor"),
    ("D", 0.40, "Damaged"),
]

# Every spelling seen in a SortSwift or eBay export, folded onto one key.
CONDITION_KEY_ALIASES = {
    "nm": "NM", "m": "NM", "mint": "NM", "near mint": "NM",
    "near mint or better": "NM", "nearmint": "NM",
    "lp": "LP", "lightly played": "LP", "excellent": "LP",
    "lightly played (excellent)": "LP",
    "mp": "MP", "moderately played": "MP", "very good": "MP", "vg": "MP",
    "good": "MP",
    "hp": "HP", "heavily played": "HP", "played": "HP", "poor": "HP",
    "d": "D", "dm": "D", "dmg": "D", "damaged": "D",
}


def normalize_condition_key(condition) -> str:
    """
    Fold a condition string onto a canonical multiplier key.

    Returns "" when the condition is unrecognised, which callers must treat as
    "no multiplier known" rather than substituting 1.0 -- silently pricing an
    unknown grade as mint is the expensive direction to be wrong in.
    """
    text = str(condition or "").strip().lower()
    if not text:
        return ""
    return CONDITION_KEY_ALIASES.get(text, "")


def apply_condition_multiplier(
    base_price: float,
    condition,
    multipliers: Dict[str, float],
) -> Tuple[float, Optional[float]]:
    """
    Discount a product-level market price for a card's grade.

    Returns (adjusted_price, multiplier_applied_or_None). The multiplier is
    None when the grade is not recognised or has no configured value, and the
    price comes back untouched -- deliberately not defaulted to 1.0, so the
    caller can say "priced as mint because the grade was unknown" instead of
    quietly doing it.
    """
    price = float(base_price or 0.0)
    key = normalize_condition_key(condition)
    if not key or key not in multipliers:
        return price, None
    factor = float(multipliers[key])
    return round(price * factor, 4), factor


def apply_pricing_rules(
    rules: List[Dict[str, Any]], base_price: float
) -> Tuple[float, Optional[Dict[str, Any]]]:
    """
    Match a base price against an already-loaded rule set.

    Split out from Database.calculate_price so a caller pricing thousands of
    cards can read the rules once instead of once per card. Pure, so the
    pricing behaviour is testable without a database.

    Note this takes the price *after* any condition multiplier: the grade
    discount shifts which tier a card lands in, which is the intended
    behaviour -- a played card should fall into a cheaper tier.

    Returns (calculated_price, matched_rule_or_None).
    """
    price = float(base_price or 0.0)

    for rule in rules:
        min_p = rule["min_price"]
        max_p = rule["max_price"]

        # Check if price falls into this rule's range
        is_match = False
        if max_p is not None:
            if min_p <= price < max_p:
                is_match = True
        else:
            if price >= min_p:
                is_match = True

        if is_match:
            r_type = rule["rule_type"]
            r_val = rule["rule_value"]

            if r_type == "fixed":
                return round(r_val, 2), rule
            elif r_type == "markup_fixed":
                return round(price + r_val, 2), rule
            elif r_type == "markup_percent":
                return round(price * (1.0 + r_val / 100.0), 2), rule

    # Fallback if no rule matched
    if price > 0:
        return round(price, 2), None
    return 1.99, None


class Database:
    """
    SQLite Database manager for TCG inventory.
    Manages the Master Catalog (manifest) and Live Store Mirror (ebay_variations).
    """

    def __init__(self, db_path: str = "data/inventory.db"):
        self.db_path = db_path
        # A connection held open for the duration of a session(), kept
        # per-thread because this object is a process-wide singleton in the web
        # app and a sqlite3 connection may not be used from another thread.
        self._local = threading.local()
        # Ensure parent directory exists
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        self.init_db()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def get_connection(self):
        """
        A connection for one operation, or the session's if one is open.

        Inside a session() the connection is borrowed and deliberately not
        closed, which is what makes bulk work fast: opening and closing a
        connection per operation costs around ten milliseconds -- the cold
        commit plus a close-time WAL checkpoint -- so a few thousand card rows
        spend minutes on connection setup alone.
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            yield existing
            return

        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def session(self):
        """
        Hold one connection open across many operations.

        Commit semantics are unchanged: every method still commits its own
        work, so a failure part-way through leaves exactly the state it would
        have left without a session. This removes only the repeated connect
        and close. Re-entrant, so nesting is harmless.
        """
        if getattr(self._local, "conn", None) is not None:
            yield self
            return

        conn = self._new_connection()
        self._local.conn = conn
        try:
            yield self
        finally:
            self._local.conn = None
            try:
                conn.commit()
            finally:
                conn.close()

    def init_db(self):
        """Initialize SQLite tables and indexes."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS manifest (
                    manifest_id TEXT PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    set_name TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    printing TEXT NOT NULL,
                    sku_id TEXT,
                    tcgplayer_id TEXT,
                    card_number TEXT,
                    set_code TEXT,
                    language TEXT DEFAULT 'EN',
                    price REAL DEFAULT 0.0,
                    market_price REAL DEFAULT 0.0,
                    quantity INTEGER DEFAULT 0,
                    cdn_image TEXT,
                    remarks TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ebay_variations (
                    manifest_id TEXT PRIMARY KEY,
                    ebay_parent_id TEXT NOT NULL,
                    custom_label TEXT,
                    last_known_qty INTEGER DEFAULT 0,
                    last_known_price REAL,
                    pending_qty INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (manifest_id) REFERENCES manifest(manifest_id) ON DELETE CASCADE
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pricing_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    min_price REAL NOT NULL,
                    max_price REAL,
                    rule_type TEXT NOT NULL,
                    rule_value REAL NOT NULL,
                    sort_order INTEGER DEFAULT 0
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tcgcsv_groups (
                    category_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    name TEXT,
                    abbreviation TEXT,
                    PRIMARY KEY (category_id, group_id)
                );
                """,
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manifest_id TEXT NOT NULL,
                    market_price REAL NOT NULL,
                    source TEXT,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
            )
            # Every verdict the automatic repricer reached, including the ones
            # that changed nothing.
            #
            # The repricer runs unattended and writes to live listings, so its
            # own account of what it did is the only way to audit it. The
            # terminal log is the same information, but a container restart
            # takes that with it, which is no basis for answering "why is this
            # card priced at $2.49".
            #
            # Holds are recorded too, not just changes: a card whose price is
            # being kept above the market is precisely the case worth being
            # able to look back at.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS reprice_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manifest_id TEXT NOT NULL,
                    ebay_parent_id TEXT,
                    verdict TEXT NOT NULL,
                    market_price REAL,
                    old_price REAL,
                    new_price REAL,
                    reason TEXT,
                    applied INTEGER NOT NULL DEFAULT 0,
                    decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS condition_multipliers (
                    user_id INTEGER NOT NULL DEFAULT 0,
                    condition_key TEXT NOT NULL,
                    multiplier REAL NOT NULL,
                    label TEXT,
                    PRIMARY KEY (user_id, condition_key)
                );
                """,
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_settings (
                    user_id INTEGER NOT NULL DEFAULT 0,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ebay_listing_overrides (
                    ebay_parent_id TEXT PRIMARY KEY,
                    cover_image_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_batches (
                    sha256 TEXT PRIMARY KEY,
                    source_name TEXT,
                    row_count INTEGER DEFAULT 0,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            # Staging for eBay changes. Every producer of change -- a batch
            # upload, a manual edit, a price refresh, a photo change, a
            # regrouping -- writes a draft plan here instead of a CSV, and
            # nothing reaches eBay until the plan is approved. The gate is
            # therefore a predicate the push worker filters on rather than a
            # property of a remote system we would have to trust.
            #
            # See docs/ebay-api-design.md for why staging is not done on eBay:
            # createOrReplaceInventoryItemGroup updates a *live* listing when
            # its membership changes, so the one operation most in need of
            # staging is the one eBay cannot stage.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_plan (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'draft',
                    source TEXT NOT NULL DEFAULT 'manual',
                    source_ref TEXT,
                    note TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    approved_by INTEGER,
                    pushed_at TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_plan_item (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL,
                    manifest_id TEXT NOT NULL,
                    group_key TEXT,
                    action TEXT NOT NULL,
                    proposed_qty INTEGER,
                    proposed_price REAL,
                    observed_qty INTEGER,
                    observed_price REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    validation TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (plan_id) REFERENCES listing_plan(id) ON DELETE CASCADE,
                    FOREIGN KEY (manifest_id) REFERENCES manifest(manifest_id)
                        ON DELETE CASCADE
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_plan_item_plan
                ON listing_plan_item(plan_id, group_key);
                """
            )
            # Listing-level decisions staged alongside a plan's items. A cover
            # photo belongs to the listing, not to any one card in it, so it
            # cannot live on listing_plan_item.
            #
            # Plan-scoped rather than written straight to
            # ebay_listing_overrides: that table is what the store currently
            # has, and writing there would apply the change before it was
            # approved -- exactly the gate this whole design exists to keep.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_plan_group (
                    plan_id INTEGER NOT NULL,
                    group_key TEXT NOT NULL,
                    cover_image_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (plan_id, group_key),
                    FOREIGN KEY (plan_id) REFERENCES listing_plan(id) ON DELETE CASCADE
                );
                """
            )
            # Only one plan may be open at a time per user. Two concurrent
            # drafts against the same cards would each be computed against
            # state the other is about to change, and approving both would
            # apply the older one's numbers second.
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_one_draft_per_user
                ON listing_plan(user_id) WHERE status = 'draft';
                """
            )
            # Idempotent migration: catalogued quantity was added after the
            # first release.
            manifest_columns = {
                row["name"] for row in cursor.execute("PRAGMA table_info(manifest)")
            }
            if "quantity" not in manifest_columns:
                cursor.execute(
                    "ALTER TABLE manifest ADD COLUMN quantity INTEGER DEFAULT 0"
                )
            # Everything an Add row needs that comes verbatim from the eBay
            # export and cannot be derived from a card's identity: the "C:"
            # item specifics, the ConditionID and Condition Descriptor, and
            # the back/stock image URLs.
            #
            # Stored as one JSON blob rather than a column each, deliberately.
            # It is opaque pass-through data that is never queried or filtered
            # on -- only written whole and read whole -- and eBay's templates
            # gain and lose fields, so a column per field would mean a
            # migration every time the export changes shape.
            #
            # Persisted because an approved plan has to be able to rebuild the
            # Add file. Previously these were read off the uploaded CSV row,
            # used in memory and discarded, which meant a draft's edits could
            # never reach eBay: only the original upload could produce a
            # listing file, and it predated any editing.
            if "ebay_fields_json" not in manifest_columns:
                cursor.execute(
                    "ALTER TABLE manifest ADD COLUMN ebay_fields_json TEXT"
                )

            # Create indexes for fast lookups
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_manifest_lookup
                ON manifest(product_name, set_name, condition, printing);
                """
            )
            # Enforce the natural key so a race between two concurrent batch
            # uploads cannot produce two manifest rows for the same card state.
            # Tolerate failure: a database that already contains duplicates from
            # before this constraint existed must still be able to start.
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_manifest_natural_key
                    ON manifest(
                        LOWER(product_name),
                        LOWER(set_name),
                        LOWER(condition),
                        LOWER(printing)
                    );
                    """
                )
            except sqlite3.IntegrityError:
                # Pre-existing duplicates; leave them for manual reconciliation
                # rather than blocking startup.
                pass
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_manifest_skuid 
                ON manifest(sku_id);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ebay_parent 
                ON ebay_variations(ebay_parent_id);
                """
            )
            
            # The exact CustomLabel eBay knows for each variation. Needed to
            # revise a listing for a card that is absent from a full inventory
            # dump: the label we originally sent embeds the bin/remark, which
            # is not part of a card's identity and so cannot be reconstructed
            # reliably. Module B fills this in from eBay's own report.
            cursor.execute("PRAGMA table_info(ebay_variations)")
            if "custom_label" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE ebay_variations ADD COLUMN custom_label TEXT"
                )

            # The quantity Module A last asked eBay for, which is not the same
            # thing as the quantity eBay reports. Module A must not write
            # last_known_qty: until the Revise file is actually uploaded and an
            # Active Listings sync run, eBay knows nothing about it, and
            # claiming otherwise hides exactly the drift the two columns exist
            # to show. NULL means nothing is outstanding.
            cursor.execute("PRAGMA table_info(ebay_variations)")
            if "pending_qty" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE ebay_variations ADD COLUMN pending_qty INTEGER"
                )

            # The price eBay reports for a variation. Without it there is no way
            # to tell whether a Revise row would change anything, so a full
            # inventory dump emitted a row for every card it had ever listed.
            # NULL means "not known", and nothing is ever suppressed on a guess.
            cursor.execute("PRAGMA table_info(ebay_variations)")
            if "last_known_price" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE ebay_variations ADD COLUMN last_known_price REAL"
                )

            # The offer backing this SKU on eBay, for listings we created
            # through the Inventory API. An offer id is the only handle that
            # can change a price, and it cannot be derived from anything else
            # we hold -- eBay generates it. NULL means either a listing made
            # through File Exchange, which has no offer in this model at all,
            # or a card eBay has never seen.
            cursor.execute("PRAGMA table_info(ebay_variations)")
            if "offer_id" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE ebay_variations ADD COLUMN offer_id TEXT"
                )

            # When the automatic repricer first computed a price *below* what
            # this variation is listed at. It is the start of a hold: a rise
            # applies at once, a fall has to keep being true for the whole
            # hold window before it is accepted, so that a single cheap day
            # on TCGplayer cannot mark a card down.
            #
            # NULL means no fall is pending, which is the normal state. The
            # anchor the window is measured against is deliberately not stored
            # alongside it -- that is ``last_known_price``, the price eBay has
            # confirmed, so lowering a price by hand on eBay is respected
            # immediately instead of being held up by a figure of ours.
            cursor.execute("PRAGMA table_info(ebay_variations)")
            if "hold_since" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE ebay_variations ADD COLUMN hold_since TIMESTAMP"
                )

            # Which of our listings the Inventory API can actually see.
            #
            # This table is the whole reason two write paths can coexist. A
            # listing created through File Exchange is invisible to the
            # Inventory API -- getOffers returns nothing for its SKUs -- so it
            # has no row here and stays on the CSV path. A listing we created
            # ourselves has a row, and can be pushed. Without the distinction
            # a push would silently create a *second* listing for cards that
            # are already on sale.
            #
            # Keyed by our own group key rather than by eBay's item number,
            # because the row has to exist between deciding to create a
            # listing and eBay telling us what its item number is.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ebay_managed_listing (
                    group_key TEXT PRIMARY KEY,
                    inventory_item_group_key TEXT,
                    ebay_parent_id TEXT,
                    managed_by TEXT NOT NULL DEFAULT 'api',
                    last_pushed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Per-user scoping migration for databases created before pricing
            # rules and listing settings became per-user. Both tables gain a
            # user_id defaulting to 0, so every row that already exists becomes
            # part of the shared baseline -- which is what the operator had
            # configured, and therefore the right thing for other users to
            # inherit rather than raw shipped defaults.
            cursor.execute("PRAGMA table_info(pricing_rules)")
            if "user_id" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE pricing_rules "
                    "ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"
                )

            # listing_settings needs its primary key widened from (key) to
            # (user_id, key), and SQLite cannot alter a primary key in place, so
            # the table is rebuilt. Guarded on the column being absent, which
            # makes it run at most once.
            cursor.execute("PRAGMA table_info(listing_settings)")
            if "user_id" not in {row["name"] for row in cursor.fetchall()}:
                cursor.execute(
                    """
                    CREATE TABLE listing_settings_scoped (
                        user_id INTEGER NOT NULL DEFAULT 0,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        PRIMARY KEY (user_id, key)
                    );
                    """
                )
                cursor.execute(
                    "INSERT INTO listing_settings_scoped (user_id, key, value) "
                    "SELECT 0, key, value FROM listing_settings"
                )
                cursor.execute("DROP TABLE listing_settings")
                cursor.execute(
                    "ALTER TABLE listing_settings_scoped RENAME TO listing_settings"
                )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_price_history_manifest
                ON price_history(manifest_id, fetched_at DESC);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reprice_history_decided
                ON reprice_history(decided_at DESC);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pricing_rules_user
                ON pricing_rules(user_id, sort_order);
                """
            )

            cursor.execute(
                "SELECT COUNT(*) AS count FROM condition_multipliers "
                "WHERE user_id = 0"
            )
            if cursor.fetchone()["count"] == 0:
                cursor.executemany(
                    """
                    INSERT INTO condition_multipliers
                        (user_id, condition_key, multiplier, label)
                    VALUES (0, ?, ?, ?)
                    """,
                    SHIPPED_CONDITION_MULTIPLIERS,
                )

            # Seed default pricing rules if empty
            cursor.execute(
                    "SELECT COUNT(*) AS count FROM pricing_rules WHERE user_id = 0"
                )
            if cursor.fetchone()["count"] == 0:
                default_rules = [
                    (0.00, 0.25, "fixed", 1.99, 1),
                    (0.25, 0.50, "fixed", 2.49, 2),
                    (0.50, 1.00, "fixed", 2.99, 3),
                    (1.00, None, "markup_fixed", 3.00, 4),
                ]
                cursor.executemany(
                    """
                    INSERT INTO pricing_rules
                        (user_id, min_price, max_price, rule_type, rule_value, sort_order)
                    VALUES (0, ?, ?, ?, ?, ?)
                    """,
                    default_rules,
                )

            # Seed default listing settings if empty
            cursor.execute(
                    "SELECT COUNT(*) AS count FROM listing_settings WHERE user_id = 0"
                )
            if cursor.fetchone()["count"] == 0:
                default_settings = [
                    ("single_threshold", "5.00"),
                    ("group_by_set", "true"),
                    ("variation_title_template", "{set_name}: Pick Your Card - {condition} - Complete Your Set"),
                    ("category_id", "183454"),
                    ("condition_descriptor_style", "label_id"),
                    ("seller_postal_code", DEFAULT_SELLER_POSTAL_CODE),
                    # Business policy names as configured on this seller's
                    # account. Editable at runtime under Listing Rules.
                    ("shipping_profile_name", DEFAULT_SHIPPING_PROFILE),
                    ("return_profile_name", DEFAULT_RETURN_PROFILE),
                    ("payment_profile_name", DEFAULT_PAYMENT_PROFILE),
                    ("default_game", DEFAULT_GAME),
                    ("variation_option_template", DEFAULT_VARIATION_OPTION_TEMPLATE),
                    # The API push addresses business policies by *id*, not by
                    # the names File Exchange uses, and an offer cannot be
                    # published without an inventory location. Both are
                    # readable from the seller's own account (the connection
                    # already holds the sell.account.readonly scope) but must
                    # be chosen, so they start empty: a push then fails with
                    # eBay's own message rather than silently listing against
                    # a default nobody picked.
                    ("shipping_policy_id", ""),
                    ("return_policy_id", ""),
                    ("payment_policy_id", ""),
                    ("merchant_location_key", ""),
                    ("marketplace_id", "EBAY_US"),
                    ("cover_image_url", ""),
                    # The automatic repricer. See REPRICE_DEFAULTS for what
                    # each of these means and why it has the value it does.
                    ("auto_reprice_enabled", DEFAULT_AUTO_REPRICE_ENABLED),
                    ("price_boundary_margin_percent",
                     DEFAULT_PRICE_BOUNDARY_MARGIN_PERCENT),
                    ("price_hold_days", DEFAULT_PRICE_HOLD_DAYS),
                    ("reprice_max_change_percent",
                     DEFAULT_REPRICE_MAX_CHANGE_PERCENT),
                ]
                cursor.executemany(
                    """
                    INSERT INTO listing_settings (user_id, key, value)
                    VALUES (0, ?, ?)
                    """,
                    default_settings,
                )

            # Backfill settings keys added after the initial seed. A key that
            # already exists but is blank is also filled, so a database created
            # before these defaults existed picks them up. A value the user has
            # actually set is never overwritten.
            for _key, _default in (
                ("condition_descriptor_style", "label_id"),
                ("seller_postal_code", DEFAULT_SELLER_POSTAL_CODE),
                ("shipping_profile_name", DEFAULT_SHIPPING_PROFILE),
                ("return_profile_name", DEFAULT_RETURN_PROFILE),
                ("payment_profile_name", DEFAULT_PAYMENT_PROFILE),
                ("default_game", DEFAULT_GAME),
                ("variation_option_template", DEFAULT_VARIATION_OPTION_TEMPLATE),
                ("auto_reprice_enabled", DEFAULT_AUTO_REPRICE_ENABLED),
                ("price_boundary_margin_percent",
                 DEFAULT_PRICE_BOUNDARY_MARGIN_PERCENT),
                ("price_hold_days", DEFAULT_PRICE_HOLD_DAYS),
                ("reprice_max_change_percent",
                 DEFAULT_REPRICE_MAX_CHANGE_PERCENT),
            ):
                cursor.execute(
                    """
                    INSERT INTO listing_settings (user_id, key, value)
                    VALUES (0, ?, ?)
                    ON CONFLICT(user_id, key) DO UPDATE SET
                        value = excluded.value
                    WHERE listing_settings.value = ''
                      AND excluded.value != ''
                    """,
                    (_key, _default),
                )

            # Migrate the title template off the hardcoded condition. A stored
            # value equal to the old default was never customised, so it is safe
            # to move it onto the {condition} placeholder; anything the user has
            # edited is left untouched.
            cursor.execute(
                """
                UPDATE listing_settings
                SET value = ?
                WHERE key = 'variation_title_template' AND value = ?
                """,
                (
                    "{set_name}: Pick Your Card - {condition} - Complete Your Set",
                    "{set_name}: Pick Your Card - Near Mint - Complete Your Set",
                ),
            )
            conn.commit()

    @staticmethod
    def _normalize(val: Optional[str]) -> str:
        """Strip whitespace and normalize spacing."""
        return " ".join(str(val or "").strip().split())

    def get_next_manifest_id(self) -> str:
        """
        Generate next sequential manifest_id in format ID1001, ID1002, etc.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT manifest_id FROM manifest WHERE manifest_id LIKE 'ID%'")
            rows = cursor.fetchall()
            max_num = 1000
            for row in rows:
                mid = str(row["manifest_id"])
                match = re.match(r"^ID(\d+)$", mid, re.IGNORECASE)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num
            return f"ID{max_num + 1}"

    def find_manifest(
        self, product_name: str, set_name: str, condition: str, printing: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find manifest row by exact card attributes (trimmed and case-insensitive check).
        """
        p_name = self._normalize(product_name)
        s_name = self._normalize(set_name)
        cond = self._normalize(condition)
        print_style = self._normalize(printing)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM manifest
                WHERE LOWER(product_name) = LOWER(?)
                  AND LOWER(set_name) = LOWER(?)
                  AND LOWER(condition) = LOWER(?)
                  AND LOWER(printing) = LOWER(?)
                LIMIT 1;
                """,
                (p_name, s_name, cond, print_style),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def get_manifest_by_id(self, manifest_id: str) -> Optional[Dict[str, Any]]:
        """Lookup manifest by ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM manifest
                WHERE manifest_id = ?
                """,
                (manifest_id.strip(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def insert_manifest(
        self,
        manifest_id: str,
        product_name: str,
        set_name: str,
        condition: str,
        printing: str,
        sku_id: Optional[str] = None,
        tcgplayer_id: Optional[str] = None,
        card_number: Optional[str] = None,
        set_code: Optional[str] = None,
        language: str = "EN",
        price: float = 0.0,
        market_price: float = 0.0,
        cdn_image: Optional[str] = None,
        remarks: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a new card into the master manifest."""
        p_name = self._normalize(product_name)
        s_name = self._normalize(set_name)
        cond = self._normalize(condition)
        print_style = self._normalize(printing)
        m_id = manifest_id.strip()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO manifest (
                    manifest_id, product_name, set_name, condition, printing,
                    sku_id, tcgplayer_id, card_number, set_code, language,
                    price, market_price, cdn_image, remarks
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    m_id,
                    p_name,
                    s_name,
                    cond,
                    print_style,
                    str(sku_id).strip() if sku_id else None,
                    str(tcgplayer_id).strip() if tcgplayer_id else None,
                    str(card_number).strip() if card_number else None,
                    str(set_code).strip() if set_code else None,
                    str(language).strip() if language else "EN",
                    float(price or 0.0),
                    float(market_price or 0.0),
                    str(cdn_image).strip() if cdn_image else None,
                    str(remarks).strip() if remarks and str(remarks).strip().lower() != "no remark" else None,
                ),
            )
            conn.commit()
            return self.get_manifest_by_id(m_id)

    def get_or_create_manifest(
        self,
        product_name: str,
        set_name: str,
        condition: str,
        printing: str,
        sku_id: Optional[str] = None,
        tcgplayer_id: Optional[str] = None,
        card_number: Optional[str] = None,
        set_code: Optional[str] = None,
        language: str = "EN",
        price: float = 0.0,
        market_price: float = 0.0,
        cdn_image: Optional[str] = None,
        remarks: Optional[str] = None,
    ) -> Tuple[str, bool, Dict[str, Any]]:
        """
        Lookup card; if not found, create with next sequential ID.
        Returns: (manifest_id, is_new_record, record_dict)

        The lookup, ID allocation and insert all run inside a single
        BEGIN IMMEDIATE transaction on one connection. Splitting them across
        connections previously allowed two concurrent batch uploads to select the
        same "next" ID and collide on the manifest primary key.
        """
        p_name = self._normalize(product_name)
        s_name = self._normalize(set_name)
        cond = self._normalize(condition)
        print_style = self._normalize(printing)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            # IMMEDIATE takes the write lock up front, so a concurrent writer
            # blocks here instead of racing us to the same manifest_id.
            cursor.execute("BEGIN IMMEDIATE;")
            try:
                cursor.execute(
                    """
                    SELECT *
                    FROM manifest
                    WHERE LOWER(product_name) = LOWER(?)
                      AND LOWER(set_name) = LOWER(?)
                      AND LOWER(condition) = LOWER(?)
                      AND LOWER(printing) = LOWER(?)
                    LIMIT 1;
                    """,
                    (p_name, s_name, cond, print_style),
                )
                row = cursor.fetchone()

                if row:
                    existing = dict(row)
                    # Backfill attributes that were missing when the card was
                    # first catalogued but are present in this batch.
                    updates: List[str] = []
                    params: List[Any] = []
                    if sku_id and not existing.get("sku_id"):
                        updates.append("sku_id = ?")
                        params.append(str(sku_id).strip())
                    if tcgplayer_id and not existing.get("tcgplayer_id"):
                        updates.append("tcgplayer_id = ?")
                        params.append(str(tcgplayer_id).strip())
                    if cdn_image and not existing.get("cdn_image"):
                        updates.append("cdn_image = ?")
                        params.append(str(cdn_image).strip())
                    if price and not existing.get("price"):
                        updates.append("price = ?")
                        params.append(float(price))
                    if market_price and not existing.get("market_price"):
                        updates.append("market_price = ?")
                        params.append(float(market_price))
                    if remarks and str(remarks).strip().lower() != "no remark":
                        updates.append("remarks = ?")
                        params.append(str(remarks).strip())

                    if updates:
                        params.append(existing["manifest_id"])
                        cursor.execute(
                            f"UPDATE manifest SET {', '.join(updates)} WHERE manifest_id = ?",
                            params,
                        )

                    cursor.execute(
                        "SELECT * FROM manifest WHERE manifest_id = ?",
                        (existing["manifest_id"],),
                    )
                    refreshed = dict(cursor.fetchone())
                    conn.commit()
                    return refreshed["manifest_id"], False, refreshed

                # Allocate the next sequential ID inside the same transaction.
                cursor.execute(
                    """
                    SELECT MAX(CAST(SUBSTR(manifest_id, 3) AS INTEGER)) AS max_num
                    FROM manifest
                    WHERE manifest_id GLOB 'ID[0-9]*';
                    """
                )
                max_row = cursor.fetchone()
                max_num = max_row["max_num"] if max_row and max_row["max_num"] else 1000
                next_id = f"ID{int(max_num) + 1}"

                cursor.execute(
                    """
                    INSERT INTO manifest (
                        manifest_id, product_name, set_name, condition, printing,
                        sku_id, tcgplayer_id, card_number, set_code, language,
                        price, market_price, cdn_image, remarks
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_id,
                        p_name,
                        s_name,
                        cond,
                        print_style,
                        str(sku_id).strip() if sku_id else None,
                        str(tcgplayer_id).strip() if tcgplayer_id else None,
                        str(card_number).strip() if card_number else None,
                        str(set_code).strip() if set_code else None,
                        str(language).strip() if language else "EN",
                        float(price or 0.0),
                        float(market_price or 0.0),
                        str(cdn_image).strip() if cdn_image else None,
                        str(remarks).strip()
                        if remarks and str(remarks).strip().lower() != "no remark"
                        else None,
                    ),
                )
                cursor.execute(
                    "SELECT * FROM manifest WHERE manifest_id = ?", (next_id,)
                )
                record = dict(cursor.fetchone())
                conn.commit()
                return next_id, True, record
            except Exception:
                conn.rollback()
                raise

    def increment_manifest_quantity(self, manifest_id: str, delta: int) -> int:
        """
        Add to the quantity we have catalogued for a card and return the total.

        This is our own running count of stock taken in from SortSwift batches.
        It is deliberately separate from ``ebay_variations.last_known_qty``,
        which is whatever eBay last reported: comparing the two is how stock
        drift becomes visible.
        """
        m_id = manifest_id.strip()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE manifest
                SET quantity = COALESCE(quantity, 0) + ?
                WHERE manifest_id = ?
                """,
                (int(delta), m_id),
            )
            conn.commit()
            cursor.execute(
                "SELECT COALESCE(quantity, 0) AS quantity FROM manifest WHERE manifest_id = ?",
                (m_id,),
            )
            row = cursor.fetchone()
            return row["quantity"] if row else 0

    def find_manifest_by_identity(
        self, product_name: str, card_number: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Find catalog cards by name, optionally narrowed by card number.

        Used to recover the catalog-to-eBay link when manifest IDs have
        diverged: the card's identity is the only reliable join left. Returns
        every match so the caller can refuse to act on an ambiguous one rather
        than guessing.
        """
        name = self._normalize(product_name)
        number = str(card_number or "").strip()

        sql = "SELECT * FROM manifest WHERE LOWER(product_name) = LOWER(?)"
        params: List[Any] = [name]
        if number:
            sql += " AND LOWER(COALESCE(card_number, '')) = LOWER(?)"
            params.append(number)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()]

    def rename_manifest(self, old_id: str, new_id: str) -> bool:
        """
        Change a card's manifest ID, carrying any store-mirror row with it.

        The ID is referenced by ebay_variations, so both tables are updated in
        one transaction. Foreign keys are disabled for the duration because the
        constraint is declared ON DELETE CASCADE, which does not help an
        UPDATE and would reject the intermediate state.
        """
        old = str(old_id).strip()
        new = str(new_id).strip()
        if not old or not new or old == new:
            return False

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF;")
            cursor.execute("BEGIN IMMEDIATE;")
            try:
                cursor.execute(
                    "UPDATE manifest SET manifest_id = ? WHERE manifest_id = ?",
                    (new, old),
                )
                changed = cursor.rowcount > 0
                cursor.execute(
                    "UPDATE ebay_variations SET manifest_id = ? WHERE manifest_id = ?",
                    (new, old),
                )
                conn.commit()
                return changed
            except Exception:
                conn.rollback()
                raise

    def get_variation(self, manifest_id: str) -> Optional[Dict[str, Any]]:
        """Get live eBay variation details for a manifest_id."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT manifest_id, ebay_parent_id, custom_label,
                       last_known_qty, last_known_price, pending_qty
                FROM ebay_variations
                WHERE manifest_id = ?
                """,
                (manifest_id.strip(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def upsert_variation(
        self,
        manifest_id: str,
        ebay_parent_id: str,
        last_known_qty: int,
        custom_label: Optional[str] = None,
        last_known_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Insert or update an ebay_variations row.

        This records what **eBay reports**, so it is Module B's to call. It
        clears ``pending_qty``: a report from eBay supersedes whatever Module A
        last asked for, whether or not the request was applied.

        ``custom_label`` is the SKU eBay knows this variation by. Passing None
        leaves any stored label alone rather than erasing it, because callers
        that only know the quantity must not destroy a label learned from
        eBay's own Active Listings report.
        """
        m_id = manifest_id.strip()
        p_id = str(ebay_parent_id).strip()
        qty = int(last_known_qty)
        label = str(custom_label).strip() if custom_label else None
        price = None if last_known_price is None else round(float(last_known_price), 2)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ebay_variations
                    (manifest_id, ebay_parent_id, custom_label, last_known_qty,
                     last_known_price, pending_qty, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(manifest_id) DO UPDATE SET
                    ebay_parent_id = excluded.ebay_parent_id,
                    custom_label = COALESCE(excluded.custom_label, ebay_variations.custom_label),
                    last_known_qty = excluded.last_known_qty,
                    last_known_price = COALESCE(excluded.last_known_price,
                                                ebay_variations.last_known_price),
                    pending_qty = NULL,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (m_id, p_id, label, qty, price),
            )
            conn.commit()
            return {
                "manifest_id": m_id,
                "ebay_parent_id": p_id,
                "custom_label": label,
                "last_known_qty": qty,
                "last_known_price": price,
            }

    def set_pending_quantity(self, manifest_id: str, quantity: int) -> None:
        """
        Record the quantity Module A last asked eBay for.

        Deliberately separate from ``last_known_qty``: that column means "what
        eBay reports", and only an Active Listings sync may set it. Writing the
        intended quantity there would make the dashboard claim eBay had been
        updated the moment a CSV was generated, before it had been uploaded.

        Only touches rows that already exist, because a card that is not linked
        to a listing has nothing pending against it.
        """
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE ebay_variations SET pending_qty = ? WHERE manifest_id = ?",
                (int(quantity), manifest_id.strip()),
            )
            conn.commit()

    def get_live_variations(self) -> List[Dict[str, Any]]:
        """
        Every card the store mirror believes is live on eBay.

        Used to find cards that are absent from a full inventory dump, which
        must be revised down to zero so a sold-out card is not left on sale.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT v.manifest_id, v.ebay_parent_id, v.custom_label,
                       v.last_known_qty, v.last_known_price, v.pending_qty,
                       v.offer_id,
                       m.product_name, m.set_name, m.condition, m.remarks
                FROM ebay_variations v
                JOIN manifest m ON m.manifest_id = v.manifest_id
                WHERE v.ebay_parent_id IS NOT NULL
                  AND TRIM(v.ebay_parent_id) != ''
                ORDER BY v.manifest_id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_manifest(self, manifest_id: str) -> bool:
        """Delete a card from manifest and cascade delete variation."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM manifest WHERE manifest_id = ?", (manifest_id.strip(),))
            conn.commit()
            return cursor.rowcount > 0

    # Columns the inventory table is allowed to sort on, mapped to qualified SQL
    # names. Anything not listed falls back to manifest_id.
    _SORTABLE_COLUMNS = {
        "manifest_id": "m.manifest_id",
        "product_name": "m.product_name",
        # Mirror the engine's variation ordering: plainly numbered cards first
        # in numeric order, then prefixed ones like TG12/TG30, then blanks. A
        # bare CAST would sort every prefixed number to 0 and hoist it to the top.
        "card_number": (
            "CASE WHEN COALESCE(m.card_number, '') = '' THEN 2 "
            "WHEN m.card_number GLOB '[0-9]*' THEN 0 ELSE 1 END",
            "CAST(m.card_number AS INTEGER)",
            "m.card_number",
        ),
        "set_name": "m.set_name",
        "condition": "m.condition",
        "printing": "m.printing",
        "quantity": "m.quantity",
        "remarks": "m.remarks",
        "sku_id": "m.sku_id",
        "ebay_parent_id": "v.ebay_parent_id",
        "last_known_qty": "v.last_known_qty",
    }

    # Columns a free-text inventory search matches against.
    _SEARCHABLE_COLUMNS = (
        "m.manifest_id",
        "m.product_name",
        "m.card_number",
        "m.set_name",
        "m.condition",
        "m.printing",
        "m.remarks",
        "m.sku_id",
        "v.ebay_parent_id",
    )

    @staticmethod
    def _build_set_filter(set_name: Optional[str]) -> Tuple[str, List[Any]]:
        """
        Build an exact-match filter on the expansion set.

        Kept separate from the free-text search so the two compose: a set can be
        selected and a search term typed at the same time.
        """
        if not set_name or not str(set_name).strip():
            return "", []
        return " LOWER(m.set_name) = LOWER(?) ", [str(set_name).strip()]

    @classmethod
    def _build_where(
        cls, search: Optional[str], set_name: Optional[str] = None
    ) -> Tuple[str, List[Any]]:
        """Combine the search and set filters into a single WHERE clause."""
        clauses: List[str] = []
        params: List[Any] = []

        search_sql, search_params = cls._build_search_clause(search)
        if search_sql:
            # _build_search_clause emits a full "WHERE (...)"; take the predicate.
            predicate = search_sql.strip()
            if predicate.upper().startswith("WHERE"):
                predicate = predicate[5:].strip()
            clauses.append(predicate)
            params.extend(search_params)

        set_sql, set_params = cls._build_set_filter(set_name)
        if set_sql:
            clauses.append("(" + set_sql.strip() + ")")
            params.extend(set_params)

        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses) + " ", params

    # Tables an inventory database must contain to be recognisable. Anything
    # added by a later migration is deliberately excluded, so a backup taken
    # from an older build still restores.
    REQUIRED_TABLES = ("manifest", "ebay_variations", "pricing_rules", "listing_settings")

    SQLITE_MAGIC = b"SQLite format 3" + bytes([0])

    def export_snapshot(self, dest_path: str) -> str:
        """
        Write a consistent single-file copy of this database.

        VACUUM INTO fully checkpoints the write-ahead log, so the result needs
        no -wal/-shm sidecars and is safe to take while the app is serving.
        Copying the .db file directly can capture a stale database whose recent
        commits are still only in the WAL.
        """
        dest = os.path.abspath(dest_path)
        parent = os.path.dirname(dest)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(dest):
            os.remove(dest)
        with self.get_connection() as conn:
            conn.execute("VACUUM INTO ?", (dest,))
        return dest

    @classmethod
    def inspect_snapshot(cls, path: str) -> Dict[str, Any]:
        """
        Check that a file really is one of our inventory databases, and
        summarise it, without touching the live database.

        Returns {"ok": bool, "error": str|None, "counts": {...}}. Used to show
        an operator what a restore would bring in before it is applied, and to
        refuse an unrelated or damaged file outright.
        """
        result: Dict[str, Any] = {"ok": False, "error": None, "counts": {}}

        try:
            with open(path, "rb") as f:
                header = f.read(16)
        except OSError as exc:
            result["error"] = f"Could not read the file: {exc}"
            return result

        if header != cls.SQLITE_MAGIC:
            result["error"] = "That is not a SQLite database file."
            return result

        conn = None
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path.replace(os.sep, "/"), uri=True)
            conn.row_factory = sqlite3.Row

            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                result["error"] = f"The database failed its integrity check: {integrity}"
                return result

            present = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = [t for t in cls.REQUIRED_TABLES if t not in present]
            if missing:
                result["error"] = (
                    "This does not look like an inventory database; it is missing: "
                    + ", ".join(missing)
                )
                return result

            for table in cls.REQUIRED_TABLES + ("processed_batches", "ebay_listing_overrides"):
                if table in present:
                    result["counts"][table] = conn.execute(
                        "SELECT COUNT(*) FROM %s" % table
                    ).fetchone()[0]

            result["ok"] = True
            return result
        except sqlite3.DatabaseError as exc:
            result["error"] = f"The file could not be opened as a database: {exc}"
            return result
        finally:
            if conn is not None:
                conn.close()

    def replace_with_snapshot(self, source_path: str) -> Dict[str, Any]:
        """
        Replace this database with a validated snapshot, backing up first.

        The current database is copied aside before anything is overwritten, so
        a restore is reversible. Stale -wal/-shm sidecars belonging to the old
        database are removed: left in place they would be applied to the new
        file and corrupt it.
        """
        check = self.inspect_snapshot(source_path)
        if not check["ok"]:
            raise ValueError(check["error"] or "The snapshot is not usable.")

        target = os.path.abspath(self.db_path)
        directory = os.path.dirname(target) or "."
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(directory, f"inventory-backup-{stamp}.db")

        backed_up = None
        if os.path.exists(target):
            self.export_snapshot(backup)
            backed_up = backup

        staged = os.path.join(directory, f".import-{stamp}.db")
        shutil.copyfile(source_path, staged)

        for sidecar in (target + "-wal", target + "-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)

        # Same directory, so this is atomic: the database is never half-written.
        os.replace(staged, target)

        # Bring the restored file up to the current schema.
        self.init_db()

        return {
            "backup_path": backed_up,
            "counts": check["counts"],
        }

    def get_ebay_listings(self) -> List[Dict[str, Any]]:
        """
        One row per live eBay listing, aggregated from the store mirror.

        The mirror is keyed by card, so a multi-variation listing appears as
        many rows sharing an ebay_parent_id. Grouping by that id gives the
        store as eBay sees it: how many cards each listing carries, what eBay
        reports it holding, and what the catalog thinks it holds. A gap between
        the last two is the same drift the inventory table highlights, rolled
        up to the listing.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    v.ebay_parent_id                        AS ebay_parent_id,
                    COUNT(*)                               AS card_count,
                    COALESCE(SUM(v.last_known_qty), 0)     AS live_quantity,
                    COALESCE(SUM(m.quantity), 0)           AS catalog_quantity,
                    COUNT(DISTINCT m.set_name)             AS set_count,
                    MIN(m.set_name)                        AS set_name,
                    COUNT(DISTINCT m.condition)            AS condition_count,
                    MIN(m.condition)                       AS condition,
                    MAX(v.updated_at)                      AS last_synced,
                    COALESCE(o.cover_image_url, '')        AS cover_image_url
                FROM ebay_variations v
                JOIN manifest m ON m.manifest_id = v.manifest_id
                LEFT JOIN ebay_listing_overrides o
                       ON o.ebay_parent_id = v.ebay_parent_id
                WHERE COALESCE(v.ebay_parent_id, '') != ''
                GROUP BY v.ebay_parent_id, o.cover_image_url
                ORDER BY MAX(v.updated_at) DESC, v.ebay_parent_id ASC
                """
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_listing_cover_image(self, ebay_parent_id: str) -> str:
        """Return the cover image recorded for a listing, or an empty string."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(cover_image_url, '') AS url "
                "FROM ebay_listing_overrides WHERE ebay_parent_id = ?",
                (str(ebay_parent_id).strip(),),
            )
            row = cursor.fetchone()
            return row["url"] if row else ""

    def set_listing_cover_image(self, ebay_parent_id: str, cover_image_url: str) -> str:
        """
        Record the cover image for one eBay listing.

        Stored per listing rather than in listing_settings, because that setting
        is the default for listings not yet created, whereas this is an override
        for a specific live listing.
        """
        item_id = str(ebay_parent_id).strip()
        url = str(cover_image_url or "").strip()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ebay_listing_overrides (ebay_parent_id, cover_image_url, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(ebay_parent_id) DO UPDATE SET
                    cover_image_url = excluded.cover_image_url,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (item_id, url),
            )
            conn.commit()
        return url

    def get_distinct_set_names(self) -> List[Dict[str, Any]]:
        """
        Every expansion set present in the catalog, with how many cards each
        holds, so the dashboard filter only ever offers sets that exist.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.set_name AS set_name, COUNT(*) AS card_count
                FROM manifest m
                WHERE COALESCE(m.set_name, '') != ''
                GROUP BY m.set_name
                ORDER BY m.set_name COLLATE NOCASE ASC
                """
            )
            return [dict(r) for r in cursor.fetchall()]

    def set_manifest_quantity(
        self, manifest_id: str, quantity: int
    ) -> Optional[Dict[str, Any]]:
        """
        Set a card's catalogued quantity to an absolute value.

        Distinct from increment_manifest_quantity, which accumulates batch
        intake. This is the manual correction path, so the figure given is the
        figure stored. Returns the before and after values, or None if the card
        does not exist.
        """
        m_id = str(manifest_id).strip()
        new_qty = max(0, int(quantity))

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(quantity, 0) AS quantity FROM manifest WHERE manifest_id = ?",
                (m_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            previous = row["quantity"]

            cursor.execute(
                "UPDATE manifest SET quantity = ? WHERE manifest_id = ?",
                (new_qty, m_id),
            )
            conn.commit()

        return {"manifest_id": m_id, "previous": previous, "current": new_qty}

    @classmethod
    def _build_search_clause(
        cls, search: Optional[str]
    ) -> Tuple[str, List[Any]]:
        """
        Build the shared WHERE clause for inventory listing and counting.

        Both queries must match on exactly the same columns; when they drifted
        apart, searching by bin location returned rows while the paging total
        under-reported them.
        """
        if not search or not search.strip():
            return "", []

        needle = f"%{search.strip()}%"
        conditions = " OR\n                    ".join(
            f"{col} LIKE ?" for col in cls._SEARCHABLE_COLUMNS
        )
        clause = f"""
                WHERE (
                    {conditions}
                )
        """
        return clause, [needle] * len(cls._SEARCHABLE_COLUMNS)

    def get_inventory(
        self,
        search: Optional[str] = None,
        sort_by: str = "manifest_id",
        sort_dir: str = "ASC",
        limit: int = 50,
        offset: int = 0,
        set_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Combined live inventory query (manifest LEFT JOIN ebay_variations).
        """
        order_spec = self._SORTABLE_COLUMNS.get(sort_by, "m.manifest_id")
        if isinstance(order_spec, str):
            order_spec = (order_spec,)
        direction = "DESC" if str(sort_dir).upper() == "DESC" else "ASC"
        order_by = ", ".join(f"{expr} {direction}" for expr in order_spec)

        query = """
            SELECT
                m.manifest_id,
                m.product_name,
                COALESCE(m.card_number, '') AS card_number,
                COALESCE(m.tcgplayer_id, '') AS tcgplayer_id,
                m.set_name,
                m.condition,
                m.printing,
                COALESCE(m.quantity, 0) AS quantity,
                COALESCE(m.remarks, '') AS remarks,
                COALESCE(m.sku_id, '') AS sku_id,
                -- The card face, for the dashboard's hover preview. Comes
                -- from the SortSwift export and is often the only picture of
                -- the actual card we hold.
                COALESCE(m.cdn_image, '') AS cdn_image,
                COALESCE(v.ebay_parent_id, '') AS ebay_parent_id,
                COALESCE(v.last_known_qty, 0) AS last_known_qty,
                v.pending_qty AS pending_qty
            FROM manifest m
            LEFT JOIN ebay_variations v ON m.manifest_id = v.manifest_id
        """
        where_clause, params = self._build_where(search, set_name)
        query += where_clause
        query += f" ORDER BY {order_by} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_inventory_count(
        self, search: Optional[str] = None, set_name: Optional[str] = None
    ) -> int:
        """Count total matching rows in inventory."""
        query = """
            SELECT COUNT(*) AS total
            FROM manifest m
            LEFT JOIN ebay_variations v ON m.manifest_id = v.manifest_id
        """
        where_clause, params = self._build_where(search, set_name)
        query += where_clause

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            row = cursor.fetchone()
            return row["total"] if row else 0

    def get_stats(self) -> Dict[str, Any]:
        """
        Aggregate catalog and inventory statistics.

        Two different units are reported and must not be confused, which is
        why they are named rather than both called a "count":

        * **cards** -- distinct catalogue rows, i.e. kinds of card. A card is
          identified by name + set + condition + printing, so the same card in
          two conditions is two.
        * **copies** -- how many physical cards that adds up to.

        ``total_cards`` / ``active_listings`` count cards; ``total_on_hand`` /
        ``total_stock`` sum copies.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) AS total_cards,
                       COALESCE(SUM(quantity), 0) AS total_on_hand
                FROM manifest
                """
            )
            row = cursor.fetchone()
            total_cards = row["total_cards"]
            total_on_hand = row["total_on_hand"]

            # Both eBay figures use the same filter, so the count of cards and
            # the sum of their copies always describe the same set of rows.
            cursor.execute(
                """
                SELECT COUNT(*) AS active_listings,
                       COALESCE(SUM(last_known_qty), 0) AS total_stock
                FROM ebay_variations
                WHERE ebay_parent_id IS NOT NULL
                  AND TRIM(ebay_parent_id) != ''
                """
            )
            row = cursor.fetchone()

            return {
                # cards (kinds)
                "total_cards": total_cards,
                "active_listings": row["active_listings"],
                # copies (units)
                "total_on_hand": total_on_hand,
                "total_stock": row["total_stock"],
            }

    SHIPPED_PRICING_RULES = [
        {"min_price": 0.00, "max_price": 0.25, "rule_type": "fixed", "rule_value": 1.99, "sort_order": 1},
        {"min_price": 0.25, "max_price": 0.50, "rule_type": "fixed", "rule_value": 2.49, "sort_order": 2},
        {"min_price": 0.50, "max_price": 1.00, "rule_type": "fixed", "rule_value": 2.99, "sort_order": 3},
        {"min_price": 1.00, "max_price": None, "rule_type": "markup_fixed", "rule_value": 3.00, "sort_order": 4},
    ]

    def has_own_pricing_rules(self, user_id: int = SHARED_SCOPE) -> bool:
        """Whether this user has saved a rule set of their own."""
        if int(user_id) == SHARED_SCOPE:
            return True
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM pricing_rules WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            )
            return cursor.fetchone() is not None

    def get_pricing_rules(self, user_id: int = SHARED_SCOPE) -> List[Dict[str, Any]]:
        """
        The rules that apply to this user, ordered by sort_order / min_price.

        A rule set is all-or-nothing: a user who has saved any rules gets
        exactly those, and one who has not inherits the shared baseline.
        Merging the two would be meaningless, because the rules partition a
        price range and a half-inherited set could leave gaps or overlaps.
        """
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if scope != SHARED_SCOPE:
                cursor.execute(
                    "SELECT 1 FROM pricing_rules WHERE user_id = ? LIMIT 1", (scope,)
                )
                if cursor.fetchone() is None:
                    scope = SHARED_SCOPE
            cursor.execute(
                """
                SELECT id, user_id, min_price, max_price, rule_type, rule_value, sort_order
                FROM pricing_rules
                WHERE user_id = ?
                ORDER BY sort_order ASC, min_price ASC
                """,
                (scope,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def set_pricing_rules(
        self, rules: List[Dict[str, Any]], user_id: int = SHARED_SCOPE
    ):
        """
        Replace this user's rule set, leaving every other scope untouched.

        The delete is scoped, so a user saving their first rule set creates
        their own copy rather than overwriting the shared baseline that
        everyone else is still inheriting.
        """
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pricing_rules WHERE user_id = ?", (scope,))
            for idx, r in enumerate(rules, start=1):
                cursor.execute(
                    """
                    INSERT INTO pricing_rules
                        (user_id, min_price, max_price, rule_type, rule_value, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope,
                        float(r.get("min_price", 0.0)),
                        float(r["max_price"]) if r.get("max_price") is not None and str(r.get("max_price")).strip() != "" else None,
                        str(r.get("rule_type", "fixed")),
                        float(r.get("rule_value", 1.99)),
                        int(r.get("sort_order", idx)),
                    ),
                )
            conn.commit()

    def reset_default_pricing_rules(self, user_id: int = SHARED_SCOPE):
        """
        Drop this scope's customisation and return the rules that now apply.

        For a user this deletes their own rule set so they inherit the shared
        baseline again. For the shared baseline itself there is nothing above
        it to inherit, so it is rewritten from the shipped defaults.
        """
        scope = int(user_id)
        if scope == SHARED_SCOPE:
            self.set_pricing_rules(self.SHIPPED_PRICING_RULES, user_id=SHARED_SCOPE)
        else:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM pricing_rules WHERE user_id = ?", (scope,))
                conn.commit()
        return self.get_pricing_rules(user_id=scope)

    def calculate_price(
        self, base_price: float, user_id: int = SHARED_SCOPE
    ) -> Tuple[float, Optional[Dict[str, Any]]]:
        """
        Calculate final eBay price using the rules that apply to this user.
        Returns: (calculated_price, matched_rule_dict)

        Reads the rules on every call. Code pricing many cards in a row should
        load them once and call apply_pricing_rules directly, rather than
        re-reading an unchanging table for every card.
        """
        return apply_pricing_rules(
            self.get_pricing_rules(user_id=user_id), base_price
        )

    def has_own_condition_multipliers(self, user_id: int = SHARED_SCOPE) -> bool:
        """Whether this user has saved a multiplier set of their own."""
        if int(user_id) == SHARED_SCOPE:
            return True
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM condition_multipliers WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            )
            return cursor.fetchone() is not None

    def get_condition_multipliers(
        self, user_id: int = SHARED_SCOPE
    ) -> List[Dict[str, Any]]:
        """
        The condition multipliers that apply to this user.

        All-or-nothing like pricing rules: a partially inherited set would
        leave some grades priced by one policy and some by another.
        """
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if scope != SHARED_SCOPE:
                cursor.execute(
                    "SELECT 1 FROM condition_multipliers WHERE user_id = ? LIMIT 1",
                    (scope,),
                )
                if cursor.fetchone() is None:
                    scope = SHARED_SCOPE
            cursor.execute(
                """
                SELECT condition_key, multiplier, label
                FROM condition_multipliers
                WHERE user_id = ?
                """,
                (scope,),
            )
            rows = [dict(r) for r in cursor.fetchall()]

        # Present in a stable, meaningful order rather than alphabetically.
        order = [key for key, _, _ in SHIPPED_CONDITION_MULTIPLIERS]
        rows.sort(key=lambda r: order.index(r["condition_key"])
                  if r["condition_key"] in order else len(order))
        return rows

    def set_condition_multipliers(
        self, multipliers: List[Dict[str, Any]], user_id: int = SHARED_SCOPE
    ):
        """Replace this user's multiplier set, leaving other scopes alone."""
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM condition_multipliers WHERE user_id = ?", (scope,)
            )
            for m in multipliers:
                key = str(m.get("condition_key", "")).strip().upper()
                if not key:
                    continue
                cursor.execute(
                    """
                    INSERT INTO condition_multipliers
                        (user_id, condition_key, multiplier, label)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, condition_key) DO UPDATE SET
                        multiplier = excluded.multiplier,
                        label = excluded.label
                    """,
                    (scope, key, float(m.get("multiplier", 1.0)),
                     str(m.get("label") or "")),
                )
            conn.commit()

    def reset_condition_multipliers(self, user_id: int = SHARED_SCOPE):
        """Drop this user's set so they inherit the shared one again."""
        scope = int(user_id)
        if scope == SHARED_SCOPE:
            self.set_condition_multipliers(
                [{"condition_key": k, "multiplier": v, "label": lab}
                 for k, v, lab in SHIPPED_CONDITION_MULTIPLIERS],
                user_id=SHARED_SCOPE,
            )
        else:
            with self.get_connection() as conn:
                conn.execute(
                    "DELETE FROM condition_multipliers WHERE user_id = ?",
                    (scope,),
                )
                conn.commit()
        return self.get_condition_multipliers(user_id=scope)

    # -- market price feed -------------------------------------------------

    def replace_tcgcsv_groups(
        self, category_id: int, rows: List[Dict[str, Any]]
    ) -> None:
        """Cache the set list used to resolve a set code to a group id."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM tcgcsv_groups WHERE category_id = ?",
                (int(category_id),),
            )
            cursor.executemany(
                """
                INSERT INTO tcgcsv_groups
                    (category_id, group_id, name, abbreviation)
                VALUES (?, ?, ?, ?)
                """,
                [(int(r["category_id"]), int(r["group_id"]),
                  r.get("name") or "", r.get("abbreviation") or "")
                 for r in rows],
            )
            conn.commit()

    def get_tcgcsv_groups_by_abbreviation(
        self, category_id: int
    ) -> Dict[str, int]:
        """
        Set abbreviation -> group id, upper-cased for matching.

        TCGCSV's abbreviation is our set_code ("SWSH06" on both sides), which
        is what lets a card be priced without a hand-maintained map.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT group_id, abbreviation FROM tcgcsv_groups "
                "WHERE category_id = ? AND TRIM(COALESCE(abbreviation,'')) != ''",
                (int(category_id),),
            )
            return {str(r["abbreviation"]).strip().upper(): int(r["group_id"])
                    for r in cursor.fetchall()}

    def get_cards_for_repricing(self) -> List[Dict[str, Any]]:
        """Every catalogued card that can be matched to a TCGplayer product."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT manifest_id, tcgplayer_id, set_code, printing, condition
                FROM manifest
                WHERE TRIM(COALESCE(tcgplayer_id, '')) != ''
                ORDER BY manifest_id
                """
            )
            return [dict(r) for r in cursor.fetchall()]

    def record_market_prices(
        self, updates: List[Tuple[str, float]], source: str = ""
    ) -> int:
        """
        Store new market prices, keeping the previous values as history.

        Writes unconditionally, unlike the backfill in get_or_create_manifest
        which only fills a blank: a refresh whose whole purpose is to change
        the number must not be blocked by there already being one.
        """
        if not updates:
            return 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for manifest_id, price in updates:
                cursor.execute(
                    "UPDATE manifest SET market_price = ? WHERE manifest_id = ?",
                    (float(price), manifest_id),
                )
                cursor.execute(
                    "INSERT INTO price_history (manifest_id, market_price, source) "
                    "VALUES (?, ?, ?)",
                    (manifest_id, float(price), source),
                )
            conn.commit()
        return len(updates)

    def get_price_history(
        self, manifest_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Recent market prices recorded for one card, newest first."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT market_price, source, fetched_at
                FROM price_history
                WHERE manifest_id = ?
                ORDER BY fetched_at DESC, id DESC
                LIMIT ?
                """,
                (manifest_id.strip(), int(limit)),
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_live_cards_for_repricing(self) -> List[Dict[str, Any]]:
        """
        Cards live on eBay, with everything a reprice row needs.

        Includes last_known_price so an unchanged row can be dropped, and the
        stored custom_label because that is the SKU eBay knows -- one rebuilt
        from a card's identity would address a variation that does not exist.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.manifest_id, m.product_name, m.set_name, m.condition,
                       m.printing, m.market_price, m.price,
                       v.ebay_parent_id, v.custom_label, v.last_known_price,
                       v.last_known_qty
                FROM manifest m
                JOIN ebay_variations v ON v.manifest_id = m.manifest_id
                WHERE TRIM(COALESCE(v.ebay_parent_id, '')) != ''
                ORDER BY m.manifest_id
                """
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_managed_cards_for_repricing(self) -> List[Dict[str, Any]]:
        """
        Cards the Inventory API can reprice, with their hold clock.

        Narrower than get_live_cards_for_repricing in two ways, and both are
        the point. The card must have an ``offer_id``, because an offer id is
        the only handle bulkUpdatePriceQuantity accepts, and its listing must
        have a row in ebay_managed_listing, because that is the record of
        which listings this application created through the API.

        A File Exchange listing satisfies neither. The Inventory API cannot
        see it at all -- getOffers returns nothing for its SKUs -- so an
        automatic reprice must not reach it, which is why this join exists
        rather than a flag on the card.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.manifest_id, m.product_name, m.set_name, m.condition,
                       m.printing, m.card_number, m.market_price, m.price,
                       COALESCE(m.quantity, 0) AS quantity,
                       v.ebay_parent_id, v.custom_label, v.offer_id,
                       v.last_known_price, v.last_known_qty, v.hold_since,
                       g.group_key
                FROM manifest m
                JOIN ebay_variations v ON v.manifest_id = m.manifest_id
                JOIN ebay_managed_listing g
                     ON TRIM(g.ebay_parent_id) = TRIM(v.ebay_parent_id)
                WHERE TRIM(COALESCE(v.ebay_parent_id, '')) != ''
                  AND TRIM(COALESCE(v.offer_id, '')) != ''
                ORDER BY m.manifest_id
                """
            )
            return [dict(r) for r in cursor.fetchall()]

    def set_variation_known_price(
        self, manifest_id: str, price: float
    ) -> None:
        """
        Record a price eBay has accepted, and nothing else.

        Deliberately not upsert_variation, which would also write
        last_known_qty and clear pending_qty. A price change must not disturb
        either: the quantity would be rewritten from a figure that goes stale
        the moment a card sells, and clearing pending_qty would discard a
        quantity change a plan is still waiting to have confirmed.
        """
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE ebay_variations
                SET last_known_price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE manifest_id = ?
                """,
                (round(float(price), 2), manifest_id),
            )
            conn.commit()

    def set_variation_hold_since(
        self, manifest_id: str, hold_since: Optional[str]
    ) -> None:
        """
        Start, extend or clear a variation's price hold.

        Passing None clears it, which is what happens the moment the computed
        price stops being below the listed one: the window measures an
        unbroken run of lower prices, so a single day back at or above the
        listed price has to reset it rather than being ignored.
        """
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE ebay_variations SET hold_since = ? WHERE manifest_id = ?",
                (hold_since, manifest_id),
            )
            conn.commit()

    def record_reprice(self, entries: List[Dict[str, Any]]) -> int:
        """Write the repricer's verdicts, applied or not, to the audit log."""
        if not entries:
            return 0
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO reprice_history (
                    manifest_id, ebay_parent_id, verdict, market_price,
                    old_price, new_price, reason, applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        e["manifest_id"],
                        e.get("ebay_parent_id"),
                        e["verdict"],
                        e.get("market_price"),
                        e.get("old_price"),
                        e.get("new_price"),
                        e.get("reason"),
                        1 if e.get("applied") else 0,
                    )
                    for e in entries
                ],
            )
            conn.commit()
        return len(entries)

    def get_reprice_history(
        self, limit: int = 200, manifest_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """The most recent repricer verdicts, newest first."""
        clauses = []
        params: List[Any] = []
        if manifest_id:
            clauses.append("r.manifest_id = ?")
            params.append(manifest_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT r.*, m.product_name, m.set_name, m.condition
                FROM reprice_history r
                LEFT JOIN manifest m ON m.manifest_id = r.manifest_id
                {where}
                ORDER BY r.decided_at DESC, r.id DESC
                LIMIT ?
                """,
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_cards_for_planning(self) -> List[Dict[str, Any]]:
        """
        Every catalogued card with what eBay is known to hold for it.

        A LEFT JOIN, unlike get_live_cards_for_repricing: a card that is not
        on eBay yet is exactly the case that produces a create, so restricting
        to live rows would make it impossible to plan a new listing.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.manifest_id, m.product_name, m.set_name, m.condition,
                       m.printing, m.card_number, m.language, m.sku_id,
                       m.tcgplayer_id, m.set_code, m.cdn_image, m.remarks,
                       m.ebay_fields_json,
                       COALESCE(m.quantity, 0) AS quantity,
                       m.price, m.market_price,
                       v.ebay_parent_id, v.custom_label,
                       v.last_known_qty, v.last_known_price, v.pending_qty
                FROM manifest m
                LEFT JOIN ebay_variations v ON v.manifest_id = m.manifest_id
                ORDER BY m.manifest_id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_listing_settings(self, user_id: int = SHARED_SCOPE) -> Dict[str, str]:
        """
        The settings that apply to this user: the shared baseline with this
        user's own overrides laid on top.

        Unlike pricing rules, these merge key by key. A user who has only ever
        changed their postal code should still pick up a new setting added by a
        later migration, and should not have to re-enter every other field.
        """
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value FROM listing_settings WHERE user_id = ?",
                (SHARED_SCOPE,),
            )
            merged = {row["key"]: row["value"] for row in cursor.fetchall()}
            if scope != SHARED_SCOPE:
                cursor.execute(
                    "SELECT key, value FROM listing_settings WHERE user_id = ?",
                    (scope,),
                )
                merged.update({row["key"]: row["value"] for row in cursor.fetchall()})
            return merged

    def get_own_listing_setting_keys(self, user_id: int = SHARED_SCOPE) -> List[str]:
        """
        The setting keys this user has overridden, as opposed to inherited.

        Used by the UI to mark which fields are the user's own, so it is clear
        what a reset would give back.
        """
        scope = int(user_id)
        if scope == SHARED_SCOPE:
            return []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key FROM listing_settings WHERE user_id = ? ORDER BY key",
                (scope,),
            )
            return [row["key"] for row in cursor.fetchall()]

    def set_listing_settings(
        self, settings: Dict[str, str], user_id: int = SHARED_SCOPE
    ):
        """
        Save settings into this user's scope only.

        Writing an override even when the value equals the inherited one is
        deliberate: it pins the value, so a later change to the shared baseline
        does not silently move a field the user has already reviewed.
        """
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for k, v in settings.items():
                cursor.execute(
                    """
                    INSERT INTO listing_settings (user_id, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
                    """,
                    (scope, str(k), str(v)),
                )
            conn.commit()

    def reset_listing_settings(self, user_id: int = SHARED_SCOPE) -> Dict[str, str]:
        """
        Drop this user's overrides so they inherit the shared baseline again.

        The shared baseline itself has nothing above it to inherit, so it is
        left alone; there is no shipped-defaults rewrite here because init_db
        re-seeds any key that is missing or blank on the next startup.
        """
        scope = int(user_id)
        if scope != SHARED_SCOPE:
            with self.get_connection() as conn:
                conn.execute(
                    "DELETE FROM listing_settings WHERE user_id = ?", (scope,)
                )
                conn.commit()
        return self.get_listing_settings(user_id=scope)

    def get_listing_setting(
        self, key: str, default: str = "", user_id: int = SHARED_SCOPE
    ) -> str:
        """Get a single listing setting, preferring this user's override."""
        scope = int(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if scope != SHARED_SCOPE:
                cursor.execute(
                    "SELECT value FROM listing_settings WHERE user_id = ? AND key = ?",
                    (scope, key),
                )
                row = cursor.fetchone()
                if row is not None:
                    return row["value"]
            cursor.execute(
                "SELECT value FROM listing_settings WHERE user_id = ? AND key = ?",
                (SHARED_SCOPE, key),
            )
            row = cursor.fetchone()
            return row["value"] if row else default

    def purge_inventory(self) -> Dict[str, int]:
        """
        Delete the catalogue, the live store mirror and the batch fingerprints.

        Configuration is deliberately preserved: pricing rules and listing
        settings (postal code, business policies, templates) survive, so a
        testing reset does not also throw away setup. Deleting the database
        file would take those with it.

        Returns the number of rows removed from each table.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            counts = {}
            for table in ("ebay_variations", "manifest", "processed_batches"):
                cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
                counts[table] = cursor.fetchone()["n"]
            # ebay_variations first: it references manifest.
            cursor.execute("DELETE FROM ebay_variations")
            cursor.execute("DELETE FROM manifest")
            cursor.execute("DELETE FROM processed_batches")
            conn.commit()
            return counts

    def find_processed_batch(self, sha256: str) -> Optional[Dict[str, Any]]:
        """Return the record of a previously processed batch upload, if any."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT sha256, source_name, row_count, processed_at
                FROM processed_batches
                WHERE sha256 = ?
                """,
                (sha256,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def record_processed_batch(
        self, sha256: str, source_name: str, row_count: int
    ) -> None:
        """
        Fingerprint a processed batch so a duplicate upload can be detected.

        Batch quantities are additive, so replaying the same export would double
        the live eBay stock for every card in it.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO processed_batches (sha256, source_name, row_count, processed_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sha256) DO UPDATE SET
                    source_name = excluded.source_name,
                    row_count = excluded.row_count,
                    processed_at = CURRENT_TIMESTAMP;
                """,
                (sha256, source_name, int(row_count)),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Draft plans: staging for every eBay-bound change
    # ------------------------------------------------------------------

    def create_plan(
        self,
        user_id: int,
        source: str = "manual",
        source_ref: Optional[str] = None,
        note: Optional[str] = None,
    ) -> int:
        """
        Open a new draft plan and return its id.

        Raises sqlite3.IntegrityError if this user already has an open draft,
        which the unique partial index enforces. That is deliberate: the
        caller must decide whether to extend the existing draft or discard it,
        because two drafts computed against the same cards would each be built
        from state the other is about to change.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO listing_plan (user_id, status, source, source_ref, note)
                VALUES (?, 'draft', ?, ?, ?)
                """,
                (int(user_id), source, source_ref, note),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_open_plan_id(self, user_id: int) -> Optional[int]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM listing_plan WHERE user_id = ? AND status = 'draft'",
                (int(user_id),),
            )
            row = cursor.fetchone()
            return int(row["id"]) if row else None

    def get_plan(self, plan_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM listing_plan WHERE id = ?", (int(plan_id),)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_plans(
        self, user_id: Optional[int] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Recent plans, newest first, each with its item counts.

        The counts are computed here rather than in the caller so the drafts
        list costs one query instead of one per plan.
        """
        clauses = []
        params: List[Any] = []
        if user_id is not None:
            clauses.append("p.user_id = ?")
            params.append(int(user_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT p.*,
                       COUNT(i.id) AS item_count,
                       SUM(CASE WHEN i.status = 'excluded' THEN 1 ELSE 0 END)
                           AS excluded_count,
                       SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END)
                           AS failed_count,
                       SUM(CASE WHEN i.status = 'pushed' THEN 1 ELSE 0 END)
                           AS pushed_count
                FROM listing_plan p
                LEFT JOIN listing_plan_item i ON i.plan_id = p.id
                {where}
                GROUP BY p.id
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT ?
                """,
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    def add_plan_items(self, plan_id: int, items: List[Dict[str, Any]]) -> int:
        """
        Append items to a plan. Returns how many were written.

        Done in one executemany inside one connection: a plan built from a
        full SortSwift dump can carry thousands of items, and a per-item
        connection would spend minutes on setup alone.
        """
        if not items:
            return 0
        rows = [
            (
                int(plan_id),
                item["manifest_id"],
                item.get("group_key"),
                item["action"],
                item.get("proposed_qty"),
                item.get("proposed_price"),
                item.get("observed_qty"),
                item.get("observed_price"),
                item.get("status", "pending"),
                item.get("validation"),
            )
            for item in items
        ]
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO listing_plan_item (
                    plan_id, manifest_id, group_key, action,
                    proposed_qty, proposed_price, observed_qty, observed_price,
                    status, validation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return len(rows)

    def get_plan_items(
        self, plan_id: int, group_key: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        A plan's items, joined to the card each one describes.

        Ordered by group then card so the drafts page can render listings as
        contiguous blocks without sorting in the browser.
        """
        clauses = ["i.plan_id = ?"]
        params: List[Any] = [int(plan_id)]
        if group_key is not None:
            clauses.append("COALESCE(i.group_key, '') = ?")
            params.append(group_key)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT i.*,
                       m.product_name, m.set_name, m.condition, m.printing,
                       m.card_number, m.language, m.quantity AS catalogued_qty,
                       m.price AS catalogued_price, m.market_price,
                       m.cdn_image, m.remarks, m.ebay_fields_json,
                       v.ebay_parent_id, v.custom_label, v.offer_id,
                       v.last_known_qty, v.last_known_price
                FROM listing_plan_item i
                JOIN manifest m ON m.manifest_id = i.manifest_id
                LEFT JOIN ebay_variations v ON v.manifest_id = i.manifest_id
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(i.group_key, ''), i.manifest_id
                """,
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_plan_item(self, item_id: int, **fields) -> bool:
        """
        Change one item's proposal, grouping or status.

        Restricted to a whitelist of columns rather than accepting arbitrary
        keys: this is reached from an HTTP endpoint, and interpolating a
        caller-supplied column name into SQL is how that becomes an injection
        point.
        """
        allowed = {
            "group_key",
            "action",
            "proposed_qty",
            "proposed_price",
            "status",
            "validation",
            "error_code",
            "error_message",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [int(item_id)]
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE listing_plan_item
                SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                params,
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_plan_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM listing_plan_item WHERE id = ?", (int(item_id),)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def set_plan_status(
        self,
        plan_id: int,
        status: str,
        approved_by: Optional[int] = None,
    ) -> bool:
        """
        Move a plan through its lifecycle, stamping the matching timestamp.

        The timestamps are set here rather than by the caller so that an
        approved plan can never lack an approval time -- which is the only
        record of who authorised an eBay write and when.
        """
        sets = ["status = ?"]
        params: List[Any] = [status]
        if status == "approved":
            sets.append("approved_at = CURRENT_TIMESTAMP")
            if approved_by is not None:
                sets.append("approved_by = ?")
                params.append(int(approved_by))
        elif status == "pushed":
            sets.append("pushed_at = CURRENT_TIMESTAMP")
        params.append(int(plan_id))
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE listing_plan SET {', '.join(sets)} WHERE id = ?", params
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_plan(self, plan_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM listing_plan WHERE id = ?", (int(plan_id),))
            conn.commit()
            return cursor.rowcount > 0

    def get_plan_groups(self, plan_id: int) -> List[Dict[str, Any]]:
        """
        One row per listing the plan touches, for the drafts page's summary.

        Grouped in SQL because eBay's unit of publication is the listing, not
        the card: a validation failure on one card blocks its whole group, so
        the group is the level at which the page has to report.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(i.group_key, '') AS group_key,
                       COUNT(*) AS item_count,
                       SUM(CASE WHEN i.status = 'excluded' THEN 1 ELSE 0 END)
                           AS excluded_count,
                       SUM(CASE WHEN i.validation IS NOT NULL
                                 AND TRIM(i.validation) != '' THEN 1 ELSE 0 END)
                           AS invalid_count,
                       SUM(COALESCE(i.proposed_qty, 0)) AS proposed_copies,
                       MIN(m.set_name) AS set_name,
                       MIN(m.condition) AS condition,
                       -- The listing this group maps onto, when it already
                       -- exists. NULL means the plan would create it, and a
                       -- cover then applies at creation rather than as a
                       -- revision.
                       MAX(NULLIF(TRIM(COALESCE(v.ebay_parent_id, '')), ''))
                           AS ebay_parent_id,
                       -- The staged cover if one has been chosen, otherwise
                       -- whatever the live listing already carries, so the
                       -- drafts page shows the current picture rather than an
                       -- empty frame.
                       COALESCE(
                           NULLIF(TRIM(COALESCE(g.cover_image_url, '')), ''),
                           MAX(NULLIF(TRIM(COALESCE(o.cover_image_url, '')), ''))
                       ) AS cover_image_url,
                       CASE WHEN NULLIF(TRIM(COALESCE(g.cover_image_url, '')), '')
                                 IS NOT NULL THEN 1 ELSE 0 END AS cover_is_staged,
                       -- A card's own picture, used as the fallback suggestion
                       -- when nothing has been chosen for the listing.
                       MIN(NULLIF(TRIM(COALESCE(m.cdn_image, '')), ''))
                           AS first_card_image
                FROM listing_plan_item i
                JOIN manifest m ON m.manifest_id = i.manifest_id
                LEFT JOIN ebay_variations v ON v.manifest_id = i.manifest_id
                LEFT JOIN ebay_listing_overrides o
                       ON o.ebay_parent_id = v.ebay_parent_id
                LEFT JOIN listing_plan_group g
                       ON g.plan_id = i.plan_id
                      AND g.group_key = COALESCE(i.group_key, '')
                WHERE i.plan_id = ?
                GROUP BY COALESCE(i.group_key, ''), g.cover_image_url
                ORDER BY COALESCE(i.group_key, '')
                """,
                (int(plan_id),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def set_manifest_ebay_fields(
        self, manifest_id: str, fields: Optional[Dict[str, Any]]
    ) -> None:
        """
        Store the export-derived fields an Add row needs for one card.

        Merged rather than replaced: a later batch may omit a column the
        earlier one supplied -- SortSwift's templates differ -- and dropping a
        previously known item specific would silently degrade the listing that
        card ends up in.
        """
        import json as _json

        if not fields:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT ebay_fields_json FROM manifest WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                return
            try:
                existing = _json.loads(row["ebay_fields_json"] or "{}")
            except (ValueError, TypeError):
                existing = {}
            if not isinstance(existing, dict):
                existing = {}

            merged = dict(existing)
            for key, value in fields.items():
                if key == "item_specifics" and isinstance(value, dict):
                    specifics = dict(existing.get("item_specifics") or {})
                    specifics.update({k: v for k, v in value.items() if v})
                    merged["item_specifics"] = specifics
                elif value not in (None, ""):
                    merged[key] = value

            cursor.execute(
                "UPDATE manifest SET ebay_fields_json = ? WHERE manifest_id = ?",
                (_json.dumps(merged), manifest_id),
            )
            conn.commit()

    def get_manifest_ebay_fields(self, manifest_id: str) -> Dict[str, Any]:
        import json as _json

        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT ebay_fields_json FROM manifest WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
        if row is None or not row["ebay_fields_json"]:
            return {}
        try:
            parsed = _json.loads(row["ebay_fields_json"])
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def set_plan_group_cover(
        self, plan_id: int, group_key: str, cover_image_url: Optional[str]
    ) -> None:
        """
        Stage a cover photo for one listing in a plan.

        An empty URL clears the staged choice rather than storing a blank,
        so the drafts page falls back to showing what the live listing
        already carries instead of claiming the cover was removed. Removing a
        photo from a live listing is a different operation, and eBay treats a
        PicURL revision as replacing the whole picture set.
        """
        cleaned = str(cover_image_url or "").strip()
        with self.get_connection() as conn:
            if not cleaned:
                conn.execute(
                    "DELETE FROM listing_plan_group WHERE plan_id = ? AND group_key = ?",
                    (int(plan_id), group_key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO listing_plan_group
                        (plan_id, group_key, cover_image_url, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(plan_id, group_key) DO UPDATE SET
                        cover_image_url = excluded.cover_image_url,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (int(plan_id), group_key, cleaned),
                )
            conn.commit()

    # -- listings the Inventory API can see -----------------------------

    def get_cards_for_listing(self, ebay_parent_id: str) -> List[Dict[str, Any]]:
        """
        Every card eBay reports as part of one listing.

        Keyed on the eBay item number rather than on a plan's grouping,
        because this answers "what is actually in that listing" -- which is
        what a repair has to act on. A plan is a proposal and may not exist
        any more; the mirror is the record of what went live.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.manifest_id, m.product_name, m.set_name, m.condition,
                       m.printing, m.card_number, m.language, m.cdn_image,
                       m.remarks, m.ebay_fields_json, m.price, m.quantity,
                       v.custom_label, v.offer_id, v.ebay_parent_id,
                       v.last_known_qty, v.last_known_price
                FROM ebay_variations v
                JOIN manifest m ON m.manifest_id = v.manifest_id
                WHERE TRIM(COALESCE(v.ebay_parent_id, '')) = ?
                ORDER BY m.card_number, m.manifest_id
                """,
                (str(ebay_parent_id).strip(),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_managed_listing_by_parent(
        self, ebay_parent_id: str
    ) -> Optional[Dict[str, Any]]:
        """The managed-listing row for an eBay item number, if we made it."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM ebay_managed_listing
                WHERE TRIM(COALESCE(ebay_parent_id, '')) = ?
                """,
                (str(ebay_parent_id).strip(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_managed_listing(self, group_key: str) -> Optional[Dict[str, Any]]:
        """
        What we know about one listing we manage through the API.

        None means the Inventory API cannot see this listing: either it does
        not exist yet, or it was created through File Exchange. Both stay on
        the CSV path, and a caller must not assume the second case is the
        first -- pushing a legacy listing's cards would create a duplicate
        listing beside the live one.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM ebay_managed_listing WHERE group_key = ?",
                (str(group_key),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_managed_listings(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM ebay_managed_listing ORDER BY group_key"
            )
            return [dict(row) for row in cursor.fetchall()]

    def upsert_managed_listing(
        self,
        group_key: str,
        inventory_item_group_key: Optional[str] = None,
        ebay_parent_id: Optional[str] = None,
        managed_by: str = "api",
        pushed: bool = False,
    ) -> None:
        """
        Record or update a listing we manage through the API.

        Every optional field COALESCEs, so a later call that knows only the
        eBay item number cannot erase the group key that made the listing
        addressable. That mattered the first time a push published
        successfully and the follow-up write dropped the key.
        """
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO ebay_managed_listing
                    (group_key, inventory_item_group_key, ebay_parent_id,
                     managed_by, last_pushed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?,
                        CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(group_key) DO UPDATE SET
                    inventory_item_group_key = COALESCE(
                        excluded.inventory_item_group_key,
                        ebay_managed_listing.inventory_item_group_key),
                    ebay_parent_id = COALESCE(
                        excluded.ebay_parent_id,
                        ebay_managed_listing.ebay_parent_id),
                    managed_by = excluded.managed_by,
                    last_pushed_at = COALESCE(
                        excluded.last_pushed_at,
                        ebay_managed_listing.last_pushed_at),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    str(group_key),
                    inventory_item_group_key,
                    ebay_parent_id,
                    managed_by,
                    1 if pushed else 0,
                ),
            )
            conn.commit()

    def set_variation_offer(self, manifest_id: str, offer_id: str) -> None:
        """
        Remember the offer eBay created for this card's SKU.

        Written during a push, not by Module B: the Active Inventory report
        carries no offer ids, so this is the only chance to record one -- and
        the offer id is the only handle that can later change a price.

        Inserts a row when the card has none, with an empty
        ``ebay_parent_id``. An offer exists before the listing it will belong
        to does, and losing the id in that window would make the next push
        create a *second* offer for a SKU that already has one, which eBay
        refuses. An empty parent already reads as "not live on eBay"
        everywhere that matters -- ``get_live_variations`` and the planner's
        own ``_is_live`` both require a non-blank value -- so the placeholder
        cannot make a card look listed when it is not.
        """
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO ebay_variations
                    (manifest_id, ebay_parent_id, last_known_qty, offer_id,
                     updated_at)
                VALUES (?, '', 0, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(manifest_id) DO UPDATE SET
                    offer_id = excluded.offer_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (manifest_id.strip(), str(offer_id)),
            )
            conn.commit()

    def get_plan_group_covers(self, plan_id: int) -> Dict[str, str]:
        """
        Every cover photo staged in a plan, keyed by group.

        Deliberately unfiltered, unlike ``get_plan_cover_revisions``: a cover
        for a listing that does not exist yet cannot be revised onto anything,
        but it is exactly what the Add file's parent row has to carry. Both
        halves are needed, and each one alone loses covers silently.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT group_key, cover_image_url
                FROM listing_plan_group
                WHERE plan_id = ?
                  AND TRIM(COALESCE(cover_image_url, '')) != ''
                """,
                (int(plan_id),),
            )
            return {
                row["group_key"]: row["cover_image_url"].strip()
                for row in cursor.fetchall()
            }

    def get_plan_cover_revisions(self, plan_id: int) -> List[Dict[str, Any]]:
        """
        Staged covers that can be applied to an existing listing.

        Restricted to groups that map onto a live listing, because a cover for
        a listing that does not exist yet is carried into its creation rather
        than revised onto it. Also skips a staged URL identical to what the
        listing already has: eBay ignores a PicURL it already holds, so the row
        would be a no-op that still costs an upload.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT g.group_key,
                       g.cover_image_url,
                       MAX(NULLIF(TRIM(COALESCE(v.ebay_parent_id, '')), ''))
                           AS ebay_parent_id,
                       MAX(NULLIF(TRIM(COALESCE(o.cover_image_url, '')), ''))
                           AS current_cover
                FROM listing_plan_group g
                JOIN listing_plan_item i
                     ON i.plan_id = g.plan_id
                    AND COALESCE(i.group_key, '') = g.group_key
                LEFT JOIN ebay_variations v ON v.manifest_id = i.manifest_id
                LEFT JOIN ebay_listing_overrides o
                       ON o.ebay_parent_id = v.ebay_parent_id
                WHERE g.plan_id = ?
                  AND TRIM(COALESCE(g.cover_image_url, '')) != ''
                GROUP BY g.group_key, g.cover_image_url
                ORDER BY g.group_key
                """,
                (int(plan_id),),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        return [
            r
            for r in rows
            if r["ebay_parent_id"] and r["cover_image_url"] != r["current_cover"]
        ]

    def export_all_manifest(self) -> List[Dict[str, Any]]:
        """Export all master manifest rows."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 
                    m.manifest_id,
                    m.product_name,
                    COALESCE(m.card_number, '') AS card_number,
                    m.set_name,
                    m.condition,
                    m.printing,
                    COALESCE(m.quantity, 0) AS quantity,
                    COALESCE(v.ebay_parent_id, '') AS ebay_parent_id,
                    COALESCE(v.last_known_qty, 0) AS last_known_qty,
                v.pending_qty AS pending_qty
                FROM manifest m
                LEFT JOIN ebay_variations v ON m.manifest_id = v.manifest_id
                ORDER BY m.manifest_id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
