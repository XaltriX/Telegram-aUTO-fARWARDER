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
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app.config import config
from app.models import DEFAULT_MEDIA_FILTERS, JobStatus, LoginState, Priority

logger = logging.getLogger(__name__)

# message-level size of a job (albums hold several messages)
_SIZE = {"$size": {"$ifNull": ["$message_ids", [1]]}}
_OPEN = [JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.FAILED]


def _today() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


def _fernet():
    if not config.SESSION_ENCRYPTION_KEY:
        return None
    from cryptography.fernet import Fernet
    return Fernet(config.SESSION_ENCRYPTION_KEY.encode())


def _enc(v: str) -> str:
    f = _fernet()
    return "enc:" + f.encrypt(v.encode()).decode() if f else v


def _dec(v):
    if v and v.startswith("enc:"):
        f = _fernet()
        if not f:
            raise RuntimeError("Stored session is encrypted but SESSION_ENCRYPTION_KEY is not set")
        return f.decrypt(v[4:].encode()).decode()
    return v

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
                    "today_date": _today(),
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
        await self.db.jobs.create_indexes([
            IndexModel([("source_channel_id", ASCENDING), ("message_ids", ASCENDING)], name="src_msgids_idx"),
            IndexModel([("status", ASCENDING), ("priority_type", ASCENDING), ("retry_after", ASCENDING)],
                       name="retry_idx"),
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
        doc = await self.db.account_session.find_one({"_id": SESSION_ID}) or {}
        if doc.get("string_session"):
            doc["string_session"] = _dec(doc["string_session"])
        return doc

    async def save_session_string(self, string_session: str, phone: str = None):
        update = {"string_session": _enc(string_session), "connected": True,
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
            "scan_done": False,
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

    # ------------------------------------------------------------------
    # Dedup: the unique (source_channel_id, source_message_id) index plus
    # this lookup of already-queued message ids (no separate ledger, so a
    # crash can never leave a message "reserved" without a job).
    # ------------------------------------------------------------------
    async def existing_message_ids(self, channel_id: int, ids: list) -> set:
        seen = set()
        cur = self.db.jobs.find({"source_channel_id": channel_id, "message_ids": {"$in": ids}},
                                {"message_ids": 1})
        async for d in cur:
            seen.update(d.get("message_ids") or [])
        return seen

    async def insert_jobs(self, jobs: list) -> list:
        """Bulk insert; duplicates (unique index) are silently skipped.
        Returns the jobs that were actually inserted."""
        if not jobs:
            return []
        try:
            await self.db.jobs.insert_many(jobs, ordered=False)
            return jobs
        except BulkWriteError as e:
            errs = e.details.get("writeErrors", [])
            if any(w.get("code") != 11000 for w in errs):
                raise
            bad = {w["index"] for w in errs}
            return [j for i, j in enumerate(jobs) if i not in bad]

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
        """Atomically lock the next job. LIVE (by sequence) beats BACKLOG
        (by source_order, message id). Jobs in retry back-off are skipped."""
        now = datetime.now(timezone.utc)
        ready = {"status": JobStatus.PENDING,
                 "$or": [{"retry_after": None}, {"retry_after": {"$lte": now}}]}
        upd = {"$set": {"status": JobStatus.PROCESSING, "started_at": now}}
        job = await self.db.jobs.find_one_and_update(
            {**ready, "priority_type": Priority.LIVE}, upd,
            sort=[("sequence", ASCENDING)], return_document=True)
        if job:
            return job
        return await self.db.jobs.find_one_and_update(
            {**ready, "priority_type": Priority.BACKLOG}, upd,
            sort=[("source_order", ASCENDING), ("source_message_id", ASCENDING)],
            return_document=True)

    async def mark_job_done(self, job_id, forwarded_message_id: Optional[int]):
        await self.db.jobs.update_one(
            {"_id": job_id},
            {"$set": {
                "status": JobStatus.DONE,
                "completed_at": datetime.now(timezone.utc),
                "forwarded_message_id": forwarded_message_id,
            }},
        )

    async def requeue_job(self, job_id, error: str, retry_after: Optional[datetime] = None,
                          count_attempt: bool = True):
        op = {"$set": {"status": JobStatus.PENDING, "last_error": error,
                       "started_at": None, "retry_after": retry_after}}
        if count_attempt:
            op["$inc"] = {"attempts": 1}
        await self.db.jobs.update_one({"_id": job_id}, op)

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
            {"$set": {"status": JobStatus.PENDING, "attempts": 0, "last_error": None, "started_at": None,
                      "retry_after": None}},
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
        """Message-level counts (an album of 5 counts as 5)."""
        pipeline = [
            {"$match": {"status": {"$in": _OPEN}}},
            {"$group": {"_id": {"p": "$priority_type", "s": "$status"}, "n": {"$sum": _SIZE}}},
        ]
        counts = {"LIVE": 0, "BACKLOG": 0, "FAILED": 0}
        async for r in self.db.jobs.aggregate(pipeline):
            if r["_id"]["s"] == JobStatus.FAILED:
                counts["FAILED"] += r["n"]
            else:
                counts[r["_id"]["p"]] = counts.get(r["_id"]["p"], 0) + r["n"]
        counts["TOTAL_QUEUED"] = counts["LIVE"] + counts["BACKLOG"]
        return counts

    async def source_progress(self) -> dict:
        """{channel_id: {"left": n, "failed": n}} - 'done' comes from source stats."""
        pipeline = [
            {"$match": {"status": {"$in": _OPEN}}},
            {"$group": {"_id": {"c": "$source_channel_id", "s": "$status"}, "n": {"$sum": _SIZE}}},
        ]
        out = {}
        async for r in self.db.jobs.aggregate(pipeline):
            d = out.setdefault(r["_id"]["c"], {"left": 0, "failed": 0})
            d["failed" if r["_id"]["s"] == JobStatus.FAILED else "left"] += r["n"]
        return out

    async def get_current_job(self) -> Optional[dict]:
        return await self.db.jobs.find_one({"status": JobStatus.PROCESSING})

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    async def get_stats(self) -> dict:
        return await self.db.stats.find_one({"_id": STATS_ID}) or {}

    async def incr_stat(self, field: str, amount: int = 1):
        await self.db.stats.update_one({"_id": STATS_ID}, {"$inc": {field: amount}})

    async def record_forward_success(self, n: int = 1):
        today = _today()
        stats = await self.get_stats()
        now = datetime.now(timezone.utc)
        if stats.get("today_date") != today:
            await self.db.stats.update_one(
                {"_id": STATS_ID},
                {"$set": {"today_date": today, "today_forwarded": n, "last_forward_at": now},
                 "$inc": {"total_forwarded": n}})
        else:
            await self.db.stats.update_one(
                {"_id": STATS_ID},
                {"$inc": {"total_forwarded": n, "today_forwarded": n},
                 "$set": {"last_forward_at": now}})

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
