"""
Queue manager: turns Telethon messages into persisted jobs in bulk.
Dedup = already-queued message ids lookup + unique index on jobs.
Albums become ONE job so they stay grouped at the destination.
"""
import logging
from datetime import datetime, timezone
from typing import List

from app.database import db
from app.models import JobStatus, Priority
from app.utils.helpers import get_media_type, is_forwardable, media_type_enabled

logger = logging.getLogger(__name__)


class QueueManager:
    async def enqueue_batch(self, source: dict, messages: list, priority_type: str) -> List[dict]:
        """Queue messages (any mix of singles/albums). Returns inserted jobs."""
        cid = source["channel_id"]
        settings = await db.get_settings()
        if settings.get("destination_channel_id") == cid:
            return []  # never forward a channel into itself (loop guard)

        msgs = [m for m in messages if is_forwardable(m)]
        if not msgs:
            return []
        seen = await db.existing_message_ids(cid, [m.id for m in msgs])
        msgs = sorted((m for m in msgs if m.id not in seen), key=lambda m: m.id)

        units, groups = [], {}
        for m in msgs:
            if m.grouped_id is None:
                units.append([m])
            else:
                g = groups.get(m.grouped_id)
                if g is None:
                    g = groups[m.grouped_id] = []
                    units.append(g)
                g.append(m)

        jobs = []
        for unit in units:
            if not any(media_type_enabled(get_media_type(m), settings) for m in unit):
                continue
            seq = await db.next_sequence() if priority_type == Priority.LIVE else 0
            jobs.append(self._build_job(source, unit, priority_type, seq))

        inserted = await db.insert_jobs(jobs)
        n = sum(len(j["message_ids"]) for j in inserted)
        if n:
            await db.incr_source_stat(cid, "discovered", n)
            await db.incr_source_stat(cid, "queued", n)
            await db.incr_stat("total_discovered", n)
            await db.incr_stat("total_queued", n)
        return inserted

    @staticmethod
    def _build_job(source: dict, unit: list, priority_type: str, sequence: int) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "source_channel_id": source["channel_id"],
            "source_message_id": unit[0].id,
            "message_ids": [m.id for m in unit],
            "media_type": get_media_type(unit[0]),
            "grouped_id": unit[0].grouped_id,
            "priority_type": priority_type,
            "status": JobStatus.PENDING,
            "source_order": source["source_order"],
            "sequence": sequence,
            "detected_at": now,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "attempts": 0,
            "last_error": None,
            "retry_after": None,
            "forwarded_message_id": None,
        }


queue_manager = QueueManager()
