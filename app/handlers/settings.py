"""Media-type filter controls for the owner dashboard."""
from telethon import Button, events

from app.bot import owner_only_callback
from app.database import db
from app.models import MEDIA_TYPE_LABELS, MEDIA_TYPES


# Keep callback data short and restrict it to values supported by the model.
_TOGGLE_PATTERN = rb"^settings:toggle:([^:]+)$"


def register(client):
    """Register the media-filter callback handlers on *client*."""

    @client.on(events.CallbackQuery(pattern=b"^settings:menu$"))
    @owner_only_callback
    async def _menu(event):
        await event.answer()
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=_TOGGLE_PATTERN))
    @owner_only_callback
    async def _toggle(event):
        media_type = event.pattern_match.group(1).decode("utf-8", errors="ignore")

        if media_type not in MEDIA_TYPES:
            # This also handles stale or manually crafted callback data.
            await event.answer("Unknown media type.", alert=True)
            return

        # Answer immediately so Telegram stops showing the callback spinner while
        # MongoDB is being updated and the menu is being rendered.
        await event.answer()
        await db.toggle_media_filter(media_type)
        await _show_menu(event)


async def _show_menu(event):
    """Render the media-filter menu using safe defaults for old DB documents."""
    settings = await db.get_settings() or {}
    stored_filters = settings.get("media_filters")
    filters = stored_filters if isinstance(stored_filters, dict) else {}

    lines = [
        "\u2699\uFE0F <b>Media Filters</b>",
        "",
        "Only enabled types are forwarded:",
    ]
    buttons = []

    for media_type in MEDIA_TYPES:
        enabled = bool(filters.get(media_type, True))
        label = MEDIA_TYPE_LABELS.get(media_type, media_type.title())
        status = "\u2705 ON" if enabled else "\u26AB OFF"
        buttons.append([
            Button.inline(
                f"{label}  {status}",
                data=f"settings:toggle:{media_type}".encode("ascii"),
            )
        ])

    buttons.append([
        Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")
    ])
    await event.edit(
        "\n".join(lines),
        parse_mode="html",
        buttons=buttons,
    )
