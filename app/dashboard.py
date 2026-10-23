"""
ONE persistent dashboard card, edited in place (live counts + per-source
progress). Menus are shown by editing the same card; while a menu is open the
live refresh pauses (and auto-returns home after MENU_IDLE seconds), so the
loop never overwrites what you are looking at.
/start deletes the old card and posts a fresh one at the bottom of the chat.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from telethon import Button
from telethon.errors import MessageIdInvalidError, MessageNotModifiedError

from app.config import config
from app.database import db
from app.telegram_client import account_manager
from app.utils.helpers import (TYPE_ICON, delete_after, escape_html, human_size, message_link,
                               progress_bar, source_progress_text, time_ago, truncate)

logger = logging.getLogger(__name__)

MENU_IDLE = 90          # seconds a menu may stay open before card returns home
MIN_EDIT_GAP = 2.0      # seconds between edits (Telegram edit limits)
CARD_SOURCES = 6        # sources shown on the card itself
LINE = "\u2501" * 18


def _fmt_uptime(started_at) -> str:
    if not started_at:
        return "n/a"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - started_at).total_seconds())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


class DashboardManager:
    def __init__(self, bot_client_getter):
        self._get_bot = bot_client_getter
        self._last_edit = 0.0
        self._lock = asyncio.Lock()
        self._dest_titles = {}
        self.view = "home"
        self._view_since = time.monotonic()

    # ---------------- view state
    def set_view(self, view: str):
        self.view = view
        self._view_since = time.monotonic()

    # ---------------- rendering
    def _buttons(self, paused: bool, failed: int = 0):
        rows = [
            [Button.inline("\U0001F4E1 Sources", b"src:menu"), Button.inline("\U0001F4E5 Queue", b"queue:menu")],
            [Button.inline("\U0001F3AF Destination", b"dest:menu"), Button.inline("\U0001F464 Account", b"account:menu")],
            [Button.inline("\U0001F4CA Stats", b"stats:menu"), Button.inline("\u2699\uFE0F Settings", b"settings:menu")],
        ]
        if failed:
            rows.append([Button.inline(f"\U0001F501 Retry {failed:,} failed", b"queue:retry_all")])
        rows.append([Button.inline("\u25B6\uFE0F Resume" if paused else "\u23F8 Pause", b"ctl:toggle_pause"),
                     Button.inline("\U0001F504 Refresh", b"ctl:home")])
        return rows

    async def _dest_title(self, dest_id) -> str:
        if not dest_id:
            return "NOT SET"
        if dest_id not in self._dest_titles:
            try:
                ent = await account_manager.get_entity(dest_id)
                self._dest_titles[dest_id] = getattr(ent, "title", None) or str(dest_id)
            except Exception:
                return str(dest_id)
        return self._dest_titles[dest_id]

    async def build(self):
        settings, session, stats, (counts, prog), current, sources = await asyncio.gather(
            db.get_settings(), db.get_session(), db.get_stats(), db.queue_snapshot(),
            db.get_current_job(), db.list_sources())
        by_id = {s["channel_id"]: s for s in sources}

        paused = settings.get("paused", False)
        flood = settings.get("flood_wait_until")
        wait = int(flood - time.time()) if flood else 0
        acct = "connected" if session.get("connected") else "\u274C DISCONNECTED"
        dest = escape_html(truncate(await self._dest_title(settings.get("destination_channel_id")), 28))

        sent = stats.get("total_forwarded", 0)
        left, failed = counts["TOTAL_QUEUED"], counts["FAILED"]
        total = sent + left + failed
        pct = int(100 * sent / total) if total else 0
        status = "\U0001F7E1 PAUSED" if paused else ("\U0001F6A6 RATE LIMITED %ds" % wait if wait > 0 else "\U0001F7E2 ONLINE")

        head = [
            f"\u26A1 <b>TG FORWARDER</b>   {status}",
            f"\U0001F464 {acct}  \u2192  \U0001F3AF <b>{dest}</b>",
            LINE,
            f"\U0001F4CA <b>OVERALL  {pct}%</b>",
            f"<code>{progress_bar(sent, total, 16)}</code>",
            f"\u2705 <b>{sent:,}</b> sent   \u23F3 <b>{left:,}</b> left   \u274C {failed:,}",
            f"\U0001F4C5 today {stats.get('today_forwarded', 0):,}   \U0001F534 live {counts['LIVE']:,}   "
            f"\U0001F4E6 backlog {counts['BACKLOG']:,}",
            LINE,
        ]

        # ---- now forwarding
        active_cid = None
        if current:
            active_cid = current["source_channel_id"]
            src = by_id.get(active_cid)
            n = len(current.get("message_ids") or [1])
            title = escape_html(truncate(current.get("title") or f"#{current['source_message_id']}", 60))
            icon = TYPE_ICON.get(current.get("media_type"), "\U0001F4C4")
            link = message_link(src, current["source_message_id"]) if src else None
            label = f'<a href="{link}">{title}</a>' if link else title
            head.append("\u26A1 <b>NOW FORWARDING</b>")
            head.append(f"{icon} {label}" + (f"  <i>(album, {n} files)</i>" if n > 1 else ""))
            meta = []
            fn = current.get("file_name")
            if fn and fn not in title:
                meta.append(f"\U0001F4CE {escape_html(truncate(fn, 38))}")
            if current.get("file_size"):
                meta.append(human_size(current["file_size"]))
            if meta:
                head.append("    " + " \u00B7 ".join(meta))
            if src:
                p = prog.get(active_cid, {})
                done = src.get("stats", {}).get("forwarded", 0)
                tot = done + p.get("left", 0) + p.get("failed", 0)
                head.append(f"    \U0001F4E1 {escape_html(truncate(src['title'], 24))} \u00B7 file {done + 1:,} of {tot:,}")
        else:
            head.append("\u26A1 <b>NOW</b>  idle \u2014 waiting for next file")

        # ---- last sent
        recent = list(reversed((stats.get("recent") or [])[-3:]))
        if recent:
            head += ["", "\U0001F9FE <b>LAST SENT</b>"]
            for r in recent:
                t = escape_html(truncate(r.get("title") or "file", 44))
                lab = f'<a href="{r["link"]}">{t}</a>' if r.get("link") else t
                extra = f" +{r['n'] - 1}" if r.get("n", 1) > 1 else ""
                head.append(f"{TYPE_ICON.get(r.get('type'), '')} {lab}{extra}  <i>{time_ago(r.get('at'))}</i>")
        head += [LINE, f"\U0001F4E1 <b>SOURCES</b> ({len(sources)})"]

        # ---- sources (as many as fit in 4096 chars)
        base = "\n".join(head)
        blocks = []
        for s in sources[:CARD_SOURCES]:
            blocks.append(source_progress_text(s, prog, active=(s["channel_id"] == active_cid)))
        if not sources:
            blocks.append("none yet \u2014 open Sources \u2192 Add")
        while blocks and len(base) + sum(len(b) + 1 for b in blocks) > 3900:
            blocks.pop()
        hidden = len(sources) - len(blocks) if sources else 0
        text = base + "\n" + "\n".join(blocks)
        if hidden > 0:
            text += f"\n\u2026 +{hidden} more (open Sources)"
        return text, paused, failed

    # ---------------- card lifecycle
    async def initialize(self, chat_id: int):
        """Post a fresh card at the bottom; remove the old one."""
        async with self._lock:
            bot = self._get_bot()
            state = await db.get_dashboard_state()
            text, paused, failed = await self.build()
            msg = await bot.send_message(chat_id, text, buttons=self._buttons(paused, failed), parse_mode="html",
                                         link_preview=False)
            old_chat, old_id = state.get("chat_id"), state.get("message_id")
            await db.set_dashboard_message(chat_id, msg.id)
            await db.set_dashboard_last_text(text)
            self.set_view("home")
            self._last_edit = time.monotonic()
            if old_id and old_id != msg.id:
                try:
                    await bot.delete_messages(old_chat or chat_id, [old_id])
                except Exception:
                    pass

    async def show_home(self, event):
        """Home/Refresh/Back button: turn the clicked message back into the card."""
        self.set_view("home")
        async with self._lock:
            text, paused, failed = await self.build()
            try:
                await event.edit(text, buttons=self._buttons(paused, failed), parse_mode="html", link_preview=False)
            except MessageNotModifiedError:
                pass
            await db.set_dashboard_message(event.chat_id, event.message_id)
            await db.set_dashboard_last_text(text)
            self._last_edit = time.monotonic()

    async def refresh(self, force: bool = False):
        if self.view != "home":
            if time.monotonic() - self._view_since < MENU_IDLE:
                return
            self.set_view("home")  # menu idle too long -> back to live card
            force = True
        state = await db.get_dashboard_state()
        if not state.get("chat_id") or not state.get("message_id"):
            return
        if not force and time.monotonic() - self._last_edit < MIN_EDIT_GAP:
            return
        async with self._lock:
            text, paused, failed = await self.build()
            if not force and text == state.get("last_text"):
                return
            bot = self._get_bot()
            try:
                await bot.edit_message(state["chat_id"], state["message_id"], text,
                                       buttons=self._buttons(paused, failed), parse_mode="html",
                                       link_preview=False)
            except MessageNotModifiedError:
                pass
            except MessageIdInvalidError:
                logger.warning("Dashboard message missing - posting a new one")
                self._lock.release()
                try:
                    await self.initialize(state["chat_id"])
                finally:
                    await self._lock.acquire()
                return
            self._last_edit = time.monotonic()
            await db.set_dashboard_last_text(text)

    async def start_loop(self):
        while True:
            await asyncio.sleep(config.DASHBOARD_UPDATE_INTERVAL)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dashboard refresh failed")

    async def notify(self, chat_id: int, text: str):
        """Temporary notification that auto-deletes."""
        bot = self._get_bot()
        try:
            msg = await bot.send_message(chat_id, text, parse_mode="html")
        except Exception:
            logger.exception("Failed to send notification")
            return
        asyncio.create_task(delete_after(bot, chat_id, msg.id, config.NOTIFICATION_DELETE_SECONDS))


dashboard_manager: "DashboardManager" = None  # set by worker.py
