import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Tuple


# Default seller details used when generating eBay Add files. The policy names
# must match the seller's eBay business policies exactly, including case.
# All of these are editable at runtime under Listing Rules.
DEFAULT_SELLER_POSTAL_CODE = "94305"
# eBay requires the "Game" item specific on card listings. Used only when the
# uploaded export does not supply one.
DEFAULT_GAME = "Pokémon TCG"
DEFAULT_SHIPPING_PROFILE = "Free Shipping Cards"
DEFAULT_RETURN_PROFILE = "No Returns"
DEFAULT_PAYMENT_PROFILE = "Immediate Payment"


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
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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
            
            # Seed default pricing rules if empty
            cursor.execute("SELECT COUNT(*) AS count FROM pricing_rules")
            if cursor.fetchone()["count"] == 0:
                default_rules = [
                    (0.00, 0.25, "fixed", 1.99, 1),
                    (0.25, 0.50, "fixed", 2.49, 2),
                    (0.50, 1.00, "fixed", 2.99, 3),
                    (1.00, None, "markup_fixed", 3.00, 4),
                ]
                cursor.executemany(
                    """
                    INSERT INTO pricing_rules (min_price, max_price, rule_type, rule_value, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    default_rules,
                )

            # Seed default listing settings if empty
            cursor.execute("SELECT COUNT(*) AS count FROM listing_settings")
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
                ]
                cursor.executemany(
                    """
                    INSERT INTO listing_settings (key, value)
                    VALUES (?, ?)
                    """,
                    default_settings,
                )

            # Backfill settings keys added after the initial seed.
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
            ):
                cursor.execute(
                    """
                    INSERT INTO listing_settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET
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
        "m.set_name",
        "m.condition",
        "m.printing",
        "m.remarks",
        "m.sku_id",
        "v.ebay_parent_id",
    )

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
    ) -> List[Dict[str, Any]]:
        """
        Combined live inventory query (manifest LEFT JOIN ebay_variations).
        """
        order_col = self._SORTABLE_COLUMNS.get(sort_by, "m.manifest_id")
        direction = "DESC" if str(sort_dir).upper() == "DESC" else "ASC"

        query = """
            SELECT
                m.manifest_id,
                m.product_name,
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
        where_clause, params = self._build_search_clause(search)
        query += where_clause
        query += f" ORDER BY {order_col} {direction} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_inventory_count(self, search: Optional[str] = None) -> int:
        """Count total matching rows in inventory."""
        query = """
            SELECT COUNT(*) AS total
            FROM manifest m
            LEFT JOIN ebay_variations v ON m.manifest_id = v.manifest_id
        """
        where_clause, params = self._build_search_clause(search)
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

    def get_pricing_rules(self) -> List[Dict[str, Any]]:
        """Get all configured pricing rules ordered by sort_order / min_price."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, min_price, max_price, rule_type, rule_value, sort_order
                FROM pricing_rules
                ORDER BY sort_order ASC, min_price ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def set_pricing_rules(self, rules: List[Dict[str, Any]]):
        """Replace all pricing rules with new configuration."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pricing_rules")
            for idx, r in enumerate(rules, start=1):
                cursor.execute(
                    """
                    INSERT INTO pricing_rules (min_price, max_price, rule_type, rule_value, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        float(r.get("min_price", 0.0)),
                        float(r["max_price"]) if r.get("max_price") is not None and str(r.get("max_price")).strip() != "" else None,
                        str(r.get("rule_type", "fixed")),
                        float(r.get("rule_value", 1.99)),
                        int(r.get("sort_order", idx)),
                    ),
                )
            conn.commit()

    def reset_default_pricing_rules(self):
        """Reset pricing rules to system defaults."""
        default_rules = [
            {"min_price": 0.00, "max_price": 0.25, "rule_type": "fixed", "rule_value": 1.99, "sort_order": 1},
            {"min_price": 0.25, "max_price": 0.50, "rule_type": "fixed", "rule_value": 2.49, "sort_order": 2},
            {"min_price": 0.50, "max_price": 1.00, "rule_type": "fixed", "rule_value": 2.99, "sort_order": 3},
            {"min_price": 1.00, "max_price": None, "rule_type": "markup_fixed", "rule_value": 3.00, "sort_order": 4},
        ]
        self.set_pricing_rules(default_rules)
        return self.get_pricing_rules()

    def calculate_price(self, base_price: float) -> Tuple[float, Optional[Dict[str, Any]]]:
        """
        Calculate final eBay price based on active pricing rules.
        Returns: (calculated_price, matched_rule_dict)
        """
        price = float(base_price or 0.0)
        rules = self.get_pricing_rules()

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

    def get_listing_settings(self) -> Dict[str, str]:
        """Get key-value listing settings dictionary."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM listing_settings")
            rows = cursor.fetchall()
            return {row["key"]: row["value"] for row in rows}

    def set_listing_settings(self, settings: Dict[str, str]):
        """Save listing settings."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for k, v in settings.items():
                cursor.execute(
                    """
                    INSERT INTO listing_settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(k), str(v)),
                )
            conn.commit()

    def get_listing_setting(self, key: str, default: str = "") -> str:
        """Get a single listing setting by key."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM listing_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default

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
