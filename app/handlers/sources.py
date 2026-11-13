"""
Source channel management: list, add (by ID or forwarded message),
initial-scan configuration, and removal with the 3 documented options
(point 8, 15, 20).
"""
import logging
from datetime import datetime, timezone

from telethon import Button, events
from telethon.tl.types import PeerChannel

from app.bot import conversation_state, owner_only_callback
from app.database import db
from app.handlers import flows
from app.models import ScanMode
from app.monitor import monitor
from app.telegram_client import account_manager

logger = logging.getLogger(__name__)

PAGE_SIZE = 6


def register(client):
    @client.on(events.CallbackQuery(pattern=b"^src:menu$"))
    @owner_only_callback
    async def _menu(event):
        await _show_list(event, page=0)

    @client.on(events.CallbackQuery(pattern=rb"^src:page:(\d+)$"))
    @owner_only_callback
    async def _page(event):
        page = int(event.pattern_match.group(1))
        await _show_list(event, page=page)

    @client.on(events.CallbackQuery(pattern=b"^src:add$"))
    @owner_only_callback
    async def _add_menu(event):
        await event.edit(
            "\u2795 <b>Add Source</b>\n\nHow would you like to identify the channel?",
            parse_mode="html",
            buttons=[
                [Button.inline("\U0001F194 Enter Channel ID", b"src:add_id"),
                 Button.inline("\U0001F4E8 Forward Message", b"src:add_fwd")],
                [Button.inline("\u2B05\uFE0F Back", b"src:menu")],
            ],
        )

    @client.on(events.CallbackQuery(pattern=b"^src:add_id$"))
    @owner_only_callback
    async def _add_by_id(event):
        conversation_state.set_flow(event.chat_id, flows.ADD_SOURCE_ID)
        await event.edit(
            "Send the channel ID (e.g. <code>-1001234567890</code>) or public "
            "<code>@username</code>.\n\nSend /cancel to abort.",
            parse_mode="html",
        )

    @client.on(events.CallbackQuery(pattern=b"^src:add_fwd$"))
    @owner_only_callback
    async def _add_by_forward(event):
        conversation_state.set_flow(event.chat_id, "")
        conversation_state.set(event.chat_id, "awaiting_forward_for", "source")
        await event.edit(
            "\U0001F4E8 Forward any message from the source channel here.\n\n"
            "(The personal account must already have access to it.)\n\nSend /cancel to abort.",
        )

    @client.on(events.CallbackQuery(pattern=rb"^src:scan:(\w+):(-?\d+)$"))
    @owner_only_callback
    async def _pick_scan_mode(event):
        mode = event.pattern_match.group(1).decode()
        channel_id = int(event.pattern_match.group(2))
        if mode == ScanMode.LAST_X:
            conversation_state.set_flow(event.chat_id, flows.ADD_SOURCE_SCAN_LAST_X)
            conversation_state.set(event.chat_id, "pending_source_channel_id", channel_id)
            await event.edit("Enter how many of the most recent files to import (e.g. <code>200</code>).",
                              parse_mode="html")
            return
        if mode == ScanMode.FROM_DATE:
            conversation_state.set_flow(event.chat_id, flows.ADD_SOURCE_SCAN_DATE)
            conversation_state.set(event.chat_id, "pending_source_channel_id", channel_id)
            await event.edit("Enter the start date as <code>YYYY-MM-DD</code>.", parse_mode="html")
            return
        await _finalize_add_source(event, channel_id, {"mode": mode})

    @client.on(events.CallbackQuery(pattern=rb"^src:view:(-?\d+)$"))
    @owner_only_callback
    async def _view(event):
        channel_id = int(event.pattern_match.group(1))
        await _show_detail(event, channel_id)

    @client.on(events.CallbackQuery(pattern=rb"^src:toggle:(-?\d+)$"))
    @owner_only_callback
    async def _toggle(event):
        channel_id = int(event.pattern_match.group(1))
        source = await db.get_source(channel_id)
        if source:
            await db.update_source(channel_id, {"enabled": not source.get("enabled", True)})
            await monitor.refresh_handlers()
        await _show_detail(event, channel_id)

    @client.on(events.CallbackQuery(pattern=rb"^src:remove:(-?\d+)$"))
    @owner_only_callback
    async def _remove_menu(event):
        channel_id = int(event.pattern_match.group(1))
        await event.edit(
            "\U0001F5D1 <b>Remove source</b>\n\nWhat should happen to its queued jobs?",
            parse_mode="html",
            buttons=[
                [Button.inline("\U0001F5D1 Remove + Clear Queue", f"src:rm_clear:{channel_id}".encode())],
                [Button.inline("\U0001F4E1 Remove + Keep Queue", f"src:rm_keep:{channel_id}".encode())],
                [Button.inline("\u274C Cancel", f"src:view:{channel_id}".encode())],
            ],
        )

    @client.on(events.CallbackQuery(pattern=rb"^src:rm_(clear|keep):(-?\d+)$"))
    @owner_only_callback
    async def _remove_do(event):
        mode = event.pattern_match.group(1).decode()
        channel_id = int(event.pattern_match.group(2))
        await db.remove_source(channel_id, clear_queue=(mode == "clear"))
        await monitor.refresh_handlers()
        await event.answer("Source removed.", alert=True)
        await _show_list(event, page=0)


