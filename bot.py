from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import random
import re
import tempfile
import time
import zipfile
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon import functions, types
from telethon.sessions import SQLiteSession, StringSession
from telethon.errors import FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError

from dialoghub import connect_to_dialoghub
from multicrm import hand_off_full_story_copy, hand_off_future_story_copy, hand_off_selected_story_copy
from storage import Store
from telegram_ops import (
    _copy_channel_posts,
    copy_stories_in_background,
    client_from_session,
    clone_profile_and_channel,
    ensure_new_dialog_folder,
    publish_queued_story,
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
# These are deliberately explicit operational settings. Profile/channel
# changes use a random pause from 3:00 through 6:59 by default; stories are
# published once per hour unless the deployment overrides the values in .env.
ACTION_PAUSE_MIN_MINUTES = max(0, int(os.environ.get("ACCOUNT_ACTION_PAUSE_MIN_MINUTES", "3")))
ACTION_PAUSE_MAX_MINUTES = max(
    ACTION_PAUSE_MIN_MINUTES,
    int(os.environ.get("ACCOUNT_ACTION_PAUSE_MAX_MINUTES", "6")),
)
STORY_INTERVAL_MINUTES = max(1, int(os.environ.get("STORY_INTERVAL_MINUTES", "60")))
STORY_INTERVAL = STORY_INTERVAL_MINUTES * 60
# Exact source IDs are an operator-controlled project setting.  The source's
# three profile pins are a separate, bounded category copied and pinned as-is.
APPLE_STATIC_STORY_IDS = (1, 3, 6, 8, 9, 10, 130, 66, 43)
APPLE_PINNED_STORY_IDS = (130, 66, 43)
DMITRY_TRAFFIC_STATIC_STORY_IDS = (1, 2, 4, 6, 7, 9, 10, 11, 12, 13, 14, 15)
APPLE_STORY_PAUSE_MIN_SECONDS = 40 * 60
APPLE_STORY_PAUSE_MAX_SECONDS = 60 * 60 + 59
# Email binding is intentionally hidden for now. The implementation remains
# below so it can be re-enabled later with ENABLE_EMAIL_BINDING=1.
EMAIL_BINDING_ENABLED = os.environ.get("ENABLE_EMAIL_BINDING", "0").lower() in {"1", "true", "yes"}

# Standard Unicode emoji that render well in both current iOS and Android
# clients. Each value is reserved permanently as soon as the bot assigns it.
DMITRY_TRAFFIC_NAME_EMOJIS = (
    "◇", "🔗", "⛓️", "📎", "🖇️", "📀", "🗝️", "⚖️", "⌘", "⌁", "⌂",
    "⏱️", "⚱️", "🧷", "🗜️", "◉", "⌗", "≡", "⋮", "⋯", "⌬", "⎇", "⎈",
)
IPHONE_NAME_EMOJIS = (
    "🖨️", "🖲️", "🕹️", "📠", "📟", "📡", "📻", "📺", "🎛️", "🎚️", "🎙️",
    "🎥", "📹", "📼", "📷", "📸", "🔌", "⚙️", "🛠️", "🪛", "🧰", "🔨", "⛏️",
    "🪚", "🧲", "🔍", "🔎", "🔑", "🔒", "🔏", "🛡️", "📦", "🧾", "📇", "🧮",
    "📐", "📏", "🗄️", "🗃️", "📂", "📁", "🗒️", "📝", "✏️", "🖊️", "🖋️", "🖌️",
    "📌", "📍", "🏷️", "🧪", "🔭", "🛰️", "🔦", "🧯", "🧱", "🗳️", "⚗️", "🧬", "🧭",
)
DEFAULT_NAME_EMOJIS = ("💼", "📊", "📈", "📌", "📋", "🗂️", "🗓️", "📝", "🤝", "🏢", "💻", "📎", "🔖", "🧩", "🛠️")

PROJECT_POLICIES = {
    "АЙФОНЫ": {
        "static_story_ids": APPLE_STATIC_STORY_IDS,
        "pinned_story_ids": APPLE_PINNED_STORY_IDS,
        # Keep the initial Apple template separate from the ongoing shared
        # traffic feed.  The latter is the same durable Multi CRM copier used
        # by the already-working Ksenia account.
        "future_story_source": "LocalTraffic",
        "pin_future_stories": True,
        "pin_static_stories": True,
        "emoji_pool": "iphone",
        "name_emojis": IPHONE_NAME_EMOJIS,
        "create_channel": True,
        "username_max_digits": 5,
        "dialoghub": True,
        "copy_premium_emoji_status": True,
        "new_dialog_folder": True,
    },
    "ДМИТРИЙ ТРАФФИК": {
        "emoji_pool": "dmitry_traffic",
        "name_emojis": DMITRY_TRAFFIC_NAME_EMOJIS,
        "full_story_source": "workseyl",
        "full_story_interval_min_minutes": 45,
        "full_story_interval_max_minutes": 65,
        "create_channel": False,
        "username_max_digits": 0,
        "dialoghub": False,
        "copy_premium_emoji_status": False,
    },
    "ДМИТРИЙ БЛОГ": {
        "emoji_pool": "dmitry_traffic",
        "name_emojis": DMITRY_TRAFFIC_NAME_EMOJIS,
        "full_story_source": "workTraf",
        "full_story_interval_min_minutes": 45,
        "full_story_interval_max_minutes": 65,
        "pin_full_stories": True,
        "create_channel": True,
        "username_max_digits": 5,
        "dialoghub": True,
        "copy_premium_emoji_status": True,
        "new_dialog_folder": True,
    },
    "АВТОМАТИЗАЦИЯ": {
        "emoji_pool": "dmitry_traffic",
        "name_emojis": DMITRY_TRAFFIC_NAME_EMOJIS,
        "full_story_source": "BizFlowTeam",
        "full_story_interval_min_minutes": 45,
        "full_story_interval_max_minutes": 65,
        "pin_full_stories": True,
        "create_channel": True,
        "username_max_digits": 5,
        # Profile usernames are deliberately left for the operator.
        "assign_profile_username": False,
        "dialoghub": True,
        "copy_premium_emoji_status": True,
        "new_dialog_folder": True,
        # Completion is sent to the bot only: no access card or session export.
        "completion_only": True,
    },
}


def project_policy(project) -> dict[str, object]:
    return PROJECT_POLICIES.get(project["name"].strip().upper(), {})

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Заполните API_ID, API_HASH и BOT_TOKEN в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
store = Store()
state: dict[int, tuple[str, int | None]] = {}
qr_flows: dict[int, dict] = {}
phone_login_flows: dict[int, dict] = {}
phone_login_batches: dict[int, dict] = {}
story_only_login_flows: dict[int, dict] = {}
email_code_waiters: dict[int, asyncio.Future[str]] = {}
jobs: dict[int, asyncio.Task] = {}
story_jobs: set[asyncio.Task] = set()
story_upload_scheduler: asyncio.Task | None = None
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
STORY_PUBLISH_START = clock_time(10, 0)
STORY_PUBLISH_END = clock_time(15, 0)


def kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows]
    )


