"""
Account menu: login (phone -> code -> optional 2FA), logout, status.
Restricted to OWNER_ID throughout (point 21/24/40).

Text-input steps of the login flow are NOT handled by a local catch-all
NewMessage handler (that would conflict with other flows' text input).
Instead, app/bot.py owns a single NewMessage router that checks
conversation_state's current flow and calls handle_text() below.
"""
import logging

from telethon import Button, events

from app.bot import conversation_state, owner_only_callback
from app.database import db
from app.handlers import flows
from app.models import LoginState
from app.monitor import monitor
from app.telegram_client import account_manager

logger = logging.getLogger(__name__)


def register(client):
    @client.on(events.CallbackQuery(pattern=b"^account:menu$"))
    @owner_only_callback
    async def _menu(event):
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=b"^account:login_start$"))
    @owner_only_callback
    async def _login_start(event):
        await _begin_phone_step(event)

    @client.on(events.CallbackQuery(pattern=b"^account:login_retry$"))
    @owner_only_callback
    async def _login_retry(event):
        # Request New Code: the previous login-in-progress client (if any)
        # was already torn down by account_manager when the code turned
        # out invalid/expired, so this just re-opens the phone step.
        await account_manager.cancel_login()
        await _begin_phone_step(event)

    @client.on(events.CallbackQuery(pattern=b"^account:logout$"))
    @owner_only_callback
    async def _logout_confirm(event):
        await event.edit(
            "\u26A0\uFE0F Logout the connected personal account? This stops all forwarding "
            "until you log in again.",
            buttons=[[Button.inline("\u2705 Confirm Logout", b"account:logout_confirm"),
                      Button.inline("\u274C Cancel", b"account:menu")]],
        )

    @client.on(events.CallbackQuery(pattern=b"^account:logout_confirm$"))
    @owner_only_callback
    async def _logout_do(event):
        await account_manager.logout()
        conversation_state.clear(event.chat_id)
        await event.answer("Account logged out.", alert=True)
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=b"^account:cancel_login$"))
    @owner_only_callback
    async def _cancel_login(event):
        await account_manager.cancel_login()
        conversation_state.clear(event.chat_id)
        await event.answer("Login cancelled.")
        await _show_menu(event)


async def _begin_phone_step(event):
    conversation_state.set_flow(event.chat_id, flows.LOGIN_PHONE)
    await db.set_login_state(LoginState.PHONE)
    await event.edit(
        "\U0001F510 <b>Login - Step 1/3</b>\n\nSend the phone number in international "
        "format, e.g. <code>+15551234567</code>.\n\nSend /cancel to abort.",
        parse_mode="html", buttons=[[Button.inline("\u2B05\uFE0F Back", b"account:menu")]],
    )


async def handle_text(client, event, flow: str) -> bool:
    """Returns True if this module consumed the message."""
    if flow == flows.LOGIN_PHONE:
        phone = event.raw_text.strip()
        result = await account_manager.start_login(phone)
        if result == "OK":
            conversation_state.set_flow(event.chat_id, flows.LOGIN_CODE)
            await event.respond(
                "\U0001F4F2 <b>Login - Step 2/3</b>\n\nEnter the login code Telegram just "
                "sent you (as digits, e.g. <code>12345</code>).\n\nSend /cancel to abort.",
                parse_mode="html",
            )
        else:
            await event.respond(f"\u274C {result}")
        return True

    if flow == flows.LOGIN_CODE:
        # Users often paste the code with spaces (e.g. "1 2 3 4 5") - strip
        # them so a cosmetic difference never triggers a false invalid-code.
        code = event.raw_text.strip().replace(" ", "")
        # We deliberately never log the code (point 21/34/40).
        result = await account_manager.submit_code(code)

        if result == "OK":
            conversation_state.clear(event.chat_id)
            await event.respond("\u2705 Logged in successfully! Personal account is now connected.")
            await monitor.refresh_handlers()
            from app.handlers.start import show_dashboard
            await show_dashboard(client, event.chat_id)

        elif result == "2FA_REQUIRED":
            conversation_state.set_flow(event.chat_id, flows.LOGIN_2FA)
            await event.respond(
                "\U0001F512 <b>Login - Step 3/3</b>\n\nThis account has 2FA enabled. "
                "Enter your Telegram password.\n\nSend /cancel to abort.",
                parse_mode="html",
            )

        elif result in ("CODE_INVALID", "CODE_EXPIRED"):
            conversation_state.clear(event.chat_id)
            why = "expired" if result == "CODE_EXPIRED" else "invalid"
            await event.respond(
                f"\u274C That code was {why}. Telegram invalidates the whole login attempt "
                f"once a wrong or expired code is submitted, so please request a brand new "
                f"code and use only the newest one Telegram sends you.",
                buttons=[[Button.inline("\U0001F504 Request New Code", b"account:login_retry")]],
            )

        else:
            # Any other terminal failure (FloodWait, unexpected error, etc.)
            conversation_state.clear(event.chat_id)
            await event.respond(
                f"\u274C {result}",
                buttons=[[Button.inline("\U0001F504 Request New Code", b"account:login_retry")]],
            )
        return True

    if flow == flows.LOGIN_2FA:
        password = event.raw_text.strip()
        # Never logged (point 21/34/40).
        result = await account_manager.submit_password(password)
        if result == "OK":
            conversation_state.clear(event.chat_id)
            await event.respond("\u2705 Logged in successfully! Personal account is now connected.")
            await monitor.refresh_handlers()
            from app.handlers.start import show_dashboard
            await show_dashboard(client, event.chat_id)
        elif result == "Incorrect 2FA password. Try again.":
            # Stays in the same flow/step so the owner can retry the password.
            await event.respond(f"\u274C {result}")
        else:
            conversation_state.clear(event.chat_id)
            await event.respond(
                f"\u274C {result}",
                buttons=[[Button.inline("\U0001F504 Request New Code", b"account:login_retry")]],
            )
        return True

    return False


async def cancel_flow(chat_id: int, flow: str):
    if flow in (flows.LOGIN_PHONE, flows.LOGIN_CODE, flows.LOGIN_2FA):
        await account_manager.cancel_login()


async def _show_menu(event):
    session = await db.get_session()
    connected = session.get("connected", False)
    phone = session.get("phone")
    lines = ["\U0001F464 <b>Account</b>", ""]
    if connected:
        phone_suffix = f" ({phone})" if phone else ""
        lines.append(f"Status: \u2705 Connected{phone_suffix}")
        buttons = [[Button.inline("\U0001F510 Logout Account", b"account:logout")]]
    else:
        lines.append("Status: \u274C Disconnected")
        buttons = [[Button.inline("\U0001F510 Login Account", b"account:login_start")]]
    buttons.append([Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")])
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
