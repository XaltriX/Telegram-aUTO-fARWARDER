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

from telethon.errors import FloodWaitError, MediaCaptionTooLongError
from telethon.extensions import html as tg_html
from telethon.tl.types import (MessageMediaDocument, MessageMediaPhoto, MessageMediaWebPage)

from app.config import config
from app.database import db
from app.models import JobStatus, SourceMessageGoneError
from app.telegram_client import account_manager
from app.utils.helpers import is_retryable_exception, message_link

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self):
        self.running = False
        self._stop_event = asyncio.Event()
        self.on_job_started: Optional[Callable] = None
        self.on_job_done: Optional[Callable] = None
        self.on_job_failed: Optional[Callable] = None
        self.on_flood_wait: Optional[Callable] = None
        self._cb_tasks = set()
        self._consec_fail = 0
        self.on_auto_pause: Optional[Callable] = None

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

                if not settings.get("destination_channel_id"):
                    await asyncio.sleep(3)  # nothing to do until a destination exists
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
            destination_id = settings.get("destination_channel_id")
            if not destination_id:
                await db.fail_job(job_id, "No destination configured")
                if self.on_job_failed:
                    await self._safe_call(self.on_job_failed, job, "No destination configured")
                return

            source_entity = await account_manager.get_entity(job["source_channel_id"])
            dest_entity = await account_manager.get_entity(destination_id)

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
            source = await db.get_source(job["source_channel_id"])
            entry = {
                "title": job.get("title") or f"{job['media_type']} #{job['source_message_id']}",
                "link": message_link(source, job["source_message_id"]) if source else None,
                "type": job["media_type"], "n": len(message_ids),
                "size": job.get("file_size") or 0, "at": datetime.now(timezone.utc),
            }
            await db.record_forward_success(len(message_ids), entry)
            self._consec_fail = 0
            await db.incr_source_stat(job["source_channel_id"], "forwarded", len(message_ids))

            if self.on_job_done:
                await self._safe_call(self.on_job_done, job, forwarded_id)

        except FloodWaitError as e:
            wait_seconds = e.seconds
            logger.warning("FloodWait: sleeping %ss (job_id=%s)", wait_seconds, job_id)
            flood_until = (datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)).timestamp()
            await db.set_flood_wait_until(flood_until)
            # Put the job back exactly where it was - no progress lost, no duplicate risk.
            await db.requeue_job(job_id, f"FloodWaitError: {wait_seconds}s", count_attempt=False)
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
                await self._count_failure(e)
                await db.record_forward_failure(error_text)
                await db.incr_source_stat(job["source_channel_id"], "failed")
                if self.on_job_failed:
                    await self._safe_call(self.on_job_failed, job, error_text)
            else:
                backoff = config.RETRY_BACKOFF_BASE * attempts
                retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff)
                await db.requeue_job(job_id, error_text, retry_after=retry_after)
                logger.info("Job %s will retry in %ss (%d/%d)", job_id, backoff, attempts, config.MAX_ATTEMPTS)

    async def _count_failure(self, exc):
        """Many terminal failures in a row = a config problem (permissions,
        wrong destination). Pause instead of burning through the whole queue."""
        if isinstance(exc, SourceMessageGoneError):
            return
        self._consec_fail += 1
        if self._consec_fail >= 10:
            self._consec_fail = 0
            await db.set_paused(True)
            logger.error("10 consecutive failures - auto-paused")
            if self.on_auto_pause:
                await self._safe_call(self.on_auto_pause)

    async def _safe_call(self, fn, *args):
        """Fire-and-forget: UI callbacks must never slow down forwarding."""
        async def _run():
            try:
                result = fn(*args)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Error in scheduler callback")
        t = asyncio.create_task(_run())
        self._cb_tasks.add(t)
        t.add_done_callback(self._cb_tasks.discard)

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
        media = msg.media
        if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
            try:
                return await client.send_file(
                    dest_entity, media, caption=msg.message or "",
                    formatting_entities=msg.entities, parse_mode=None)
            except MediaCaptionTooLongError:
                # account can't attach this long a caption: send file, then the full text under it
                sent = await client.send_file(dest_entity, media)
                await client.send_message(dest_entity, msg.message, formatting_entities=msg.entities,
                                          parse_mode=None, reply_to=sent)
                return sent
        if media is None or isinstance(media, MessageMediaWebPage):
            return await client.send_message(
                dest_entity, msg.message or "", formatting_entities=msg.entities,
                parse_mode=None, link_preview=isinstance(media, MessageMediaWebPage))
        # polls, locations, contacts...: can't be re-sent by reference -> native forward
        return await client.forward_messages(dest_entity, msg)

    @staticmethod
    async def _copy_album(client, dest_entity, msgs):
        msgs = [m for m in sorted(msgs, key=lambda m: m.id) if m.media]
        captions = [tg_html.unparse(m.message or "", m.entities or []) for m in msgs]
        media = [m.media for m in msgs]
        try:
            return await client.send_file(dest_entity, media, caption=captions, parse_mode="html")
        except MediaCaptionTooLongError:
            sent = await client.send_file(dest_entity, media)
            text = "\n\n".join(c for c in captions if c)
            if text:
                await client.send_message(dest_entity, text[:4096], parse_mode="html", reply_to=sent[0])
            return sent


scheduler = Scheduler()
