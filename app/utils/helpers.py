"""
Misc small helpers shared across modules.
"""
import asyncio
import html
import time
from datetime import datetime, timezone
from typing import Optional

from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    Message,
)

from app.models import MEDIA_TYPES


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_ts() -> float:
    return time.time()


def get_media_type(message: Message) -> str:
    """
    Classify a Telethon Message into one of MEDIA_TYPES.
    Falls back to 'text' for plain text-only messages.
    """
    if message is None:
        return "text"

    if message.voice:
        return "voice"

    if message.video or message.video_note:
        return "video"

    if message.photo:
        return "photo"

    if message.audio:
        return "audio"

    if message.document:
        # A document might still be a video/audio/voice sent as a generic
        # document (e.g. .mp4 uploaded as file) - inspect attributes.
        doc = message.document
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeVideo):
                return "video"
            if isinstance(attr, DocumentAttributeAudio):
                return "voice" if attr.voice else "audio"
        return "document"

    if message.text or message.message:
        return "text"

    return "document"


def media_type_enabled(media_type: str, settings: dict) -> bool:
    filters = settings.get("media_filters", {}) if settings else {}
    return bool(filters.get(media_type, True))


def truncate(text: Optional[str], length: int = 60) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= length else text[: length - 1] + "\u2026"


def escape_html(text: Optional[str]) -> str:
    return html.escape(text or "")


def get_filename(message: Message) -> str:
    if not message:
        return "unknown"
    if message.file and message.file.name:
        return message.file.name
    media_type = get_media_type(message)
    if media_type == "photo":
        return "photo.jpg"
    if media_type == "text":
        return truncate(message.text, 40) or "text message"
    return f"{media_type}_{message.id}"


def is_retryable_exception(exc: Exception) -> bool:
    """
    Best-effort classification. FloodWaitError is handled separately
    (it is always 'retryable' but with a mandatory wait). This function
    is used for everything else: network blips vs. permanent/config errors.
    """
    from telethon.errors import (
        ChannelPrivateError,
        ChatAdminRequiredError,
        ChatWriteForbiddenError,
        UserBannedInChannelError,
        PeerIdInvalidError,
        ChannelInvalidError,
        MessageIdInvalidError,
        UsernameNotOccupiedError,
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedBanError,
    )
    from app.models import SourceMessageGoneError

    permanent_types = (
        ChannelPrivateError,
        ChatAdminRequiredError,
        ChatWriteForbiddenError,
        UserBannedInChannelError,
        PeerIdInvalidError,
        ChannelInvalidError,
        MessageIdInvalidError,
        UsernameNotOccupiedError,
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedBanError,
        SourceMessageGoneError,
    )
    if isinstance(exc, permanent_types):
        return False
    # Default: treat unknown errors (network, timeouts, generic RPC hiccups)
    # as retryable, up to MAX_ATTEMPTS.
    return True


async def delete_after(client, chat_id, message_id, delay: float):
    """Fire-and-forget deletion of a temporary notification message."""
    try:
        await asyncio.sleep(delay)
        await client.delete_messages(chat_id, [message_id])
    except Exception:
        # Message may already be gone (chat cleared, etc). Non-fatal.
        pass


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]
