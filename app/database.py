"""
MongoDB persistence layer (Motor - async driver).

Collections:
  sources                 - configured source channels
  jobs                     - forwarding job queue (LIVE + BACKLOG)
  processed_message_ids    - dedup ledger (source_channel_id, message_id) -> job_id
  settings                 - single document: global settings (destination, filters, pause state)
  account_session          - single document: personal account session + login state
  stats                    - single document: aggregate counters
  dashboard_state          - single document: pinned dashboard message location
  counters                 - atomic sequence counters (used for LIVE ordering)

All "single document" collections use a fixed _id so they can be
upserted/read with a simple find_one/update_one call.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

from app.config import config
from app.models import DEFAULT_MEDIA_FILTERS, JobStatus, LoginState, Priority

logger = logging.getLogger(__name__)

SETTINGS_ID = "settings"
SESSION_ID = "account_session"
STATS_ID = "stats"
DASHBOARD_ID = "dashboard"
LIVE_SEQ_ID = "live_sequence"


class Database:
    def __init__(self):
        self.client: Optional[AsyncIOMotorClient] = None
        self.db = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self):
        self.client = AsyncIOMotorClient(
            config.MONGO_URI,
            maxPoolSize=20,
            minPoolSize=1,
            serverSelectionTimeoutMS=15000,
            retryWrites=True,
        )
        # Fail fast if the URI/credentials are wrong.
        await self.client.admin.command("ping")
        self.db = self.client[config.MONGO_DB_NAME]
        await self._ensure_documents()
        await self.create_indexes()
        logger.info("Connected to MongoDB (db=%s)", config.MONGO_DB_NAME)

    async def close(self):
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")

    async def _ensure_documents(self):
        await self.db.settings.update_one(
            {"_id": SETTINGS_ID},
            {
                "$setOnInsert": {
                    "_id": SETTINGS_ID,
                    "destination_channel_id": None,
                    "media_filters": DEFAULT_MEDIA_FILTERS,
                    "paused": False,
                    "flood_wait_until": None,
                    "hide_forward_tag": True,
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        await self.db.account_session.update_one(
            {"_id": SESSION_ID},
            {
                "$setOnInsert": {
                    "_id": SESSION_ID,
                    "string_session": None,
                    "phone": None,
                    "login_state": LoginState.IDLE,
                    "connected": False,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        await self.db.stats.update_one(
            {"_id": STATS_ID},
            {
                "$setOnInsert": {
                    "_id": STATS_ID,
                    "total_discovered": 0,
                    "total_queued": 0,
                    "total_forwarded": 0,
                    "total_failed": 0,
                    "today_forwarded": 0,
                    "today_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "last_forward_at": None,
                    "last_error": None,
                    "started_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        await self.db.dashboard_state.update_one(
            {"_id": DASHBOARD_ID},
            {"$setOnInsert": {"_id": DASHBOARD_ID, "chat_id": None, "message_id": None,
                               "last_text": None, "last_updated_at": None}},
            upsert=True,
        )

    async def create_indexes(self):
        await self.db.sources.create_indexes([
            IndexModel([("channel_id", ASCENDING)], unique=True, name="uniq_channel_id"),
            IndexModel([("source_order", ASCENDING)], name="source_order_idx"),
        ])
        await self.db.jobs.create_indexes([
            IndexModel(
                [("status", ASCENDING), ("priority_type", ASCENDING), ("sequence", ASCENDING)],
                name="live_pick_idx",
            ),
            IndexModel(
                [("status", ASCENDING), ("priority_type", ASCENDING),
                 ("source_order", ASCENDING), ("source_message_id", ASCENDING)],
                name="backlog_pick_idx",
            ),
            IndexModel([("source_channel_id", ASCENDING), ("source_message_id", ASCENDING)],
                       unique=True, name="uniq_source_message"),
            IndexModel([("status", ASCENDING)], name="status_idx"),
            IndexModel([("grouped_id", ASCENDING)], name="grouped_id_idx"),
        ])
        await self.db.processed_message_ids.create_indexes([
            IndexModel([("_id", ASCENDING)], name="pk_idx"),
        ])
        await self.db.counters.create_indexes([IndexModel([("_id", ASCENDING)])])

    # ------------------------------------------------------------------
    # Counters (atomic sequence generator for global LIVE arrival order)
    # ------------------------------------------------------------------
    async def next_sequence(self) -> int:
        doc = await self.db.counters.find_one_and_update(
            {"_id": LIVE_SEQ_ID},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=True,
        )
        return doc["value"]

    async def next_source_order(self) -> int:
        doc = await self.db.counters.find_one_and_update(
            {"_id": "source_order"},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=True,
        )
        return doc["value"]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def get_settings(self) -> dict:
        # Defensive: _ensure_documents() always creates this on connect, but
        # returning {} instead of None protects every caller that does
        # settings.get(...) from crashing if the document is ever missing
        # (e.g. manual DB edits, a fresh DB mid-migration).
        return await self.db.settings.find_one({"_id": SETTINGS_ID}) or {}

    async def update_settings(self, update: dict):
        await self.db.settings.update_one({"_id": SETTINGS_ID}, {"$set": update})

    async def toggle_media_filter(self, media_type: str) -> dict:
        settings = await self.get_settings()
        filters = settings.get("media_filters", dict(DEFAULT_MEDIA_FILTERS))
        filters[media_type] = not filters.get(media_type, True)
        await self.update_settings({"media_filters": filters})
        return filters

    async def toggle_hide_forward_tag(self) -> bool:
        settings = await self.get_settings()
        new_value = not settings.get("hide_forward_tag", True)
        await self.update_settings({"hide_forward_tag": new_value})
        return new_value

    async def set_paused(self, paused: bool):
        await self.update_settings({"paused": paused})

    async def set_flood_wait_until(self, ts: Optional[float]):
        await self.update_settings({"flood_wait_until": ts})

    # ------------------------------------------------------------------
    # Account session
    # ------------------------------------------------------------------
    async def get_session(self) -> dict:
        return await self.db.account_session.find_one({"_id": SESSION_ID}) or {}

    async def save_session_string(self, string_session: str, phone: str = None):
        update = {"string_session": string_session, "connected": True,
                  "login_state": LoginState.COMPLETE, "updated_at": datetime.now(timezone.utc)}
        if phone:
            update["phone"] = phone
        await self.db.account_session.update_one({"_id": SESSION_ID}, {"$set": update})

    async def set_login_state(self, state: str, phone: str = None):
        update = {"login_state": state, "updated_at": datetime.now(timezone.utc)}
        if phone is not None:
            update["phone"] = phone
        await self.db.account_session.update_one({"_id": SESSION_ID}, {"$set": update})

    async def clear_session(self):
        await self.db.account_session.update_one(
            {"_id": SESSION_ID},
            {"$set": {"string_session": None, "connected": False,
                      "login_state": LoginState.IDLE, "updated_at": datetime.now(timezone.utc)}},
        )

    async def set_connected(self, connected: bool):
        await self.db.account_session.update_one(
            {"_id": SESSION_ID}, {"$set": {"connected": connected}}
        )

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    async def add_source(self, channel_id: int, title: str, username: Optional[str],
                          scan_config: dict) -> dict:
        order = await self.next_source_order()
        doc = {
            "channel_id": channel_id,
            "title": title,
            "username": username,
            "enabled": True,
            "source_order": order,
            "created_at": datetime.now(timezone.utc),
            "last_seen_message_id": 0,
            "last_sync_at": None,
            "scan_config": scan_config,
            "stats": {"discovered": 0, "queued": 0, "forwarded": 0, "failed": 0},
        }
        try:
            await self.db.sources.insert_one(doc)
        except DuplicateKeyError:
            return await self.db.sources.find_one({"channel_id": channel_id})
        return doc

    async def get_source(self, channel_id: int) -> Optional[dict]:
        return await self.db.sources.find_one({"channel_id": channel_id})

    async def get_source_by_order(self, source_order: int) -> Optional[dict]:
        return await self.db.sources.find_one({"source_order": source_order})

    async def list_sources(self, enabled_only: bool = False) -> list:
        query = {"enabled": True} if enabled_only else {}
        cursor = self.db.sources.find(query).sort("source_order", ASCENDING)
        return [doc async for doc in cursor]

    async def update_source(self, channel_id: int, update: dict):
        await self.db.sources.update_one({"channel_id": channel_id}, {"$set": update})

    async def update_last_seen(self, channel_id: int, message_id: int):
        await self.db.sources.update_one(
            {"channel_id": channel_id, "last_seen_message_id": {"$lt": message_id}},
            {"$set": {"last_seen_message_id": message_id}},
        )

    async def touch_sync(self, channel_id: int):
        await self.db.sources.update_one(
            {"channel_id": channel_id}, {"$set": {"last_sync_at": datetime.now(timezone.utc)}}
        )

    async def incr_source_stat(self, channel_id: int, field: str, amount: int = 1):
        await self.db.sources.update_one(
            {"channel_id": channel_id}, {"$inc": {f"stats.{field}": amount}}
        )

    async def remove_source(self, channel_id: int, clear_queue: bool):
        await self.db.sources.delete_one({"channel_id": channel_id})
        if clear_queue:
            await self.db.jobs.delete_many(
                {"source_channel_id": channel_id, "status": {"$in": [JobStatus.PENDING, JobStatus.FAILED]}}
            )
            await self.db.processed_message_ids.delete_many({"source_channel_id": channel_id})

    # ------------------------------------------------------------------
    # Dedup ledger
    # ------------------------------------------------------------------
    async def reserve_message_ids(self, channel_id: int, message_ids: list, job_key: str) -> list:
        """
        Attempt to reserve each (channel_id, message_id) pair as 'claimed'.
        Returns the subset of message_ids that were newly reserved (not
        already processed). If a message id is already reserved, it is
        skipped (duplicate protection).
        """
        new_ids = []
        for mid in message_ids:
            doc_id = f"{channel_id}:{mid}"
            try:
                await self.db.processed_message_ids.insert_one({
                    "_id": doc_id,
                    "source_channel_id": channel_id,
                    "message_id": mid,
                    "job_key": job_key,
                    "reserved_at": datetime.now(timezone.utc),
                })
                new_ids.append(mid)
            except DuplicateKeyError:
                continue
        return new_ids

    async def release_message_ids(self, channel_id: int, message_ids: list):
        """Rollback reservation (used if job creation fails after reserving)."""
        ids = [f"{channel_id}:{mid}" for mid in message_ids]
        await self.db.processed_message_ids.delete_many({"_id": {"$in": ids}})

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    async def insert_job(self, job: dict) -> Optional[dict]:
        try:
            await self.db.jobs.insert_one(job)
            return job
        except DuplicateKeyError:
            logger.debug("Duplicate job skipped source_message_id=%s", job.get("source_message_id"))
            return None

    async def claim_next_job(self) -> Optional[dict]:
        """
        Atomically pick and lock the next job to process.
        LIVE (by sequence) always takes priority over BACKLOG
        (by source_order, then source_message_id).
        """
        now = datetime.now(timezone.utc)
        job = await self.db.jobs.find_one_and_update(
            {"status": JobStatus.PENDING, "priority_type": Priority.LIVE},
            {"$set": {"status": JobStatus.PROCESSING, "started_at": now}},
            sort=[("sequence", ASCENDING)],
            return_document=True,
        )
        if job:
            return job
        job = await self.db.jobs.find_one_and_update(
            {"status": JobStatus.PENDING, "priority_type": Priority.BACKLOG},
            {"$set": {"status": JobStatus.PROCESSING, "started_at": now}},
            sort=[("source_order", ASCENDING), ("source_message_id", ASCENDING)],
            return_document=True,
        )
        return job

    async def mark_job_done(self, job_id, forwarded_message_id: Optional[int]):
        await self.db.jobs.update_one(
            {"_id": job_id},
            {"$set": {
                "status": JobStatus.DONE,
                "completed_at": datetime.now(timezone.utc),
                "forwarded_message_id": forwarded_message_id,
            }},
        )

    async def requeue_job(self, job_id, error: str, retry_after: Optional[datetime] = None):
        """Put a job back to PENDING (kept in its original priority queue)."""
        update = {
            "status": JobStatus.PENDING,
            "last_error": error,
            "started_at": None,
        }
        if retry_after:
            update["retry_after"] = retry_after
        await self.db.jobs.update_one({"_id": job_id}, {"$inc": {"attempts": 1}, "$set": update})

    async def fail_job(self, job_id, error: str):
        await self.db.jobs.update_one(
            {"_id": job_id},
            {"$inc": {"attempts": 1},
             "$set": {"status": JobStatus.FAILED, "last_error": error,
                       "completed_at": datetime.now(timezone.utc)}},
        )

    async def get_job(self, job_id) -> Optional[dict]:
        return await self.db.jobs.find_one({"_id": job_id})

    async def list_failed_jobs(self, limit: int = 20) -> list:
        cursor = self.db.jobs.find({"status": JobStatus.FAILED}).sort("completed_at", DESCENDING).limit(limit)
        return [doc async for doc in cursor]

    async def requeue_all_failed(self) -> int:
        result = await self.db.jobs.update_many(
            {"status": JobStatus.FAILED},
            {"$set": {"status": JobStatus.PENDING, "attempts": 0, "last_error": None, "started_at": None}},
        )
        return result.modified_count

    async def recover_stuck_jobs(self):
        """
        On startup, any job left in PROCESSING (e.g. dyno was killed mid
        forward) is safely reset to PENDING. Because forward operations are
        not confirmed-committed until mark_job_done runs, this cannot create
        a duplicate forward - the job simply gets retried, and the
        source_message_id unique index guarantees no other duplicate job
        exists for the same message.
        """
        result = await self.db.jobs.update_many(
            {"status": JobStatus.PROCESSING},
            {"$set": {"status": JobStatus.PENDING, "started_at": None}},
        )
        if result.modified_count:
            logger.warning("Recovered %d job(s) stuck in PROCESSING after restart", result.modified_count)
        return result.modified_count

    async def queue_counts(self) -> dict:
        pipeline = [
            {"$match": {"status": {"$in": [JobStatus.PENDING, JobStatus.PROCESSING]}}},
            {"$group": {"_id": "$priority_type", "count": {"$sum": 1}}},
        ]
        counts = {"LIVE": 0, "BACKLOG": 0}
        async for row in self.db.jobs.aggregate(pipeline):
            counts[row["_id"]] = row["count"]
        failed = await self.db.jobs.count_documents({"status": JobStatus.FAILED})
        counts["FAILED"] = failed
        counts["TOTAL_QUEUED"] = counts["LIVE"] + counts["BACKLOG"]
        return counts

    async def get_current_job(self) -> Optional[dict]:
        return await self.db.jobs.find_one({"status": JobStatus.PROCESSING})

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    async def get_stats(self) -> dict:
        return await self.db.stats.find_one({"_id": STATS_ID}) or {}

    async def incr_stat(self, field: str, amount: int = 1):
        await self.db.stats.update_one({"_id": STATS_ID}, {"$inc": {field: amount}})

    async def record_forward_success(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats = await self.get_stats()
        now = datetime.now(timezone.utc)
        if stats.get("today_date") != today:
            # A Python dict literal cannot have two "$set" keys - the second
            # one silently overwrites the first, so today_date/today_forwarded
            # were never actually applied. Merge into a single $set instead.
            await self.db.stats.update_one(
                {"_id": STATS_ID},
                {"$set": {"today_date": today, "today_forwarded": 1, "last_forward_at": now},
                 "$inc": {"total_forwarded": 1}},
            )
        else:
            await self.db.stats.update_one(
                {"_id": STATS_ID},
                {"$inc": {"total_forwarded": 1, "today_forwarded": 1},
                 "$set": {"last_forward_at": now}},
            )

    async def record_forward_failure(self, error: str):
        await self.db.stats.update_one(
            {"_id": STATS_ID},
            {"$inc": {"total_failed": 1}, "$set": {"last_error": error[:300]}},
        )

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    async def get_dashboard_state(self) -> dict:
        return await self.db.dashboard_state.find_one({"_id": DASHBOARD_ID}) or {}

    async def set_dashboard_message(self, chat_id: int, message_id: int):
        await self.db.dashboard_state.update_one(
            {"_id": DASHBOARD_ID}, {"$set": {"chat_id": chat_id, "message_id": message_id}}
        )

    async def set_dashboard_last_text(self, text: str):
        await self.db.dashboard_state.update_one(
            {"_id": DASHBOARD_ID},
            {"$set": {"last_text": text, "last_updated_at": datetime.now(timezone.utc)}},
        )


db = Database()
