"""Telegram user-account operations used by the setup workflow."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import email
import imaplib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, functions, types, utils
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameOccupiedError
from telethon.errors.common import TypeNotFoundError
from telethon.sessions import StringSession


Progress = Callable[[str], Awaitable[None]]
CodeProvider = Callable[[], Awaitable[str]]


def without_emoji(value: str) -> str:
    """Return a profile-name value without pictographic emoji characters.

    A profile template may use an emoji in its visible first name. That is
    distinct from Telegram's ``emoji_status`` field, so both need to be
    handled when preparing a plain template for a later manual edit.
    """
    def is_emoji_codepoint(char: str) -> bool:
        codepoint = ord(char)
        return (
            0x1F000 <= codepoint <= 0x1FAFF  # most modern emoji and flags
            or 0x2600 <= codepoint <= 0x27BF  # dingbats and misc symbols
            or codepoint in {0x200D, 0x20E3, 0xFE0E, 0xFE0F}  # emoji joiners/selectors
        )

    # Normalization makes the result deterministic for names containing
    # composed characters, without changing ordinary letters.
    plain = "".join(char for char in unicodedata.normalize("NFC", value) if not is_emoji_codepoint(char))
    return re.sub(r"\s+", " ", plain).strip()


def _latest_rambler_code(address: str, password: str) -> str | None:
    with imaplib.IMAP4_SSL("imap.rambler.ru", 993) as inbox:
        inbox.login(address, password)
        inbox.select("INBOX")
        _, result = inbox.search(None, "ALL")
        for message_id in reversed(result[0].split()[-30:]):
            _, raw = inbox.fetch(message_id, "(RFC822)")
            message = email.message_from_bytes(raw[0][1])
            sender = (message.get("From") or "").lower()
            if "telegram" not in sender:
                continue
            parts = []
            if message.is_multipart():
                for part in message.walk():
                    if part.get_content_type() in {"text/plain", "text/html"}:
                        payload = part.get_payload(decode=True) or b""
                        parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            else:
                payload = message.get_payload(decode=True) or b""
                parts.append(payload.decode(message.get_content_charset() or "utf-8", errors="replace"))
            match = re.search(r"(?<!\d)(\d{5,6})(?!\d)", "\n".join(parts))
            if match:
                return match.group(1)
    return None


async def wait_for_rambler_code(address: str, password: str, timeout_seconds: int = 600) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            code = await asyncio.to_thread(_latest_rambler_code, address, password)
            if code:
                return code
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(10)
    if last_error:
        raise RuntimeError(f"Не удалось получить код из Rambler: {last_error}")
    raise RuntimeError("Код Telegram не пришёл на Rambler за 10 минут")


async def ensure_new_dialog_folder(client: TelegramClient) -> int:
    response = await client(functions.messages.GetDialogFiltersRequest())
    filters = list(getattr(response, "filters", []))
    existing = next(
        (
            item
            for item in filters
            if getattr(getattr(item, "title", None), "text", None) == "новое"
        ),
        None,
    )
    used_ids = [int(getattr(item, "id", -1)) for item in filters]
    folder_id = int(existing.id) if existing else next(
        value for value in range(2, 100) if value not in used_ids
    )
    folder = types.DialogFilter(
        id=folder_id,
        title=types.TextWithEntities(text="новое", entities=[]),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        contacts=True,
        non_contacts=True,
        exclude_read=True,
    )
    await client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=folder))
    verified = await client(functions.messages.GetDialogFiltersRequest())
    actual = next(
        (item for item in getattr(verified, "filters", []) if int(getattr(item, "id", -1)) == folder_id),
        None,
    )
    if not actual or not (actual.contacts and actual.non_contacts and actual.exclude_read):
        raise RuntimeError("Telegram did not confirm folder новое")
    return folder_id


def client_from_session(session: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(StringSession(session), api_id, api_hash, device_model="TG Account Setup")


def username_base(value: str) -> str:
    # Telegram treats usernames case-insensitively but retains the entered
    # casing in the profile UI, so preserve the project's visual spelling.
    clean = re.sub(r"[^a-zA-Z0-9_]", "", value.lstrip("@"))
    return (clean or "tgprofile")[:18]


def _username_words(base: str) -> list[str]:
    """Split CamelCase project bases into human-readable username words."""
    compact = username_base(base).replace("_", "")
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+", compact)
    return words or [compact]


def _title_word(value: str) -> str:
    return value[:1].upper() + value[1:].lower()


def _short_word(value: str) -> str:
    if len(value) <= 4:
        return value
    return value[:random.randint(3, min(5, len(value)))]


def random_username(base: str, channel: bool = False, max_digits: int = 5) -> str:
    """Build varied, readable names; channel names are independent variants."""
    words = _username_words(base)
    title_words = [_title_word(word) for word in words]
    title = "".join(title_words)
    camel = title_words[0].lower() + "".join(title_words[1:])
    lower = title.lower()
    shortened = "".join(_short_word(word).lower() for word in words)
    shortened_title = "".join(_title_word(_short_word(word)) for word in words)
    swapped = "".join(reversed(title_words)) if len(words) > 1 else title
    swapped_camel = swapped[:1].lower() + swapped[1:]
    no_vowels = "".join(re.sub(r"[aeiouAEIOU]", "", word)[:6] for word in words)
    forms = [title, camel, lower, shortened, shortened_title, swapped, swapped_camel, no_vowels]
    body = random.choice(forms)

    # Profile and channel each draw a fully independent version. This keeps
    # their usernames different instead of mechanically appending a marker.
    if max_digits and random.random() < 0.72:
        digits = "".join(random.choices("23456789", k=random.randint(1, min(max_digits, 4))))
        return f"{body[:32 - len(digits)]}{digits}"[:32]
    if not max_digits:
        # A short lower-case tail keeps word-based names available without
        # forcing digits or underscores into projects that prohibit them.
        suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(2, 5)))
        return f"{body[:32 - len(suffix)]}{suffix}"[:32]
    suffix = random.choice(("Gen", "Pro", "Hub", "Go", "Lab", "Now"))
    return f"{body[:32 - len(suffix)]}{suffix}"[:32]


async def choose_usernames(
    client: TelegramClient,
    base: str,
    reserved_usernames: set[str] | None = None,
    max_digits: int = 5,
) -> tuple[str, str]:
    """Reserve distinct, readable usernames after Telegram checks availability."""
    reserved = {item.lower() for item in (reserved_usernames or set())}
    for _ in range(120):
        account_name = random_username(base, max_digits=max_digits)
        if account_name.lower() in reserved:
            continue
        try:
            account_free = await client(functions.account.CheckUsernameRequest(account_name))
            if not account_free:
                continue
            for _ in range(30):
                channel_name = random_username(base, channel=True, max_digits=max_digits)
                if channel_name.lower() not in reserved and channel_name.lower() != account_name.lower():
                    # The channel does not exist yet. Its availability is checked when created.
                    return account_name, channel_name
        except (UsernameInvalidError, UsernameOccupiedError):
            continue
    raise RuntimeError("Не удалось подобрать разные свободные username за 120 попыток")


async def _human_pause(min_minutes: int, max_minutes: int) -> None:
    """Wait a random 3:00–6:59-style interval between mutations."""
    min_seconds = max(0, min_minutes) * 60
    max_seconds = max(min_seconds, max(0, max_minutes) * 60 + 59)
    await asyncio.sleep(random.randint(min_seconds, max_seconds))


async def _photo_path(client: TelegramClient, entity: object, directory: Path, name: str) -> Path | None:
    try:
        result = await client.download_profile_photo(entity, file=directory / name)
        return Path(result) if result else None
    except Exception:
        return None


async def replace_profile_photos(
    source_client: TelegramClient,
    target_client: TelegramClient,
    source_profile: object,
    directory: Path,
    action_pause_min_minutes: int,
    action_pause_max_minutes: int,
    limit: int = 5,
) -> int:
    """Replace target avatar history with up to five photos from the source.

    Telegram lists profile pictures newest first. Uploading them oldest first
    retains the source's current photo as the target's current photo. Removing
    the existing target photos first prevents stale images when a setup is
    repaired or run again.
    """
    source_photos = [photo async for photo in source_client.iter_profile_photos(source_profile, limit=limit)]
    if not source_photos:
        return 0
    # Download every source image before touching the target.  A transport
    # failure while fetching an old source picture must leave the target's
    # existing main avatar intact.  Telegram returns the photos newest first;
    # uploading this reversed list keeps the source's current avatar current.
    local_paths: list[Path] = []
    for index, photo in enumerate(reversed(source_photos), start=1):
        local_path = await source_client.download_media(photo, file=directory / f"profile-{index}.jpg")
        if not local_path:
            raise RuntimeError(f"не удалось скачать фото профиля №{index}")
        local_paths.append(Path(local_path))
    target_photos = [photo async for photo in target_client.iter_profile_photos("me")]
    input_photos = []
    for photo in target_photos:
        with contextlib.suppress(TypeError, ValueError):
            input_photos.append(utils.get_input_photo(photo))
    if input_photos:
        await target_client(functions.photos.DeletePhotosRequest(id=input_photos))
        await _human_pause(action_pause_min_minutes, action_pause_max_minutes)
    uploaded = 0
    for index, local_path in enumerate(local_paths, start=1):
        await target_client(functions.photos.UploadProfilePhotoRequest(file=await target_client.upload_file(local_path)))
        uploaded += 1
        if index < len(local_paths):
            await _human_pause(action_pause_min_minutes, action_pause_max_minutes)
    return uploaded


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
    local_path: str | None = None
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
    finally:
        # Uploading has completed by this point; retaining every source file
        # until a long archive finishes makes a large queue consume the disk.
        if local_path:
            with contextlib.suppress(OSError):
                Path(local_path).unlink()
    return None


async def publish_queued_story(
    target_session: str,
    api_id: int,
    api_hash: str,
    *,
    file_id: str,
    media_kind: str,
    file_name: str | None,
    mime_type: str | None,
    caption: str,
    video_duration: int,
    video_width: int,
    video_height: int,
    duration_seconds: int,
    idempotency_id: int,
    download_file: Callable[[str, Path], Awaitable[object]],
) -> int:
    """Publish one queued Bot API file and remove its local copy immediately.

    The queue stores Telegram's reusable ``file_id``, never the media bytes.
    Consequently an arbitrarily long batch has a bounded disk footprint.
    ``random_id`` remains stable across retries so a timeout after Telegram
    accepts the story cannot turn into a duplicate post.
    """
    if media_kind not in {"photo", "video"}:
        raise ValueError("unsupported story media kind")
    client = client_from_session(target_session, api_id, api_hash)
    suffix = Path(file_name or ("story.mp4" if media_kind == "video" else "story.jpg")).suffix
    if not suffix or len(suffix) > 12:
        suffix = ".mp4" if media_kind == "video" else ".jpg"
    try:
        await client.connect()
        with tempfile.TemporaryDirectory(prefix="tg-story-upload-") as directory:
            path = Path(directory) / f"story{suffix}"
            await download_file(file_id, path)
            uploaded = await client.upload_file(path)
            if media_kind == "photo":
                media = types.InputMediaUploadedPhoto(file=uploaded)
            else:
                media = types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type=mime_type or "video/mp4",
                    attributes=[
                        types.DocumentAttributeVideo(
                            duration=max(0, int(video_duration)),
                            w=max(0, int(video_width)),
                            h=max(0, int(video_height)),
                            supports_streaming=True,
                        )
                    ],
                )
            response = await client(
                functions.stories.SendStoryRequest(
                    peer=types.InputPeerSelf(),
                    media=media,
                    caption=caption[:2048],
                    entities=[],
                    privacy_rules=[types.InputPrivacyValueAllowAll()],
                    period=duration_seconds,
                    pinned=False,
                    noforwards=False,
                    random_id=_story_random_id(target_session, idempotency_id),
                )
            )
            return _sent_story_id(response)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _source_stories(client: TelegramClient, peer: object) -> list[object]:
    """The original full copier copies pinned stories; active stories are added as well."""
    found: dict[int, object] = {}
    with contextlib.suppress(Exception):
        offset_id = 0
        while True:
            pinned = await client(functions.stories.GetPinnedStoriesRequest(peer=peer, offset_id=offset_id, limit=100))
            page = getattr(pinned, "stories", [])
            for item in page:
                found[item.id] = item
            if len(page) < 100:
                break
            next_offset = page[-1].id
            if next_offset == offset_id:
                break
            offset_id = next_offset
    with contextlib.suppress(Exception):
        active = await client(functions.stories.GetPeerStoriesRequest(peer=peer))
        active_items = getattr(active, "stories", [])
        # Telethon wraps the list in PeerStories on current Telegram layers.
        if not isinstance(active_items, (list, tuple)):
            active_items = getattr(active_items, "stories", [])
        for item in active_items:
            found[item.id] = item
    # Telegram returns newest-first in some endpoints. Copy chronologically so
    # the first source story is also the first story on the new account.
    return sorted(found.values(), key=lambda story: (getattr(story, "date", None), story.id))


async def _target_story_count(client: TelegramClient) -> int:
    """Count both archived/pinned and active target stories without duplicates."""
    found: set[int] = set()
    offset_id = 0
    while True:
        pinned = await client(
            functions.stories.GetPinnedStoriesRequest(peer=types.InputPeerSelf(), offset_id=offset_id, limit=100)
        )
        page = getattr(pinned, "stories", [])
        found.update(story.id for story in page)
        if len(page) < 100:
            break
        offset_id = page[-1].id
    active = await client(functions.stories.GetPeerStoriesRequest(peer=types.InputPeerSelf()))
    active_items = getattr(active, "stories", [])
    if not isinstance(active_items, (list, tuple)):
        active_items = getattr(active_items, "stories", [])
    found.update(story.id for story in active_items)
    return len(found)


async def copy_full_stories(
    source_client: TelegramClient,
    target_client: TelegramClient,
    source: object,
    progress: Progress,
    interval_seconds: int,
) -> int:
    """Port of the FULL_CRM full-story approach: all pinned stories, pinned on target, in order."""
    stories = await _source_stories(source_client, source)
    if not stories:
        await progress("У источника нет доступных историй для полного копирования")
        return 0
    # A restart must continue after the last successfully sent story rather
    # than duplicate the beginning of the archive.
    existing = min(await _target_story_count(target_client), len(stories))
    directory = Path(tempfile.mkdtemp(prefix="tg_stories_"))
    copied = existing
    try:
        for index, story in enumerate(stories[existing:], start=existing + 1):
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
            except Exception as exc:
                await progress(f"История {index}/{len(stories)} пропущена: {type(exc).__name__}")
                continue
            # Keep the configured cadence exact: one story per hour by default.
            if index < len(stories) and interval_seconds > 0:
                await asyncio.sleep(interval_seconds)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return copied


async def _pinned_source_stories(client: TelegramClient, peer: object) -> list[object]:
    """Return only permanent profile stories, in Telegram's display order."""
    found: list[object] = []
    offset_id = 0
    while True:
        pinned = await client(functions.stories.GetPinnedStoriesRequest(peer=peer, offset_id=offset_id, limit=100))
        page = list(getattr(pinned, "stories", []) or [])
        found.extend(page)
        if len(page) < 100:
            break
        next_offset = page[-1].id
        if next_offset == offset_id:
            break
        offset_id = next_offset
    return found


