"""
Queue manager: turns Telethon messages into persisted jobs in bulk.
Dedup layers:
  1. same source message already queued (existing_message_ids + unique index)
  2. same FILE already in the destination (dest_index) or already queued from
     any source (open job fingerprints)  -> skipped, counted as "skipped"
Albums become ONE job so they stay grouped at the destination.
"""
import logging
from datetime import datetime, timezone
from typing import List, Tuple

from app.database import db
from app.models import JobStatus, Priority
from app.utils.helpers import (caption_title, file_name_of, fingerprints, get_media_type,
                               is_forwardable, media_type_enabled)

logger = logging.getLogger(__name__)


class QueueManager:
    async def enqueue_batch(self, source: dict, messages: list, priority_type: str) -> Tuple[List[dict], int]:
        """Queue messages. Returns (inserted_jobs, skipped_duplicates)."""
        cid = source["channel_id"]
        settings = await db.get_settings()
        dest = settings.get("destination_channel_id")
        if dest == cid:
            return [], 0  # never forward a channel into itself

        msgs = [m for m in messages if is_forwardable(m)]
        if not msgs:
            return [], 0
        seen = await db.existing_message_ids(cid, [m.id for m in msgs])
        msgs = sorted((m for m in msgs if m.id not in seen), key=lambda m: m.id)

        fp_of = {m.id: fingerprints(m) for m in msgs}
        skipped = 0
        if dest and settings.get("dedupe", True):
            def checks(m):  # text hashes only dedupe old (backlog) posts, never live ones
                return [f for f in fp_of[m.id] if priority_type == Priority.BACKLOG or not f.startswith("t:")]
            allf = list({f for m in msgs for f in checks(m)})
            blocked = (await db.indexed(dest, allf)) | (await db.open_job_fps(allf))
            kept, seen_b = [], set()
            for m in msgs:
                cs = checks(m)
                if any(f in blocked or f in seen_b for f in cs):
                    skipped += 1
                    continue
                seen_b.update(cs)
                kept.append(m)
            msgs = kept

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
            jobs.append(self._build_job(source, unit, priority_type, seq, fp_of))

        inserted = await db.insert_jobs(jobs)
        n = sum(len(j["message_ids"]) for j in inserted)
        if n:
            await db.incr_source_stat(cid, "discovered", n)
            await db.incr_source_stat(cid, "queued", n)
            await db.incr_stat("total_discovered", n)
            await db.incr_stat("total_queued", n)
        if skipped:
            await db.incr_source_stat(cid, "skipped", skipped)
            await db.incr_stat("total_skipped", skipped)
        return inserted, skipped

    @staticmethod
    def _build_job(source: dict, unit: list, priority_type: str, sequence: int, fp_of: dict) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "source_channel_id": source["channel_id"],
            "source_message_id": unit[0].id,
            "message_ids": [m.id for m in unit],
            "media_type": get_media_type(unit[0]),
            "title": next((caption_title(m) for m in unit if (m.message or "").strip()), caption_title(unit[0])),
            "file_name": file_name_of(unit[0]),
            "file_size": sum((getattr(m.file, "size", 0) or 0) for m in unit if m.file),
            "fps": [f for m in unit for f in fp_of.get(m.id, [])],
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
