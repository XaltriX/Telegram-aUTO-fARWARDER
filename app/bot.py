"""
Control/admin bot (Telethon client authenticated with BOT_TOKEN).
This client is ONLY a dashboard/control interface - it never touches
source or destination channels directly; all channel access/forwarding
goes through the personal account client (app/telegram_client.py).

Owner-only enforcement: every command and callback query is checked
against config.OWNER_ID before any handler logic runs.
"""
import logging

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from app.config import config
from app.database import db

logger = logging.getLogger(__name__)


class ConversationState:
    """
    Tiny in-memory per-chat conversation state for multi-step flows
    (add source, set destination, login phone/code/2FA, etc).
    This is intentionally NOT persisted: if the dyno restarts mid-flow,
    the admin simply restarts that flow from the dashboard. Login state
    itself (LOGIN_* enum) IS persisted in MongoDB so the bot can tell the
    owner "you were mid-login, please restart" after a restart.
    """
    def __init__(self):
        self._state = {}

    def set(self, chat_id: int, key: str, value):
        self._state.setdefault(chat_id, {})[key] = value

    def get(self, chat_id: int, key: str, default=None):
        return self._state.get(chat_id, {}).get(key, default)

    def clear(self, chat_id: int):
        self._state.pop(chat_id, None)

    def get_flow(self, chat_id: int) -> str:
        return self._state.get(chat_id, {}).get("flow", "")

    def set_flow(self, chat_id: int, flow: str):
        self.set(chat_id, "flow", flow)


conversation_state = ConversationState()


def owner_only_message(handler):
    async def wrapper(event):
        if event.sender_id != config.OWNER_ID:
            return
        return await handler(event)
    return wrapper


def owner_only_callback(handler):
    async def wrapper(event):
        if event.sender_id != config.OWNER_ID:
            await event.answer("Not authorized.", alert=True)
            return
        return await handler(event)
    return wrapper


async def create_bot_client() -> TelegramClient:
    # MemorySession: nothing written to disk. Bot logins are token-based and
    # cheap to redo on every restart, so there is no need to persist this.
    client = TelegramClient(MemorySession(), config.API_ID, config.API_HASH,
                             connection_retries=10, retry_delay=2)
    await client.start(bot_token=config.BOT_TOKEN)
    me = await client.get_me()
    logger.info("Control bot connected as @%s", me.username)
    return client


def register_handlers(client: TelegramClient):
    from app.handlers import start as h_start
    from app.handlers import account as h_account
    from app.handlers import sources as h_sources
    from app.handlers import destination as h_destination
    from app.handlers import queue as h_queue
    from app.handlers import settings as h_settings
    from app.handlers import stats as h_stats

    h_start.register(client)
    h_account.register(client)
    h_sources.register(client)
    h_destination.register(client)
    h_queue.register(client)
    h_settings.register(client)
    h_stats.register(client)

    @client.on(events.NewMessage(pattern=r"^/(start|dashboard)$"))
    @owner_only_message
    async def _start(event):
        await h_start.show_dashboard(client, event.chat_id)

    @client.on(events.NewMessage(pattern=r"^/cancel$"))
    @owner_only_message
    async def _cancel_cmd(event):
        flow = conversation_state.get_flow(event.chat_id)
        if flow:
            await h_account.cancel_flow(event.chat_id, flow)
        conversation_state.clear(event.chat_id)
        await event.respond("Cancelled.")

    @client.on(events.NewMessage(forwards=True))
    @owner_only_message
    async def _forward_router(event):
        # A forwarded message is used to identify a source or destination
        # channel (points 8 and 9). Each handler checks whether it is the
        # one currently awaiting a forward for this chat.
        if await h_sources.handle_forward(client, event):
            return
        if await h_destination.handle_forward(client, event):
            return

    @client.on(events.NewMessage(incoming=True, forwards=False))
    @owner_only_message
    async def _text_router(event):
        # Plain text input for whichever multi-step flow is currently open.
        if not event.raw_text or event.raw_text.startswith("/"):
            return
        flow = conversation_state.get_flow(event.chat_id)
        if not flow:
            return
        if await h_account.handle_text(client, event, flow):
            return
        if await h_sources.handle_text(client, event, flow):
            return
        if await h_destination.handle_text(client, event, flow):
            return

    logger.info("All bot handlers registered")
