"""
Queue view: live/backlog/failed counts and failed-job retry (point 18/19).
"""
from telethon import Button, events

from app.bot import owner_only_callback
from app.database import db


def register(client):
    @client.on(events.CallbackQuery(pattern=b"^queue:menu$"))
    @owner_only_callback
    async def _menu(event):
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=b"^queue:failed$"))
    @owner_only_callback
    async def _failed(event):
        await _show_failed(event)

    @client.on(events.CallbackQuery(pattern=b"^queue:retry_all$"))
    @owner_only_callback
    async def _retry(event):
        count = await db.requeue_all_failed()
        await event.answer(f"Requeued {count} failed job(s).", alert=True)
        await _show_menu(event)

    @client.on(events.CallbackQuery(pattern=b"^ctl:toggle_pause$"))
    @owner_only_callback
    async def _toggle_pause(event):
        settings = await db.get_settings()
        new_state = not settings.get("paused", False)
        await db.set_paused(new_state)
        await event.answer("Paused." if new_state else "Resumed.")
        from app.dashboard import dashboard_manager
        if dashboard_manager:
            await dashboard_manager.refresh(force=True)

    @client.on(events.CallbackQuery(pattern=b"^ctl:refresh$"))
    @owner_only_callback
    async def _refresh(event):
        from app.handlers.start import show_dashboard
        await event.delete()
        await show_dashboard(client, event.chat_id)


async def _show_menu(event):
    counts = await db.queue_counts()
    lines = [
        "\U0001F4E5 <b>Queue</b>", "",
        f"\U0001F534 LIVE       {counts['LIVE']:,}",
        f"\U0001F4E6 BACKLOG    {counts['BACKLOG']:,}",
        f"\u274C FAILED     {counts['FAILED']:,}",
        f"\u2795 TOTAL      {counts['TOTAL_QUEUED']:,}",
    ]
    buttons = [
        [Button.inline("\u274C Failed", b"queue:failed")],
        [Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")],
    ]
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)


async def _show_failed(event):
    jobs = await db.list_failed_jobs(limit=10)
    lines = ["\u274C <b>Failed Jobs</b> (latest 10)", ""]
    if not jobs:
        lines.append("None \U0001F389")
    for j in jobs:
        lines.append(f"\u2022 src {j['source_channel_id']} / msg {j['source_message_id']} "
                      f"\u2014 {j.get('last_error', 'unknown error')[:60]}")
    buttons = [
        [Button.inline("\U0001F501 Retry Failed", b"queue:retry_all")],
        [Button.inline("\u2B05\uFE0F Back", b"queue:menu")],
    ]
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
