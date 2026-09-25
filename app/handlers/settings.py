"""
Media type filter toggles (point 10).
"""
from telethon import Button, events

from app.bot import owner_only_callback
from app.database import db
from app.models import MEDIA_TYPE_LABELS, MEDIA_TYPES


def register(client):
    @client.on(events.CallbackQuery(pattern=b"^settings:menu$"))
    @owner_only_callback
    async def _menu(event):
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=rb"^settings:toggle:(\w+)$"))
    @owner_only_callback
    async def _toggle(event):
        media_type = event.pattern_match.group(1).decode()
        if media_type in MEDIA_TYPES:
            await db.toggle_media_filter(media_type)
        await _show_menu(event)


async def _show_menu(event):
    settings = await db.get_settings()
    filters = settings.get("media_filters", {})
    lines = ["\u2699\uFE0F <b>Media Filters</b>", "", "Only enabled types are forwarded:"]
    buttons = []
    for mt in MEDIA_TYPES:
        on = filters.get(mt, True)
        label = f"{MEDIA_TYPE_LABELS[mt]}  {'\u2705 ON' if on else '\u26AB OFF'}"
        buttons.append([Button.inline(label, f"settings:toggle:{mt}".encode())])
    buttons.append([Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")])
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
