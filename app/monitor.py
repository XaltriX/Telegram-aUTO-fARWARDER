"""
Hybrid monitoring:
  A) live Telethon handlers push new messages into the LIVE queue.
  B) recovery sync resumes from `last_seen_message_id` and enqueues anything
     missed into BACKLOG. Live handlers do NOT move the checkpoint (that used
     to hide gaps); duplicates are filtered by the dedup in queue_manager.
Also: initial history scan when a source is added (ALL/LAST_X/FROM_DATE/NEW_ONLY),
streamed in batches so memory stays flat.
"""
import asyncio
import logging
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
        self._handlers_installed = False
        self._recovery_running = False
        self._installed_handlers = []
        self.on_queue_change: Optional[Callable] = None

    # ------------------------------------------------------------------
    async def install_handlers(self):
        client = await account_manager.get_client()
        if not client or self._handlers_installed:
            return
        chat_ids = [s["channel_id"] for s in await db.list_sources(enabled_only=True)]
        if not chat_ids:
            return  # chats=None would listen to EVERY chat of the account

        nm = events.NewMessage(chats=chat_ids)
        al = events.Album(chats=chat_ids)

        async def _on_new_message(event):
            if event.message.grouped_id is None:
                await self._handle(event.chat_id, [event.message])

        async def _on_album(event):
            await self._handle(event.chat_id, list(event.messages))

        client.add_event_handler(_on_new_message, nm)
        client.add_event_handler(_on_album, al)
        self._installed_handlers = [(_on_new_message, nm), (_on_album, al)]
        self._handlers_installed = True
        logger.info("Live monitoring installed for %d source(s)", len(chat_ids))

    async def refresh_handlers(self):
        client = await account_manager.get_client()
        if not client:
            return
        for cb, ev in self._installed_handlers:
            client.remove_event_handler(cb, ev)
        self._installed_handlers = []
        self._handlers_installed = False
        await self.install_handlers()

    async def _handle(self, chat_id: int, messages: list):
        try:
            source = await db.get_source(chat_id)
            if not source or not source.get("enabled"):
                return
            jobs, _ = await queue_manager.enqueue_batch(source, messages, Priority.LIVE)
            if jobs:
                self._changed()
        except Exception:
            logger.exception("Live handler failed (recovery sync will catch up)")

    def _changed(self):
        if self.on_queue_change:
            try:
                res = self.on_queue_change()
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                logger.exception("on_queue_change failed")

    # ------------------------------------------------------------------
    # Recovery sync
    # ------------------------------------------------------------------
    async def recovery_loop(self):
        await asyncio.sleep(15)  # first catch-up shortly after start
        from app.dest_index import dest_indexer
        while True:
            try:
                await dest_indexer.ensure()
                await self.run_recovery_sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Recovery sync cycle failed")
            await asyncio.sleep(config.RECOVERY_SYNC_INTERVAL)

    async def run_recovery_sync(self):
        if self._recovery_running:
            return
        client = await account_manager.get_client()
        if not client:
            return
        self._recovery_running = True
        try:
            for source in await db.list_sources(enabled_only=True):
                if source.get("scan_done") is False:
                    continue  # initial scan still owns this source
                try:
                    await self._sync_source(client, source)
                except FloodWaitError as e:
                    logger.warning("FloodWait during recovery: %ss", e.seconds)
                    await asyncio.sleep(e.seconds)
                except Exception:
                    logger.exception("Recovery sync failed for %s", source["channel_id"])
                await asyncio.sleep(config.SCAN_BATCH_DELAY)
        finally:
            self._recovery_running = False

    async def _top_id(self, entity) -> int:
        latest = await account_manager.client.get_messages(entity, limit=1)
        return latest[0].id if latest else 0

    async def _sync_source(self, client, source: dict):
        cid = source["channel_id"]
        if not await db.dest_index_ready():
            return  # duplicates can't be filtered until the destination is indexed
        min_id = source.get("last_seen_message_id", 0)
        entity = await account_manager.get_entity(cid)

        if min_id == 0 and (source.get("scan_config") or {}).get("mode") == ScanMode.NEW_ONLY:
            await db.update_last_seen(cid, await self._top_id(entity))
            await db.touch_sync(cid)
            return

        total = 0
        for _ in range(20):  # up to 20 batches per cycle, then continue next cycle
            batch = [m async for m in client.iter_messages(
                entity, min_id=min_id, reverse=True, limit=config.SCAN_BATCH_SIZE)]
            if not batch:
                break
            full = len(batch) >= config.SCAN_BATCH_SIZE
            if full and batch[-1].grouped_id is not None:  # don't cut an album in half
                gid = batch[-1].grouped_id
                trimmed = [m for m in batch if m.grouped_id != gid]
                batch = trimmed or batch
            max_seen = max(m.id for m in batch)
            jobs, _ = await queue_manager.enqueue_batch(source, batch, Priority.BACKLOG)
            total += sum(len(j["message_ids"]) for j in jobs)
            await db.update_last_seen(cid, max_seen)
            min_id = max_seen
            if not full:
                break
            await asyncio.sleep(config.SCAN_BATCH_DELAY)
        await db.touch_sync(cid)
        if total:
            logger.info("Recovery sync: source=%s queued %d message(s)", cid, total)
            self._changed()

    # ------------------------------------------------------------------
    # Initial scan for a newly added source
    # ------------------------------------------------------------------
    async def _set_scan(self, cid, state, scanned=0, total=0, skipped=0):
        await db.update_source(cid, {"scan_progress": {
            "state": state, "scanned": scanned, "total": total, "skipped": skipped}})
        self._changed()

    async def _flush(self, source, buf, final, acc):
        keep = []
        if not final and buf and buf[-1].grouped_id is not None:
            gid = buf[-1].grouped_id
            keep = [m for m in buf if m.grouped_id == gid]
            buf = [m for m in buf if m.grouped_id != gid]
        if buf:
            jobs, sk = await queue_manager.enqueue_batch(source, buf, Priority.BACKLOG)
            acc["queued"] += sum(len(j["message_ids"]) for j in jobs)
            acc["skipped"] += sk
            await db.update_last_seen(source["channel_id"], max(m.id for m in buf))
        await self._set_scan(source["channel_id"], "scanning", acc["scanned"], acc["total"], acc["skipped"])
        return keep

    async def scan_initial_backlog(self, source: dict) -> int:
        from app.dest_index import dest_indexer
        client = await account_manager.get_client()
        if not client:
            raise RuntimeError("Account not connected")
        cid = source["channel_id"]
        sc = source.get("scan_config") or {"mode": ScanMode.NEW_ONLY}
        mode = sc.get("mode", ScanMode.NEW_ONLY)

        # wait until the destination is indexed, so existing files are skipped
        while not await db.dest_index_ready():
            await self._set_scan(cid, "waiting")
            await dest_indexer.ensure()
            await asyncio.sleep(5)

        entity = await account_manager.get_entity(cid)
        total = (await client.get_messages(entity, limit=0)).total or 0
        acc = {"scanned": 0, "total": total, "queued": 0, "skipped": 0}

        if mode == ScanMode.NEW_ONLY:
            await db.update_last_seen(cid, await self._top_id(entity))
        elif mode == ScanMode.LAST_X:
            limit = int(sc.get("value", 100))
            acc["total"] = min(total, limit)
            msgs = [m async for m in client.iter_messages(entity, limit=limit)]
            msgs.sort(key=lambda m: m.id)
            for i in range(0, len(msgs), config.SCAN_BATCH_SIZE):
                chunk = msgs[i:i + config.SCAN_BATCH_SIZE]
                acc["scanned"] += len(chunk)
                await self._flush(source, chunk, True, acc)
        else:  # ALL / FROM_DATE - stream oldest -> newest
            kwargs = {"reverse": True}
            if mode == ScanMode.FROM_DATE:
                kwargs["offset_date"] = sc.get("value")
            buf, last_id = [], 0
            while True:
                try:
                    async for m in client.iter_messages(entity, **kwargs):
                        buf.append(m)
                        last_id = m.id
                        acc["scanned"] += 1
                        if len(buf) >= config.SCAN_BATCH_SIZE:
                            buf = await self._flush(source, buf, False, acc)
                            await asyncio.sleep(config.SCAN_BATCH_DELAY)
                    break
                except FloodWaitError as e:
                    logger.warning("FloodWait during scan: %ss", e.seconds)
                    await asyncio.sleep(e.seconds)
                    kwargs = {"reverse": True, "min_id": last_id}  # resume
            await self._flush(source, buf, True, acc)

        await self._set_scan(cid, "done", acc["scanned"], acc["total"], acc["skipped"])
        await db.update_source(cid, {"scan_done": True})
        await db.touch_sync(cid)
        return acc["queued"]


monitor = Monitor()
