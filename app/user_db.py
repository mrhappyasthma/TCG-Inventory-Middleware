import os
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any, List


class UserDatabase:
    """
    SQLite user database for Google Sign-In accounts and admin approvals.

    Users are keyed on the Google ``sub`` claim rather than on their email
    address: ``sub`` is the stable, immutable account identifier, whereas an
    email address can be changed on the Google side, which would otherwise
    orphan the local account and silently create a duplicate.
    """

    def __init__(self, db_path: str = "data/users.db"):
        self.db_path = db_path
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        self.init_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE,
                    google_sub TEXT UNIQUE,
                    auth_provider TEXT DEFAULT 'google',
                    role TEXT DEFAULT 'user',
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                );
                """
            )

            # Idempotent migration for databases created before Google-only auth.
            # Any legacy password_hash column is left physically in place but is
            # never read or written, which avoids a destructive table rebuild.
            existing_columns = {
                row["name"] for row in cursor.execute("PRAGMA table_info(users)")
            }
            if "google_sub" not in existing_columns:
                cursor.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")

            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
                ON users(google_sub)
                WHERE google_sub IS NOT NULL;
                """
            )

            # The connected eBay account. One row, ever: there is one eBay
            # store behind this application, the inventory it manages is
            # shared rather than per-user, and letting two users each connect
            # a different account would make it ambiguous which store a push
            # targets. The CHECK is what enforces that rather than convention.
            #
            # It lives in the users database rather than beside the inventory
            # deliberately. The inventory database is the one that gets
            # restored from a snapshot in the admin Database panel, and a
            # restore must not silently cost the eBay connection -- the
            # refresh token is the only credential here that cannot be
            # recreated without an interactive re-consent.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ebay_connection (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    token_json TEXT NOT NULL,
                    connected_by INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()

    # -- eBay connection ---------------------------------------------------

    def get_ebay_token(self) -> Optional[Dict[str, Any]]:
        """
        The stored eBay token record, or None when no account is connected.

        Returns the decoded payload rather than the raw row: the caller is
        ebay_client's TokenStore, which is deliberately ignorant of SQLite.
        """
        import json

        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT token_json FROM ebay_connection WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            token = json.loads(row["token_json"])
        except (ValueError, TypeError):
            # A corrupt row is treated as "not connected" rather than raising.
            # The remedy is the same either way -- reconnect -- and raising
            # here would take out every request that checks eBay status.
            return None
        return token or None

    def save_ebay_token(
        self, token: Optional[Dict[str, Any]], connected_by: Optional[int] = None
    ) -> None:
        """
        Persist the eBay token record, replacing any previous connection.

        An empty or falsy token clears the row, which is how disconnecting
        works: ebay_client's TokenStore.clear() saves an empty dict, and
        leaving a blank record behind would report a connection that cannot
        authenticate.
        """
        import json

        with self.get_connection() as conn:
            if not token:
                conn.execute("DELETE FROM ebay_connection WHERE id = 1")
                conn.commit()
                return
            conn.execute(
                """
                INSERT INTO ebay_connection (id, token_json, connected_by, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    token_json = excluded.token_json,
                    connected_by = excluded.connected_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (json.dumps(token), connected_by),
            )
            conn.commit()

    def get_ebay_connection_meta(self) -> Optional[Dict[str, Any]]:
        """
        Who connected the eBay account and when, without the token itself.

        Split from get_ebay_token so the dashboard can show the connection
        without the refresh token ever entering a response payload.
        """
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT c.connected_by, c.updated_at, u.username, u.email
                FROM ebay_connection c
                LEFT JOIN users u ON u.id = c.connected_by
                WHERE c.id = 1
                """
            ).fetchone()
        return dict(row) if row else None

    def get_user_count(self) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS total FROM users")
            return cursor.fetchone()["total"]

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
                (username.strip(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_by_google_sub(self, google_sub: str) -> Optional[Dict[str, Any]]:
        """Look up a user by their immutable Google subject identifier."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE google_sub = ?", (str(google_sub).strip(),)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_user(
        self,
        username: str,
        google_sub: str,
        email: Optional[str] = None,
        auth_provider: str = "google",
    ) -> Dict[str, Any]:
        """
        Create a new user.

        The first account ever created is automatically granted the 'admin' role
        with 'active' status so the instance has an owner. Every subsequent
        account lands in 'pending' and must be approved by an admin.
        """
        is_first_user = self.get_user_count() == 0
        role = "admin" if is_first_user else "user"
        status = "active" if is_first_user else "pending"

        u_name = username.strip()
        e_mail = email.strip() if email else None

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (username, email, google_sub, auth_provider, role, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (u_name, e_mail, str(google_sub).strip(), auth_provider, role, status),
            )
            user_id = cursor.lastrowid
            conn.commit()
            return self.get_user_by_id(user_id)

    def update_last_login(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?",
                (user_id,),
            )
            conn.commit()

    def update_profile_from_google(
        self, user_id: int, email: Optional[str] = None
    ) -> None:
        """Keep the cached email in step with the verified Google claim."""
        if not email:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET email = ? WHERE id = ?", (email.strip(), user_id)
            )
            conn.commit()

    def set_user_status(self, user_id: int, status: str) -> bool:
        """Set user status: 'active', 'pending', or 'disabled'."""
        if status not in ("active", "pending", "disabled"):
            raise ValueError("Invalid status value")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET status = ? WHERE id = ?", (status, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_user_role(self, user_id: int, role: str) -> bool:
        """Set user role: 'admin' or 'user'."""
        if role not in ("admin", "user"):
            raise ValueError("Invalid role value")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            conn.commit()
            return cursor.rowcount > 0

    def list_all_users(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, username, email, auth_provider, role, status, created_at, last_login
                FROM users
                ORDER BY created_at ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_owner_user_id(self) -> Optional[int]:
        """
        The account an unattended job should act as, or None if there is none.

        Background work has no signed-in user, but pricing rules and listing
        settings are per-user: a job that fell back to the shared baseline
        would quietly price from different rules than the ones the operator
        sees on screen. The oldest active admin is the deployment's owner --
        the first account ever created is granted admin automatically -- so
        acting as them is what makes the nightly repricer agree with the
        Listing Rules panel.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id FROM users
                WHERE role = 'admin' AND status = 'active'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            return int(row["id"]) if row else None

    def delete_user(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount > 0

    def count_users(self) -> int:
        """How many accounts the database holds, for backup summaries."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS count FROM users")
            return int(cursor.fetchone()["count"])

    def export_snapshot(self, dest_path: str) -> str:
        """
        Write a consistent single-file copy of the user database.

        Mirrors the inventory database's exporter: VACUUM INTO checkpoints the
        write-ahead log into the output, so the result carries every committed
        change and needs no -wal/-shm sidecars. Copying users.db by hand while
        the app is running can miss the most recent sign-in.
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
