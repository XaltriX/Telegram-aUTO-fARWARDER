"""
Misc small helpers shared across modules.
"""
import asyncio
import hashlib
import html
import time
from datetime import datetime, timezone
from typing import Optional

from telethon.tl.types import (
    Message,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
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


def fingerprints(m) -> list:
    """Identity keys of a message's content, used for duplicate detection.
    d:/p: = Telegram document/photo id (stays the same when a file is re-sent by
    reference), n:/s: = name+size / size+duration fallback, t: = text hash."""
    if not isinstance(m, Message) or getattr(m, "action", None):
        return []
    fps = []
    try:
        if m.document:
            d = m.document
            fps.append(f"d:{d.id}")
            f = m.file
            name = (f.name or "").strip().lower() if f else ""
            if name:
                fps.append(f"n:{name}|{d.size}")
            elif f and f.duration:
                fps.append(f"s:{d.size}|{f.duration}")
        elif m.photo and not m.web_preview:
            fps.append(f"p:{m.photo.id}")
        elif not m.media or m.web_preview:
            text = (m.message or "").strip()
            if len(text) >= 20:
                fps.append("t:" + hashlib.sha1(text.lower().encode()).hexdigest()[:20])
    except Exception:
        pass
    return fps


def is_forwardable(message) -> bool:
    """False for service messages (pins, joins...), empty/deleted messages."""
    if not isinstance(message, Message) or getattr(message, "action", None):
        return False
    return bool(message.media or (message.message or "").strip())


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "\u2591" * width
    filled = min(width, int(width * done / total))
    return "\u2593" * filled + "\u2591" * (width - filled)


TYPE_ICON = {"video": "\U0001F3AC", "photo": "\U0001F5BC", "audio": "\U0001F3B5",
             "voice": "\U0001F399", "document": "\U0001F4C4", "text": "\U0001F4DD"}


def human_size(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def time_ago(dt) -> str:
    if not dt:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return f"{max(s, 0)}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def message_link(source: dict, msg_id: int) -> str:
    """Public link t.me/<user>/<id>, or private-channel link t.me/c/<id>/<id>."""
    if source.get("username"):
        return f"https://t.me/{source['username']}/{msg_id}"
    s = str(abs(int(source["channel_id"])))
    if s.startswith("100") and len(s) > 10:
        s = s[3:]
    return f"https://t.me/c/{s}/{msg_id}"


def file_name_of(message) -> str:
    try:
        if message.file and message.file.name:
            return message.file.name
        if message.file:
            return f"{get_media_type(message)}_{message.id}{message.file.ext or ''}"
    except Exception:
        pass
    return ""


def caption_title(message, limit: int = 80) -> str:
    """Best human label: first caption line, else file name, else type+id."""
    for line in (message.message or "").splitlines():
        if line.strip():
            return truncate(line.strip(), limit)
    return file_name_of(message) or f"{get_media_type(message)} #{message.id}"


def source_progress_text(s: dict, prog: dict, active: bool = False) -> str:
    """Multi-line HTML block: bar, sent / left / total for one source."""
    st = s.get("stats", {})
    done = st.get("forwarded", 0)
    p = prog.get(s["channel_id"], {})
    left, failed = p.get("left", 0), p.get("failed", 0)
    total = done + left + failed
    pct = int(100 * done / total) if total else 0
    icon = "\u25B6\uFE0F" if active else ("\U0001F7E2" if s.get("enabled", True) else "\u26AA")
    out = (f"{icon} <b>{escape_html(truncate(s['title'], 28))}</b>\n"
           f"   {progress_bar(done, total)} {pct}%\n"
           f"   \u2705 {done:,} sent  \u23F3 {left:,} left  \U0001F4E5 {total:,} total")
    if failed:
        out += f"  \u274C {failed:,}"
    if st.get("skipped"):
        out += f"\n   \u267B\uFE0F {st['skipped']:,} duplicates skipped"
    if s.get("scan_done") is False:
        sp = s.get("scan_progress") or {}
        if sp.get("state") == "scanning" and sp.get("total"):
            out += (f"\n   \U0001F50D scanning {progress_bar(sp['scanned'], sp['total'])} "
                    f"{int(100 * sp['scanned'] / sp['total'])}% ({sp['scanned']:,}/{sp['total']:,})")
        else:
            out += "\n   \u23F3 waiting for destination index\u2026"
    return out


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
    import telethon.errors as _te
    restricted = getattr(_te, "ChatForwardsRestrictedError", None)

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
    ) + ((restricted,) if restricted else ())
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
