from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import random
import re
import tempfile
from pathlib import Path

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon import functions, types
from telethon.errors import SessionPasswordNeededError

from storage import Store
from telegram_ops import (
    _copy_channel_posts,
    copy_stories_in_background,
    client_from_session,
    clone_profile_and_channel,
    set_password_and_email,
    set_password_only,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg-account-setup")

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(item) for item in os.environ.get("ADMIN_IDS", "").split(",") if item.strip().isdigit()}
STORY_INTERVAL = max(0, int(os.environ.get("STORY_INTERVAL_MINUTES", "15")))
BUSINESS_EMOJIS = ("💼", "📊", "📈", "📌", "📋", "🗂️", "🗓️", "📝", "🤝", "🏢", "💻", "📎", "🔖", "🧩", "🛠️")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Заполните API_ID, API_HASH и BOT_TOKEN в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
store = Store()
state: dict[int, tuple[str, int | None]] = {}
qr_flows: dict[int, dict] = {}
email_code_waiters: dict[int, asyncio.Future[str]] = {}
jobs: dict[int, asyncio.Task] = {}
story_jobs: set[asyncio.Task] = set()


def kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows]
    )


def home_kb() -> InlineKeyboardMarkup:
    return kb(
        ("➕ Добавить аккаунт по QR", "add_qr"),
        ("📦 Оформить аккаунт", "accounts"),
        ("📁 Проекты-источники", "projects"),
        ("⚙️ Источник и настройки", "settings"),
        ("📧 Загрузить почты", "mail_import"),
        ("📊 Статусы", "status"),
    )


def back() -> InlineKeyboardMarkup:
    return kb(("◀️ В меню", "home"))


def email_wait_kb(account_id: int) -> InlineKeyboardMarkup:
    return kb(("⏭️ Пропустить привязку почты", f"skip_email:{account_id}"), ("◀️ В меню", "home"))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or store.is_admin(user_id)


async def ensure_admin(message: Message | CallbackQuery) -> bool:
    user = message.from_user
    if not user:
        return False
    if is_admin(user.id):
        return True
    # A new private bot has no public audience. Bootstrap permits a zero-config first launch,
    # while ADMIN_IDS in .env remains the recommended production lock.
    if not ADMIN_IDS and not store.has_admins():
        store.add_admin(user.id)
        return True
    target = message.message if isinstance(message, CallbackQuery) else message
    await target.answer("Нет доступа.")
    return False


async def show_home(message: Message, text: str = "TG Account Setup") -> None:
    await message.answer(text, reply_markup=home_kb())


async def safe_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup)


async def progress_message(chat_id: int, text: str) -> None:
    await bot.send_message(chat_id, text)


def schedule_story_copy(chat_id: int, account_id: int, target_session: str, project, clear_existing: bool = False) -> None:
    async def progress(text: str) -> None:
        await progress_message(chat_id, f"Аккаунт №{account_id}: {text}")

    task = asyncio.create_task(
        copy_stories_in_background(
            target_session, project["session"] if project["source_account_id"] else None,
            project["source_username"], API_ID, API_HASH, progress, STORY_INTERVAL, clear_existing,
        )
    )
    story_jobs.add(task)
    task.add_done_callback(story_jobs.discard)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not await ensure_admin(message):
        return
    await show_home(message, "TG Account Setup\n\nВыберите действие.")


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    state.pop(callback.from_user.id, None)
    await safe_edit(callback, "TG Account Setup\n\nВыберите действие.", home_kb())


