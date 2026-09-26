"""
Account menu: login via a pasted session string, logout, status.
Restricted to OWNER_ID throughout (point 21/24/40).

WHY NOT OTP-IN-BOT: Telegram's own anti-scam system blocks a login the
moment its OTP code is typed into any Telegram chat (including this bot),
so an in-bot phone/code/2FA flow cannot work reliably - see
app/telegram_client.py's module docstring and README section 8.
Instead, the owner runs generate_session.py locally (outside any Telegram
chat) and pastes the resulting session string here.
"""
import logging

from telethon import Button, events

from app.bot import conversation_state, owner_only_callback
from app.database import db
from app.handlers import flows
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
        await _prompt_for_string(event)

    @client.on(events.CallbackQuery(pattern=b"^account:logout$"))
    @owner_only_callback
    async def _logout_confirm(event):
        await event.edit(
            "\u26A0\uFE0F Logout the connected personal account? This stops all forwarding "
            "until you connect a new session.",
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
        conversation_state.clear(event.chat_id)
        await event.answer("Cancelled.")
        await _show_menu(event)


async def _prompt_for_string(event):
    conversation_state.set_flow(event.chat_id, flows.LOGIN_SESSION_STRING)
    await event.edit(
        "\U0001F511 <b>Login - Paste Session String</b>\n\n"
        "1. On your computer, run <code>python generate_session.py</code> "
        "(instructions in the README).\n"
        "2. Log in there with your phone/code/2FA \u2014 that happens outside "
        "Telegram entirely, so it won't get blocked.\n"
        "3. Copy the printed session string and paste it here as a single message.\n\n"
        "\u26A0\uFE0F This string grants full account access \u2014 treat it like a password. "
        "Your message will be deleted automatically right after processing.\n\n"
        "Send /cancel to abort.",
        parse_mode="html",
        buttons=[[Button.inline("\u2B05\uFE0F Back", b"account:menu")]],
    )


async def handle_text(client, event, flow: str) -> bool:
    if flow != flows.LOGIN_SESSION_STRING:
        return False

    # Never logged (point 21/34/40) - the string is as sensitive as a password.
    session_string = event.raw_text.strip()
    result = await account_manager.login_with_string(session_string)

    # Delete the message containing the session string immediately, whether
    # login succeeded or failed, so it doesn't linger in chat history.
    try:
        await event.delete()
    except Exception:
        pass

    if result == "OK":
        conversation_state.clear(event.chat_id)
        await event.respond("\u2705 Connected successfully! Personal account is now linked.")
        await monitor.refresh_handlers()
        from app.handlers.start import show_dashboard
        await show_dashboard(client, event.chat_id)
    else:
        conversation_state.clear(event.chat_id)
        await event.respond(
            f"\u274C {result}",
            buttons=[[Button.inline("\U0001F504 Try Again", b"account:login_start")]],
        )
    return True


async def cancel_flow(chat_id: int, flow: str):
    # Nothing to tear down server-side for this flow - login_with_string()
    # is a single atomic call, not a multi-step session.
    pass


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
        buttons = [[Button.inline("\U0001F511 Login (Paste Session String)", b"account:login_start")]]
    buttons.append([Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")])
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