async def handle_text(client, event, flow: str) -> bool:
    if flow == flows.ADD_SOURCE_ID:
        raw = event.raw_text.strip()
        await _resolve_and_prompt_scan(client, event, raw)
        return True

    if flow == flows.ADD_SOURCE_SCAN_LAST_X:
        try:
            value = int(event.raw_text.strip())
        except ValueError:
            await event.respond("Please send a whole number, e.g. 200.")
            return True
        channel_id = conversation_state.get(event.chat_id, "pending_source_channel_id")
        conversation_state.clear(event.chat_id)
        await _finalize_add_source(event, channel_id, {"mode": ScanMode.LAST_X, "value": value}, is_edit=False)
        return True

    if flow == flows.ADD_SOURCE_SCAN_DATE:
        try:
            dt = datetime.strptime(event.raw_text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            await event.respond("Please use the format YYYY-MM-DD, e.g. 2026-01-01.")
            return True
        channel_id = conversation_state.get(event.chat_id, "pending_source_channel_id")
        conversation_state.clear(event.chat_id)
        await _finalize_add_source(event, channel_id, {"mode": ScanMode.FROM_DATE, "value": dt}, is_edit=False)
        return True

    return False


async def handle_forward(client, event) -> bool:
    """Handles a forwarded message when awaiting_forward_for == 'source'."""
    if conversation_state.get(event.chat_id, "awaiting_forward_for") != "source":
        return False
    conversation_state.clear(event.chat_id)

    fwd = event.message.fwd_from
    if not fwd or not fwd.from_id or not isinstance(fwd.from_id, PeerChannel):
        await event.respond(
            "\u274C Couldn't identify a channel from that forward. Forwards must have "
            "'show sender' enabled, or use Enter Channel ID instead."
        )
        return True

    channel_id_raw = fwd.from_id.channel_id
    # Telethon/Bot API channel ids are commonly represented as -100<id>.
    channel_id = int(f"-100{channel_id_raw}")
    await _resolve_and_prompt_scan(client, event, str(channel_id))
    return True


async def _resolve_and_prompt_scan(client, event, identifier: str):
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

    # Normalize: Telethon entities report a positive `.id` for channels;
    # jobs/sources are keyed everywhere on the -100 prefixed form.
    from telethon.utils import get_peer_id
    channel_id = get_peer_id(entity)

    title = getattr(entity, "title", None) or getattr(entity, "first_name", "") or str(channel_id)
    username = getattr(entity, "username", None)

    conversation_state.clear(event.chat_id)
    await event.respond(
        f"\u2705 Found: <b>{title}</b>{f' (@{username})' if username else ''}\n\n"
        f"What existing content should be imported?",
        parse_mode="html",
        buttons=[
            [Button.inline("\U0001F4E6 All Files", f"src:scan:{ScanMode.ALL}:{channel_id}".encode())],
            [Button.inline("\U0001F522 Last X Files", f"src:scan:{ScanMode.LAST_X}:{channel_id}".encode())],
            [Button.inline("\U0001F4C5 From Date", f"src:scan:{ScanMode.FROM_DATE}:{channel_id}".encode())],
            [Button.inline("\U0001F195 New Files Only", f"src:scan:{ScanMode.NEW_ONLY}:{channel_id}".encode())],
        ],
    )
    conversation_state.set(event.chat_id, "pending_title", title)
    conversation_state.set(event.chat_id, "pending_username", username)


async def _finalize_add_source(event, channel_id: int, scan_config: dict, is_edit: bool = True):
    title = conversation_state.get(event.chat_id, "pending_title") or str(channel_id)
    username = conversation_state.get(event.chat_id, "pending_username")
    source = await db.add_source(channel_id, title, username, scan_config)
    await monitor.refresh_handlers()

    reply = event.edit if is_edit else event.respond
    try:
        await reply(f"\u2705 Source added: <b>{title}</b>\n\nStarting initial scan in the background\u2026",
                     parse_mode="html")
    except Exception:
        await event.respond(f"\u2705 Source added: <b>{title}</b>\n\nStarting initial scan in the background\u2026",
                             parse_mode="html")

    import asyncio
    asyncio.create_task(_run_initial_scan(source))


async def _run_initial_scan(source: dict):
    try:
        queued = await monitor.scan_initial_backlog(source)
        logger.info("Initial scan for %s queued %d job(s)", source["channel_id"], queued)
    except Exception:
        logger.exception("Initial scan failed for source %s", source["channel_id"])


async def _show_list(event, page: int):
    sources = await db.list_sources()
    total = len(sources)
    start = page * PAGE_SIZE
    page_items = sources[start:start + PAGE_SIZE]

    lines = [f"\U0001F4E1 <b>Sources</b> ({total})", ""]
    buttons = []
    if not page_items:
        lines.append("No sources configured yet.")
    for s in page_items:
        status = "\U0001F7E2" if s.get("enabled") else "\u26AA"
        lines.append(f"{status} {s['title']}  \u2014  queued {s['stats'].get('queued', 0)}, "
                      f"fwd {s['stats'].get('forwarded', 0)}")
        buttons.append([Button.inline(f"{status} {s['title'][:28]}", f"src:view:{s['channel_id']}".encode())])

    nav = []
    if page > 0:
        nav.append(Button.inline("\u2B05\uFE0F", f"src:page:{page-1}".encode()))
    if start + PAGE_SIZE < total:
        nav.append(Button.inline("\u27A1\uFE0F", f"src:page:{page+1}".encode()))
    if nav:
        buttons.append(nav)

    buttons.append([Button.inline("\u2795 Add Source", b"src:add")])
    buttons.append([Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")])

    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)


async def _show_detail(event, channel_id: int):
    s = await db.get_source(channel_id)
    if not s:
        await event.answer("Source no longer exists.", alert=True)
        await _show_list(event, page=0)
        return
    stats = s.get("stats", {})
    status = "Enabled \U0001F7E2" if s.get("enabled") else "Disabled \u26AA"
    lines = [
        f"\U0001F4E1 <b>{s['title']}</b>",
        f"ID: <code>{s['channel_id']}</code>",
        f"Status: {status}",
        f"Order: #{s['source_order']}",
        "",
        f"Discovered: {stats.get('discovered', 0)}",
        f"Queued: {stats.get('queued', 0)}",
        f"Forwarded: {stats.get('forwarded', 0)}",
        f"Failed: {stats.get('failed', 0)}",
        f"Last sync: {s.get('last_sync_at') or 'never'}",
    ]
    buttons = [
        [Button.inline("\u26AA Disable" if s.get("enabled") else "\U0001F7E2 Enable",
                        f"src:toggle:{channel_id}".encode())],
        [Button.inline("\U0001F5D1 Remove", f"src:remove:{channel_id}".encode())],
        [Button.inline("\u2B05\uFE0F Back", b"src:menu")],
    ]
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
