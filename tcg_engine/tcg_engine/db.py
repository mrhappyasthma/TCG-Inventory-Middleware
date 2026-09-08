import os
import re
import shutil
import sqlite3
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

# Pricing rules and listing settings are per-user, so every row in those two
# tables carries the id of the user who owns it. Scope 0 is the shared baseline
# that a user inherits until they save a change of their own; no real user can
# ever have id 0, because SQLite AUTOINCREMENT starts at 1. A literal 0 is used
# rather than NULL so the tables can carry a real composite primary key --
# NULLs compare as distinct in SQLite, which would let duplicates through.
SHARED_SCOPE = 0


class Database:
    """
    SQLite Database manager for TCG inventory.
    Manages the Master Catalog (manifest) and Live Store Mirror (ebay_variations).
    """

    def __init__(self, db_path: str = "data/inventory.db"):
        self.db_path = db_path
        # Ensure parent directory exists
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        self.init_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
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
                    last_known_qty INTEGER DEFAULT 0,
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
            # Idempotent migration: catalogued quantity was added after the
            # first release.
            manifest_columns = {
                row["name"] for row in cursor.execute("PRAGMA table_info(manifest)")
            }
            if "quantity" not in manifest_columns:
                cursor.execute(
                    "ALTER TABLE manifest ADD COLUMN quantity INTEGER DEFAULT 0"
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
                CREATE INDEX IF NOT EXISTS idx_pricing_rules_user
                ON pricing_rules(user_id, sort_order);
                """
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
                    ("cover_image_url", ""),
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
                SELECT manifest_id, ebay_parent_id, last_known_qty
                FROM ebay_variations
                WHERE manifest_id = ?
                """,
                (manifest_id.strip(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def upsert_variation(
        self, manifest_id: str, ebay_parent_id: str, last_known_qty: int
    ) -> Dict[str, Any]:
        """
        Insert or update an ebay_variations row.
        """
        m_id = manifest_id.strip()
        p_id = str(ebay_parent_id).strip()
        qty = int(last_known_qty)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ebay_variations (manifest_id, ebay_parent_id, last_known_qty, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(manifest_id) DO UPDATE SET
                    ebay_parent_id = excluded.ebay_parent_id,
                    last_known_qty = excluded.last_known_qty,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (m_id, p_id, qty),
            )
            conn.commit()
            return {
                "manifest_id": m_id,
                "ebay_parent_id": p_id,
                "last_known_qty": qty,
            }

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
                COALESCE(v.ebay_parent_id, '') AS ebay_parent_id,
                COALESCE(v.last_known_qty, 0) AS last_known_qty
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
        """Aggregate catalog and inventory statistics."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS total_cards FROM manifest")
            total_cards = cursor.fetchone()["total_cards"]

            cursor.execute("SELECT COUNT(*) AS active_listings FROM ebay_variations WHERE ebay_parent_id != ''")
            active_listings = cursor.fetchone()["active_listings"]

            cursor.execute("SELECT SUM(last_known_qty) AS total_stock FROM ebay_variations")
            total_stock_row = cursor.fetchone()["total_stock"]
            total_stock = total_stock_row if total_stock_row is not None else 0

            return {
                "total_cards": total_cards,
                "active_listings": active_listings,
                "total_stock": total_stock,
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
        """
        price = float(base_price or 0.0)
        rules = self.get_pricing_rules(user_id=user_id)

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
                    COALESCE(v.last_known_qty, 0) AS last_known_qty
                FROM manifest m
                LEFT JOIN ebay_variations v ON m.manifest_id = v.manifest_id
                ORDER BY m.manifest_id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
