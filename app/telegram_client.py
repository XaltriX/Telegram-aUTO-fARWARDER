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

IMPORTANT: `self.client` only ever holds a fully-authorized client (the one
the scheduler/monitor use to read channels and forward). A login-in-progress
client (connected, but not yet signed in) is held separately in
`self._login_client` so `get_client()`/`is_connected()` can never hand a
half-authorized client to the rest of the app, and so "an account is
already connected" only fires when a real account is actually connected.
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
        self.client: Optional[TelegramClient] = None  # authorized only
        self._login_client: Optional[TelegramClient] = None  # login-in-progress only
        self._login_lock = asyncio.Lock()
        self._phone_code_hash: Optional[str] = None
        self._pending_phone: Optional[str] = None

    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        """True only when a fully-authorized account client is connected."""
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
        await self._safe_disconnect(self._login_client)

    @staticmethod
    async def _safe_disconnect(client: Optional[TelegramClient]):
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Login state machine (each step called from the bot, owner-only)
    # ------------------------------------------------------------------
    async def start_login(self, phone: str) -> str:
        async with self._login_lock:
            if self.is_connected():
                return "An account is already connected. Logout first."

            # Any stale/abandoned login-in-progress client is cleaned up
            # before starting a fresh attempt, so a previous expired/invalid
            # code never causes a false "already connected".
            await self._cleanup_login_client()

            client = TelegramClient(StringSession(), config.API_ID, config.API_HASH,
                                     connection_retries=10, retry_delay=2)
            await client.connect()
            try:
                sent = await client.send_code_request(phone)
            except PhoneNumberInvalidError:
                await self._safe_disconnect(client)
                return "Invalid phone number format. Use international format, e.g. +15551234567."
            except FloodWaitError as e:
                await self._safe_disconnect(client)
                return f"Telegram asked us to wait {e.seconds}s before requesting another code."
            except Exception as e:
                await self._safe_disconnect(client)
                logger.exception("Unexpected error requesting login code")
                return f"Couldn't request a login code: {type(e).__name__}. Try again."

            self._phone_code_hash = sent.phone_code_hash
            self._pending_phone = phone
            self._login_client = client
            await db.set_login_state(LoginState.CODE, phone=phone)
            return "OK"

    async def submit_code(self, code: str) -> str:
        async with self._login_lock:
            if not self._login_client or not self._pending_phone or not self._phone_code_hash:
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.IDLE)
                return "No login in progress. Press Login Account to start again."

            try:
                await self._login_client.sign_in(
                    phone=self._pending_phone, code=code, phone_code_hash=self._phone_code_hash
                )
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                # Telegram invalidates the whole login attempt once a wrong
                # or expired code is submitted - retrying the same code
                # never succeeds, so the login client must be torn down and
                # the owner must request a brand new code.
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.FAILED)
                reason = "expired" if isinstance(e, PhoneCodeExpiredError) else "invalid"
                return f"CODE_{reason.upper()}"
            except SessionPasswordNeededError:
                await db.set_login_state(LoginState.PASSWORD, phone=self._pending_phone)
                return "2FA_REQUIRED"
            except FloodWaitError as e:
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.FAILED)
                return f"Telegram asked us to wait {e.seconds}s. Press Login Account to try again after that."
            except Exception as e:
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.FAILED)
                logger.exception("Unexpected error submitting login code")
                return f"Login failed: {type(e).__name__}. Press Login Account to start again."

            return await self._finalize_login()

    async def submit_password(self, password: str) -> str:
        async with self._login_lock:
            if not self._login_client or not self._pending_phone:
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.IDLE)
                return "No login in progress. Press Login Account to start again."
            try:
                await self._login_client.sign_in(password=password)
            except PasswordHashInvalidError:
                return "Incorrect 2FA password. Try again."
            except FloodWaitError as e:
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.FAILED)
                return f"Telegram asked us to wait {e.seconds}s. Press Login Account to try again after that."
            except Exception as e:
                await self._cleanup_login_client()
                await db.set_login_state(LoginState.FAILED)
                logger.exception("Unexpected error submitting 2FA password")
                return f"Login failed: {type(e).__name__}. Press Login Account to start again."

            return await self._finalize_login()

    async def _finalize_login(self) -> str:
        try:
            me = await self._login_client.get_me()
            string_session = self._login_client.session.save()
            await db.save_session_string(string_session, phone=self._pending_phone)
        except Exception as e:
            # Saving the session failed (e.g. transient Mongo error) - do not
            # leave a half-finished login lying around; the owner must retry.
            await self._cleanup_login_client()
            await db.set_login_state(LoginState.FAILED)
            logger.exception("Failed to finalize login (session save failed)")
            return f"Login almost succeeded but saving the session failed: {type(e).__name__}. Please try again."

        await db.set_connected(True)
        self.client = self._login_client
        self._login_client = None
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
                await self._safe_disconnect(self.client)
            self.client = None
            await self._cleanup_login_client()
            await db.clear_session()

    async def cancel_login(self):
        async with self._login_lock:
            await self._cleanup_login_client()
            await db.set_login_state(LoginState.IDLE)

    async def _cleanup_login_client(self):
        """Tear down any in-progress (unauthorized) login client and state."""
        if self._login_client:
            await self._safe_disconnect(self._login_client)
        self._login_client = None
        self._phone_code_hash = None
        self._pending_phone = None


account_manager = AccountManager()
