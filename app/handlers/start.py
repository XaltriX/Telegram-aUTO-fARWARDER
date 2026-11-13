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
    # Always send a FRESH dashboard at the bottom of the chat. The saved
    # message id may belong to an old message far up the chat (it persists in
    # MongoDB across restarts/new Heroku apps), and editing that one in place
    # looks like the bot ignored /start.
    state = await db.get_dashboard_state()
    if state.get("chat_id") and state.get("message_id"):
        try:
            await bot_client.delete_messages(state["chat_id"], [state["message_id"]])
        except Exception:
            pass  # already deleted / not deletable - fine
    await dashboard_manager.initialize(chat_id)
