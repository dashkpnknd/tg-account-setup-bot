"""Telegram user-account operations used by the setup workflow."""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameOccupiedError
from telethon.sessions import StringSession


Progress = Callable[[str], Awaitable[None]]
CodeProvider = Callable[[], Awaitable[str]]


def client_from_session(session: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(StringSession(session), api_id, api_hash, device_model="TG Account Setup")


def username_base(value: str) -> str:
    # Telegram treats usernames case-insensitively but retains the entered
    # casing in the profile UI, so preserve the project's visual spelling.
    clean = re.sub(r"[^a-zA-Z0-9_]", "", value.lstrip("@"))
    return (clean or "tgprofile")[:18]


def random_username(base: str, channel: bool = False) -> str:
    """Build a readable but varied username from the selected project base."""
    base = username_base(base)
    word = random.choice(("pro", "team", "hub", "media", "work", "studio", "group", "online", "office", "club"))
    letters = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(2, 4)))
    digits = "".join(random.choices("23456789", k=random.randint(3, 5)))
    stem = base[:14]
    variants = (
        f"{stem}{word}{letters}{digits}",
        f"{stem}{letters}{word}{digits}",
        f"{stem}_{word}{letters}{digits}",
        f"{stem}{letters}_{word}{digits}",
    )
    candidate = random.choice(variants)
    if channel:
        candidate = f"{candidate[:29]}ch"
    return candidate[:32]


async def choose_usernames(client: TelegramClient, base: str) -> tuple[str, str]:
    """Reserve distinct, readable usernames after Telegram checks availability."""
    for _ in range(60):
        clean_base = username_base(base)
        suffix = "".join(random.choices("23456789", k=4))
        account_name = f"{clean_base[:28]}{suffix}"[:32]
        channel_name = f"{clean_base[:26]}Ch{suffix}"[:32]
        try:
            account_free = await client(functions.account.CheckUsernameRequest(account_name))
            if not account_free:
                continue
            # The channel does not exist yet. Availability is checked again when it is created.
            return account_name, channel_name
        except (UsernameInvalidError, UsernameOccupiedError):
            continue
    raise RuntimeError("Не удалось подобрать свободный username за 60 попыток")


async def _human_pause() -> None:
    """Spread account changes over small, natural-looking editing intervals."""
    await asyncio.sleep(random.uniform(2.5, 6.5))


async def _photo_path(client: TelegramClient, entity: object, directory: Path, name: str) -> Path | None:
    try:
        result = await client.download_profile_photo(entity, file=directory / name)
        return Path(result) if result else None
    except Exception:
        return None


async def _copy_channel_posts(source_client: TelegramClient, target_client: TelegramClient, source: object, target: object, progress: Progress) -> int:
    """Copies message text and media. Statistics and original forwarding metadata are not copied."""
    copied = 0
    async for message in source_client.iter_messages(source, reverse=True):
        if message.action:
            continue
        try:
            if message.media:
                media_path = await source_client.download_media(message.media)
                if not media_path:
                    continue
                try:
                    await target_client.send_file(
                        target, media_path, caption=message.message or "",
                        formatting_entities=message.entities or [],
                    )
                finally:
                    with contextlib.suppress(OSError):
                        Path(media_path).unlink()
            elif message.message:
                await target_client.send_message(
                    target, message.message, formatting_entities=message.entities or [],
                )
            else:
                continue
            copied += 1
            if copied % 20 == 0:
                await progress(f"Скопировано постов: {copied}")
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
        except Exception:
            # One unavailable post must not stop the account preparation.
            continue
    return copied


async def _story_media(source_client: TelegramClient, target_client: TelegramClient, story: object, directory: Path) -> object | None:
    media = getattr(story, "media", None)
    if not media:
        return None
    try:
        local_path = await source_client.download_media(media, file=directory)
        if not local_path:
            return None
        uploaded = await target_client.upload_file(local_path)
        if isinstance(media, types.MessageMediaPhoto):
            return types.InputMediaUploadedPhoto(file=uploaded)
        document = getattr(media, "document", None)
        if document:
            return types.InputMediaUploadedDocument(
                file=uploaded,
                mime_type=document.mime_type or "application/octet-stream",
                attributes=document.attributes or [],
            )
    except Exception:
        return None
    return None


