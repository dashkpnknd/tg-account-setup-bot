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
    clean = re.sub(r"[^a-zA-Z0-9_]", "", value.lstrip("@")).lower()
    return (clean or "tgprofile")[:18]


async def choose_usernames(client: TelegramClient, base: str) -> tuple[str, str]:
    """Reserve matched, readable names only after both candidates are available."""
    base = username_base(base)
    for _ in range(60):
        suffix = "".join(random.choices("23456789", k=2))
        account_name = f"{base}{suffix}"[:32]
        channel_name = f"{base}_ch{suffix}"[:32]
        try:
            account_free = await client(functions.account.CheckUsernameRequest(account_name))
            if not account_free:
                continue
            # The channel does not exist yet. Availability is checked again when it is created.
            return account_name, channel_name
        except (UsernameInvalidError, UsernameOccupiedError):
            continue
    raise RuntimeError("Не удалось подобрать свободный username за 60 попыток")


async def _photo_path(client: TelegramClient, entity: object, directory: Path, name: str) -> Path | None:
    try:
        result = await client.download_profile_photo(entity, file=directory / name)
        return Path(result) if result else None
    except Exception:
        return None


async def _copy_channel_posts(client: TelegramClient, source: object, target: object, progress: Progress) -> int:
    """Copies message text and media. Statistics and original forwarding metadata are not copied."""
    copied = 0
    async for message in client.iter_messages(source, reverse=True):
        if message.action:
            continue
        try:
            if message.media:
                await client.send_file(target, message.media, caption=message.message or "")
            elif message.message:
                await client.send_message(target, message.message)
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


async def _story_media(client: TelegramClient, story: object, directory: Path) -> object | None:
    media = getattr(story, "media", None)
    if not media:
        return None
    try:
        local_path = await client.download_media(media, file=directory)
        if not local_path:
            return None
        uploaded = await client.upload_file(local_path)
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
    client: TelegramClient,
    source: object,
    progress: Progress,
    interval_minutes: int,
) -> int:
    """Port of the FULL_CRM full-story approach: all pinned stories, pinned on target, in order."""
    stories = await _source_stories(client, source)
    if not stories:
        await progress("У источника нет доступных историй для полного копирования")
        return 0
    directory = Path(tempfile.mkdtemp(prefix="tg_stories_"))
    copied = 0
    try:
        for index, story in enumerate(stories, start=1):
            media = await _story_media(client, story, directory)
            if not media:
                continue
            try:
                await client(
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


async def clone_profile_and_channel(
    client: TelegramClient,
    source_profile_ref: str,
    source_channel_ref: str,
    username_seed: str,
    story_interval_minutes: int,
    progress: Progress,
) -> dict[str, object]:
    """Copies the source profile, creates an equivalent channel, copies posts and full stories."""
    source_profile = await client.get_entity(source_profile_ref)
    source_channel = await client.get_entity(source_channel_ref)
    source_full = await client(functions.users.GetFullUserRequest(source_profile))
    bio = getattr(getattr(source_full, "full_user", None), "about", "") or ""
    first_name = getattr(source_profile, "first_name", "") or "Telegram"
    last_name = getattr(source_profile, "last_name", "") or ""
    await progress("Копирую имя и описание профиля")
    await client(functions.account.UpdateProfileRequest(first_name=first_name, last_name=last_name, about=bio))

    account_username, channel_username = await choose_usernames(client, username_seed)
    await client(functions.account.UpdateUsernameRequest(account_username))
    await progress(f"Username профиля: @{account_username}")

    temp_dir = Path(tempfile.mkdtemp(prefix="tg_setup_"))
    try:
        profile_photo = await _photo_path(client, source_profile, temp_dir, "profile.jpg")
        if profile_photo:
            file = await client.upload_file(profile_photo)
            await client(functions.photos.UploadProfilePhotoRequest(file=file))

        channel_title = getattr(source_channel, "title", "") or first_name
        source_channel_full = await client(functions.channels.GetFullChannelRequest(source_channel))
        channel_about = getattr(getattr(source_channel_full, "full_chat", None), "about", "") or ""
        created = await client(
            functions.channels.CreateChannelRequest(title=channel_title, about=channel_about, megagroup=False)
        )
        channel = created.chats[0]
        for attempt in range(20):
            candidate = channel_username if attempt == 0 else f"{channel_username[:28]}{attempt + 1}"
            try:
                available = await client(functions.channels.CheckUsernameRequest(channel=channel, username=candidate))
                if available:
                    await client(functions.channels.UpdateUsernameRequest(channel=channel, username=candidate))
                    channel_username = candidate
                    break
            except (UsernameInvalidError, UsernameOccupiedError):
                continue
        else:
            raise RuntimeError("Не удалось назначить username канала")

        channel_photo = await _photo_path(client, source_channel, temp_dir, "channel.jpg")
        if channel_photo:
            await client(
                functions.channels.EditPhotoRequest(
                    channel=channel,
                    photo=types.InputChatUploadedPhoto(file=await client.upload_file(channel_photo)),
                )
            )

        await progress("Копирую посты канала")
        post_count = await _copy_channel_posts(client, source_channel, channel, progress)
        with contextlib.suppress(Exception):
            await client(functions.account.UpdatePersonalChannelRequest(channel=channel))

        await progress("Запускаю полное копирование историй")
        stories = await copy_full_stories(client, source_profile, progress, story_interval_minutes)
        return {
            "username": account_username,
            "channel_username": channel_username,
            "channel_id": channel.id,
            "posts": post_count,
            "stories": stories,
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
