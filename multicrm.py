"""Register prepared accounts and durable story jobs in Multi CRM."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from types import SimpleNamespace
from pathlib import Path

from pyrogram import Client
from telethon import TelegramClient


MULTICRM_ROOT = Path(os.environ.get("MULTICRM_ROOT", "/root/FULL_CRM"))
MULTICRM_DB = MULTICRM_ROOT / "database.db"
MULTICRM_SESSIONS = MULTICRM_ROOT / "accounts"


async def _write_pyrogram_session(client: TelegramClient, api_id: int, api_hash: str, session_path: Path, user_id: int) -> None:
    """Atomically refresh MultiCRM's Pyrogram session from live Telethon auth.

    A converted file can become invalid when the account is re-authorized.
    Build and verify a separate file first; the live CRM file is replaced only
    after Pyrogram can read it and fetch the authenticated user.
    """
    MULTICRM_SESSIONS.mkdir(parents=True, exist_ok=True)
    temporary_base = session_path.with_name(f".{session_path.name}.refresh-{uuid.uuid4().hex}")
    # ``temporary_base`` starts with a dot and also contains dots from the
    # account name; appending is intentional here, unlike ``with_suffix``.
    temporary_file = Path(f"{temporary_base}.session")
    bridge = Client(str(temporary_base), api_id=api_id, api_hash=api_hash, no_updates=True)
    try:
        await bridge.connect()
        await bridge.storage.dc_id(client.session.dc_id)
        await bridge.storage.auth_key(client.session.auth_key.key)
        await bridge.storage.user_id(user_id)
        await bridge.storage.is_bot(False)
    finally:
        if bridge.is_connected:
            await bridge.disconnect()
    verifier = Client(str(temporary_base), api_id=api_id, api_hash=api_hash, no_updates=True)
    try:
        await verifier.start()
        me = await verifier.get_me()
        if int(me.id) != int(user_id):
            raise RuntimeError("MultiCRM session verification returned another account")
    finally:
        if verifier.is_connected:
            await verifier.stop()
    os.replace(temporary_file, session_path.with_suffix(".session"))


async def _register_account(client: TelegramClient, *, refresh_session: bool = False) -> tuple[object, str]:
    """Register a target once; never rewrite a session used by a live CRM app."""
    try:
        me = await client.get_me()
    except Exception:
        # New Telegram user constructors can temporarily be ahead of the
        # installed Telethon layer. The connected client still knows its own
        # numeric ID after authorization; that is sufficient to register the
        # Pyrogram session and story job without touching the profile again.
        self_id = getattr(client, "_self_id", None)
        if not self_id:
            raise
        me = SimpleNamespace(id=int(self_id), phone="", username="", first_name="", last_name="")
    owner_id = int(os.environ.get("MULTICRM_OWNER_ID", "888792903"))
    api_id = int(os.environ["MULTICRM_API_ID"])
    api_hash = os.environ["MULTICRM_API_HASH"]
    session_path = MULTICRM_SESSIONS / str(me.id)
    if refresh_session or not session_path.with_suffix(".session").exists():
        await _write_pyrogram_session(client, api_id, api_hash, session_path, me.id)

    name = " ".join(part for part in (me.first_name, me.last_name) if part).strip() or "Аккаунт"

    def register() -> None:
        with sqlite3.connect(MULTICRM_DB, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute(
                """
                INSERT INTO account(id, owner, phone, username, name, is_started, config)
                VALUES(?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner=excluded.owner, phone=excluded.phone, username=excluded.username,
                    name=excluded.name, is_started=1
                """,
                (me.id, owner_id, me.phone or "", me.username or "", name, json.dumps({"proxy": None})),
            )

    await asyncio.to_thread(register)
    return me, name


async def hand_off_selected_story_copy(
    client: TelegramClient,
    source_username: str,
    story_ids: tuple[int, ...],
    *,
    pinned_story_ids: tuple[int, ...] = (),
    already_completed_story_ids: tuple[int, ...] = (),
    interval_min_seconds: int = 40 * 60,
    interval_max_seconds: int = 60 * 60 + 59,
    # The project publication window is six hours.  Pinned copies must then
    # remain on the profile; selected_story_cloner verifies that separately.
    publish_duration_hours: int = 6,
    pin_copied_stories: bool = True,
) -> str:
    """Create one exact-ID story job owned and executed by Multi CRM.

    The setup bot never opens a second Pyrogram client for individual stories.
    This avoids SQLite session locks and makes retries/checkpoints durable.
    """
    source_username = source_username.lstrip("@").strip()
    selected = tuple(dict.fromkeys(int(value) for value in story_ids))
    # Telegram profile presentation is limited to the three source stories
    # designated as pins.  Never let a stale caller turn every selected story
    # into a profile pin.
    pins = tuple(dict.fromkeys(int(value) for value in pinned_story_ids))
    completed = tuple(story_id for story_id in dict.fromkeys(int(value) for value in already_completed_story_ids) if story_id in selected)
    if not source_username or not selected:
        raise RuntimeError("для выбранных историй нужен источник и непустой список ID")
    me, name = await _register_account(client, refresh_session=True)

    def register() -> None:
        with sqlite3.connect(MULTICRM_DB, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS selected_story_cloner (
                    account_id INTEGER PRIMARY KEY,
                    source_username TEXT NOT NULL,
                    story_ids TEXT NOT NULL,
                    pinned_story_ids TEXT NOT NULL DEFAULT '[]',
                    interval_min_seconds INTEGER NOT NULL,
                    interval_max_seconds INTEGER NOT NULL,
                    publish_duration_hours INTEGER NOT NULL,
                    completed_story_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    next_at INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS selected_story_delivery (
                    account_id INTEGER NOT NULL,
                    source_username TEXT NOT NULL,
                    source_story_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    target_story_id INTEGER,
                    detail TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(account_id, source_username, source_story_id)
                )
                """
            )
            # Legacy static copies are confirmed publications.  Seed them as
            # immutable checkpoints before a selected-story job ever runs.
            # This migration is deliberately insert-only: no newer state is
            # overwritten and no source story can be resent by accident.
            for story_id in completed:
                db.execute(
                    """
                    INSERT OR IGNORE INTO selected_story_delivery(
                        account_id, source_username, source_story_id, state, detail
                    ) VALUES (?, ?, ?, 'published', 'migrated from setup static copy')
                    """,
                    (me.id, source_username, story_id),
                )
            current = db.execute(
                "SELECT account_id FROM selected_story_cloner WHERE account_id = ?", (me.id,)
            ).fetchone()
            if not current:
                db.execute(
                    """
                    INSERT INTO selected_story_cloner(
                        account_id, source_username, story_ids, pinned_story_ids,
                        interval_min_seconds, interval_max_seconds, publish_duration_hours,
                        completed_story_ids, status, next_at, retry_count, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 'active', 0, 0, NULL)
                    """,
                    (me.id, source_username, json.dumps(list(selected)), json.dumps(list(pins)),
                     interval_min_seconds, interval_max_seconds, publish_duration_hours),
                )
            else:
                # Changing project settings never clears a delivery checkpoint
                # and never wakes a paused/uncertain job.  Explicit operator
                # review is required before any further publication.
                db.execute(
                    """
                    UPDATE selected_story_cloner
                    SET source_username=?, story_ids=?, pinned_story_ids=?,
                        interval_min_seconds=?, interval_max_seconds=?, publish_duration_hours=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE account_id=?
                    """,
                    (source_username, json.dumps(list(selected)), json.dumps(list(pins)),
                     interval_min_seconds, interval_max_seconds, publish_duration_hours, me.id),
                )
            # The generic worker may copy *future* stories from the project
            # source, but it must never decide profile pins. Exact selected
            # jobs own their three-pin policy.
            row = db.execute("SELECT config FROM account WHERE id=?", (me.id,)).fetchone()
            try:
                config = json.loads(row[0] or "{}") if row else {}
            except (TypeError, json.JSONDecodeError):
                config = {}
            config.update(
                {
                    "is_copying_enabled": False,
                    "copy_from_accounts": [],
                    "pin_copied_stories": False,
                }
            )
            # ``watch_external_accounts`` launches only rows marked started.
            # Set this here as well as in the initial upsert so an existing
            # stopped CRM account cannot silently retain a dormant queue.
            db.execute("UPDATE account SET config=?, is_started=1 WHERE id=?", (json.dumps(config), me.id))
            db.execute("DELETE FROM full_story_cloner WHERE account_id=?", (me.id,))

    await asyncio.to_thread(register)
    return name