async def _source_stories(client: TelegramClient, peer: object) -> list[object]:
    """The original full copier copies pinned stories; active stories are added as well."""
    found: dict[int, object] = {}
    with contextlib.suppress(Exception):
        pinned = await client(functions.stories.GetPinnedStoriesRequest(peer=peer, offset_id=0, limit=100))
        for item in getattr(pinned, "stories", []):
            found[item.id] = item
    with contextlib.suppress(Exception):
        active = await client(functions.stories.GetPeerStoriesRequest(peer=peer))
        for item in getattr(active, "stories", []):
            found[item.id] = item
    return list(found.values())


async def copy_full_stories(
    source_client: TelegramClient,
    target_client: TelegramClient,
    source: object,
    progress: Progress,
    interval_minutes: int,
) -> int:
    """Port of the FULL_CRM full-story approach: all pinned stories, pinned on target, in order."""
    stories = await _source_stories(source_client, source)
    if not stories:
        await progress("У источника нет доступных историй для полного копирования")
        return 0
    directory = Path(tempfile.mkdtemp(prefix="tg_stories_"))
    copied = 0
    try:
        for index, story in enumerate(stories, start=1):
            media = await _story_media(source_client, target_client, story, directory)
            if not media:
                continue
            try:
                await target_client(
                    functions.stories.SendStoryRequest(
                        peer=types.InputPeerSelf(),
                        media=media,
                        caption=getattr(story, "caption", "") or "",
                        entities=[],
                        privacy_rules=[types.InputPrivacyValueAllowAll()],
                        period=6 * 3600,
                        pinned=True,
                        noforwards=False,
                    )
                )
                copied += 1
                await progress(f"Истории: {copied}/{len(stories)}")
            except FloodWaitError as exc:
                await asyncio.sleep(exc.seconds)
                continue
            except Exception:
                continue
            # Same paced full-copy behaviour as the existing bot; no delay after the last story.
            if index < len(stories) and interval_minutes > 0:
                await asyncio.sleep(interval_minutes * 60)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return copied


async def copy_stories_in_background(
    target_session: str,
    source_session: str | None,
    source_profile_ref: str | None,
    api_id: int,
    api_hash: str,
    progress: Progress,
    interval_minutes: int,
    clear_existing: bool = False,
) -> int:
    """Copy stories independently, so 2FA setup is never held up by long queues."""
    target = client_from_session(target_session, api_id, api_hash)
    source = client_from_session(source_session or target_session, api_id, api_hash)
    try:
        await target.connect()
        if source is not target:
            await source.connect()
        source_profile = await source.get_entity(source_profile_ref) if source_profile_ref else await source.get_me()
        if clear_existing:
            own = await target(functions.stories.GetPeerStoriesRequest(peer=types.InputPeerSelf()))
            block = getattr(own, "stories", None)
            ids = [story.id for story in getattr(block, "stories", [])]
            if ids:
                with contextlib.suppress(Exception):
                    await target(functions.stories.DeleteStoriesRequest(peer=types.InputPeerSelf(), id=ids))
        return await copy_full_stories(source, target, source_profile, progress, interval_minutes)
    finally:
        with contextlib.suppress(Exception):
            await target.disconnect()
        if source is not target:
            with contextlib.suppress(Exception):
                await source.disconnect()