def home_kb() -> InlineKeyboardMarkup:
    rows = [
        ("➕ Добавить аккаунт по QR", "add_qr"),
        ("📱 Добавить по номерам", "add_phone"),
        ("📦 Оформить аккаунт", "accounts"),
        ("📚 Очередь историй", "story_batches"),
        ("📁 Проекты-источники", "projects"),
        ("⚙️ Источник и настройки", "settings"),
        ("📊 Статусы", "status"),
    ]
    if EMAIL_BINDING_ENABLED:
        rows.insert(4, ("📧 Загрузить почты", "mail_import"))
    return kb(*rows)


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


async def _download_queued_file(file_id: str, destination: Path) -> None:
    """Download one Bot API object only when it is about to be published."""
    await bot.download(file_id, destination=destination)


def random_story_timestamp(day, *, not_before: datetime | None = None) -> int | None:
    """Return a random second in the allowed 10:00–15:00 Moscow window."""
    start = datetime.combine(day, STORY_PUBLISH_START, tzinfo=MOSCOW_TZ)
    end = datetime.combine(day, STORY_PUBLISH_END, tzinfo=MOSCOW_TZ)
    if not_before:
        start = max(start, not_before.astimezone(MOSCOW_TZ))
    if start >= end:
        return None
    return random.randrange(int(start.timestamp()), int(end.timestamp()))


def make_random_story_schedule(item_ids: list[int], now: int) -> list[tuple[int, int]]:
    """Shuffle once and assign a persistent random Moscow publication plan."""
    shuffled = list(item_ids)
    random.shuffle(shuffled)
    current = datetime.fromtimestamp(now, MOSCOW_TZ)
    first = random_story_timestamp(current.date(), not_before=current + timedelta(minutes=5))
    if first is None:
        first = random_story_timestamp(current.date() + timedelta(days=1))
    assert first is not None
    result = [(shuffled[0], first)]
    previous = datetime.fromtimestamp(first, MOSCOW_TZ)
    for item_id in shuffled[1:]:
        next_day = previous.date() + timedelta(days=random.choice((1, 2)))
        scheduled = random_story_timestamp(next_day)
        assert scheduled is not None
        result.append((item_id, scheduled))
        previous = datetime.fromtimestamp(scheduled, MOSCOW_TZ)
    return result


def format_story_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, MOSCOW_TZ).strftime("%d.%m в %H:%M по Москве")


async def run_story_upload_scheduler() -> None:
    """Publish at most one due user-uploaded story at a time.

    There is intentionally no media cache: queue rows retain only Telegram's
    file IDs and ``publish_queued_story`` removes its temporary file directly
    after the upload to the destination account.
    """
    while True:
        item = store.claim_due_story_upload(int(time.time()))
        if not item:
            await asyncio.sleep(30)
            continue
        try:
            target_story_id = await publish_queued_story(
                item["session"], API_ID, API_HASH,
                file_id=item["telegram_file_id"],
                media_kind=item["media_kind"],
                file_name=item["file_name"],
                mime_type=item["mime_type"],
                caption=item["caption"],
                video_duration=item["video_duration"],
                video_width=item["video_width"],
                video_height=item["video_height"],
                duration_seconds=item["duration_seconds"],
                idempotency_id=item["id"],
                download_file=_download_queued_file,
            )
            store.mark_story_upload_published(item["id"], target_story_id, int(time.time()))
            log.info("Published queued story %s as %s", item["id"], target_story_id)
        except FloodWaitError as exc:
            store.retry_story_upload(item["id"], f"FloodWait {exc.seconds}s", int(time.time()) + exc.seconds + 5)
        except Exception as exc:
            # A transient Bot API or Telegram failure is retried with bounded
            # backoff; the stable random_id makes a late retry idempotent.
            attempts = int(item["attempts"]) + 1
            delay = min(6 * 60 * 60, 5 * 60 * (2 ** min(attempts - 1, 6)))
            store.retry_story_upload(item["id"], f"{type(exc).__name__}: {exc}", int(time.time()) + delay)
            log.exception("Queued story %s was deferred for %s seconds", item["id"], delay)
        await asyncio.sleep(1)


def schedule_story_copy(chat_id: int, account_id: int, target_session: str, project, clear_existing: bool = False) -> None:
    async def progress(_text: str) -> None:
        # Story publication is deliberately silent. Only failures are reported.
        return

    # Keep the job in SQLite.  The bot can be redeployed while a long story
    # archive is being copied; the next process then resumes it automatically.
    queue_key = f"story_queue:{account_id}"
    store.set_setting(queue_key, f"{project['id']}:{1 if clear_existing else 0}")

    async def run() -> None:
        queued = store.get_setting(queue_key)
        initial_clear = queued.endswith(":1")
        if initial_clear:
            # Mark it as cleared before uploads begin.  A restart after this
            # point keeps already copied stories and resumes after them.
            store.set_setting(queue_key, f"{project['id']}:0")
        try:
            await copy_stories_in_background(
                target_session, project["session"] if project["source_account_id"] else None,
                project["source_username"], API_ID, API_HASH, progress, STORY_INTERVAL, initial_clear,
            )
        except Exception:
            log.exception("Story copy failed for account %s", account_id)
            await progress_message(chat_id, f"Аккаунт №{account_id}: ошибка копирования историй")
            return
        store.set_setting(queue_key, "")

    task = asyncio.create_task(run())
    story_jobs.add(task)
    task.add_done_callback(story_jobs.discard)