async def _selected_source_stories(
    client: TelegramClient, peer: object, story_ids: tuple[int, ...], include_pinned: bool = True
) -> tuple[list[object], list[int], set[int]]:
    """Keep the fixed order, then append the latest three pinned stories."""
    requested = await client(functions.stories.GetStoriesByIDRequest(peer=peer, id=list(story_ids)))
    by_id = {story.id: story for story in (getattr(requested, "stories", []) or [])}
    missing = [story_id for story_id in story_ids if story_id not in by_id]
    selected = [by_id[story_id] for story_id in story_ids if story_id in by_id]
    selected_ids = {story.id for story in selected}
    target_pinned_ids: set[int] = set()
    if include_pinned:
        # Telegram returns the pinned list in profile display order. Only its
        # first three entries are requested for the durable Apple set and stay
        # last in the target publication sequence.
        for story in (await _pinned_source_stories(client, peer))[:3]:
            target_pinned_ids.add(story.id)
            if story.id not in selected_ids:
                selected.append(story)
                selected_ids.add(story.id)
    return selected, missing, target_pinned_ids


def _sent_story_id(response: object) -> int:
    """Telethon returns an Updates container for SendStoryRequest."""
    for update in getattr(response, "updates", []) or []:
        story = getattr(update, "story", None)
        story_id = getattr(story, "id", None)
        if story_id is not None:
            return int(story_id)
    raise RuntimeError("Telegram accepted историю, но не вернул её ID")


