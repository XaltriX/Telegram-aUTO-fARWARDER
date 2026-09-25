"""
Single persistent, in-place-edited dashboard message (point 25/26), plus
throttled temporary notifications (point 27) that auto-delete.
"""

import asyncio
import logging
from datetime import datetime, timezone

from telethon import Button
from telethon.errors import (
    MessageIdInvalidError,
    MessageNotModifiedError,
)

from app.config import config
from app.database import db
from app.utils.helpers import delete_after, escape_html

logger = logging.getLogger(__name__)


def _fmt_uptime(started_at) -> str:
    if not started_at:
        return "n/a"

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    delta = datetime.now(timezone.utc) - started_at
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    days, hours = divmod(hours, 24)

    if days:
        return f"{days}d {hours}h {minutes}m"

    return f"{hours}h {minutes}m"


class DashboardManager:
    def __init__(self, bot_client_getter):
        self._get_bot = bot_client_getter
        self._last_edit_ts = 0.0

    def _buttons(self, paused: bool):
        return [
            [
                Button.inline(
                    "\U0001F4E1 Sources",
                    b"src:menu",
                ),
                Button.inline(
                    "\U0001F4E5 Queue",
                    b"queue:menu",
                ),
            ],
            [
                Button.inline(
                    "\U0001F4CA Statistics",
                    b"stats:menu",
                ),
                Button.inline(
                    "\u2699\uFE0F Settings",
                    b"settings:menu",
                ),
            ],
            [
                Button.inline(
                    "\u25B6\uFE0F Resume"
                    if paused
                    else "\u23F8 Pause",
                    b"ctl:toggle_pause",
                ),
                Button.inline(
                    "\U0001F504 Refresh",
                    b"ctl:refresh",
                ),
            ],
        ]

    async def build_text(self) -> str:
        settings = await db.get_settings()
        session = await db.get_session()
        stats = await db.get_stats()
        counts = await db.queue_counts()
        current = await db.get_current_job()
        sources = await db.list_sources()

        account_status = (
            "CONNECTED"
            if session.get("connected")
            else "DISCONNECTED"
        )

        dest_status = (
            "SET"
            if settings.get("destination_channel_id")
            else "NOT SET"
        )

        rate_status = (
            "RATE LIMITED"
            if settings.get("flood_wait_until")
            else "NORMAL"
        )

        paused = settings.get("paused", False)
        system_status = "PAUSED" if paused else "ONLINE"

        current_block = "idle"

        if current:
            src = await db.get_source(
                current["source_channel_id"]
            )

            src_title = (
                src["title"]
                if src
                else str(current["source_channel_id"])
            )

            current_block = (
                f"{escape_html(src_title)}\n"
                f"\u2514\u2500 msg #{current['source_message_id']} "
                f"({current['media_type']})"
            )

        # Emoji ko f-string expression ke bahar define karna zaroori hai.
        # Isse Python 3.11 ka:
        # SyntaxError: f-string expression part cannot include a backslash
        # error nahi aayega.
        system_icon = (
            "\U0001F7E2"
            if not paused
            else "\U0001F7E1"
        )

        lines = [
            "\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557",
            "\u26A1 TG FORWARDER",
            "\u255A\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255D",
            "",
            f"{system_icon} SYSTEM      {system_status}",
            f"\U0001F464 ACCOUNT     {account_status}",
            f"\U0001F4E1 SOURCES     {len(sources)}",
            f"\U0001F3AF DESTINATION {dest_status}",
            "",
            f"\U0001F4E6 QUEUED      "
            f"{counts['TOTAL_QUEUED']:,}  "
            f"(LIVE {counts['LIVE']} / BACKLOG {counts['BACKLOG']})",
            f"\U0001F4E4 FORWARDED   "
            f"{stats.get('total_forwarded', 0):,}  "
            f"(today {stats.get('today_forwarded', 0):,})",
            f"\u274C FAILED      {counts['FAILED']:,}",
            "",
            "\u26A1 CURRENT",
            current_block,
            "",
            f"\U0001F6E1\uFE0F RATE        {rate_status}",
            "\U0001F4BE DATABASE    CONNECTED",
            f"\u23F1\uFE0F UPTIME      "
            f"{_fmt_uptime(stats.get('started_at'))}",
        ]

        return "\n".join(lines)

    async def initialize(self, chat_id: int):
        bot = self._get_bot()
        text = await self.build_text()
        settings = await db.get_settings()

        msg = await bot.send_message(
            chat_id,
            text,
            buttons=self._buttons(
                settings.get("paused", False)
            ),
            parse_mode="html",
        )

        await db.set_dashboard_message(chat_id, msg.id)
        await db.set_dashboard_last_text(text)

    async def refresh(self, force: bool = False):
        state = await db.get_dashboard_state()

        if (
            not state
            or not state.get("chat_id")
            or not state.get("message_id")
        ):
            return

        now = asyncio.get_event_loop().time()

        if not force and now - self._last_edit_ts < 1.0:
            return

        text = await self.build_text()

        if not force and text == state.get("last_text"):
            return

        bot = self._get_bot()
        settings = await db.get_settings()

        try:
            await bot.edit_message(
                state["chat_id"],
                state["message_id"],
                text,
                buttons=self._buttons(
                    settings.get("paused", False)
                ),
                parse_mode="html",
            )

            await db.set_dashboard_last_text(text)
            self._last_edit_ts = now

        except MessageNotModifiedError:
            pass

        except MessageIdInvalidError:
            logger.warning(
                "Dashboard message missing - re-initializing"
            )
            await self.initialize(state["chat_id"])

    async def start_loop(self):
        while True:
            await asyncio.sleep(
                config.DASHBOARD_UPDATE_INTERVAL
            )

            try:
                await self.refresh()

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Dashboard refresh failed"
                )

    async def notify(self, chat_id: int, text: str):
        """Temporary notification that auto-deletes."""
        bot = self._get_bot()

        try:
            msg = await bot.send_message(
                chat_id,
                text,
                parse_mode="html",
            )

        except Exception:
            logger.exception(
                "Failed to send notification"
            )
            return

        asyncio.create_task(
            delete_after(
                bot,
                chat_id,
                msg.id,
                config.NOTIFICATION_DELETE_SECONDS,
            )
        )


dashboard_manager: "DashboardManager" = None
