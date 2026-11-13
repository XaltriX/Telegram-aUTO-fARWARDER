"""
Queue manager: turns raw Telethon messages into persisted job documents,
with strict duplicate protection and the LIVE/BACKLOG priority model.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from telethon.tl.types import Message

from app.database import db
from app.models import JobStatus, Priority
from app.utils.helpers import get_media_type, media_type_enabled

logger = logging.getLogger(__name__)


class QueueManager:
    async def enqueue_single(self, source: dict, message: Message, priority_type: str) -> Optional[dict]:
        """Enqueue one non-grouped message. Returns the job dict, or None if
        it was a duplicate or filtered out."""
        media_type = get_media_type(message)
        settings = await db.get_settings()
        if not media_type_enabled(media_type, settings):
            return None

        channel_id = source["channel_id"]
        reserved = await db.reserve_message_ids(channel_id, [message.id], job_key=f"single:{message.id}")
        if not reserved:
            return None  # already processed/queued before

        job = await self._build_job(source, message, [message.id], media_type,
                                     priority_type, grouped_id=None)
        inserted = await db.insert_job(job)
        if inserted is None:
            # Unique index caught a race we didn't catch via the ledger - rollback ledger entry.
            await db.release_message_ids(channel_id, [message.id])
            return None

        await db.incr_source_stat(channel_id, "discovered")
        await db.incr_source_stat(channel_id, "queued")
        await db.incr_stat("total_discovered")
        await db.incr_stat("total_queued")
        return inserted

    async def enqueue_album(self, source: dict, messages: List[Message], priority_type: str) -> Optional[dict]:
        """
        Enqueue a full album (grouped media) as a SINGLE job so it can be
        forwarded in one Telegram operation and stay grouped at the
        destination. If every message in the album is filtered out by media
        settings, the whole album is skipped. If some (but not all) types
        are disabled, we still forward the whole album intact - Telegram
        albums cannot be partially forwarded without breaking the grouping,
        and the spec prioritizes preserving album integrity.
        """
        settings = await db.get_settings()
        eligible = [m for m in messages if media_type_enabled(get_media_type(m), settings)]
        if not eligible:
            return None

        messages = sorted(messages, key=lambda m: m.id)
        channel_id = source["channel_id"]
        ids = [m.id for m in messages]
        reserved = await db.reserve_message_ids(channel_id, ids, job_key=f"album:{ids[0]}")
        if not reserved:
            return None
        if len(reserved) != len(ids):
            # Partial overlap with an existing job - forward only the newly
            # reserved subset to avoid re-sending already-handled messages.
            messages = [m for m in messages if m.id in reserved]
            ids = reserved

        first = messages[0]
        media_type = get_media_type(first)
        job = await self._build_job(source, first, ids, media_type, priority_type,
                                     grouped_id=first.grouped_id)
        inserted = await db.insert_job(job)
        if inserted is None:
            await db.release_message_ids(channel_id, ids)
            return None

        await db.incr_source_stat(channel_id, "discovered", len(ids))
        await db.incr_source_stat(channel_id, "queued")
        await db.incr_stat("total_discovered", len(ids))
        await db.incr_stat("total_queued")
        return inserted

    async def _build_job(self, source: dict, anchor_message: Message, message_ids: List[int],
                          media_type: str, priority_type: str, grouped_id) -> dict:
        settings = await db.get_settings()
        sequence = await db.next_sequence() if priority_type == Priority.LIVE else 0
        return {
            "source_channel_id": source["channel_id"],
            "source_message_id": anchor_message.id,
            "message_ids": message_ids,
            "destination_channel_id": settings.get("destination_channel_id"),
            "media_type": media_type,
            "grouped_id": grouped_id,
            "priority_type": priority_type,
            "status": JobStatus.PENDING,
            "source_order": source["source_order"],
            "sequence": sequence,
            "detected_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
            "attempts": 0,
            "last_error": None,
            "retry_after": None,
            "forwarded_message_id": None,
        }


queue_manager = QueueManager()