def _story_random_id(target_session: str, source_story_id: int) -> int:
    """Keep retries idempotent when Telegram accepted a story before a crash."""
    payload = f"{target_session}:{source_story_id}".encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big", signed=True)


async def _multicrm_story_bridge(payload: dict[str, object]) -> dict[str, object]:
    """Run the same Kurigram `file_id` story path used by Multi CRM."""
    bridge = Path(__file__).with_name("multicrm_story_bridge.py")
    runtime = Path(os.environ.get("MULTICRM_PYTHON", "/root/FULL_CRM/venv/bin/python"))
    if not bridge.exists() or not runtime.exists():
        raise RuntimeError("не найден runtime Multi CRM для копирования историй")
    directory = Path(tempfile.mkdtemp(prefix="tg_multicrm_story_"))
    config_path = directory / "request.json"
    try:
        request = dict(payload)
        request["work_dir"] = str(directory)
        config_path.write_text(json.dumps(request, ensure_ascii=False))
        os.chmod(config_path, 0o600)

        def execute() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(runtime), str(bridge), str(config_path)],
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )

        result = await asyncio.to_thread(execute)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or not lines:
            detail = (result.stderr or result.stdout or "нет ответа от Multi CRM").strip()
            raise RuntimeError(f"Multi CRM story bridge: {detail[-500:]}")
        try:
            response = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("Multi CRM вернул некорректный ответ") from exc
        if not isinstance(response, dict):
            raise RuntimeError("Multi CRM вернул некорректный ответ")
        return response
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def copy_selected_stories_in_background(
    target_session: str,
    source_session: str | None,
    source_profile_ref: str,
    api_id: int,
    api_hash: str,
    story_ids: tuple[int, ...],
    completed_source_ids: set[int],
    mark_copied: Callable[[int, int], Awaitable[None]],
    progress: Progress,
    interval_min_seconds: int,
    interval_max_seconds: int,
    schedule_next_pause: Callable[[int], Awaitable[None]] | None = None,
    include_pinned: bool = True,
    target_phone: str | None = None,
) -> int:
    """Copy fixed source IDs through Multi CRM's proven file-id publisher."""
    copied = 0
    response = await _multicrm_story_bridge(
        {"target_session": target_session, "source_username": source_profile_ref, "target_phone": target_phone or ""}
    )
    available_ids = {int(value) for value in response.get("story_ids", [])}
    missing = [story_id for story_id in story_ids if story_id not in available_ids]
    if missing:
        await progress("Не найдены истории источника: " + ", ".join(str(item) for item in missing))
        raise RuntimeError("часть обязательных историй источника недоступна")
    ordered_ids = list(story_ids)
    target_pinned_ids: set[int] = set()
    try:
        if include_pinned:
            target = client_from_session(target_session, api_id, api_hash)
            source = client_from_session(source_session or target_session, api_id, api_hash)
            try:
                await target.connect()
                await source.connect()
                source_profile = await source.get_entity(source_profile_ref)
                pinned = [int(story.id) for story in (await _pinned_source_stories(source, source_profile))[:3]]
                target_pinned_ids = set(pinned)
                for story_id in pinned:
                    if story_id not in available_ids:
                        raise RuntimeError(f"закреплённая история {story_id} недоступна")
                    if story_id not in ordered_ids:
                        ordered_ids.append(story_id)
            finally:
                with contextlib.suppress(Exception):
                    await target.disconnect()
                with contextlib.suppress(Exception):
                    await source.disconnect()
        if not ordered_ids:
            raise RuntimeError("у источника нет доступных выбранных историй")
        await progress(f"Фоновая очередь историй: {len(ordered_ids)}; уже готово: {len(completed_source_ids)}")
        failed_story_ids: list[int] = []
        for position, source_story_id in enumerate(ordered_ids, start=1):
            if source_story_id in completed_source_ids:
                continue
            try:
                sent = await _multicrm_story_bridge(
                    {
                        "target_session": target_session,
                        "source_username": source_profile_ref,
                        "target_phone": target_phone or "",
                        "story_id": source_story_id,
                        "period_seconds": 6 * 3600,
                        "pinned": source_story_id in target_pinned_ids,
                    }
                )
            except Exception as exc:
                await progress(f"История {source_story_id} временно не отправлена: {type(exc).__name__}")
                failed_story_ids.append(source_story_id)
                continue
            if not sent.get("ok"):
                await progress(f"История {source_story_id} временно не отправлена: {sent.get('error', 'неизвестная ошибка')}")
                failed_story_ids.append(source_story_id)
                continue
            await mark_copied(source_story_id, int(sent["target_story_id"]))
            completed_source_ids.add(source_story_id)
            copied += 1
            await progress(f"История {source_story_id} опубликована ({position}/{len(ordered_ids)})")
            if any(item not in completed_source_ids for item in ordered_ids[position:]):
                pause = random.randint(interval_min_seconds, interval_max_seconds)
                if schedule_next_pause:
                    await schedule_next_pause(int(time.time()) + pause)
                await progress(f"Следующая история через {pause // 60} мин {pause % 60} сек")
                await asyncio.sleep(pause)
        if failed_story_ids:
            raise RuntimeError("не опубликованы истории: " + ", ".join(str(item) for item in failed_story_ids))
    finally:
        # Per-story bridge directories are cleaned inside the bridge helper.
        pass
    return copied


