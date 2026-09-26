"""
Media type filter toggles (point 10) + the "hide forward tag" toggle.
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

    @client.on(events.CallbackQuery(pattern=b"^settings:toggle_forward_tag$"))
    @owner_only_callback
    async def _toggle_forward_tag(event):
        await db.toggle_hide_forward_tag()
        await _show_menu(event)


async def _show_menu(event):
    settings = await db.get_settings()
    filters = settings.get("media_filters", {})
    hide_tag = settings.get("hide_forward_tag", True)

    lines = ["\u2699\uFE0F <b>Settings</b>", "", "<b>Media Filters</b> \u2014 only enabled types are forwarded:"]
    buttons = []
    for mt in MEDIA_TYPES:
        on = filters.get(mt, True)
        state_label = "\u2705 ON" if on else "\u26AB OFF"
        label = f"{MEDIA_TYPE_LABELS[mt]}  {state_label}"
        buttons.append([Button.inline(label, f"settings:toggle:{mt}".encode())])

    forward_state_label = "\u2705 ON (no source tag shown)" if hide_tag else "\u26AB OFF (shows 'Forwarded from')"
    lines.append("")
    lines.append("<b>Hide Forward Tag</b> \u2014 when ON, messages are re-sent as new "
                  "posts instead of native forwards, so the destination never shows "
                  "\"Forwarded from &lt;source&gt;\".")
    buttons.append([Button.inline(f"\U0001F648 Hide Forward Tag  {forward_state_label}",
                                   b"settings:toggle_forward_tag")])

    buttons.append([Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")])
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
