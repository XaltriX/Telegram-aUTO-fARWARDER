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
    """Always post a fresh card at the bottom of the chat (old card is deleted)."""
    from app.dashboard import dashboard_manager
    if dashboard_manager is not None:
        await dashboard_manager.initialize(chat_id)