async def copy_stories_in_background(
    target_session: str,
    source_session: str | None,
    source_profile_ref: str | None,
    api_id: int,
    api_hash: str,
    progress: Progress,
    interval_seconds: int,
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
        return await copy_full_stories(source, target, source_profile, progress, interval_seconds)
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
    name_emoji: str | None,
    reserved_usernames: set[str],
    action_pause_min_minutes: int,
    action_pause_max_minutes: int,
    progress: Progress,
    create_channel: bool = True,
    username_max_digits: int = 5,
    assign_profile_username: bool = True,
    copy_premium_emoji_status: bool = False,
    copy_profile_color: bool = True,
) -> dict[str, object]:
    """Copies the source profile and, when requested, its personal channel."""
    source_profile = await source_client.get_entity(source_profile_ref) if source_profile_ref else await source_client.get_me()
    source_full_result = await source_client(functions.users.GetFullUserRequest(source_profile))
    source_full = source_full_result.full_user
    source_channel = None
    if create_channel:
        source_channel_id = getattr(source_full, "personal_channel_id", None)
        if not source_channel_id:
            raise RuntimeError("У аккаунта-источника не указан личный канал")
        source_channel = next(
            (chat for chat in getattr(source_full_result, "chats", []) if getattr(chat, "id", None) == source_channel_id),
            None,
        )
        if not source_channel:
            source_channel = await source_client.get_entity(types.PeerChannel(source_channel_id))
    # Telegram limits an account bio more strictly than some source profiles.
    # Keep the start of the source text so ordinary (non-Premium) setup cannot
    # fail solely because a source description is longer than the API allows.
    bio = (getattr(source_full, "about", "") or "")[:70]
    first_name = without_emoji(getattr(source_profile, "first_name", "") or "") or "Telegram"
    last_name = without_emoji(getattr(source_profile, "last_name", "") or "")
    source_emoji_status = getattr(source_profile, "emoji_status", None)
    if copy_premium_emoji_status and source_emoji_status:
        # Emoji status is optional presentation, never a prerequisite for a
        # usable account.  Telegram can return newer TL constructors here, so
        # a failed cosmetic copy must not abort the whole preparation.
        try:
            await client(functions.account.UpdateEmojiStatusRequest(source_emoji_status))
            await progress("Скопирован Premium emoji-status; жду перед изменением профиля")
            await _human_pause(action_pause_min_minutes, action_pause_max_minutes)
        except Exception as error:
            await progress(f"Не удалось скопировать Premium emoji-status: {type(error).__name__}")
    await progress("Копирую имя и описание профиля; добавляю уникальный деловой эмодзи")
    try:
        await client(
            functions.account.UpdateProfileRequest(
                first_name=f"{first_name} {name_emoji or ''}".strip(), last_name=last_name, about=bio
            )
        )
    except TypeNotFoundError:
        # Telegram can accept the update but return a newer User constructor
        # unknown to this Telethon layer. The profile change is already live;
        # continue rather than creating a false failed setup.
        await progress("Имя обновлено; Telegram вернул неподдерживаемый объект профиля")
    await _human_pause(action_pause_min_minutes, action_pause_max_minutes)

    profile_color = getattr(source_profile, "profile_color", None)
    if copy_profile_color and profile_color:
        with contextlib.suppress(Exception):
            await client(functions.account.UpdateColorRequest(for_profile=True, color=profile_color))
            await progress("Скоплены цвета профиля")
            await _human_pause(action_pause_min_minutes, action_pause_max_minutes)

    # The separately assigned project emoji exists only in the visible name.

    current_username = (getattr(await client.get_me(), "username", None) or "").lstrip("@").strip()
    if current_username:
        # A retry after a partial setup must keep the already accepted profile
        # username. Only the channel still needs an independent candidate.
        occupied = set(reserved_usernames)
        occupied.add(current_username.lower())
        _unused_profile_username, channel_username = await choose_usernames(
            client, username_seed, occupied, max_digits=username_max_digits
        )
        account_username = current_username
    elif assign_profile_username:
        account_username, channel_username = await choose_usernames(
            client, username_seed, reserved_usernames, max_digits=username_max_digits
        )
        await client(functions.account.UpdateUsernameRequest(account_username))
        await progress(f"Username профиля: @{account_username}")
        await _human_pause(action_pause_min_minutes, action_pause_max_minutes)
    else:
        # The profile username is operator-managed, but the personal channel
        # still needs its own generated public username.
        account_username = None
        _unused_profile_username, channel_username = await choose_usernames(
            client, username_seed, reserved_usernames, max_digits=username_max_digits
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="tg_setup_"))
    try:
        await replace_profile_photos(
            source_client,
            client,
            source_profile,
            temp_dir,
            action_pause_min_minutes,
            action_pause_max_minutes,
            limit=5,
        )

        if not create_channel:
            return {
                "username": account_username,
                "name_emoji": name_emoji,
                "channel_username": None,
                "channel_id": None,
                "posts": 0,
                "stories": 0,
            }

        channel_title = getattr(source_channel, "title", "") or first_name
        source_channel_full = await source_client(functions.channels.GetFullChannelRequest(source_channel))
        channel_about = getattr(getattr(source_channel_full, "full_chat", None), "about", "") or ""
        created = await client(
            functions.channels.CreateChannelRequest(title=channel_title, about=channel_about, megagroup=False)
        )
        channel = created.chats[0]
        await _human_pause(action_pause_min_minutes, action_pause_max_minutes)
        taken_usernames = {item.lower() for item in reserved_usernames}
        if account_username:
            taken_usernames.add(account_username.lower())
        for attempt in range(30):
            candidate = channel_username if attempt == 0 else random_username(username_seed, channel=True)
            if candidate.lower() in taken_usernames:
                continue
            try:
                available = await client(functions.channels.CheckUsernameRequest(channel=channel, username=candidate))
                if available:
                    await client(functions.channels.UpdateUsernameRequest(channel=channel, username=candidate))
                    channel_username = candidate
                    taken_usernames.add(candidate.lower())
                    await _human_pause(action_pause_min_minutes, action_pause_max_minutes)
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
            await _human_pause(action_pause_min_minutes, action_pause_max_minutes)

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
