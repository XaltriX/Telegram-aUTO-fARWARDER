"""
Destination indexer: scans the DESTINATION channel's history and remembers a
fingerprint of every file already there (media id, name+size fallback, text
hash). Source scans wait for this to finish, then skip anything already
present. Safe to re-run (idempotent) - this is the disaster-recovery path when
the database is lost.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from telethon.errors import FloodWaitError

from app.database import db
from app.telegram_client import account_manager
from app.utils.helpers import fingerprints

logger = logging.getLogger(__name__)


class DestIndexer:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._dest = None
        self.on_change: Optional[Callable] = None

    def _changed(self):
        if self.on_change:
            try:
                res = self.on_change()
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                logger.exception("dest index on_change failed")

    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def ensure(self):
        """Start indexing if the current destination isn't indexed yet."""
        s = await db.get_settings()
        dest = s.get("destination_channel_id")
        if not dest or not await account_manager.get_client():
            return
        if self.running():
            if self._dest == dest:
                return
            self._task.cancel()  # destination changed mid-scan
        di = s.get("dest_index") or {}
        if di.get("dest_id") == dest and di.get("state") == "done":
            return
        self._dest = dest
        self._task = asyncio.create_task(self._run(dest))

    async def reindex(self):
        if self.running():
            self._task.cancel()
        await db.update_settings({"dest_index": {"state": "pending"}})
        await self.ensure()

    async def skip(self):
        """Give up indexing: sources may scan right away (duplicates not filtered)."""
        if self.running():
            self._task.cancel()
        s = await db.get_settings()
        await db.update_settings({"dest_index": {
            "dest_id": s.get("destination_channel_id"), "state": "done", "skipped": True,
            "scanned": 0, "total": 0, "files": 0}})
        self._changed()

    async def _run(self, dest):
        try:
            client = await account_manager.get_client()
            entity = await account_manager.get_entity(dest)
            total = (await client.get_messages(entity, limit=0)).total or 0
            await db.update_settings({"dest_index": {
                "dest_id": dest, "state": "running", "scanned": 0, "total": total, "files": 0,
                "started_at": datetime.now(timezone.utc)}})
            self._changed()

            docs, scanned, files, offset = [], 0, 0, 0
            while True:
                try:
                    async for m in client.iter_messages(entity, offset_id=offset):
                        offset = m.id
                        scanned += 1
                        fps = fingerprints(m)
                        if fps:
                            files += 1
                            docs += [{"_id": f"{dest}|{f}", "msg_id": m.id} for f in fps]
                        if scanned % 200 == 0:
                            await db.insert_ignore_dups(db.db.dest_index, docs)
                            docs = []
                            await db.update_settings({"dest_index.scanned": scanned,
                                                      "dest_index.files": files})
                            self._changed()
                            await asyncio.sleep(0.2)
                    break
                except FloodWaitError as e:
                    logger.warning("FloodWait while indexing destination: %ss", e.seconds)
                    await asyncio.sleep(e.seconds)
            await db.insert_ignore_dups(db.db.dest_index, docs)
            await db.update_settings({"dest_index": {
                "dest_id": dest, "state": "done", "scanned": scanned, "total": scanned,
                "files": files, "finished_at": datetime.now(timezone.utc)}})
            logger.info("Destination indexed: %d messages, %d with fingerprints", scanned, files)
            self._changed()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Destination indexing failed (will retry)")
            await db.update_settings({"dest_index": {"dest_id": dest, "state": "error",
                                                     "error": f"{type(e).__name__}: {e}"[:200]}})
            self._changed()


dest_indexer = DestIndexer()