async def clone_profile_and_channel(
    client: TelegramClient,
    source_client: TelegramClient,
    source_profile_ref: str | None,
    username_seed: str,
    name_emoji: str,
    story_interval_minutes: int,
    progress: Progress,
) -> dict[str, object]:
    """Copies the source profile, creates an equivalent channel, copies posts and full stories."""
    source_profile = await source_client.get_entity(source_profile_ref) if source_profile_ref else await source_client.get_me()
    source_full_result = await source_client(functions.users.GetFullUserRequest(source_profile))
    source_full = source_full_result.full_user
    source_channel_id = getattr(source_full, "personal_channel_id", None)
    if not source_channel_id:
        raise RuntimeError("У аккаунта-источника не указан личный канал")
    source_channel = next(
        (chat for chat in getattr(source_full_result, "chats", []) if getattr(chat, "id", None) == source_channel_id),
        None,
    )
    if not source_channel:
        source_channel = await source_client.get_entity(types.PeerChannel(source_channel_id))
    bio = getattr(source_full, "about", "") or ""
    first_name = getattr(source_profile, "first_name", "") or "Telegram"
    last_name = getattr(source_profile, "last_name", "") or ""
    await progress("Копирую имя и описание профиля")
    await client(functions.account.UpdateProfileRequest(first_name=f"{first_name} {name_emoji}", last_name=last_name, about=bio))
    await _human_pause()

    profile_color = getattr(source_profile, "profile_color", None)
    if profile_color:
        with contextlib.suppress(Exception):
            await client(functions.account.UpdateColorRequest(for_profile=True, color=profile_color))
            await progress("Скоплены цвета профиля")
            await _human_pause()

    # Premium badge is shown by Telegram automatically. The additional custom
    # Premium emoji next to the name is stored in `emoji_status`; replicate it
    # from the source whenever it has one.
    emoji_status = getattr(source_profile, "emoji_status", None)
    if emoji_status:
        try:
            await client(functions.account.UpdateEmojiStatusRequest(emoji_status=emoji_status))
            await progress("Скопирован Premium эмодзи-статус")
            await _human_pause()
        except Exception:
            await progress("Не удалось скопировать Premium эмодзи-статус — продолжаю оформление")

    account_username, channel_username = await choose_usernames(client, username_seed)
    await client(functions.account.UpdateUsernameRequest(account_username))
    await progress(f"Username профиля: @{account_username}")
    await _human_pause()

    temp_dir = Path(tempfile.mkdtemp(prefix="tg_setup_"))
    try:
        profile_photo = await _photo_path(source_client, source_profile, temp_dir, "profile.jpg")
        if profile_photo:
            file = await client.upload_file(profile_photo)
            await client(functions.photos.UploadProfilePhotoRequest(file=file))
            await _human_pause()

        channel_title = getattr(source_channel, "title", "") or first_name
        source_channel_full = await source_client(functions.channels.GetFullChannelRequest(source_channel))
        channel_about = getattr(getattr(source_channel_full, "full_chat", None), "about", "") or ""
        created = await client(
            functions.channels.CreateChannelRequest(title=channel_title, about=channel_about, megagroup=False)
        )
        channel = created.chats[0]
        await _human_pause()
        for attempt in range(20):
            candidate = channel_username if attempt == 0 else f"{channel_username[:28]}{attempt + 1}"
            try:
                available = await client(functions.channels.CheckUsernameRequest(channel=channel, username=candidate))
                if available:
                    await client(functions.channels.UpdateUsernameRequest(channel=channel, username=candidate))
                    channel_username = candidate
                    await _human_pause()
                    break
            except (UsernameInvalidError, UsernameOccupiedError):
                continue
        else:
            raise RuntimeError("Не удалось назначить username канала")

        channel_photo = await _photo_path(source_client, source_channel, temp_dir, "channel.jpg")
        if channel_photo:
            await client(
                functions.channels.EditPhotoRequest(
                    channel=channel,
                    photo=types.InputChatUploadedPhoto(file=await client.upload_file(channel_photo)),
                )
            )
            # Telegram creates a separate "photo updated" service event. It is
            # not part of the source content, so remove it when the API permits.
            with contextlib.suppress(Exception):
                recent = [m.id async for m in client.iter_messages(channel, limit=8) if m.action and "EditPhoto" in type(m.action).__name__]
                if recent:
                    await client(functions.channels.DeleteMessagesRequest(channel=channel, id=recent))
            await _human_pause()

        await progress("Копирую посты канала")
        post_count = await _copy_channel_posts(source_client, client, source_channel, channel, progress)
        with contextlib.suppress(Exception):
            await client(functions.account.UpdatePersonalChannelRequest(channel=channel))

        return {
            "username": account_username,
            "name_emoji": name_emoji,
            "channel_username": channel_username,
            "channel_id": channel.id,
            "posts": post_count,
            "stories": 0,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def set_password_and_email(
    client: TelegramClient,
    old_password: str | None,
    new_password: str,
    email: str,
    code_provider: CodeProvider,
) -> None:
    """Telethon performs Telegram's SRP exchange and pauses only for the email confirmation code."""
    await client.edit_2fa(
        current_password=old_password or None,
        new_password=new_password,
        hint="",
        email=email,
        email_code_callback=code_provider,
    )


async def set_password_only(client: TelegramClient, old_password: str | None, new_password: str) -> None:
    """Change 2FA without assigning a recovery email; the owner handles it manually."""
    await client.edit_2fa(current_password=old_password or None, new_password=new_password, hint="")
