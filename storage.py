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
                    name_emoji TEXT,
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
                    source_account_id INTEGER UNIQUE,
                    source_username TEXT UNIQUE,
                    username_base TEXT NOT NULL DEFAULT 'tgprofile',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(source_account_id) REFERENCES accounts(id),
                    CHECK(source_account_id IS NOT NULL OR source_username IS NOT NULL)
                );
                CREATE TABLE IF NOT EXISTS used_name_emojis (
                    emoji TEXT PRIMARY KEY,
                    account_id INTEGER,
                    source TEXT NOT NULL DEFAULT 'setup',
                    allocated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS project_name_emojis (
                    project_key TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    account_id INTEGER NOT NULL,
                    allocated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_key, emoji),
                    UNIQUE (account_id)
                );
                CREATE TABLE IF NOT EXISTS static_story_copies (
                    account_id INTEGER NOT NULL,
                    source_username TEXT NOT NULL,
                    source_story_id INTEGER NOT NULL,
                    target_story_id INTEGER NOT NULL,
                    copied_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, source_username, source_story_id)
                );
                CREATE TABLE IF NOT EXISTS story_upload_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    cadence_days INTEGER NOT NULL CHECK(cadence_days IN (1, 2)),
                    duration_seconds INTEGER NOT NULL DEFAULT 172800,
                    created_by INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                );
                CREATE TABLE IF NOT EXISTS story_upload_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    telegram_file_id TEXT NOT NULL,
                    media_kind TEXT NOT NULL CHECK(media_kind IN ('photo', 'video')),
                    file_name TEXT,
                    mime_type TEXT,
                    caption TEXT NOT NULL DEFAULT '',
                    video_duration INTEGER NOT NULL DEFAULT 0,
                    video_width INTEGER NOT NULL DEFAULT 0,
                    video_height INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'draft',
                    scheduled_at INTEGER,
                    retry_at INTEGER,
                    published_at INTEGER,
                    target_story_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(batch_id, position),
                    FOREIGN KEY(batch_id) REFERENCES story_upload_batches(id)
                );
                CREATE INDEX IF NOT EXISTS story_upload_due
                    ON story_upload_items(status, scheduled_at, id);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            if "name_emoji" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN name_emoji TEXT")
            project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
            if "source_username" not in project_columns:
                db.executescript(
                    """
                    CREATE TABLE projects_migrated (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        source_account_id INTEGER UNIQUE,
                        source_username TEXT UNIQUE,
                        username_base TEXT NOT NULL DEFAULT 'tgprofile',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(source_account_id) REFERENCES accounts(id),
                        CHECK(source_account_id IS NOT NULL OR source_username IS NOT NULL)
                    );
                    INSERT INTO projects_migrated(id, name, source_account_id, username_base, created_at)
                    SELECT id, name, source_account_id, username_base, created_at FROM projects;
                    DROP TABLE projects;
                    ALTER TABLE projects_migrated RENAME TO projects;
                    """
                )
            elif "username_base" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN username_base TEXT NOT NULL DEFAULT 'tgprofile'")
            story_item_columns = {row["name"] for row in db.execute("PRAGMA table_info(story_upload_items)")}
            if "retry_at" not in story_item_columns:
                db.execute("ALTER TABLE story_upload_items ADD COLUMN retry_at INTEGER")

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

    def claim_setting_lock(self, key: str, owner: str) -> bool:
        """Atomically claim a durable single-worker lock."""
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                (key, owner),
            )
            return cursor.rowcount == 1

    def release_setting_lock(self, key: str, owner: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM settings WHERE key = ? AND value = ?", (key, owner))

    def clear_setting_locks(self, prefix: str) -> None:
        """Clear process-local locks after a clean bot restart."""
        with self._db() as db:
            db.execute("DELETE FROM settings WHERE key LIKE ?", (f"{prefix}%",))

    def is_admin(self, user_id: int) -> bool:
        with self._db() as db:
            return bool(db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone())

    def has_admins(self) -> bool:
        with self._db() as db:
            return bool(db.execute("SELECT 1 FROM admins LIMIT 1").fetchone())

    def add_admin(self, user_id: int) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (user_id,))

    def first_admin_id(self) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT user_id FROM admins ORDER BY user_id LIMIT 1").fetchone()
            return int(row["user_id"]) if row else None

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
                    "LEFT JOIN accounts a ON a.id = p.source_account_id ORDER BY p.name"
                )
            )

    def project(self, project_id: int) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute(
                "SELECT p.*, a.phone, a.username, a.session FROM projects p "
                "LEFT JOIN accounts a ON a.id = p.source_account_id WHERE p.id = ?",
                (project_id,),
            ).fetchone()

    def add_account_project(self, name: str, source_account_id: int, username_base: str) -> int:
        with self._db() as db:
            db.execute(
                "INSERT INTO projects(name, source_account_id, username_base) VALUES (?, ?, ?)",
                (name.strip(), source_account_id, username_base.strip()),
            )
            return int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def add_public_project(self, name: str, source_username: str, username_base: str) -> int:
        with self._db() as db:
            db.execute(
                "INSERT INTO projects(name, source_username, username_base) VALUES (?, ?, ?)",
                (name.strip(), source_username.lstrip("@").strip(), username_base.strip()),
            )
            return int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def source_account_ids(self) -> set[int]:
        with self._db() as db:
            return {
                int(row["source_account_id"])
                for row in db.execute("SELECT source_account_id FROM projects WHERE source_account_id IS NOT NULL")
            }

    def used_name_emojis(self) -> set[str]:
        with self._db() as db:
            allocated = {row["emoji"] for row in db.execute("SELECT emoji FROM used_name_emojis")}
            allocated.update(
                row["name_emoji"] for row in db.execute("SELECT name_emoji FROM accounts WHERE name_emoji IS NOT NULL")
            )
            return allocated

    def reserve_name_emoji(self, account_id: int, candidates: Iterable[str]) -> str | None:
        """Atomically allocate one name emoji, never reusing an allocated value."""
        with self._db() as db:
            existing = db.execute(
                "SELECT emoji FROM used_name_emojis WHERE account_id = ?", (account_id,)
            ).fetchone()
            if existing:
                return str(existing["emoji"])
            account_emojis = {
                row["name_emoji"]
                for row in db.execute("SELECT name_emoji FROM accounts WHERE name_emoji IS NOT NULL")
            }
            for emoji in candidates:
                if emoji in account_emojis:
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO used_name_emojis(emoji, account_id, source) VALUES (?, ?, 'setup')",
                    (emoji, account_id),
                )
                if cursor.rowcount:
                    return emoji
            return None

    def reserve_project_name_emoji(
        self, project_key: str, account_id: int, candidates: Iterable[str]
    ) -> str | None:
        """Reserve a name emoji inside one project pool only.

        The legacy pool was global, so editing Dmitry Traffic's list silently
        changed every other project.  The pool key is deliberately explicit
        and persisted alongside the allocation.
        """
        with self._db() as db:
            existing = db.execute(
                "SELECT emoji FROM project_name_emojis WHERE account_id = ?", (account_id,)
            ).fetchone()
            if existing:
                return str(existing["emoji"])
            for emoji in candidates:
                normalized = emoji.strip()
                if not normalized:
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO project_name_emojis(project_key, emoji, account_id) VALUES (?, ?, ?)",
                    (project_key, normalized, account_id),
                )
                if cursor.rowcount:
                    return normalized
            return None

    def clear_project_name_emoji(self, account_id: int) -> None:
        """Release a project's prior cosmetic assignment before a full rebuild."""
        with self._db() as db:
            db.execute("DELETE FROM project_name_emojis WHERE account_id = ?", (account_id,))

    def reserve_known_name_emojis(self, emojis: Iterable[str], source: str = "inventory") -> int:
        """Record existing account-name emoji so future setup runs cannot reuse them."""
        added = 0
        with self._db() as db:
            for emoji in emojis:
                normalized = emoji.strip()
                if not normalized:
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO used_name_emojis(emoji, source) VALUES (?, ?)",
                    (normalized, source),
                )
                added += cursor.rowcount
        return added

    def reserved_usernames(self) -> set[str]:
        """Return every profile/channel username already allocated by this setup bot."""
        with self._db() as db:
            rows = db.execute(
                "SELECT username AS value FROM accounts WHERE username IS NOT NULL "
                "UNION SELECT channel_username AS value FROM accounts WHERE channel_username IS NOT NULL"
            )
            return {str(row["value"]).lower() for row in rows if row["value"]}

    def update_account(self, account_id: int, **fields: object) -> None:
        allowed = {
            "old_password", "username", "name_emoji", "channel_username", "channel_id", "email",
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

    def assign_mailbox(self, account_id: int, address: str, password: str) -> sqlite3.Row:
        """Assign a chosen mailbox to an account, returning any previous one to the pool."""
        normalized = address.strip().lower()
        with self._db() as db:
            db.execute("UPDATE mailboxes SET account_id = NULL WHERE account_id = ?", (account_id,))
            db.execute(
                "INSERT INTO mailboxes(address, password, account_id) VALUES (?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET password=excluded.password, account_id=excluded.account_id, reserved_at=CURRENT_TIMESTAMP",
                (normalized, password.strip(), account_id),
            )
            # The access card is built from the account record, not from the
            # mailbox pool.  Keep both stores in sync before a binding flow
            # can publish its final card.
            db.execute(
                "UPDATE accounts SET email = ?, email_password = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (normalized, password.strip(), account_id),
            )
            return db.execute("SELECT * FROM mailboxes WHERE address = ?", (normalized,)).fetchone()

    def copied_static_story_ids(self, account_id: int, source_username: str) -> set[int]:
        with self._db() as db:
            rows = db.execute(
                "SELECT source_story_id FROM static_story_copies WHERE account_id = ? AND source_username = ?",
                (account_id, source_username.lstrip("@").lower()),
            ).fetchall()
            return {int(row["source_story_id"]) for row in rows}

    def mark_static_story_copied(
        self, account_id: int, source_username: str, source_story_id: int, target_story_id: int
    ) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO static_story_copies(account_id, source_username, source_story_id, target_story_id) "
                "VALUES (?, ?, ?, ?)",
                (account_id, source_username.lstrip("@").lower(), source_story_id, target_story_id),
            )

    def create_story_upload_batch(self, account_id: int, cadence_days: int, created_by: int) -> int:
        """Create a draft batch. Files remain at Telegram until publication."""
        if cadence_days not in (1, 2):
            raise ValueError("cadence_days must be 1 or 2")
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO story_upload_batches(account_id, cadence_days, created_by) VALUES (?, ?, ?)",
                (account_id, cadence_days, created_by),
            )
            return int(cursor.lastrowid)

    def add_story_upload_item(
        self,
        batch_id: int,
        *,
        telegram_file_id: str,
        media_kind: str,
        file_name: str | None = None,
        mime_type: str | None = None,
        caption: str = "",
        video_duration: int = 0,
        video_width: int = 0,
        video_height: int = 0,
    ) -> int:
        if media_kind not in {"photo", "video"}:
            raise ValueError("only photo and video stories are supported")
        with self._db() as db:
            position = int(
                db.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM story_upload_items WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()["position"]
            )
            cursor = db.execute(
                """
                INSERT INTO story_upload_items(
                    batch_id, position, telegram_file_id, media_kind, file_name, mime_type, caption,
                    video_duration, video_width, video_height
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (batch_id, position, telegram_file_id, media_kind, file_name, mime_type, caption[:2048],
                 max(0, video_duration), max(0, video_width), max(0, video_height)),
            )
            return int(cursor.lastrowid)

    def story_upload_batch(self, batch_id: int) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute(
                """
                SELECT b.*, COUNT(i.id) AS item_count
                FROM story_upload_batches b LEFT JOIN story_upload_items i ON i.batch_id = b.id
                WHERE b.id = ? GROUP BY b.id
                """,
                (batch_id,),
            ).fetchone()

    def draft_story_upload_item_ids(self, batch_id: int) -> list[int]:
        with self._db() as db:
            return [
                int(row["id"])
                for row in db.execute(
                    "SELECT id FROM story_upload_items WHERE batch_id = ? AND status = 'draft' ORDER BY id",
                    (batch_id,),
                )
            ]

    def discard_story_upload_batch(self, batch_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM story_upload_items WHERE batch_id = ? AND status = 'draft'", (batch_id,))
            db.execute("DELETE FROM story_upload_batches WHERE id = ? AND status = 'draft'", (batch_id,))

    def start_random_story_upload_batch(
        self, batch_id: int, schedule: list[tuple[int, int]]
    ) -> sqlite3.Row | None:
        """Activate one draft with a fixed random order and publication plan.

        ``schedule`` is persisted before the scheduler sees the batch. A
        restart therefore cannot reshuffle the content or change its times.
        """
        with self._db() as db:
            batch = db.execute("SELECT * FROM story_upload_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch or batch["status"] != "draft":
                return None
            draft_ids = [
                int(row["id"])
                for row in db.execute(
                    "SELECT id FROM story_upload_items WHERE batch_id = ? AND status = 'draft'", (batch_id,)
                )
            ]
            planned_ids = [item_id for item_id, _scheduled_at in schedule]
            if not draft_ids or len(draft_ids) != len(schedule) or set(draft_ids) != set(planned_ids):
                return None
            db.execute(
                "UPDATE story_upload_batches SET status = 'active', started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (batch_id,),
            )
            for item_id, scheduled_at in schedule:
                db.execute(
                    """
                    UPDATE story_upload_items
                    SET status = 'queued', scheduled_at = ?, retry_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND batch_id = ? AND status = 'draft'
                    """,
                    (scheduled_at, item_id, batch_id),
                )
            return db.execute("SELECT * FROM story_upload_batches WHERE id = ?", (batch_id,)).fetchone()

    def reset_story_upload_sending(self) -> None:
        """A stopped process may leave one claimed item; retry it with the same idempotency key."""
        with self._db() as db:
            db.execute(
                "UPDATE story_upload_items SET status = 'queued', retry_at = 0, updated_at = CURRENT_TIMESTAMP WHERE status = 'sending'"
            )

    def claim_due_story_upload(self, now: int) -> sqlite3.Row | None:
        """Atomically reserve one due item so the scheduler never publishes in parallel."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT i.*, b.account_id, b.duration_seconds, a.session
                FROM story_upload_items i
                JOIN story_upload_batches b ON b.id = i.batch_id
                JOIN accounts a ON a.id = b.account_id
                WHERE i.status = 'queued' AND i.scheduled_at <= ?
                  AND (i.retry_at IS NULL OR i.retry_at <= ?)
                  AND b.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM story_upload_items earlier
                    WHERE earlier.batch_id = i.batch_id
                      AND earlier.status IN ('queued', 'sending')
                      AND (
                        earlier.scheduled_at < i.scheduled_at
                        OR (earlier.scheduled_at = i.scheduled_at AND earlier.id < i.id)
                      )
                  )
                ORDER BY i.scheduled_at, i.id LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if not row:
                return None
            changed = db.execute(
                "UPDATE story_upload_items SET status = 'sending', attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'queued'",
                (row["id"],),
            ).rowcount
            if not changed:
                return None
            return row

    def mark_story_upload_published(self, item_id: int, target_story_id: int, now: int) -> None:
        with self._db() as db:
            db.execute(
                """
                UPDATE story_upload_items
                SET status = 'published', published_at = ?, target_story_id = ?, retry_at = NULL, last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (now, target_story_id, item_id),
            )

    def retry_story_upload(self, item_id: int, error: str, retry_at: int) -> None:
        with self._db() as db:
            db.execute(
                """
                UPDATE story_upload_items
                SET status = 'queued', retry_at = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (retry_at, error[:900], item_id),
            )

    def story_upload_summary(self, account_id: int) -> list[sqlite3.Row]:
        with self._db() as db:
            return list(
                db.execute(
                    """
                    SELECT b.id, b.cadence_days, b.status, COUNT(i.id) AS total,
                           SUM(i.status = 'published') AS published,
                           MIN(CASE WHEN i.status = 'queued' THEN i.scheduled_at END) AS next_at
                    FROM story_upload_batches b LEFT JOIN story_upload_items i ON i.batch_id = b.id
                    WHERE b.account_id = ? GROUP BY b.id ORDER BY b.id DESC LIMIT 5
                    """,
                    (account_id,),
                )
            )

    def mailbox_count(self) -> tuple[int, int]:
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM mailboxes").fetchone()["n"]
            free = db.execute("SELECT COUNT(*) AS n FROM mailboxes WHERE account_id IS NULL").fetchone()["n"]
            return int(total), int(free)
