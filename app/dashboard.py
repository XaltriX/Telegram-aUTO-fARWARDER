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
from app.utils.helpers import delete_after, escape_html, source_progress_text, truncate

logger = logging.getLogger(__name__)

MENU_IDLE = 90          # seconds a menu may stay open before card returns home
MIN_EDIT_GAP = 2.0      # seconds between edits (Telegram edit limits)
CARD_SOURCES = 8        # sources shown on the card itself


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
    def _buttons(self, paused: bool):
        return [
            [Button.inline("\U0001F4E1 Sources", b"src:menu"), Button.inline("\U0001F4E5 Queue", b"queue:menu")],
            [Button.inline("\U0001F3AF Destination", b"dest:menu"), Button.inline("\U0001F464 Account", b"account:menu")],
            [Button.inline("\U0001F4CA Stats", b"stats:menu"), Button.inline("⚙️ Settings", b"settings:menu")],
            [Button.inline("▶️ Resume" if paused else "⏸ Pause", b"ctl:toggle_pause"),
             Button.inline("\U0001F504 Refresh", b"ctl:home")],
        ]

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
        settings, session, stats, counts, current, sources, prog = await asyncio.gather(
            db.get_settings(), db.get_session(), db.get_stats(), db.queue_counts(),
            db.get_current_job(), db.list_sources(), db.source_progress())

        paused = settings.get("paused", False)
        flood = settings.get("flood_wait_until")
        wait = int(flood - time.time()) if flood else 0
        rate = f"\U0001F6A6 LIMITED {wait}s" if wait > 0 else "NORMAL"
        acct = "✅ connected" if session.get("connected") else "❌ disconnected"
        dest = escape_html(truncate(await self._dest_title(settings.get("destination_channel_id")), 30))

        now_line = "idle"
        if current:
            src = next((s for s in sources if s["channel_id"] == current["source_channel_id"]), None)
            name = escape_html(truncate(src["title"], 24)) if src else str(current["source_channel_id"])
            n = len(current.get("message_ids") or [1])
            now_line = f"{name} › #{current['source_message_id']} ({current['media_type']}{f' x{n}' if n > 1 else ''})"

        lines = [
            "⚡ <b>TG FORWARDER</b>   " + ("\U0001F7E1 PAUSED" if paused else "\U0001F7E2 ONLINE"),
            f"\U0001F464 {acct}   \U0001F3AF {dest}",
            "━" * 16,
            f"\U0001F4E6 <b>Left to send: {counts['TOTAL_QUEUED']:,}</b>   (\U0001F534 live {counts['LIVE']:,} · backlog {counts['BACKLOG']:,})",
            f"\U0001F4E4 <b>Sent: {stats.get('total_forwarded', 0):,}</b>   (today {stats.get('today_forwarded', 0):,})   ❌ failed {counts['FAILED']:,}",
            f"⚡ now: {now_line}",
            f"\U0001F6E1 rate: {rate}   ⏱ up {_fmt_uptime(stats.get('started_at'))}",
            "",
            f"\U0001F4E1 <b>SOURCES</b> ({len(sources)})",
        ]
        if not sources:
            lines.append("none yet — open Sources → Add")
        for s in sources[:CARD_SOURCES]:
            lines.append(source_progress_text(s, prog))
        if len(sources) > CARD_SOURCES:
            lines.append(f"… +{len(sources) - CARD_SOURCES} more (see Sources)")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "…"
        return text, paused

    # ---------------- card lifecycle
    async def initialize(self, chat_id: int):
        """Post a fresh card at the bottom; remove the old one."""
        async with self._lock:
            bot = self._get_bot()
            state = await db.get_dashboard_state()
            text, paused = await self.build()
            msg = await bot.send_message(chat_id, text, buttons=self._buttons(paused), parse_mode="html")
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
            text, paused = await self.build()
            try:
                await event.edit(text, buttons=self._buttons(paused), parse_mode="html")
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
            text, paused = await self.build()
            if not force and text == state.get("last_text"):
                return
            bot = self._get_bot()
            try:
                await bot.edit_message(state["chat_id"], state["message_id"], text,
                                       buttons=self._buttons(paused), parse_mode="html")
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
