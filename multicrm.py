"""Hand completed accounts to the existing FULL_CRM story copier."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

from pyrogram import Client
from telethon import TelegramClient


MULTICRM_ROOT = Path(os.environ.get("MULTICRM_ROOT", "/root/FULL_CRM"))
MULTICRM_DB = MULTICRM_ROOT / "database.db"
MULTICRM_SESSIONS = MULTICRM_ROOT / "accounts"
MULTICRM_OWNER_ID = int(os.environ.get("MULTICRM_OWNER_ID", "888792903"))
MULTICRM_API_ID = int(os.environ["MULTICRM_API_ID"])
MULTICRM_API_HASH = os.environ["MULTICRM_API_HASH"]


async def hand_off_story_copy(client: TelegramClient, source_username: str) -> str:
    """Create a FULL_CRM account and its durable full-story task."""
    source_username = source_username.lstrip("@").strip()
    if not source_username:
        raise RuntimeError("не указан username источника для Multi CRM")
    me = await client.get_me()
    session_name = str(me.id)
    session_path = MULTICRM_SESSIONS / session_name
    MULTICRM_SESSIONS.mkdir(parents=True, exist_ok=True)

    bridge = Client(str(session_path), api_id=MULTICRM_API_ID, api_hash=MULTICRM_API_HASH, no_updates=True)
    try:
        await bridge.connect()
        await bridge.storage.dc_id(client.session.dc_id)
        await bridge.storage.auth_key(client.session.auth_key.key)
        await bridge.storage.user_id(me.id)
        await bridge.storage.is_bot(False)
    finally:
        if bridge.is_connected:
            await bridge.disconnect()

    name = " ".join(part for part in (me.first_name, me.last_name) if part).strip() or "Аккаунт"
    config = json.dumps({"proxy": None}, ensure_ascii=False)

    def register() -> None:
        with sqlite3.connect(MULTICRM_DB, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute(
                """
                INSERT INTO account(id, owner, phone, username, name, is_started, config)
                VALUES(?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    phone=excluded.phone, username=excluded.username,
                    name=excluded.name, is_started=1
                """,
                (me.id, MULTICRM_OWNER_ID, me.phone or "", me.username or "", name, config),
            )
            existing = db.execute("SELECT id FROM full_story_cloner WHERE account_id=?", (me.id,)).fetchone()
            if not existing:
                db.execute(
                    """
                    INSERT INTO full_story_cloner(account_id, username, interval, is_copy_prohibited,
                        publish_duration, skip_stories_with_at, copied)
                    VALUES(?, ?, 1, 0, 6, 0, '[]')
                    """,
                    (me.id, source_username),
                )

    await asyncio.to_thread(register)
    return name
