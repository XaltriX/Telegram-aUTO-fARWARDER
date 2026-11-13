"""
Destination channel configuration - exactly ONE destination (point 9).
"""
import logging

from telethon import Button, events
from telethon.tl.types import PeerChannel

from app.bot import conversation_state, owner_only_callback
from app.database import db
from app.handlers import flows
from app.telegram_client import account_manager

logger = logging.getLogger(__name__)


def register(client):
    @client.on(events.CallbackQuery(pattern=b"^dest:menu$"))
    @owner_only_callback
    async def _menu(event):
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=b"^dest:set$"))
    @owner_only_callback
    async def _set_menu(event):
        await event.edit(
            "\U0001F3AF <b>Destination</b>\n\nHow would you like to identify the channel?",
            parse_mode="html",
            buttons=[
                [Button.inline("\U0001F194 Enter Channel ID", b"dest:set_id"),
                 Button.inline("\U0001F4E8 Forward Message", b"dest:set_fwd")],
                [Button.inline("\u2B05\uFE0F Back", b"dest:menu")],
            ],
        )

    @client.on(events.CallbackQuery(pattern=b"^dest:set_id$"))
    @owner_only_callback
    async def _set_by_id(event):
        conversation_state.set_flow(event.chat_id, flows.SET_DESTINATION_ID)
        await event.edit("Send the destination channel ID or <code>@username</code>.\n\nSend /cancel to abort.",
                          parse_mode="html")

    @client.on(events.CallbackQuery(pattern=b"^dest:set_fwd$"))
    @owner_only_callback
    async def _set_by_forward(event):
        conversation_state.set_flow(event.chat_id, "")
        conversation_state.set(event.chat_id, "awaiting_forward_for", "destination")
        await event.edit("\U0001F4E8 Forward any message from the destination channel here.\n\nSend /cancel to abort.")


async def handle_text(client, event, flow: str) -> bool:
    if flow == flows.SET_DESTINATION_ID:
        await _resolve_and_set(event, event.raw_text.strip())
        return True
    return False


async def handle_forward(client, event) -> bool:
    if conversation_state.get(event.chat_id, "awaiting_forward_for") != "destination":
        return False
    conversation_state.clear(event.chat_id)

    fwd = event.message.fwd_from
    if not fwd or not fwd.from_id or not isinstance(fwd.from_id, PeerChannel):
        await event.respond("\u274C Couldn't identify a channel from that forward. Try Enter Channel ID instead.")
        return True

    channel_id = int(f"-100{fwd.from_id.channel_id}")
    await _resolve_and_set(event, str(channel_id))
    return True


async def _resolve_and_set(event, identifier: str):
    account_client = await account_manager.get_client()
    if not account_client:
        await event.respond("\u274C The personal account must be connected first (Account \u2192 Login).")
        return

    try:
        target = int(identifier) if identifier.lstrip("-").isdigit() else identifier
        entity = await account_client.get_entity(target)
    except Exception as e:
        await event.respond(f"\u274C Couldn't access that channel with the personal account: {e}")
        return

    from telethon.utils import get_peer_id
    channel_id = get_peer_id(entity)

    # Validate the account can actually send/forward into this channel.
    try:
        perms = await account_client.get_permissions(entity, "me")
        if not (perms.post_messages or perms.is_admin or perms.is_creator):
            await event.respond(
                "\u26A0\uFE0F Saved, but the personal account may not have permission to post "
                "in this channel. Forwarding will fail until that's fixed."
            )
    except Exception:
        pass

    title = getattr(entity, "title", None) or str(channel_id)
    conversation_state.clear(event.chat_id)
    await db.update_settings({"destination_channel_id": channel_id})
    await event.respond(f"\u2705 Destination set to: <b>{title}</b>", parse_mode="html")


async def _show_menu(event):
    settings = await db.get_settings()
    dest_id = settings.get("destination_channel_id")
    lines = ["\U0001F3AF <b>Destination</b>", ""]
    if dest_id:
        title = str(dest_id)
        account_client = await account_manager.get_client()
        if account_client:
            try:
                entity = await account_client.get_entity(dest_id)
                title = getattr(entity, "title", title)
            except Exception:
                pass
        lines.append(f"Current: <b>{title}</b>\nID: <code>{dest_id}</code>")
    else:
        lines.append("Not configured yet.")
    buttons = [
        [Button.inline("\U0001F3AF Set Destination", b"dest:set")],
        [Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")],
    ]
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
