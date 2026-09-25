"""
/start and /dashboard - initializes or re-shows the single persistent
dashboard message.
"""
import logging

from telethon import events

from app.config import config
from app.database import db

logger = logging.getLogger(__name__)


def register(client):
    pass  # /start is wired directly in app/bot.py::register_handlers


async def show_dashboard(bot_client, chat_id: int):
    from app.dashboard import dashboard_manager
    if dashboard_manager is None:
        return
    state = await db.get_dashboard_state()
    if state and state.get("message_id"):
        try:
            await dashboard_manager.refresh(force=True)
            return
        except Exception:
            logger.exception("Failed to refresh existing dashboard, re-initializing")
    await dashboard_manager.initialize(chat_id)
