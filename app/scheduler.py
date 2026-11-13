"""
The single authoritative forwarding scheduler.

There is exactly ONE instance of this class running, and exactly one
asyncio task executing its main loop, guaranteeing there is never more
than one forwarding operation in flight through the personal account at
a time (point 16/36 of the spec).

Priority rule (point 41/42): on every iteration we ask the database for
the next job - LIVE jobs (ordered by global arrival sequence) are always
returned before BACKLOG jobs (ordered by source registration order, then
message id). This check happens after EVERY completed job, so a stream of
continuously arriving LIVE files can indefinitely delay backlog without
ever forcing a backlog item in between.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

from app.config import config
from app.database import db
from app.models import JobStatus, SourceMessageGoneError
from app.telegram_client import account_manager
from app.utils.helpers import is_retryable_exception

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self):
        self.running = False
        self._stop_event = asyncio.Event()
        self.on_job_started: Optional[Callable] = None
        self.on_job_done: Optional[Callable] = None
        self.on_job_failed: Optional[Callable] = None
        self.on_flood_wait: Optional[Callable] = None
        self._entity_cache = {}

    async def _get_entity(self, client, channel_id: int):
        if channel_id in self._entity_cache:
            return self._entity_cache[channel_id]
        entity = await client.get_entity(channel_id)
        self._entity_cache[channel_id] = entity
        return entity

    async def run(self):
        self.running = True
        logger.info("Scheduler started")
        # Recover jobs that were left mid-flight by an unclean shutdown.
        await db.recover_stuck_jobs()

        while self.running:
            try:
                settings = await db.get_settings()

                if settings.get("paused"):
                    await asyncio.sleep(1)
                    continue

                flood_until = settings.get("flood_wait_until")
                if flood_until and datetime.now(timezone.utc).timestamp() < flood_until:
                    await asyncio.sleep(1)
                    continue

                client = await account_manager.get_client()
                if client is None:
                    # No connected account yet - nothing we can do.
                    await asyncio.sleep(3)
                    continue

                job = await db.claim_next_job()
                if job is None:
                    await asyncio.sleep(1.5)
                    continue

                await self._process_job(client, job)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected scheduler loop error - continuing")
                await asyncio.sleep(2)

        logger.info("Scheduler stopped")

    async def stop(self):
        self.running = False

    # ------------------------------------------------------------------
    async def _process_job(self, client, job: dict):
        job_id = job["_id"]
        if self.on_job_started:
            await self._safe_call(self.on_job_started, job)

        try:
            settings = await db.get_settings()
            destination_id = job.get("destination_channel_id") or settings.get("destination_channel_id")
            if not destination_id:
                await db.fail_job(job_id, "No destination configured")
                if self.on_job_failed:
                    await self._safe_call(self.on_job_failed, job, "No destination configured")
                return

            source_entity = await self._get_entity(client, job["source_channel_id"])
            dest_entity = await self._get_entity(client, destination_id)

            message_ids = job.get("message_ids") or [job["source_message_id"]]
            hide_tag = settings.get("hide_forward_tag", True)

            if hide_tag:
                result = await self._copy_messages(client, source_entity, dest_entity, message_ids)
            else:
                result = await client.forward_messages(dest_entity, message_ids, source_entity)

            forwarded_id = None
            if isinstance(result, list) and result:
                forwarded_id = getattr(result[0], "id", None)
            elif result is not None:
                forwarded_id = getattr(result, "id", None)

            await db.mark_job_done(job_id, forwarded_id)
            await db.record_forward_success()
            await db.incr_source_stat(job["source_channel_id"], "forwarded", len(message_ids))

            if self.on_job_done:
                await self._safe_call(self.on_job_done, job, forwarded_id)

        except FloodWaitError as e:
            wait_seconds = e.seconds
            logger.warning("FloodWait: sleeping %ss (job_id=%s)", wait_seconds, job_id)
            flood_until = (datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)).timestamp()
            await db.set_flood_wait_until(flood_until)
            # Put the job back exactly where it was - no progress lost, no duplicate risk.
            await db.requeue_job(job_id, f"FloodWaitError: {wait_seconds}s")
            if self.on_flood_wait:
                await self._safe_call(self.on_flood_wait, job, wait_seconds)
            await asyncio.sleep(wait_seconds)
            await db.set_flood_wait_until(None)

        except Exception as e:
            error_text = f"{type(e).__name__}: {e}"
            logger.error("Job %s failed: %s", job_id, error_text)
            attempts = job.get("attempts", 0) + 1

            if not is_retryable_exception(e) or attempts >= config.MAX_ATTEMPTS:
                await db.fail_job(job_id, error_text)
                await db.record_forward_failure(error_text)
                await db.incr_source_stat(job["source_channel_id"], "failed")
                if self.on_job_failed:
                    await self._safe_call(self.on_job_failed, job, error_text)
            else:
                backoff = config.RETRY_BACKOFF_BASE * attempts
                retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff)
                await db.requeue_job(job_id, error_text, retry_after=retry_after)
                if self.on_job_failed:
                    await self._safe_call(self.on_job_failed, job, f"{error_text} (retry {attempts}/{config.MAX_ATTEMPTS})")
                await asyncio.sleep(min(backoff, 10))

    @staticmethod
    async def _safe_call(fn, *args):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error in scheduler callback")

    # ------------------------------------------------------------------
    # "Hide forward tag" mode: re-send the content as a brand new message
    # instead of using Telegram's native forward, so the destination never
    # shows "Forwarded from <source channel>". This re-sends the existing
    # media object by reference (no re-download/re-upload of bytes) and
    # copies the caption/text with its original formatting preserved via
    # Telethon's raw `formatting_entities`.
    # ------------------------------------------------------------------
    async def _copy_messages(self, client, source_entity, dest_entity, message_ids: list):
        messages = await client.get_messages(source_entity, ids=message_ids)
        if not isinstance(messages, list):
            messages = [messages]
        if any(m is None for m in messages):
            raise SourceMessageGoneError(
                "One or more source messages no longer exist (deleted before forwarding)."
            )

        if len(messages) > 1:
            return await self._copy_album(client, dest_entity, messages)
        return await self._copy_single(client, dest_entity, messages[0])

    @staticmethod
    async def _copy_single(client, dest_entity, msg):
        if msg.file:
            return await client.send_file(
                dest_entity, msg.media,
                caption=msg.message or "",
                formatting_entities=msg.entities,
                parse_mode=None,
            )
        return await client.send_message(
            dest_entity, msg.message or "",
            formatting_entities=msg.entities,
            parse_mode=None,
        )

    @staticmethod
    async def _copy_album(client, dest_entity, msgs):
        msgs = sorted(msgs, key=lambda m: m.id)
        media_list = [m.media for m in msgs if m.media]
        # NOTE: Telethon's album send only supports one caption string (or
        # a list of per-item plain strings) - per-item rich formatting
        # entities are not preserved for albums the way they are for single
        # messages. This is a known, documented simplification.
        captions = [m.message or "" for m in msgs if m.media]
        return await client.send_file(dest_entity, media_list, caption=captions, parse_mode=None)


class SourceMessageGoneError(Exception):
    """Raised when a source message was deleted before we could copy it."""
    pass


class SourceMessageGoneError(Exception):
    """Raised when a source message was deleted before we could copy it."""
    pass


scheduler = Scheduler()
