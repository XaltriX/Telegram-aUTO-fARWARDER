"""
Manages the PERSONAL Telegram account (the one that actually reads source
channels and forwards messages). This is a Telethon MTProto user client.

Session persistence: Heroku's filesystem is ephemeral, so we never rely on
a local .session file. Telethon's StringSession is used instead - the
session string is stored in MongoDB and loaded back on every process start.

Login flow is a small state machine restricted to OWNER_ID, driven by the
control bot (see handlers/account.py):

    LOGIN_IDLE -> LOGIN_PHONE -> LOGIN_CODE -> [LOGIN_2FA] -> LOGIN_COMPLETE
                                                            -> LOGIN_FAILED
"""
import asyncio
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    FloodWaitError,
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
)
from telethon.sessions import StringSession

from app.config import config
from app.database import db
from app.models import LoginState

logger = logging.getLogger(__name__)


class AccountManager:
    """
    Owns the lifecycle of the single personal-account Telethon client.
    Only one login attempt may be in flight at a time (protected by a lock).
    """

    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self._login_lock = asyncio.Lock()
        self._phone_code_hash: Optional[str] = None
        self._pending_phone: Optional[str] = None

    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected()

    async def get_client(self) -> Optional[TelegramClient]:
        return self.client if self.is_connected() else None

    async def bootstrap(self):
        """
        Called at process startup. If a session string is already stored,
        reconnect without requiring OTP (unless Telegram has revoked it).
        """
        session_doc = await db.get_session()
        string_session = session_doc.get("string_session") if session_doc else None
        if not string_session:
            logger.info("No stored account session - login required via the bot.")
            return

        client = TelegramClient(
            StringSession(string_session), config.API_ID, config.API_HASH,
            connection_retries=10, retry_delay=2,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("Stored session is no longer authorized. Clearing it; re-login required.")
                await db.clear_session()
                await client.disconnect()
                return
            self.client = client
            await db.set_connected(True)
            me = await client.get_me()
            logger.info("Personal account reconnected: %s (id=%s)", me.username or me.first_name, me.id)
        except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
            logger.error("Stored session invalid: %s. Clearing session.", type(e).__name__)
            await db.clear_session()
        except Exception:
            logger.exception("Failed to reconnect personal account on startup")

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Login state machine (each step called from the bot, owner-only)
    # ------------------------------------------------------------------
    async def start_login(self, phone: str) -> str:
        async with self._login_lock:
            if self.is_connected():
                return "An account is already connected. Logout first."
            client = TelegramClient(StringSession(), config.API_ID, config.API_HASH,
                                     connection_retries=10, retry_delay=2)
            await client.connect()
            try:
                sent = await client.send_code_request(phone)
            except PhoneNumberInvalidError:
                await client.disconnect()
                return "Invalid phone number format. Use international format, e.g. +15551234567."
            except FloodWaitError as e:
                await client.disconnect()
                return f"Telegram asked us to wait {e.seconds}s before requesting another code."
            self._phone_code_hash = sent.phone_code_hash
            self._pending_phone = phone
            self.client = client  # not authorized yet, but kept for the next step
            await db.set_login_state(LoginState.CODE, phone=phone)
            return "OK"

    async def submit_code(self, code: str) -> str:
        async with self._login_lock:
            if not self.client or not self._pending_phone:
                return "No login in progress. Start again with /account."
            try:
                await self.client.sign_in(
                    phone=self._pending_phone, code=code, phone_code_hash=self._phone_code_hash
                )
            except PhoneCodeInvalidError:
                return "Invalid code. Try again."
            except PhoneCodeExpiredError:
                await db.set_login_state(LoginState.FAILED)
                return "Code expired. Start login again."
            except SessionPasswordNeededError:
                await db.set_login_state(LoginState.PASSWORD, phone=self._pending_phone)
                return "2FA_REQUIRED"
            return await self._finalize_login()

    async def submit_password(self, password: str) -> str:
        async with self._login_lock:
            if not self.client:
                return "No login in progress. Start again with /account."
            try:
                await self.client.sign_in(password=password)
            except PasswordHashInvalidError:
                return "Incorrect 2FA password. Try again."
            return await self._finalize_login()

    async def _finalize_login(self) -> str:
        me = await self.client.get_me()
        string_session = self.client.session.save()
        await db.save_session_string(string_session, phone=self._pending_phone)
        await db.set_connected(True)
        self._phone_code_hash = None
        self._pending_phone = None
        logger.info("Personal account login complete: %s (id=%s)", me.username or me.first_name, me.id)
        return "OK"

    async def logout(self):
        async with self._login_lock:
            if self.client:
                try:
                    await self.client.log_out()
                except Exception:
                    logger.exception("Error during log_out(), clearing local state anyway")
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = None
            self._phone_code_hash = None
            self._pending_phone = None
            await db.clear_session()

    async def cancel_login(self):
        async with self._login_lock:
            if self.client and not await self._safe_is_authorized():
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
            self._phone_code_hash = None
            self._pending_phone = None
            await db.set_login_state(LoginState.IDLE)

    async def _safe_is_authorized(self) -> bool:
        try:
            return await self.client.is_user_authorized()
        except Exception:
            return False


account_manager = AccountManager()
