"""Small SQLite store. The database is deliberately kept out of Git."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


class Store:
    def __init__(self, path: str | Path = "data/setup.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS mailboxes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    account_id INTEGER,
                    reserved_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    session TEXT NOT NULL,
                    old_password TEXT,
                    username TEXT,
                    channel_username TEXT,
                    channel_id INTEGER,
                    email TEXT,
                    email_password TEXT,
                    status TEXT NOT NULL DEFAULT 'ready',
                    error TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    source_account_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(source_account_id) REFERENCES accounts(id)
                );
                """
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_admin(self, user_id: int) -> bool:
        with self._db() as db:
            return bool(db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone())

    def has_admins(self) -> bool:
        with self._db() as db:
            return bool(db.execute("SELECT 1 FROM admins LIMIT 1").fetchone())

    def add_admin(self, user_id: int) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (user_id,))

    def add_account(self, phone: str, session: str, old_password: str | None = None) -> int:
        with self._db() as db:
            db.execute(
                "INSERT INTO accounts(phone, session, old_password) VALUES (?, ?, ?) "
                "ON CONFLICT(phone) DO UPDATE SET session=excluded.session, old_password=excluded.old_password, "
                "status='ready', error=NULL, updated_at=CURRENT_TIMESTAMP",
                (phone, session, old_password or None),
            )
            row = db.execute("SELECT id FROM accounts WHERE phone = ?", (phone,)).fetchone()
            return int(row["id"])

    def accounts(self) -> list[sqlite3.Row]:
        with self._db() as db:
            return list(db.execute("SELECT * FROM accounts ORDER BY id DESC"))

    def account(self, account_id: int) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def projects(self) -> list[sqlite3.Row]:
        with self._db() as db:
            return list(
                db.execute(
                    "SELECT p.*, a.phone, a.username FROM projects p "
                    "JOIN accounts a ON a.id = p.source_account_id ORDER BY p.name"
                )
            )

    def project(self, project_id: int) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute(
                "SELECT p.*, a.phone, a.username, a.session FROM projects p "
                "JOIN accounts a ON a.id = p.source_account_id WHERE p.id = ?",
                (project_id,),
            ).fetchone()

    def add_project(self, name: str, source_account_id: int) -> int:
        with self._db() as db:
            db.execute(
                "INSERT INTO projects(name, source_account_id) VALUES (?, ?)",
                (name.strip(), source_account_id),
            )
            return int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def source_account_ids(self) -> set[int]:
        with self._db() as db:
            return {int(row["source_account_id"]) for row in db.execute("SELECT source_account_id FROM projects")}

    def update_account(self, account_id: int, **fields: object) -> None:
        allowed = {
            "old_password", "username", "channel_username", "channel_id", "email",
            "email_password", "status", "error", "session",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        clause = ", ".join(f"{key} = ?" for key in values)
        with self._db() as db:
            db.execute(
                f"UPDATE accounts SET {clause}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                (*values.values(), account_id),
            )

    def import_mailboxes(self, values: Iterable[tuple[str, str]]) -> tuple[int, int]:
        added = skipped = 0
        with self._db() as db:
            for address, password in values:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO mailboxes(address, password, account_id) VALUES (?, ?, NULL)",
                    (address.strip().lower(), password.strip()),
                )
                if cursor.rowcount:
                    added += 1
                else:
                    skipped += 1
        return added, skipped

    def reserve_mailbox(self, account_id: int) -> sqlite3.Row | None:
        with self._db() as db:
            old = db.execute("SELECT * FROM mailboxes WHERE account_id = ?", (account_id,)).fetchone()
            if old:
                return old
            row = db.execute(
                "SELECT * FROM mailboxes WHERE account_id IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE mailboxes SET account_id = ?, reserved_at=CURRENT_TIMESTAMP WHERE id = ? AND account_id IS NULL",
                (account_id, row["id"]),
            )
            if db.total_changes < 1:
                return None
            return db.execute("SELECT * FROM mailboxes WHERE id = ?", (row["id"],)).fetchone()

    def mailbox_count(self) -> tuple[int, int]:
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM mailboxes").fetchone()["n"]
            free = db.execute("SELECT COUNT(*) AS n FROM mailboxes WHERE account_id IS NULL").fetchone()["n"]
            return int(total), int(free)