async def schedule_static_story_copy(client, account_id: int, project) -> None:
    """Hand the project's approved exact IDs to the Multi CRM worker."""
    policy = project_policy(project)
    story_ids = tuple(policy.get("static_story_ids", ()))
    if not story_ids:
        return
    source_username = (project["source_username"] or "").lstrip("@").strip()
    if not source_username:
        raise RuntimeError("для проекта не указан аккаунт-источник историй")
    # These IDs are an approved project template, not a live Telethon lookup.
    # Telegram recently added a constructor which the installed Telethon build
    # cannot parse while resolving a public profile. Multi CRM's Pyrogram
    # worker reads the stories itself, and its durable delivery log ensures
    # each selected ID is published at most once.
    # The Apple template must persist on the profile just like the ongoing
    # LocalTraffic feed. These are source IDs; the CRM worker pins the
    # corresponding destination stories after each confirmed upload.
    pinned_story_ids = (
        story_ids
        if policy.get("pin_static_stories", False)
        else tuple(int(value) for value in policy.get("pinned_story_ids", ()))
    )
    completed = tuple(
        story_id for story_id in story_ids
        if story_id in store.copied_static_story_ids(account_id, source_username)
    )
    await hand_off_selected_story_copy(
        client, source_username, story_ids,
        pinned_story_ids=pinned_story_ids,
        already_completed_story_ids=completed,
        interval_min_seconds=APPLE_STORY_PAUSE_MIN_SECONDS,
        interval_max_seconds=APPLE_STORY_PAUSE_MAX_SECONDS,
    )


async def enable_future_stories_last(client, project) -> None:
    """Enable MultiCRM's permanent copier for future source stories.

    This remains inside the same MultiCRM runtime as the exact-ID setup queue;
    it tracks only new active stories from the configured future source.
    """
    source = project_policy(project).get("future_story_source")
    if not source:
        return
    await hand_off_future_story_copy(
        client,
        str(source),
        pin_copied_stories=bool(project_policy(project).get("pin_future_stories", False)),
    )


async def enable_project_stories(client, account_id: int, project) -> None:
    """Configure the single story mode selected for this project."""
    policy = project_policy(project)
    full_source = policy.get("full_story_source")
    if full_source:
        await hand_off_full_story_copy(
            client,
            str(full_source),
            interval_min_minutes=int(policy.get("full_story_interval_min_minutes", 45)),
            interval_max_minutes=int(policy.get("full_story_interval_max_minutes", 65)),
            publish_duration_hours=6,
            pin_copied_stories=bool(policy.get("pin_full_stories", True)),
        )
        return
    if policy.get("static_story_ids"):
        await schedule_static_story_copy(client, account_id, project)
        # Selected archive stories and the ongoing LocalTraffic feed are
        # complementary jobs.  Do not let the initial archive suppress the
        # normal copier for newly prepared iPhone accounts.
        await enable_future_stories_last(client, project)
        return
    await enable_future_stories_last(client, project)


async def connect_project_integrations(client, account_id: int, project) -> None:
    """Keep projects that do not use Dialog Hub out of its database."""
    if project_policy(project).get("dialoghub", True):
        await connect_to_dialoghub(client, account_id, project["name"], API_ID, API_HASH)


async def start_next_queued_setup(chat_id: int, admin_id: int) -> bool:
    """Start one waiting account only after the prior core setup has succeeded."""
    if store.get_setting("setup_queue_paused", "0") == "1":
        return False
    queue = [item for item in store.get_setting("setup_queue", "").split(",") if item]
    if not queue or any(row["status"] == "оформляется" for row in store.accounts()):
        return False
    item = queue.pop(0)
    try:
        account_id, project_id = (int(value) for value in item.split(":", 1))
    except ValueError:
        store.set_setting("setup_queue", ",".join(queue))
        return False
    row, project = store.account(account_id), store.project(project_id)
    if not row or not project:
        store.set_setting("setup_queue", ",".join(queue))
        return False
    fields = {"status": "оформляется", "error": None}
    if EMAIL_BINDING_ENABLED and store.get_setting(f"setup_skip_email:{account_id}", "0") != "1":
        mailbox = store.reserve_mailbox(account_id)
        if not mailbox:
            await bot.send_message(chat_id, f"Аккаунт №{account_id}: в очереди нет свободной почты.")
            return False
        fields.update(email=mailbox["address"], email_password=mailbox["password"])
    store.set_setting("setup_queue", ",".join(queue))
    store.update_account(account_id, **fields)
    jobs[account_id] = asyncio.create_task(process_account(chat_id, admin_id, account_id, project_id))
    return True


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not await ensure_admin(message):
        return
    await show_home(message, "TG Account Setup\n\nВыберите действие.")


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    await cleanup_phone_login(callback.from_user.id)
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
        "Emoji-status и эмодзи в имени из шаблона очищаются. Нужный эмодзи можно выставить вручную после оформления.",
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
    if not EMAIL_BINDING_ENABLED:
        await callback.answer("Привязка почты временно отключена.", show_alert=True)
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