@dp.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    report = store.get_setting("report_channel", "не задан")
    password = "задан" if store.get_setting("new_password") else "не задан"
    await safe_edit(
        callback,
        "Общие настройки\n\n"
        "Источник выбирается через проект: у проекта один привязанный аккаунт, "
        "а его личный канал определяется автоматически.\n\n"
        f"Канал «Telegram доступ»: {report}\n"
        f"Формат доступов: автоматически из оформленного аккаунта\nНовый пароль: {password}\n\n"
        "Основа username задаётся для каждого проекта отдельно; к ней добавляются случайные буквы, слова и 3–5 цифр.\n"
        "К имени каждого нового аккаунта добавляется уникальный деловой эмодзи.",
        kb(
            ("📬 Канал с доступами", "set_report_channel"),
            ("🔐 Новый пароль", "set_new_password"),
            ("◀️ В меню", "home"),
        ),
    )


SETTING_ACTIONS = {
    "set_report_channel": ("report_channel", "Пришлите @username или ID закрытого канала «Telegram доступ», либо перешлите из него любое сообщение. Бот должен быть в нём администратором."),
    "set_new_password": ("new_password", "Пришлите новый пароль Telegram 2FA. Сообщение будет удалено после сохранения."),
}


@dp.callback_query(F.data == "projects")
async def projects(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    rows = store.projects()
    lines = ["Проекты-источники\n\nДля каждого проекта один раз привяжите аккаунт-источник. Его профиль и личный канал будут использоваться автоматически."]
    if rows:
        lines += [
            f"• {row['name']} — {'@' + row['source_username'] if row['source_username'] else 'аккаунт ' + row['phone']} "
            f"— @{row['username_base']}…"
            for row in rows
        ]
    else:
        lines.append("\nПроектов пока нет.")
    await safe_edit(callback, "\n".join(lines), kb(("➕ Открытый источник по @username", "project_public"), ("🔐 Источник через QR-аккаунт", "project_account_add"), ("◀️ В меню", "home")))


@dp.callback_query(F.data == "project_account_add")
async def project_account_add(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    used = store.source_account_ids()
    rows = [row for row in store.accounts() if row['id'] not in used]
    if not rows:
        await safe_edit(callback, "Нет свободных аккаунтов. Сначала добавьте аккаунт-источник через QR.", back())
        return
    buttons = [[InlineKeyboardButton(text=row['phone'], callback_data=f"project_source:{row['id']}")] for row in rows[:40]]
    buttons.append([InlineKeyboardButton(text="◀️ К проектам", callback_data="projects")])
    await safe_edit(callback, "Выберите аккаунт-источник для нового проекта:", InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "project_public")
async def project_public(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    state[callback.from_user.id] = ("project_public_username", None)
    await safe_edit(callback, "Пришлите открытый @username профиля-источника. Его личный канал тоже должен быть доступен публично.", back())


@dp.callback_query(F.data.startswith("project_source:"))
async def project_source(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    account_id = int(callback.data.split(":", 1)[1])
    state[callback.from_user.id] = ("project_name", account_id)
    await safe_edit(callback, "Пришлите название проекта. Например: «Магазин А».", back())


@dp.callback_query(F.data.in_(SETTING_ACTIONS))
async def request_setting(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    key, prompt = SETTING_ACTIONS[callback.data]
    state[callback.from_user.id] = (f"setting:{key}", None)
    await safe_edit(callback, prompt, back())


@dp.callback_query(F.data == "mail_import")
async def mail_import(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    state[callback.from_user.id] = ("mail_import", None)
    await safe_edit(
        callback,
        "Пришлите .txt файл или текст со строками вида:\nemail@example.com:пароль\n\n"
        "Почты сохраняются в локальной базе бота и выдаются по одной на аккаунт.",
        back(),
    )


@dp.callback_query(F.data == "add_qr")
async def add_qr(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    user_id = callback.from_user.id
    await cleanup_qr(user_id)
    await safe_edit(callback, "Создаю QR-код для входа…")
    client = client_from_session("", API_ID, API_HASH)
    try:
        await client.connect()
        login = await client.qr_login()
        image = qrcode.make(login.url)
        path = Path(tempfile.gettempdir()) / f"tg_setup_qr_{user_id}.png"
        image.save(path)
        photo = await bot.send_photo(
            callback.message.chat.id,
            FSInputFile(path),
            caption="Откройте Telegram → Настройки → Устройства → Подключить устройство.\n"
            "После сканирования бот сам сохранит аккаунт.",
        )
        task = asyncio.create_task(wait_qr(callback.message.chat.id, user_id, client, login, photo.message_id, path))
        qr_flows[user_id] = {"client": client, "task": task, "path": path}
        await callback.message.answer("QR отправлен. Ожидаю подтверждение до 2 минут…", reply_markup=back())
    except Exception as exc:
        await client.disconnect()
        await callback.message.answer(f"Не удалось создать QR: {exc}", reply_markup=back())


async def cleanup_qr(user_id: int) -> None:
    flow = qr_flows.pop(user_id, None)
    if not flow:
        return
    task = flow.get("task")
    if task and not task.done():
        task.cancel()
    with contextlib.suppress(Exception):
        await flow["client"].disconnect()
    with contextlib.suppress(OSError):
        Path(flow["path"]).unlink()


async def finish_authorized(chat_id: int, user_id: int, client, old_password: str | None = None) -> None:
    me = await client.get_me()
    session = client.session.save()
    account_id = store.add_account(me.phone or str(me.id), session, old_password)
    await client.disconnect()
    await progress_message(chat_id, f"✅ Аккаунт {me.phone or me.id} добавлен (№ {account_id}).",)
    await show_home_message(chat_id)


async def show_home_message(chat_id: int) -> None:
    await bot.send_message(chat_id, "Выберите действие.", reply_markup=home_kb())


async def wait_qr(chat_id: int, user_id: int, client, login, photo_id: int, path: Path) -> None:
    try:
        await asyncio.wait_for(login.wait(), timeout=120)
        await finish_authorized(chat_id, user_id, client)
    except SessionPasswordNeededError:
        qr_flows[user_id] = {"client": client, "task": asyncio.current_task(), "path": path}
        state[user_id] = ("qr_password", None)
        await progress_message(chat_id, "QR подтверждён. Пришлите текущий пароль 2FA для этого аккаунта.")
        return
    except asyncio.TimeoutError:
        await progress_message(chat_id, "QR-код истёк. Запустите вход ещё раз.")
    except Exception as exc:
        log.exception("QR login failed")
        await progress_message(chat_id, f"Не удалось завершить QR-вход: {exc}")
    finally:
        flow = qr_flows.get(user_id)
        if flow and flow.get("client") is client and state.get(user_id, ("", None))[0] != "qr_password":
            qr_flows.pop(user_id, None)
            with contextlib.suppress(Exception):
                await client.disconnect()
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id, photo_id)


@dp.callback_query(F.data == "accounts")
async def accounts(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    rows = store.accounts()
    if not rows:
        await safe_edit(callback, "Аккаунтов пока нет. Добавьте их через QR.", back())
        return
    buttons = []
    source_ids = store.source_account_ids()
    for row in rows[:40]:
        if row['id'] in source_ids:
            continue
        label = f"{row['phone']} — {row['status']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"account:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="home")])
    await safe_edit(callback, "Выберите аккаунт для оформления:", InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("account:"))
async def account_menu(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    account_id = int(callback.data.split(":", 1)[1])
    row = store.account(account_id)
    if not row:
        await safe_edit(callback, "Аккаунт не найден.", back())
        return
    await safe_edit(
        callback,
        f"Аккаунт №{account_id}\nТелефон: {row['phone']}\nСтатус: {row['status']}\n"
        f"Username: @{row['username'] or '—'}\nИмя: {row['name_emoji'] or '—'}\nКанал: @{row['channel_username'] or '—'}\n"
        f"Почта: {row['email'] or '—'}",
        kb(
            ("▶️ Запустить оформление", f"run:{account_id}"),
            ("🔐 Указать старый пароль", f"oldpass:{account_id}"),
            ("◀️ К аккаунтам", "accounts"),
        ),
    )


@dp.callback_query(F.data.startswith("oldpass:"))
async def request_old_password(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    account_id = int(callback.data.split(":", 1)[1])
    state[callback.from_user.id] = ("old_password", account_id)
    await safe_edit(callback, "Пришлите текущий пароль 2FA. Сообщение будет удалено.", back())


def setup_ready() -> str | None:
    required = ("report_channel", "new_password")
    missing = [name for name in required if not store.get_setting(name)]
    return ", ".join(missing) if missing else None


@dp.callback_query(F.data.startswith("run:"))
async def run_account(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    account_id = int(callback.data.split(":", 1)[1])
    missing = setup_ready()
    if missing:
        await callback.answer(f"Сначала заполните: {missing}", show_alert=True)
        return
    if account_id in jobs and not jobs[account_id].done():
        await callback.answer("Этот аккаунт уже оформляется.", show_alert=True)
        return
    if not store.account(account_id):
        return
    projects = store.projects()
    if not projects:
        await callback.answer("Сначала создайте проект и привяжите аккаунт-источник.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=row['name'], callback_data=f"runproject:{account_id}:{row['id']}")] for row in projects]
    buttons.append([InlineKeyboardButton(text="◀️ К аккаунтам", callback_data="accounts")])
    await safe_edit(callback, "Выберите проект для оформления:", InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("runproject:"))
async def run_project(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    _, account_id_raw, project_id_raw = callback.data.split(":")
    account_id, project_id = int(account_id_raw), int(project_id_raw)
    if account_id in jobs and not jobs[account_id].done():
        await callback.answer("Этот аккаунт уже оформляется.", show_alert=True)
        return
    if not store.account(account_id) or not store.project(project_id):
        await callback.answer("Аккаунт или проект не найден.", show_alert=True)
        return
    mailbox = store.reserve_mailbox(account_id)
    if not mailbox:
        await callback.answer("Нет свободных почт. Загрузите базу почт.", show_alert=True)
        return
    store.update_account(
        account_id,
        status="оформляется",
        error=None,
        email=mailbox["address"],
        email_password=mailbox["password"],
    )
    await safe_edit(callback, f"Аккаунт №{account_id} запущен. Отправлю статус по этапам.", back())
    jobs[account_id] = asyncio.create_task(process_account(callback.message.chat.id, callback.from_user.id, account_id, project_id))


async def process_account(chat_id: int, admin_id: int, account_id: int, project_id: int) -> None:
    row = store.account(account_id)
    if not row:
        return
    client = client_from_session(row["session"], API_ID, API_HASH)
    project = store.project(project_id)
    if not project:
        return
    source_client = client_from_session(project["session"], API_ID, API_HASH) if project["source_account_id"] else client
    store.set_setting(f"active_project:{account_id}", str(project_id))
    name_emoji = row["name_emoji"] or next(
        (emoji for emoji in random.sample(BUSINESS_EMOJIS, len(BUSINESS_EMOJIS)) if emoji not in store.used_name_emojis()),
        random.choice(BUSINESS_EMOJIS),
    )
    store.update_account(account_id, name_emoji=name_emoji)

    async def progress(text: str) -> None:
        await progress_message(chat_id, f"Аккаунт №{account_id}: {text}")

    try:
        await client.connect()
        if source_client is not client:
            await source_client.connect()
        me = await client.get_me()
        if not getattr(me, "premium", False):
            raise RuntimeError("на аккаунте не найден Telegram Premium")
        result = await clone_profile_and_channel(
            client,
            source_client,
            project["source_username"],
            project["username_base"],
            name_emoji,
            STORY_INTERVAL,
            progress,
        )
        store.update_account(account_id, **result)
        schedule_story_copy(chat_id, account_id, row["session"], project)
        await progress("Истории копируются в фоне — продолжаю привязку почты")
        await bot.send_message(
            chat_id,
            f"Аккаунт №{account_id}: откройте почту {row['email']} и пришлите сюда код Telegram.\n"
            f"Пароль почты: {row['email_password']}",
            reply_markup=email_wait_kb(account_id),
        )
        async def code_provider(_code_length: int = 0) -> str:
            return await asyncio.wait_for(future, timeout=15 * 60)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        email_code_waiters[admin_id] = future
        state[admin_id] = ("email_code", account_id)
        await set_password_and_email(client, row["old_password"], store.get_setting("new_password"), row["email"], code_provider)
        store.update_account(account_id, old_password=store.get_setting("new_password"))
        store.update_account(account_id, status="готов", error=None)
        final = store.account(account_id)
        await publish_result(chat_id, final, project["name"])
    except Exception as exc:
        log.exception("Setup failed for account %s", account_id)
        store.update_account(account_id, status="ошибка", error=str(exc)[:900])
        await progress(f"Ошибка: {exc}")
    finally:
        email_code_waiters.pop(admin_id, None)
        if state.get(admin_id) == ("email_code", account_id):
            state.pop(admin_id, None)
        with contextlib.suppress(Exception):
            await client.disconnect()
        if source_client is not client:
            with contextlib.suppress(Exception):
                await source_client.disconnect()


async def resume_existing_account(chat_id: int, admin_id: int, account_id: int, project_id: int) -> None:
    """One-time continuation for an interrupted setup without creating another channel."""
    row = store.account(account_id)
    project = store.project(project_id)
    if not row or not project or not row["channel_username"]:
        return
    client = client_from_session(row["session"], API_ID, API_HASH)

    async def progress(text: str) -> None:
        await progress_message(chat_id, f"Аккаунт №{account_id}: {text}")

    try:
        store.update_account(account_id, status="оформляется", error=None)
        await client.connect()
        source_profile = await client.get_entity(project["source_username"])
        source_full = await client(functions.users.GetFullUserRequest(source_profile))
        source_channel_id = getattr(source_full.full_user, "personal_channel_id", None)
        source_channel = next((item for item in source_full.chats if getattr(item, "id", None) == source_channel_id), None)
        if not source_channel:
            raise RuntimeError("Не найден личный канал источника")
        channel = await client.get_entity(f"@{row['channel_username']}")
        await progress("Очищаю и заново копирую посты с форматированием")
        old_posts = [message.id async for message in client.iter_messages(channel) if not message.action]
        if old_posts:
            await client(functions.channels.DeleteMessagesRequest(channel=channel, id=old_posts))
        await _copy_channel_posts(client, client, source_channel, channel, progress)

        schedule_story_copy(chat_id, account_id, row["session"], project, clear_existing=True)
        await progress("Истории копируются в фоне — продолжаю привязку почты")

        await progress(f"Откройте почту {row['email']} и пришлите сюда код Telegram.\nПароль почты: {row['email_password']}")
        async def code_provider(_code_length: int = 0) -> str:
            return await asyncio.wait_for(future, timeout=15 * 60)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        email_code_waiters[admin_id] = future
        state[admin_id] = ("email_code", account_id)
        await set_password_and_email(client, store.get_setting("new_password"), store.get_setting("new_password"), row["email"], code_provider)
        store.update_account(account_id, old_password=store.get_setting("new_password"))
        store.update_account(account_id, status="готов", error=None)
        await publish_result(chat_id, store.account(account_id), project["name"])
    except Exception as exc:
        log.exception("Resume failed for account %s", account_id)
        store.update_account(account_id, status="ошибка", error=str(exc)[:900])
        await progress(f"Ошибка: {exc}")
    finally:
        email_code_waiters.pop(admin_id, None)
        if state.get(admin_id) == ("email_code", account_id):
            state.pop(admin_id, None)
        with contextlib.suppress(Exception):
            await client.disconnect()


async def bind_existing_email(chat_id: int, admin_id: int, account_id: int, project_id: int) -> None:
    """Bind the already assigned mailbox to an existing account and wait for a user-supplied code."""
    row, project = store.account(account_id), store.project(project_id)
    if not row or not project or not row["email"] or not row["email_password"]:
        return
    client = client_from_session(row["session"], API_ID, API_HASH)
    try:
        store.update_account(account_id, status="оформляется", error=None)
        await client.connect()
        with contextlib.suppress(Exception):
            await client(functions.account.CancelPasswordEmailRequest())
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        email_code_waiters[admin_id] = future
        state[admin_id] = ("email_code", account_id)
        await progress_message(
            chat_id,
            f"Аккаунт №{account_id}: привязываю новую почту.\n\n"
            f"Откройте: {row['email']}\nПароль: {row['email_password']}\n\n"
            "Когда придёт письмо Telegram, пришлите код сюда одним сообщением.",
        )
        await bot.send_message(chat_id, "Если код не приходит, можно продолжить без привязки почты.", reply_markup=email_wait_kb(account_id))

        async def code_provider(_code_length: int = 0) -> str:
            return await asyncio.wait_for(future, timeout=15 * 60)

        password = store.get_setting("new_password")
        await set_password_and_email(client, password, password, row["email"], code_provider)
        store.update_account(account_id, old_password=password, status="готов", error=None)
        await publish_result(chat_id, store.account(account_id), project["name"])
    except Exception as exc:
        log.exception("Email binding failed for account %s", account_id)
        store.update_account(account_id, status="ошибка", error=str(exc)[:900])
        await progress_message(chat_id, f"Аккаунт №{account_id}: ошибка привязки почты: {exc}")
    finally:
        email_code_waiters.pop(admin_id, None)
        if state.get(admin_id) == ("email_code", account_id):
            state.pop(admin_id, None)
        with contextlib.suppress(Exception):
            await client.disconnect()


async def skip_email_binding(chat_id: int, admin_id: int, account_id: int) -> None:
    """Cancel the pending email confirmation and finish the account without it."""
    row = store.account(account_id)
    project_id = int(store.get_setting(f"active_project:{account_id}", "0") or 0)
    project = store.project(project_id)
    if not row or not project:
        await bot.send_message(chat_id, "Не удалось определить проект для этого аккаунта.")
        return
    future = email_code_waiters.get(admin_id)
    if future and not future.done():
        future.cancel()
    client = client_from_session(row["session"], API_ID, API_HASH)
    try:
        await client.connect()
        with contextlib.suppress(Exception):
            await client(functions.account.CancelPasswordEmailRequest())
        await set_password_only(client, row["old_password"] or store.get_setting("new_password"), store.get_setting("new_password"))
        store.update_account(account_id, old_password=store.get_setting("new_password"), status="готов", error=None)
        await publish_result(chat_id, store.account(account_id), project["name"])
    except Exception as exc:
        store.update_account(account_id, status="ошибка", error=str(exc)[:900])
        await bot.send_message(chat_id, f"Не удалось пропустить привязку почты: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


@dp.callback_query(F.data.startswith("skip_email:"))
async def skip_email(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    account_id = int(callback.data.split(":", 1)[1])
    await callback.answer("Пропускаю привязку почты…")
    await callback.message.edit_reply_markup(reply_markup=None)
    asyncio.create_task(skip_email_binding(callback.message.chat.id, callback.from_user.id, account_id))


def display_phone(phone: str | None) -> str:
    digits = "".join(char for char in (phone or "") if char.isdigit())
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}"
    return f"+{digits}" if digits else "—"


async def actual_account_identity(row) -> tuple[str, str]:
    client = client_from_session(row["session"], API_ID, API_HASH)
    try:
        await client.connect()
        me = await client.get_me()
        name = " ".join(part for part in (me.first_name, me.last_name) if part).strip() or "Аккаунт"
        return name, me.username or row["username"] or "—"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def publish_result(chat_id: int, row, project_name: str) -> None:
    name, username = await actual_account_identity(row)
    card = (
        f"{name} ({project_name.lower()})\n@{username}\n\n"
        f"логин: {display_phone(row['phone'])}\n"
        f"пароль: {store.get_setting('new_password')}\n\n"
        f"почта для входа: {row['email'] or '—'}\n"
        f"пароль: {row['email_password'] or '—'}"
    )
    await bot.send_message(chat_id, "✅ Аккаунт оформлен.\n\n" + card)
    report = store.get_setting("report_channel")
    try:
        await bot.send_message(report, card)
    except Exception:
        log.exception("Could not publish access card to the configured channel")
        await bot.send_message(chat_id, "⚠️ Карточка не отправлена в канал доступов: проверьте, что бот в нём администратор с правом публикации.")


def parse_mailboxes(text: str) -> list[tuple[str, str]]:
    parsed = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        address, password = line.split(":", 1)
        if "@" in address and password.strip():
            parsed.append((address.strip(), password.strip()))
    return parsed


@dp.message(F.document)
async def file_input(message: Message) -> None:
    if not await ensure_admin(message) or state.get(message.from_user.id, ("", None))[0] != "mail_import":
        return
    stream = io.BytesIO()
    await bot.download(message.document, destination=stream)
    await import_mailboxes(message, stream.getvalue().decode("utf-8", errors="replace"))


@dp.message(F.text)
async def text_input(message: Message) -> None:
    if not await ensure_admin(message):
        return
    current = state.get(message.from_user.id)
    if not current:
        return
    kind, account_id = current
    value = message.text.strip()
    if kind.startswith("setting:"):
        key = kind.split(":", 1)[1]
        if key == "report_channel" and message.forward_from_chat:
            value = str(message.forward_from_chat.id)
        store.set_setting(key, value)
        if key == "new_password":
            with contextlib.suppress(Exception):
                await message.delete()
        state.pop(message.from_user.id, None)
        await show_home(message, "✅ Настройка сохранена.")
    elif kind == "project_name" and account_id:
        state[message.from_user.id] = (f"project_username_base:{value}", account_id)
        await message.answer(
            "Теперь пришлите основу username именно для этого проекта — одно короткое слово латиницей, без @ и пробелов.\n"
            "Например: mebel, appiphone или brandshop.",
            reply_markup=back(),
        )
    elif kind.startswith("project_username_base:") and account_id:
        project_name = kind.split(":", 1)[1]
        if not re.fullmatch(r"[A-Za-z0-9_]{3,18}", value):
            await message.answer("Используйте 3–18 латинских букв, цифр или _. Например: mebel, appiphone, brand_shop.")
            return
        try:
            project_id = store.add_account_project(project_name, account_id, value)
        except Exception as exc:
            await message.answer(f"Не удалось создать проект: {exc}")
            return
        state.pop(message.from_user.id, None)
        await message.answer(f"✅ Проект №{project_id} создан. Источник будет браться из этого аккаунта автоматически.", reply_markup=home_kb())
    elif kind == "project_public_username":
        username = value.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await message.answer("Пришлите корректный публичный username, например @brandshop.")
            return
        state[message.from_user.id] = (f"project_public_name:{username}", None)
        await message.answer("Пришлите название проекта.", reply_markup=back())
    elif kind.startswith("project_public_name:"):
        source_username = kind.split(":", 1)[1]
        state[message.from_user.id] = (f"project_public_base:{source_username}:{value}", None)
        await message.answer("Пришлите основу username для нового аккаунта: 3–18 латинских букв, цифр или _. Например: brandshop.", reply_markup=back())
    elif kind.startswith("project_public_base:"):
        _, source_username, project_name = kind.split(":", 2)
        if not re.fullmatch(r"[A-Za-z0-9_]{3,18}", value):
            await message.answer("Используйте 3–18 латинских букв, цифр или _. Например: mebel, appiphone, brand_shop.")
            return
        try:
            project_id = store.add_public_project(project_name, source_username, value)
        except Exception as exc:
            await message.answer(f"Не удалось создать проект: {exc}")
            return
        state.pop(message.from_user.id, None)
        await message.answer(f"✅ Проект №{project_id} создан. Источник @{source_username} будет использоваться без входа в него.", reply_markup=home_kb())
    elif kind == "mail_import":
        await import_mailboxes(message, value)
    elif kind == "qr_password":
        flow = qr_flows.pop(message.from_user.id, None)
        if not flow:
            await message.answer("QR-сессия уже недоступна. Запустите вход повторно.")
            return
        try:
            await flow["client"].sign_in(password=value)
            with contextlib.suppress(Exception):
                await message.delete()
            state.pop(message.from_user.id, None)
            await finish_authorized(message.chat.id, message.from_user.id, flow["client"], value)
        except Exception as exc:
            await message.answer(f"Пароль не подошёл: {exc}")
    elif kind == "old_password" and account_id:
        store.update_account(account_id, old_password=value)
        with contextlib.suppress(Exception):
            await message.delete()
        state.pop(message.from_user.id, None)
        await message.answer("✅ Старый пароль сохранён. Откройте аккаунт и запустите оформление.", reply_markup=home_kb())
    elif kind == "email_code" and account_id:
        future = email_code_waiters.get(message.from_user.id)
        if future and not future.done():
            future.set_result(value)
            with contextlib.suppress(Exception):
                await message.delete()
            await message.answer("Код принят, завершаю привязку почты…")
        else:
            await message.answer("Сейчас нет активного ожидания кода.")


async def import_mailboxes(message: Message, text: str) -> None:
    records = parse_mailboxes(text)
    if not records:
        await message.answer("Не нашёл строк формата email:пароль. Попробуйте ещё раз.")
        return
    added, skipped = store.import_mailboxes(records)
    total, free = store.mailbox_count()
    state.pop(message.from_user.id, None)
    await message.answer(
        f"✅ Почты загружены: новых {added}, повторов {skipped}.\nВсего: {total}; свободно: {free}.",
        reply_markup=home_kb(),
    )


@dp.callback_query(F.data == "status")
async def status(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    rows = store.accounts()
    total, free = store.mailbox_count()
    lines = [f"Аккаунтов: {len(rows)} | свободных почт: {free}/{total}"]
    lines += [f"№{r['id']} {r['phone']} — {r['status']}" for r in rows[:30]]
    await safe_edit(callback, "\n".join(lines), back())


async def main() -> None:
    manual_email_bind = store.get_setting("manual_email_bind")
    if manual_email_bind:
        store.set_setting("manual_email_bind", "")
        account_id, project_id = (int(value) for value in manual_email_bind.split(":", 1))
        admin_id = store.first_admin_id()
        if admin_id:
            jobs[account_id] = asyncio.create_task(bind_existing_email(admin_id, admin_id, account_id, project_id))
    manual_stories = store.get_setting("manual_stories")
    if manual_stories:
        store.set_setting("manual_stories", "")
        account_id, project_id = (int(value) for value in manual_stories.split(":", 1))
        row, project = store.account(account_id), store.project(project_id)
        admin_id = store.first_admin_id()
        if row and project and admin_id:
            schedule_story_copy(admin_id, account_id, row["session"], project, clear_existing=False)
    manual_resume = store.get_setting("manual_resume")
    if manual_resume:
        store.set_setting("manual_resume", "")
        account_id, project_id = (int(value) for value in manual_resume.split(":", 1))
        admin_id = store.first_admin_id()
        if admin_id:
            jobs[account_id] = asyncio.create_task(resume_existing_account(admin_id, admin_id, account_id, project_id))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
