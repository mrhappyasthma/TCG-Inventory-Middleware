import os
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any, List


class UserDatabase:
    """
    SQLite User Database for authentication and admin approvals.
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
                    password_hash TEXT,
                    auth_provider TEXT DEFAULT 'local',
                    role TEXT DEFAULT 'user',
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                );
                """
            )
            conn.commit()

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
            cursor.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username.strip(),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_user(
        self,
        username: str,
        password_hash: Optional[str] = None,
        email: Optional[str] = None,
        auth_provider: str = "local",
    ) -> Dict[str, Any]:
        """
        Create a new user.
        If this is the first user in the database, automatically grant 'admin' role and 'active' status.
        Otherwise, default to 'user' role and 'pending' status.
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
                INSERT INTO users (username, email, password_hash, auth_provider, role, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (u_name, e_mail, password_hash, auth_provider, role, status),
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

    def set_user_status(self, user_id: int, status: str) -> bool:
        """Set user status: 'active', 'pending', or 'disabled'."""
        if status not in ("active", "pending", "disabled"):
            raise ValueError("Invalid status value")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
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

    def delete_user(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount > 0
