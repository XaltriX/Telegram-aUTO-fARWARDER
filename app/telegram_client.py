"""
Manages the PERSONAL Telegram account (the one that actually reads source
channels and forwards messages). This is a Telethon MTProto user client.

LOGIN MODEL: session-string paste, not in-bot OTP.
----------------------------------------------------
Telegram's server-side anti-scam system blocks a login the moment its OTP
code is typed into *any* Telegram chat - including a private chat with our
own control bot ("this code was previously shared by your account"). That
block happens on Telegram's side regardless of what this application does,
so an in-bot phone -> code -> 2FA flow cannot work reliably.

Instead, the owner generates a Telethon StringSession once, locally,
completely outside of any Telegram chat (see generate_session.py at the
project root, run from a terminal), and pastes that string into the bot.
The string itself is not a login code, so Telegram does not intercept it.

Session persistence: Heroku's filesystem is ephemeral, so the session is
never kept as a local file - the StringSession is stored in MongoDB and
reloaded on every process start. A StringSession does not expire on its
own; it stays valid until the owner explicitly logs out / terminates that
session from Telegram's "Active Sessions", or Telegram revokes it for
security reasons (e.g. password change).
"""
import asyncio
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError
from telethon.sessions import StringSession

from app.config import config
from app.database import db
from app.models import LoginState

logger = logging.getLogger(__name__)


class AccountManager:
    """Owns the lifecycle of the single personal-account Telethon client."""

    def __init__(self):
        self.client: Optional[TelegramClient] = None  # authorized only
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected()

    async def get_client(self) -> Optional[TelegramClient]:
        return self.client if self.is_connected() else None

    async def bootstrap(self):
        """
        Called at process startup. If a session string is already stored,
        reconnect without any owner interaction (unless Telegram has
        revoked it, in which case the owner must paste a fresh string).
        """
        session_doc = await db.get_session()
        string_session = session_doc.get("string_session") if session_doc else None
        if not string_session:
            logger.info("No stored account session - paste a session string via the bot.")
            return

        client = TelegramClient(
            StringSession(string_session), config.API_ID, config.API_HASH,
            connection_retries=10, retry_delay=2,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("Stored session is no longer authorized. Clearing it; a fresh session string is required.")
                await db.clear_session()
                await self._safe_disconnect(client)
                return
            self.client = client
            await db.set_connected(True)
            await db.set_login_state(LoginState.COMPLETE)
            me = await client.get_me()
            logger.info("Personal account reconnected: %s (id=%s)", me.username or me.first_name, me.id)
        except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
            logger.error("Stored session invalid: %s. Clearing session.", type(e).__name__)
            await db.clear_session()
            await self._safe_disconnect(client)
        except Exception:
            logger.exception("Failed to reconnect personal account on startup")
            await self._safe_disconnect(client)
            await db.set_connected(False)

    async def disconnect(self):
        await self._safe_disconnect(self.client)

    @staticmethod
    async def _safe_disconnect(client: Optional[TelegramClient]):
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Login by pasted session string (owner-only, see handlers/account.py)
    # ------------------------------------------------------------------
    async def login_with_string(self, string_session: str) -> str:
        """
        Validate and connect using an owner-supplied StringSession. Returns
        "OK" on success, or a human-readable error otherwise. The raw
        string is never logged (point 21/34/40) - only success/failure and
        the resulting account's username/id are.
        """
        async with self._login_lock:
            if self.is_connected():
                return "An account is already connected. Logout first."

            session_str = (string_session or "").strip()
            if not session_str:
                return "That doesn't look like a session string. Paste the full string with nothing else."

            try:
                client = TelegramClient(
                    StringSession(session_str), config.API_ID, config.API_HASH,
                    connection_retries=10, retry_delay=2,
                )
                await client.connect()
            except Exception as e:
                logger.warning("Could not connect with supplied session string: %s", type(e).__name__)
                return f"Couldn't connect with that session string: {type(e).__name__}."

            try:
                authorized = await client.is_user_authorized()
            except Exception as e:
                await self._safe_disconnect(client)
                return f"Couldn't verify that session: {type(e).__name__}."

            if not authorized:
                await self._safe_disconnect(client)
                return ("That session string is not authorized (it may be invalid, "
                        "logged out, or revoked). Generate a fresh one with generate_session.py.")

            try:
                me = await client.get_me()
            except Exception as e:
                await self._safe_disconnect(client)
                return f"Session connected but couldn't fetch account info: {type(e).__name__}."

            await db.save_session_string(client.session.save(), phone=getattr(me, "phone", None))
            await db.set_connected(True)
            self.client = client
            logger.info("Personal account connected via session string: %s (id=%s)",
                        me.username or me.first_name, me.id)
            return "OK"

    async def logout(self):
        async with self._login_lock:
            if self.client:
                try:
                    await self.client.log_out()
                except Exception:
                    logger.exception("Error during log_out(), clearing local state anyway")
                await self._safe_disconnect(self.client)
            self.client = None
            await db.clear_session()


account_manager = AccountManager()