@dp.callback_query(F.data == "add_phone")
async def add_phone(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    missing = setup_ready()
    if missing:
        await callback.answer(f"Сначала заполните: {missing}", show_alert=True)
        return
    projects = store.projects()
    if not projects:
        await callback.answer("Сначала добавьте хотя бы один проект-источник.", show_alert=True)
        return
    await cleanup_phone_login(callback.from_user.id)
    buttons = [[InlineKeyboardButton(text=row["name"], callback_data=f"phone_project:{row['id']}")] for row in projects]
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="home")])
    await safe_edit(
        callback,
        "Выберите один проект для пакета новых аккаунтов. Затем можно прислать сколько угодно номеров.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("phone_project:"))
async def choose_phone_project(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    project_id = int(callback.data.split(":", 1)[1])
    project = store.project(project_id)
    if not project:
        await callback.answer("Проект не найден.", show_alert=True)
        return
    state[callback.from_user.id] = (f"phone_numbers:{project_id}", None)
    await safe_edit(
        callback,
        f"Проект: {project['name']}.\n\nПришлите номера одним сообщением — по одному на строку, либо через запятую. "
        "Например:\n+79991234567\n+79997654321\n\nКоды будут запрашиваться строго по одному номеру за раз.",
        back(),
    )


@dp.callback_query(F.data == "phone_retry")
async def retry_phone_code(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    if not phone_login_batches.get(callback.from_user.id):
        await callback.answer("Пакет уже закрыт. Начните добавление заново.", show_alert=True)
        return
    await callback.answer("Запрашиваю код…")
    await request_next_phone_code(callback.message.chat.id, callback.from_user.id)


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


async def cleanup_phone_login(user_id: int) -> None:
    """Discard an unfinished SMS/code login without retaining its session."""
    flow = phone_login_flows.pop(user_id, None)
    if flow:
        with contextlib.suppress(Exception):
            await flow["client"].disconnect()
    phone_login_batches.pop(user_id, None)


def normalize_phone_list(text: str) -> list[str]:
    """Accept one E.164-style phone per line/comma, retaining no duplicates."""
    phones: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\s,;]+", text):
        digits = "".join(char for char in item if char.isdigit())
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not 10 <= len(digits) <= 15:
            continue
        normalized = "+" + digits
        if normalized not in seen:
            phones.append(normalized)
            seen.add(normalized)
    return phones


def append_setup_queue(account_id: int, project_id: int) -> None:
    item = f"{account_id}:{project_id}"
    queue = [value for value in store.get_setting("setup_queue", "").split(",") if value]
    if item not in queue:
        queue.append(item)
        store.set_setting("setup_queue", ",".join(queue))


async def request_next_phone_code(chat_id: int, user_id: int) -> None:
    """Request exactly one Telegram login code at a time for a batch."""
    batch = phone_login_batches.get(user_id)
    if not batch:
        return
    index = int(batch["index"])
    phones: list[str] = batch["phones"]
    if index >= len(phones):
        project_id = int(batch["project_id"])
        accepted = int(batch["accepted"])
        phone_login_batches.pop(user_id, None)
        state.pop(user_id, None)
        started = await start_next_queued_setup(chat_id, user_id)
        suffix = "Первый аккаунт уже запущен." if started else "Очередь сохранена: следующий запуск начнётся после текущего оформления."
        await bot.send_message(
            chat_id,
            f"✅ Авторизовано и поставлено в очередь: {accepted}. Проект №{project_id}.\n{suffix}",
            reply_markup=home_kb(),
        )
        return

    phone = phones[index]
    client = client_from_session("", API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except FloodWaitError as exc:
        with contextlib.suppress(Exception):
            await client.disconnect()
        state[user_id] = ("phone_login_wait", None)
        await bot.send_message(
            chat_id,
            f"Telegram попросил паузу {int(exc.seconds)} сек. Номер {phone} не пропущен и не потерян. "
            "После паузы нажмите «Повторить запрос кода».",
            reply_markup=kb(("🔄 Повторить запрос кода", "phone_retry"), ("◀️ В меню", "home")),
        )
        return
    except Exception as exc:
        with contextlib.suppress(Exception):
            await client.disconnect()
        await bot.send_message(chat_id, f"Не удалось запросить код для {phone}: {exc}. Пришлите следующий номер или отмените пакет.")
        batch["index"] = index + 1
        await request_next_phone_code(chat_id, user_id)
        return

    phone_login_flows[user_id] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": sent.phone_code_hash,
    }
    state[user_id] = ("phone_login_code", None)
    await bot.send_message(
        chat_id,
        f"Номер {index + 1}/{len(phones)}: {phone}. Telegram отправил код. Пришлите его одним сообщением.\n"
        "Если появится запрос 2FA, следующим сообщением пришлите пароль — бот удалит его.",
        reply_markup=back(),
    )


async def finish_phone_login(chat_id: int, user_id: int, old_password: str | None = None) -> None:
    flow = phone_login_flows.pop(user_id, None)
    batch = phone_login_batches.get(user_id)
    if not flow or not batch:
        await bot.send_message(chat_id, "Сессия входа уже истекла. Запустите добавление номеров заново.")
        return
    client = flow["client"]
    try:
        me = await client.get_me()
        if not me:
            raise RuntimeError("Telegram не подтвердил авторизацию")
        account_id = store.add_account(me.phone or str(me.id), client.session.save(), old_password)
        store.set_setting(f"active_project:{account_id}", str(batch["project_id"]))
        if account_id in store.source_account_ids():
            await bot.send_message(chat_id, f"{flow['phone']} — это аккаунт-источник; в очередь оформления он не добавлен.")
        else:
            store.update_account(account_id, status="в очереди", error=None)
            append_setup_queue(account_id, int(batch["project_id"]))
            batch["accepted"] = int(batch["accepted"]) + 1
            await bot.send_message(chat_id, f"✅ {flow['phone']} авторизован и добавлен в очередь.")
        batch["index"] = int(batch["index"]) + 1
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    await request_next_phone_code(chat_id, user_id)


async def finish_authorized(chat_id: int, user_id: int, client, old_password: str | None = None) -> None:
    me = await client.get_me()
    session = client.session.save()
    account_id = store.add_account(me.phone or str(me.id), session, old_password)
    await client.disconnect()
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


@dp.callback_query(F.data == "story_batches")
async def story_batches(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    source_ids = store.source_account_ids()
    rows = [row for row in store.accounts() if row["id"] not in source_ids]
    if not rows:
        await safe_edit(callback, "Сначала добавьте целевой аккаунт через QR или номера.", back())
        return
    buttons = [
        [InlineKeyboardButton(text=f"{row['phone']} — {row['status']}", callback_data=f"story_target:{row['id']}")]
        for row in rows[:40]
    ]
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="home")])
    await safe_edit(
        callback,
        "Выберите аккаунт для очереди историй. Медиа не будут сохраняться на сервере: сохраняются только file_id Telegram.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("story_target:"))
async def story_target(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    account_id = int(callback.data.split(":", 1)[1])
    if not store.account(account_id):
        await safe_edit(callback, "Аккаунт не найден.", back())
        return
    await safe_edit(
        callback,
        "Истории будут жить 48 часов. Пачка один раз перемешается без повторов.\n\n"
        "Первая история выйдет сегодня в случайное время с 10:00 до 15:00 по Москве, "
        "если это окно ещё не закончилось; иначе — завтра. Каждая следующая выйдет "
        "через 1 или 2 дня, тоже случайно с 10:00 до 15:00.\n\n"
        "Нажмите «Создать случайную очередь», затем пришлите фото и видео.",
        kb(
            ("🎲 Создать случайную очередь", f"story_random:{account_id}"),
            ("◀️ К аккаунтам", "story_batches"),
        ),
    )


@dp.callback_query(F.data.startswith("story_random:"))
@dp.callback_query(F.data.startswith("story_cadence:"))
async def story_random(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    parts = callback.data.split(":")
    account_id = int(parts[1])
    if not store.account(account_id):
        await callback.answer("Настройка очереди устарела. Начните заново.", show_alert=True)
        return
    # cadence_days stays at 1 only for backward compatibility with existing
    # rows. The actual schedule is generated only after all media arrives.
    batch_id = store.create_story_upload_batch(account_id, 1, callback.from_user.id)
    state[callback.from_user.id] = (f"story_upload:{batch_id}", account_id)
    await safe_edit(
        callback,
        "Случайная очередь создана: каждая история на 48 часов.\n\n"
        "Пришлите фото и/или видео по одному или альбомами — порядок отправки не важен: "
        "после «Готово» бот перемешает пачку без повторов. "
        "Можно добавить подпись к каждому. Когда закончите, нажмите «Готово».\n\n"
        "На сервер не загружается вся пачка: до публикации хранится только file_id Telegram.",
        kb(("✅ Готово, запустить очередь", "story_finish"), ("✖️ Отменить пачку", "story_cancel")),
    )


@dp.callback_query(F.data == "story_finish")
async def story_finish(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    current = state.get(callback.from_user.id, ("", None))[0]
    if not current.startswith("story_upload:"):
        await callback.answer("Нет открытой пачки историй.", show_alert=True)
        return
    batch_id = int(current.split(":", 1)[1])
    item_ids = store.draft_story_upload_item_ids(batch_id)
    schedule = make_random_story_schedule(item_ids, int(time.time())) if item_ids else []
    batch = store.start_random_story_upload_batch(batch_id, schedule)
    state.pop(callback.from_user.id, None)
    if not batch:
        await safe_edit(callback, "Пачка пуста или уже запущена. Добавьте хотя бы одно фото или видео.", back())
        return
    count = int(store.story_upload_batch(batch_id)["item_count"])
    first_at = min(scheduled_at for _item_id, scheduled_at in schedule)
    await safe_edit(
        callback,
        f"✅ В очереди {count} историй, порядок перемешан без повторов. "
        f"Первая публикация: {format_story_time(first_at)}. Дальше — случайно через 1 или 2 дня, "
        "в окне 10:00–15:00 по Москве. Каждая история живёт 48 часов.",
        back(),
    )


@dp.callback_query(F.data == "story_cancel")
async def story_cancel(callback: CallbackQuery) -> None:
    if not await ensure_admin(callback):
        return
    current = state.get(callback.from_user.id, ("", None))[0]
    if current.startswith("story_upload:"):
        store.discard_story_upload_batch(int(current.split(":", 1)[1]))
    state.pop(callback.from_user.id, None)
    await safe_edit(callback, "Пачка отменена. Медиа на сервер не сохранялись.", back())


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
            ("📚 Добавить истории", f"story_target:{account_id}"),
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
    account_fields = {"status": "оформляется", "error": None}
    if EMAIL_BINDING_ENABLED and store.get_setting(f"setup_skip_email:{account_id}", "0") != "1":
        mailbox = store.reserve_mailbox(account_id)
        if not mailbox:
            await callback.answer("Нет свободных почт. Загрузите базу почт.", show_alert=True)
            return
        account_fields.update(email=mailbox["address"], email_password=mailbox["password"])
    store.update_account(account_id, **account_fields)
    await safe_edit(callback, f"Аккаунт №{account_id} запущен. Отправлю статус по этапам.", back())
    jobs[account_id] = asyncio.create_task(process_account(callback.message.chat.id, callback.from_user.id, account_id, project_id))


async def process_account(
    chat_id: int, admin_id: int, account_id: int, project_id: int, *, skip_email_step: bool = False
) -> None:
    row = store.account(account_id)
    if not row:
        return
    client = client_from_session(row["session"], API_ID, API_HASH)
    project = store.project(project_id)
    if not project:
        return
    source_client = client_from_session(project["session"], API_ID, API_HASH) if project["source_account_id"] else client
    store.set_setting(f"active_project:{account_id}", str(project_id))
    policy = project_policy(project)
    skip_email = store.get_setting(f"setup_skip_email:{account_id}", "0") == "1"
    skip_stories = store.get_setting(f"setup_skip_stories:{account_id}", "0") == "1"
    skip_password = store.get_setting(f"setup_skip_password:{account_id}", "0") == "1"
    skip_report_channel = store.get_setting(f"setup_skip_report_channel:{account_id}", "0") == "1"
    skip_premium_features = store.get_setting(
        f"setup_skip_premium_features:{account_id}", "0"
    ) == "1"
    emoji_pool = str(policy.get("emoji_pool", "default"))
    emoji_choices = tuple(policy.get("name_emojis", DEFAULT_NAME_EMOJIS))
    # This is a normal Unicode character in the visible name, not a Premium
    # emoji status or profile decoration. It is required for both projects.
    name_emoji = row["name_emoji"] or store.reserve_project_name_emoji(
        emoji_pool, account_id, random.sample(emoji_choices, len(emoji_choices))
    )
    if not name_emoji:
        store.update_account(
            account_id,
            status="ошибка",
            error="закончились свободные эмодзи для имени; добавьте новые в NAME_EMOJIS",
        )
        return
    store.update_account(account_id, name_emoji=name_emoji)

    async def progress(_text: str) -> None:
        # Routine setup stages are silent by product requirement.
        return

    try:
        await client.connect()
        if source_client is not client:
            await source_client.connect()
        me = await client.get_me()
        if not getattr(me, "premium", False) and not skip_premium_features:
            raise RuntimeError("на аккаунте не найден Telegram Premium")
        # Recovery email is the first visible stage.  The operator receives
        # the Telegram confirmation request before any profile/channel work.
        if EMAIL_BINDING_ENABLED and not skip_email and not skip_email_step:
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

            async def code_provider(_code_length: int = 0) -> str:
                email_code_waiters[admin_id] = future
                state[admin_id] = ("email_code", account_id)
                await bot.send_message(
                    chat_id,
                    f"Аккаунт №{account_id}: сначала подтвердите почту {row['email']}.\n"
                    f"Пароль почты: {row['email_password']}\n\n"
                    "Telegram отправил код на эту почту. Пришлите его сюда.",
                    reply_markup=email_wait_kb(account_id),
                )
                return await asyncio.wait_for(future, timeout=15 * 60)

            await set_password_and_email(
                client, row["old_password"], store.get_setting("new_password"), row["email"], code_provider
            )
        elif not skip_email_step and not skip_password:
            await set_password_only(client, row["old_password"], store.get_setting("new_password"))
        if not skip_email_step and not skip_password:
            store.update_account(account_id, old_password=store.get_setting("new_password"))
        result = await clone_profile_and_channel(
            client,
            source_client,
            project["source_username"],
            project["username_base"],
            name_emoji,
            store.reserved_usernames(),
            ACTION_PAUSE_MIN_MINUTES,
            ACTION_PAUSE_MAX_MINUTES,
            progress,
            create_channel=bool(policy.get("create_channel", True)),
            username_max_digits=int(policy.get("username_max_digits", 5)),
            assign_profile_username=bool(policy.get("assign_profile_username", True)),
            copy_premium_emoji_status=bool(policy.get("copy_premium_emoji_status", False))
            and not skip_premium_features,
            copy_profile_color=not skip_premium_features,
        )
        store.update_account(account_id, **result)
        if policy.get("new_dialog_folder", False):
            folder_id = await ensure_new_dialog_folder(client)
            await progress(f"Создана папка новое (ID {folder_id})")
        await connect_project_integrations(client, account_id, project)
        store.update_account(account_id, status="готов", error=None)
        final = store.account(account_id)
        await publish_result(
            chat_id, final, project["name"],
            publish_report=not skip_report_channel,
            completion_only=bool(policy.get("completion_only", False)),
        )
        if not skip_stories:
            try:
                await enable_project_stories(client, account_id, project)
            except Exception:
                # Core account setup is already complete at this point. A story
                # queue failure must not relabel the account as broken or stop the
                # next account in the setup queue.
                log.exception("Story setup failed for account %s", account_id)
                await progress_message(chat_id, f"Аккаунт №{account_id}: истории не подключены; оформление аккаунта завершено")
        await start_next_queued_setup(chat_id, admin_id)
    except Exception as exc:
        log.exception("Setup failed for account %s", account_id)
        store.update_account(account_id, status="ошибка", error=str(exc)[:900])
        await progress_message(chat_id, f"Аккаунт №{account_id}: ошибка оформления: {exc}")
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

    async def progress(_text: str) -> None:
        return

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

        if not project_policy(project).get("static_story_ids") and not project_policy(project).get("full_story_source"):
            schedule_story_copy(chat_id, account_id, row["session"], project, clear_existing=True)
            await progress("Истории копируются в фоне — продолжаю привязку почты")

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        async def code_provider(_code_length: int = 0) -> str:
            email_code_waiters[admin_id] = future
            state[admin_id] = ("email_code", account_id)
            await bot.send_message(
                chat_id,
                f"Аккаунт №{account_id}: код Telegram отправлен на почту {row['email']}.\n"
                f"Пароль почты: {row['email_password']}",
                reply_markup=email_wait_kb(account_id),
            )
            return await asyncio.wait_for(future, timeout=15 * 60)
        await set_password_and_email(client, store.get_setting("new_password"), store.get_setting("new_password"), row["email"], code_provider)
        store.update_account(account_id, old_password=store.get_setting("new_password"))
        store.update_account(account_id, status="готов", error=None)
        await publish_result(chat_id, store.account(account_id), project["name"])
        await enable_project_stories(client, account_id, project)
    except Exception as exc:
        log.exception("Resume failed for account %s", account_id)
        store.update_account(account_id, status="ошибка", error=str(exc)[:900])
        await progress_message(chat_id, f"Аккаунт №{account_id}: ошибка оформления: {exc}")
    finally:
        email_code_waiters.pop(admin_id, None)
        if state.get(admin_id) == ("email_code", account_id):
            state.pop(admin_id, None)
        with contextlib.suppress(Exception):
            await client.disconnect()


async def complete_existing_profile(client, row, project) -> None:
    """Finish and verify a pre-created profile without creating another channel.

    A resumed account may already have its channel and photo while its profile
    bio and personal-channel link were never applied.  Do those two updates
    explicitly and verify Telegram accepted the personal-channel relation
    before the caller may mark the account as complete.
    """
    source = await client.get_entity(project["source_username"])
    source_full = await client(functions.users.GetFullUserRequest(source))
    bio = getattr(source_full.full_user, "about", "") or ""
    if not row["channel_username"]:
        raise RuntimeError("у возобновляемого аккаунта не указан личный канал")
    channel = await client.get_entity(f"@{row['channel_username']}")
    await client(functions.account.UpdateProfileRequest(about=bio))
    await client(functions.account.UpdatePersonalChannelRequest(channel=channel))
    own_full = await client(functions.users.GetFullUserRequest(await client.get_input_entity("me")))
    if int(getattr(own_full.full_user, "personal_channel_id", 0) or 0) != int(channel.id):
        raise RuntimeError("Telegram не подтвердил привязку личного канала")


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

        async def code_provider(_code_length: int = 0) -> str:
            email_code_waiters[admin_id] = future
            state[admin_id] = ("email_code", account_id)
            await progress_message(
                chat_id,
                f"Аккаунт №{account_id}: код Telegram отправлен на почту.\n\n"
                f"Откройте: {row['email']}\nПароль: {row['email_password']}\n\n"
                "Пришлите код сюда одним сообщением.",
            )
            return await asyncio.wait_for(future, timeout=15 * 60)

        current_password = row["old_password"] or store.get_setting("new_password")
        password = store.get_setting("new_password")
        await set_password_and_email(client, current_password, password, row["email"], code_provider)
        await complete_existing_profile(client, row, project)
        await connect_project_integrations(client, account_id, project)
        store.update_account(account_id, old_password=password, status="готов", error=None)
        await publish_result(chat_id, store.account(account_id), project["name"])
        await enable_project_stories(client, account_id, project)
        await start_next_queued_setup(chat_id, admin_id)
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
        await connect_project_integrations(client, account_id, project)
        store.update_account(account_id, old_password=store.get_setting("new_password"), status="готов", error=None)
        await publish_result(chat_id, store.account(account_id), project["name"])
        await enable_project_stories(client, account_id, project)
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
    if not EMAIL_BINDING_ENABLED:
        await callback.answer("Привязка почты временно отключена.", show_alert=True)
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


async def session_json_export(row) -> Path:
    """Export one TgNinja-compatible Session+JSON pair for an account."""
    descriptor, raw_path = tempfile.mkstemp(prefix=f"tg-account-{row['id']}-", suffix="-session-json.zip")
    os.close(descriptor)
    archive_path = Path(raw_path)
    stem = f"account_{row['id']}"
    with tempfile.TemporaryDirectory(prefix=f"tg-export-{row['id']}-") as directory:
        session_base = Path(directory) / stem
        source = StringSession(row["session"])
        sqlite_session = SQLiteSession(str(session_base))
        try:
            sqlite_session.set_dc(source.dc_id, source.server_address, source.port)
            sqlite_session.auth_key = source.auth_key
            sqlite_session.save()
        finally:
            sqlite_session.close()
        # TgNinja's Session+Json importer requires exactly one matching
        # `name.session` + `name.json` pair.  A second Pyrogram session in the
        # same archive is interpreted as a separate account and rejected.
        identity = client_from_session(row["session"], API_ID, API_HASH)
        try:
            await identity.connect()
            me = await identity.get_me()
        finally:
            with contextlib.suppress(Exception):
                await identity.disconnect()
        payload = {
            # Standard Session+Json metadata (the keys are deliberately
            # named `app_id` and `app_hash`, not Telethon's api_id/api_hash).
            "app_id": API_ID,
            "app_hash": API_HASH,
            "device": "PC 64bit",
            "sdk": "Linux",
            "app_version": "1.0",
            "system_lang_pack": "en-US",
            "system_lang_code": "en-US",
            "lang_pack": "tdesktop",
            "lang_code": "en",
            "session_file": stem,
            "id": me.id,
            "phone": row["phone"],
            "username": me.username or row["username"],
            "is_premium": bool(getattr(me, "premium", False)),
        }
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(session_base.with_suffix(".session"), arcname=f"{stem}.session")
            archive.writestr(f"{stem}.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return archive_path


async def publish_result(
    chat_id: int, row, project_name: str, *, publish_report: bool = True,
    completion_only: bool = False,
) -> None:
    if completion_only:
        await bot.send_message(chat_id, f"✅ Аккаунт №{row['id']} оформлен ({project_name}).")
        return
    name, username = await actual_account_identity(row)
    card = (
        f"{name} ({project_name.lower()})\n@{username}\n\n"
        f"логин: {display_phone(row['phone'])}\n"
        f"пароль: {store.get_setting('new_password')}\n\n"
        f"почта для входа: {row['email'] or '—'}\n"
        f"пароль: {row['email_password'] or '—'}\n\n"
        "https://firstmail.ltd/webmail/login/"
    )
    await bot.send_message(chat_id, "✅ Аккаунт оформлен.\n\n" + card)
    export_path: Path | None = None
    try:
        export_path = await session_json_export(row)
        filename = f"account_{row['id']}_session_json.zip"
        await bot.send_document(chat_id, FSInputFile(export_path, filename=filename))
    except Exception:
        log.exception("Could not create or send session+JSON export for account %s", row["id"])
        await bot.send_message(chat_id, "⚠️ Карточка отправлена, но Session + JSON не удалось приложить.")
    if not publish_report:
        if export_path:
            with contextlib.suppress(FileNotFoundError):
                export_path.unlink()
        return
    report = store.get_setting("report_channel")
    try:
        await bot.send_message(report, card)
        if export_path:
            await bot.send_document(report, FSInputFile(export_path, filename=f"account_{row['id']}_session_json.zip"))
    except Exception:
        log.exception("Could not publish access card to the configured channel")
        await bot.send_message(chat_id, "⚠️ Карточка не отправлена в канал доступов: проверьте, что бот в нём администратор с правом публикации.")
    finally:
        if export_path:
            with contextlib.suppress(FileNotFoundError):
                export_path.unlink()


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


async def accept_story_upload(
    message: Message,
    *,
    file_id: str,
    media_kind: str,
    file_name: str | None = None,
    mime_type: str | None = None,
    video_duration: int = 0,
    video_width: int = 0,
    video_height: int = 0,
) -> bool:
    """Append one incoming Bot API file to the open draft, without downloading it."""
    current = state.get(message.from_user.id, ("", None))[0]
    if not current.startswith("story_upload:"):
        return False
    batch_id = int(current.split(":", 1)[1])
    batch = store.story_upload_batch(batch_id)
    if not batch or batch["status"] != "draft":
        state.pop(message.from_user.id, None)
        await message.answer("Эта пачка уже закрыта. Откройте новую очередь историй.", reply_markup=back())
        return True
    store.add_story_upload_item(
        batch_id,
        telegram_file_id=file_id,
        media_kind=media_kind,
        file_name=file_name,
        mime_type=mime_type,
        caption=message.caption or "",
        video_duration=video_duration,
        video_width=video_width,
        video_height=video_height,
    )
    count = int(store.story_upload_batch(batch_id)["item_count"])
    await message.answer(f"Добавлено в пачку: {count}.", reply_markup=kb(("✅ Готово, запустить очередь", "story_finish"), ("✖️ Отменить пачку", "story_cancel")))
    return True


@dp.message(F.photo)
async def story_photo_input(message: Message) -> None:
    if not await ensure_admin(message):
        return
    photo = message.photo[-1]
    await accept_story_upload(message, file_id=photo.file_id, media_kind="photo")


@dp.message(F.video)
async def story_video_input(message: Message) -> None:
    if not await ensure_admin(message):
        return
    video = message.video
    await accept_story_upload(
        message,
        file_id=video.file_id,
        media_kind="video",
        file_name=video.file_name,
        mime_type=video.mime_type,
        video_duration=video.duration or 0,
        video_width=video.width or 0,
        video_height=video.height or 0,
    )


@dp.message(F.document)
async def file_input(message: Message) -> None:
    if not await ensure_admin(message):
        return
    document = message.document
    mime_type = document.mime_type or ""
    if mime_type.startswith("video/"):
        await accept_story_upload(
            message,
            file_id=document.file_id,
            media_kind="video",
            file_name=document.file_name,
            mime_type=mime_type,
        )
        return
    if mime_type.startswith("image/"):
        await accept_story_upload(
            message,
            file_id=document.file_id,
            media_kind="photo",
            file_name=document.file_name,
            mime_type=mime_type,
        )
        return
    if state.get(message.from_user.id, ("", None))[0] != "mail_import":
        return
    stream = io.BytesIO()
    await bot.download(document, destination=stream)
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
    if kind.startswith("phone_numbers:"):
        project_id = int(kind.split(":", 1)[1])
        phones = normalize_phone_list(value)
        if not phones:
            await message.answer("Не нашёл корректных номеров. Пришлите номера в международном виде, например +79991234567.")
            return
        phone_login_batches[message.from_user.id] = {
            "project_id": project_id,
            "phones": phones,
            "index": 0,
            "accepted": 0,
        }
        await request_next_phone_code(message.chat.id, message.from_user.id)
    elif kind == "phone_login_code":
        flow = phone_login_flows.get(message.from_user.id)
        if not flow:
            state.pop(message.from_user.id, None)
            await message.answer("Код уже истёк. Запустите добавление номеров заново.")
            return
        code = "".join(char for char in value if char.isdigit())
        if not 4 <= len(code) <= 8:
            await message.answer("Пришлите код Telegram цифрами, без других символов.")
            return
        try:
            await flow["client"].sign_in(flow["phone"], code, phone_code_hash=flow["phone_code_hash"])
        except PhoneCodeInvalidError:
            await message.answer("Код неверный. Попробуйте ещё раз.")
            return
        except PhoneCodeExpiredError:
            with contextlib.suppress(Exception):
                await flow["client"].disconnect()
            phone_login_flows.pop(message.from_user.id, None)
            state.pop(message.from_user.id, None)
            await message.answer("Код истёк. Запустите добавление номеров заново.")
            return
        except SessionPasswordNeededError:
            state[message.from_user.id] = ("phone_login_password", None)
            with contextlib.suppress(Exception):
                await message.delete()
            await message.answer("Нужен текущий пароль 2FA этого аккаунта. Пришлите его одним сообщением — бот удалит сообщение.")
            return
        except Exception as exc:
            await message.answer(f"Не удалось подтвердить код: {exc}")
            return
        with contextlib.suppress(Exception):
            await message.delete()
        await finish_phone_login(message.chat.id, message.from_user.id)
    elif kind == "phone_login_password":
        flow = phone_login_flows.get(message.from_user.id)
        if not flow:
            state.pop(message.from_user.id, None)
            await message.answer("Сессия входа истекла. Запустите добавление номеров заново.")
            return
        try:
            await flow["client"].sign_in(password=value)
        except Exception as exc:
            await message.answer(f"Пароль 2FA не подошёл: {exc}")
            return
        with contextlib.suppress(Exception):
            await message.delete()
        await finish_phone_login(message.chat.id, message.from_user.id, old_password=value)
    elif kind.startswith("setting:"):
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
    # Only a process crash can leave an item claimed. Requeue it before the
    # scheduler starts; publication uses a stable Telegram random_id, so the
    # retry remains safe even if the prior process died after sending.
    store.reset_story_upload_sending()
    global story_upload_scheduler
    story_upload_scheduler = asyncio.create_task(run_story_upload_scheduler())
    # Resume durable story queues first.  Existing target stories are used as
    # the checkpoint, so copied stories are neither deleted nor duplicated.
    admin_id = store.first_admin_id()
    if admin_id:
        for row in store.accounts():
            queued = store.get_setting(f"story_queue:{row['id']}")
            if not queued:
                continue
            try:
                project_id, clear = (int(value) for value in queued.split(":", 1))
            except ValueError:
                store.set_setting(f"story_queue:{row['id']}", "")
                continue
            project = store.project(project_id)
            if project and not project_policy(project).get("full_story_source"):
                schedule_story_copy(admin_id, row["id"], row["session"], project, clear_existing=bool(clear))
            elif project:
                store.set_setting(f"story_queue:{row['id']}", "")
    manual_setup = store.get_setting("manual_setup")
    if manual_setup:
        store.set_setting("manual_setup", "")
        admin_id = store.first_admin_id()
        for item in manual_setup.split(","):
            try:
                account_id, project_id = (int(value) for value in item.split(":", 1))
            except ValueError:
                continue
            row, project = store.account(account_id), store.project(project_id)
            if not row or not project or not admin_id:
                continue
            fields = {"status": "оформляется", "error": None}
            if EMAIL_BINDING_ENABLED and store.get_setting(f"setup_skip_email:{account_id}", "0") != "1":
                mailbox = store.reserve_mailbox(account_id)
                if not mailbox:
                    await bot.send_message(admin_id, f"Аккаунт №{account_id}: нет свободных почт, запуск отменён.")
                    continue
                fields.update(email=mailbox["address"], email_password=mailbox["password"])
            store.update_account(account_id, **fields)
            jobs[account_id] = asyncio.create_task(process_account(admin_id, admin_id, account_id, project_id))
    # A service restart must not silently strand an account after it has been
    # marked as in progress.  Resume from its durable project assignment and
    # ask for a fresh e-mail code when that is the pending step.
    if admin_id:
        for row in store.accounts():
            account_id = int(row["id"])
            if row["status"] != "оформляется" or account_id in jobs:
                continue
            project_id = int(store.get_setting(f"active_project:{account_id}", "0") or 0)
            project = store.project(project_id)
            if not project:
                continue
            if row["username"] and row["email"] and row["email_password"]:
                jobs[account_id] = asyncio.create_task(
                    bind_existing_email(admin_id, admin_id, account_id, project_id)
                )
            else:
                jobs[account_id] = asyncio.create_task(
                    process_account(admin_id, admin_id, account_id, project_id)
                )
    if admin_id:
        await start_next_queued_setup(admin_id, admin_id)
    manual_email_bind = store.get_setting("manual_email_bind") if EMAIL_BINDING_ENABLED else ""
    if manual_email_bind:
        store.set_setting("manual_email_bind", "")
        account_id, project_id = (int(value) for value in manual_email_bind.split(":", 1))
        admin_id = store.first_admin_id()
        if admin_id:
            jobs[account_id] = asyncio.create_task(bind_existing_email(admin_id, admin_id, account_id, project_id))
    manual_stories = store.get_setting("manual_stories")
    if manual_stories:
        store.set_setting("manual_stories", "")
        admin_id = store.first_admin_id()
        for item in manual_stories.split(","):
            account_id, project_id = (int(value) for value in item.split(":", 1))
            row, project = store.account(account_id), store.project(project_id)
            if row and project and admin_id and not project_policy(project).get("full_story_source"):
                schedule_story_copy(admin_id, account_id, row["session"], project, clear_existing=True)
    manual_resume = store.get_setting("manual_resume")
    if manual_resume:
        store.set_setting("manual_resume", "")
        account_id, project_id = (int(value) for value in manual_resume.split(":", 1))
        admin_id = store.first_admin_id()
        if admin_id:
            jobs[account_id] = asyncio.create_task(resume_existing_account(admin_id, admin_id, account_id, project_id))
    manual_rebuild = store.get_setting("manual_rebuild")
    if manual_rebuild:
        store.set_setting("manual_rebuild", "")
        account_id, project_id = (int(value) for value in manual_rebuild.split(":", 1))
        admin_id = store.first_admin_id()
        if admin_id and store.account(account_id) and store.project(project_id):
            # A full visual rebuild deliberately leaves the confirmed recovery
            # email and 2FA untouched, but starts profile/channel work anew.
            store.clear_project_name_emoji(account_id)
            store.update_account(account_id, name_emoji=None, status="оформляется", error=None)
            jobs[account_id] = asyncio.create_task(
                process_account(admin_id, admin_id, account_id, project_id, skip_email_step=True)
            )
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