async def hand_off_full_story_copy(
    client: TelegramClient,
    source_username: str,
    *,
    interval_min_minutes: int = 45,
    interval_max_minutes: int = 65,
    publish_duration_hours: int = 6,
    pin_copied_stories: bool = True,
) -> str:
    """Copy every public pinned story from one source, with a random pause.

    The source is resolved by the target CRM account, so no source-account
    session is needed. This mode replaces any selected- or future-story job
    for the target to prevent duplicate publications.
    """
    source_username = source_username.lstrip("@").strip()
    if not source_username:
        raise RuntimeError("не указан публичный источник для полного копирования историй")
    minimum = max(1, int(interval_min_minutes))
    maximum = max(minimum, int(interval_max_minutes))
    duration = max(1, int(publish_duration_hours))
    me, name = await _register_account(client, refresh_session=True)

    def register() -> None:
        with sqlite3.connect(MULTICRM_DB, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            row = db.execute("SELECT config FROM account WHERE id=?", (me.id,)).fetchone()
            try:
                config = json.loads(row[0] or "{}") if row else {}
            except (TypeError, json.JSONDecodeError):
                config = {}
            config.update(
                {
                    "is_copying_enabled": False,
                    "copy_from_accounts": [],
                    "full_story_interval_min_minutes": minimum,
                    "full_story_interval_max_minutes": maximum,
                    "full_story_pin_copied": bool(pin_copied_stories),
                }
            )
            db.execute("UPDATE account SET is_started=1, config=? WHERE id=?", (json.dumps(config), me.id))
            # This project intentionally replaces the exact-ID queue with a
            # complete public history. Remove its delivery records as well so
            # a later switch back to selected mode starts from an explicit
            # operator choice, not stale checkpoints.
            for table in ("selected_story_delivery", "selected_story_cloner"):
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if exists:
                    db.execute(f"DELETE FROM {table} WHERE account_id=?", (me.id,))
            existing = db.execute("SELECT id FROM full_story_cloner WHERE account_id=?", (me.id,)).fetchone()
            if existing:
                db.execute(
                    """UPDATE full_story_cloner
                       SET username=?, interval=?, is_copy_prohibited=0,
                           publish_duration=?, skip_stories_with_at=0, copied='[]'
                       WHERE account_id=?""",
                    (source_username, minimum, duration, me.id),
                )
            else:
                db.execute(
                    """INSERT INTO full_story_cloner(
                        account_id, username, interval, is_copy_prohibited,
                        publish_duration, skip_stories_with_at, copied
                    ) VALUES (?, ?, ?, 0, ?, 0, '[]')""",
                    (me.id, source_username, minimum, duration),
                )

    await asyncio.to_thread(register)
    return name


async def hand_off_future_story_copy(
    client: TelegramClient,
    source_username: str = "LocalTraffic",
    *,
    pin_copied_stories: bool = False,
) -> str:
    """Register an account and enable only the ordinary future-story copier.

    This deliberately removes any ``full_story_cloner`` task for the account:
    Multi CRM's normal ``story_cloner`` is the mode shown in the supplied
    settings screenshot and only tracks newly observed active stories.
    """
    source_username = source_username.lstrip("@").strip()
    if not source_username:
        raise RuntimeError("не указан источник будущих историй для Multi CRM")
    me, name = await _register_account(client)
    future_story_config = {
        "is_copying_enabled": True,
        "copy_from_accounts": [source_username],
        "is_copy_prohibited": False,
        "delete_if_original_deleted": True,
        "publish_duration": 6,
        # Keep profile visibility under the project policy.  Regular projects
        # preserve their prior behavior; iPhone's shared LocalTraffic feed
        # pins each completed copy automatically.
        "pin_copied_stories": bool(pin_copied_stories),
        "skip_stories_with_at": False,
        "copy_to_channel_enabled": False,
        "copy_channel_id": "",
    }

    def register() -> None:
        with sqlite3.connect(MULTICRM_DB, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            row = db.execute("SELECT config FROM account WHERE id = ?", (me.id,)).fetchone()
            try:
                config = json.loads(row[0] or "{}") if row else {}
            except (TypeError, json.JSONDecodeError):
                config = {}
            config.update(future_story_config)
            # _register_account has inserted this row first.  Updating it is
            # safer than another UPSERT that could accidentally blank owner.
            db.execute(
                "UPDATE account SET phone=?, username=?, name=?, is_started=1, config=? WHERE id=?",
                (me.phone or "", me.username or "", name, json.dumps(config, ensure_ascii=False), me.id),
            )
            # Do not allow an old archive-copy job to coexist with the requested
            # future-stories-only setup.
            db.execute("DELETE FROM full_story_cloner WHERE account_id = ?", (me.id,))

    await asyncio.to_thread(register)
    return name
