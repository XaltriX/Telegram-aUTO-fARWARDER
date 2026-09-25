"""
Hybrid monitoring (point 14):

  A) Live update monitoring - Telethon event handlers push new messages
     into the LIVE queue the instant they arrive.

  B) Periodic recovery/sync - a background task that, per source, resumes
     from the stored `last_seen_message_id` checkpoint and enqueues
     anything that was missed (network blip, restart, reconnect) into the
     BACKLOG queue (since it wasn't seen "live"). Duplicate protection
     guarantees nothing already handled gets re-forwarded.

Also provides the initial-history backlog scan used when a source is first
added (ALL / LAST_X / FROM_DATE / NEW_ONLY).
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

from telethon import events
from telethon.errors import FloodWaitError

from app.config import config
from app.database import db
from app.models import Priority, ScanMode
from app.queue_manager import queue_manager
from app.telegram_client import account_manager

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self):
        self._registered_chat_ids = set()
        self._handlers_installed = False
        self._recovery_task: Optional[asyncio.Task] = None
        self._recovery_running = False
        self.on_new_live_item: Optional[Callable] = None

    # ------------------------------------------------------------------
    async def install_handlers(self):
        client = await account_manager.get_client()
        if not client:
            return
        if self._handlers_installed:
            return

        sources = await db.list_sources(enabled_only=True)
        chat_ids = [s["channel_id"] for s in sources]
        self._registered_chat_ids = set(chat_ids)

        @client.on(events.NewMessage(chats=chat_ids or None, incoming=True))
        async def _on_new_message(event):
            if event.message.grouped_id is not None:
                return  # handled by the Album handler instead
            await self._handle_new_message(event.chat_id, event.message)

        @client.on(events.Album(chats=chat_ids or None))
        async def _on_album(event):
            await self._handle_album(event.chat_id, event.messages)

        self._handlers_installed = True
        logger.info("Live monitoring installed for %d source(s)", len(chat_ids))

    async def refresh_handlers(self):
        """Call after sources are added/removed to re-scope the listeners."""
        client = await account_manager.get_client()
        if not client:
            return
        client.remove_event_handler(None)
        self._handlers_installed = False
        await self.install_handlers()

    async def _handle_new_message(self, chat_id: int, message):
        source = await db.get_source(chat_id)
        if not source or not source.get("enabled"):
            return
        job = await queue_manager.enqueue_single(source, message, Priority.LIVE)
        await db.update_last_seen(chat_id, message.id)
        if job and self.on_new_live_item:
            await self._safe_call(self.on_new_live_item, source, message, job)

    async def _handle_album(self, chat_id: int, messages: list):
        source = await db.get_source(chat_id)
        if not source or not source.get("enabled"):
            return
        job = await queue_manager.enqueue_album(source, messages, Priority.LIVE)
        max_id = max(m.id for m in messages)
        await db.update_last_seen(chat_id, max_id)
        if job and self.on_new_live_item:
            await self._safe_call(self.on_new_live_item, source, messages[0], job)

    @staticmethod
    async def _safe_call(fn, *args):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error in monitor callback")

    # ------------------------------------------------------------------
    # Periodic recovery sync
    # ------------------------------------------------------------------
    async def recovery_loop(self):
        while True:
            await asyncio.sleep(config.RECOVERY_SYNC_INTERVAL)
            try:
                await self.run_recovery_sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Recovery sync cycle failed")

    async def run_recovery_sync(self):
        if self._recovery_running:
            return
        client = await account_manager.get_client()
        if not client:
            return
        self._recovery_running = True
        try:
            sources = await db.list_sources(enabled_only=True)
            for source in sources:
                try:
                    await self._sync_source(client, source)
                except FloodWaitError as e:
                    logger.warning("FloodWait during recovery sync: %ss", e.seconds)
                    await asyncio.sleep(e.seconds)
                except Exception:
                    logger.exception("Recovery sync failed for source %s", source["channel_id"])
                await asyncio.sleep(config.SCAN_BATCH_DELAY)
        finally:
            self._recovery_running = False

    async def _sync_source(self, client, source: dict):
        channel_id = source["channel_id"]
        min_id = source.get("last_seen_message_id", 0)
        entity = await client.get_entity(channel_id)

        groups = defaultdict(list)
        singles = []
        max_seen = min_id
        count = 0

        async for message in client.iter_messages(entity, min_id=min_id, reverse=True,
                                                     limit=config.SCAN_BATCH_SIZE):
            count += 1
            max_seen = max(max_seen, message.id)
            if message.grouped_id is not None:
                groups[message.grouped_id].append(message)
            else:
                singles.append(message)

        for message in singles:
            await queue_manager.enqueue_single(source, message, Priority.BACKLOG)
        for group_messages in groups.values():
            await queue_manager.enqueue_album(source, group_messages, Priority.BACKLOG)

        if max_seen > min_id:
            await db.update_last_seen(channel_id, max_seen)
        await db.touch_sync(channel_id)

        if count:
            logger.info("Recovery sync: source=%s recovered %d message(s)", channel_id, count)

    # ------------------------------------------------------------------
    # Initial backlog scan for a newly added source
    # ------------------------------------------------------------------
    async def scan_initial_backlog(self, source: dict):
        client = await account_manager.get_client()
        if not client:
            return 0

        scan_config = source.get("scan_config", {"mode": ScanMode.NEW_ONLY})
        mode = scan_config.get("mode", ScanMode.NEW_ONLY)
        channel_id = source["channel_id"]
        entity = await client.get_entity(channel_id)

        if mode == ScanMode.NEW_ONLY:
            # Just record the current top message id as the checkpoint;
            # nothing existing gets queued.
            try:
                latest = await client.get_messages(entity, limit=1)
                top_id = latest[0].id if latest else 0
            except Exception:
                top_id = 0
            await db.update_last_seen(channel_id, top_id)
            return 0

        kwargs = {"reverse": True}
        if mode == ScanMode.LAST_X:
            limit = int(scan_config.get("value", 100))
            kwargs["limit"] = limit
            kwargs["reverse"] = False  # take the most recent N, then we'll re-sort
        elif mode == ScanMode.FROM_DATE:
            kwargs["offset_date"] = scan_config.get("value")
            kwargs["reverse"] = True
        # mode == ALL -> no extra kwargs, iterate everything

        collected = []
        try:
            async for message in client.iter_messages(entity, **kwargs):
                collected.append(message)
                if len(collected) % config.SCAN_BATCH_SIZE == 0:
                    await asyncio.sleep(config.SCAN_BATCH_DELAY)
        except FloodWaitError as e:
            logger.warning("FloodWait during initial scan: %ss", e.seconds)
            await asyncio.sleep(e.seconds)

        collected.sort(key=lambda m: m.id)

        groups = defaultdict(list)
        singles = []
        for message in collected:
            if message.grouped_id is not None:
                groups[message.grouped_id].append(message)
            else:
                singles.append(message)

        queued = 0
        for message in singles:
            job = await queue_manager.enqueue_single(source, message, Priority.BACKLOG)
            if job:
                queued += 1
        for group_messages in groups.values():
            job = await queue_manager.enqueue_album(source, group_messages, Priority.BACKLOG)
            if job:
                queued += len(group_messages)

        if collected:
            await db.update_last_seen(channel_id, max(m.id for m in collected))
        await db.touch_sync(channel_id)
        return queued


monitor = Monitor()
