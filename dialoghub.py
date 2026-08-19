"""Connect newly prepared accounts to the local DialogHub CRM."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

from pyrogram import Client
from telethon import TelegramClient


DIALOGHUB_DB_PATH = Path(os.environ.get("DIALOGHUB_DB_PATH", "/opt/dialoghub/data/dialoghub.sqlite3"))
DIALOGHUB_SESSIONS_DIR = Path(os.environ.get("DIALOGHUB_SESSIONS_DIR", "/root/FULL_CRM/accounts"))

# The setup bot and DialogHub use slightly different project labels.  Keep the
# mapping explicit, so a new or mistyped project can never land in a wrong CRM
# folder.
PROJECT_ALIASES = {
    "АЙФОНЫ": "АЙФОНЫ",
    "ТРЕЙДИНГ": "ТРЕЙДИНГ",
    "ЗАКУПКИ": "ГОСЗАКУПКИ",
    "ГОСЗАКУПКИ": "ГОСЗАКУПКИ",
}


def _dialoghub_project_id(project_name: str) -> int:
    target_name = PROJECT_ALIASES.get(project_name.strip().upper())
    if not target_name:
        raise RuntimeError(f"для проекта «{project_name}» не настроено подключение к DialogHub")
    with sqlite3.connect(DIALOGHUB_DB_PATH) as db:
        row = db.execute(
            "SELECT id FROM projects WHERE lower(name)=lower(?)", (target_name,)
        ).fetchone()
    if not row:
        raise RuntimeError(f"проект «{target_name}» не найден в DialogHub")
    return int(row[0])


async def connect_to_dialoghub(
    client: TelegramClient,
    account_id: int,
    project_name: str,
    api_id: int,
    api_hash: str,
) -> str:
    """Persist the live Telethon account as a Pyrogram DialogHub session.

    DialogHub owns Pyrogram sessions, while the setup bot owns Telethon string
    sessions.  Both clients use the same Telegram API application, therefore
    the existing authorization key can be written directly to a fresh
    Pyrogram session without another login or a verification code.
    """
    project_id = await asyncio.to_thread(_dialoghub_project_id, project_name)
    DIALOGHUB_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    me = await client.get_me()
    title = " ".join(part for part in (me.first_name, me.last_name) if part).strip() or f"Аккаунт {account_id}"
    session_name = f"setup_account_{account_id}"
    session_path = DIALOGHUB_SESSIONS_DIR / session_name

    bootstrap = Client(str(session_path), api_id=api_id, api_hash=api_hash, no_updates=True)
    try:
        await bootstrap.connect()
        await bootstrap.storage.dc_id(client.session.dc_id)
        await bootstrap.storage.auth_key(client.session.auth_key.key)
        await bootstrap.storage.user_id(me.id)
        await bootstrap.storage.is_bot(False)
    finally:
        if bootstrap.is_connected:
            await bootstrap.disconnect()

    def register() -> None:
        with sqlite3.connect(DIALOGHUB_DB_PATH) as db:
            db.execute(
                """
                INSERT INTO accounts(session_name, project_id, title)
                VALUES(?, ?, ?)
                ON CONFLICT(session_name) DO UPDATE SET
                  project_id=excluded.project_id, title=excluded.title, enabled=1
                """,
                (session_name, project_id, title),
            )

    await asyncio.to_thread(register)
    restart = await asyncio.create_subprocess_exec("systemctl", "restart", "dialoghub")
    if await restart.wait() != 0:
        raise RuntimeError("не удалось перезапустить DialogHub после добавления аккаунта")
    return title
