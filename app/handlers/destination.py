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
from app.utils.helpers import escape_html

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

    @client.on(events.CallbackQuery(pattern=b"^dest:reindex$"))
    @owner_only_callback
    async def _reindex(event):
        from app.dest_index import dest_indexer
        await dest_indexer.reindex()
        await event.answer("Indexing destination\u2026")
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=b"^dest:skip_index$"))
    @owner_only_callback
    async def _skip_index(event):
        from app.dest_index import dest_indexer
        await dest_indexer.skip()
        await event.answer("Indexing skipped - duplicates will NOT be filtered.", alert=True)
        await _show_menu(event)


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
        entity = await account_manager.get_entity(target)
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
    from app.dest_index import dest_indexer
    await dest_indexer.ensure()  # start scanning the new destination for existing files
    from app import dashboard as _d
    if _d.dashboard_manager:
        _d.dashboard_manager._dest_titles.pop(channel_id, None)
    await event.respond(f"\u2705 Destination set to: <b>{escape_html(title)}</b>", parse_mode="html")


async def _show_menu(event):
    from app.utils.helpers import progress_bar
    settings = await db.get_settings()
    dest_id = settings.get("destination_channel_id")
    lines = ["\U0001F3AF <b>Destination</b>", ""]
    buttons = []
    if dest_id:
        title = str(dest_id)
        if await account_manager.get_client():
            try:
                title = getattr(await account_manager.get_entity(dest_id), "title", title)
            except Exception:
                pass
        lines.append(f"Current: <b>{escape_html(title)}</b>\nID: <code>{dest_id}</code>")
        di = settings.get("dest_index") or {}
        state = di.get("state") if di.get("dest_id") == dest_id else None
        lines.append("")
        if state == "running":
            sc, tot = di.get("scanned", 0), max(di.get("total", 0), 1)
            lines.append(f"\U0001F50D <b>Indexing</b> {min(100, int(100 * sc / tot))}%\n"
                         f"<code>{progress_bar(sc, tot, 16)}</code>\n{sc:,}/{tot:,} messages \u00B7 {di.get('files', 0):,} files")
            buttons.append([Button.inline("\u23ED Skip indexing", b"dest:skip_index")])
        elif state == "done":
            lines.append(f"\u2705 Indexed {di.get('files', 0):,} files \u2014 duplicates are skipped."
                         + (" (indexing was skipped)" if di.get("skipped") else ""))
        else:
            lines.append("\U0001F50D Index not ready yet (starting / retrying)\u2026")
        buttons.append([Button.inline("\U0001F50D Re-index destination", b"dest:reindex"),
                        Button.inline("\U0001F504 Refresh", b"dest:menu")])
    else:
        lines.append("Not configured yet.")
    buttons.append([Button.inline("\U0001F3AF Set Destination", b"dest:set")])
    buttons.append([Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:home")])
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
