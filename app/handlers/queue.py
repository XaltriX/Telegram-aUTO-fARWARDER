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


    @client.on(events.CallbackQuery(pattern=b"^ctl:(refresh|home)$"))
    @owner_only_callback
    async def _refresh(event):
        from app.dashboard import dashboard_manager
        await dashboard_manager.show_home(event)
        await event.answer()


async def _show_menu(event):
    from app.utils.helpers import TYPE_ICON, escape_html, message_link, truncate
    counts = await db.queue_counts()
    sources = {s["channel_id"]: s for s in await db.list_sources()}
    lines = [
        "\U0001F4E5 <b>QUEUE</b>", "\u2501" * 18,
        f"\U0001F534 live {counts['LIVE']:,}    \U0001F4E6 backlog {counts['BACKLOG']:,}    \u274C failed {counts['FAILED']:,}",
        f"\u23F3 <b>{counts['TOTAL_QUEUED']:,}</b> files left to send", "",
        "\u23ED <b>UP NEXT</b>",
    ]
    nxt = await db.next_jobs(5)
    if not nxt:
        lines.append("nothing queued")
    for j in nxt:
        src = sources.get(j["source_channel_id"])
        t = escape_html(truncate(j.get("title") or f"#{j['source_message_id']}", 46))
        link = message_link(src, j["source_message_id"]) if src else None
        lab = f'<a href="{link}">{t}</a>' if link else t
        n = len(j.get("message_ids") or [1])
        lines.append(f"{TYPE_ICON.get(j['media_type'], '')} {lab}" + (f" (+{n-1})" if n > 1 else ""))
    buttons = []
    if counts["FAILED"]:
        buttons.append([Button.inline(f"\u274C View failed ({counts['FAILED']:,})", b"queue:failed"),
                        Button.inline("\U0001F501 Retry all", b"queue:retry_all")])
    buttons.append([Button.inline("\U0001F504 Refresh", b"queue:menu"),
                    Button.inline("\u2B05\uFE0F Back", b"ctl:home")])
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons, link_preview=False)


async def _show_failed(event):
    from app.utils.helpers import escape_html, truncate
    jobs = await db.list_failed_jobs(limit=8)
    lines = ["\u274C <b>FAILED</b> (latest 8)", "\u2501" * 18]
    if not jobs:
        lines.append("None \U0001F389")
    for j in jobs:
        t = escape_html(truncate(j.get("title") or f"msg {j['source_message_id']}", 40))
        err = escape_html(truncate(j.get("last_error") or "unknown error", 70))
        lines.append(f"\u2022 {t}\n   <i>{err}</i>")
    buttons = [
        [Button.inline("\U0001F501 Retry all", b"queue:retry_all")],
        [Button.inline("\u2B05\uFE0F Back", b"queue:menu")],
    ]
    await event.edit("\n".join(lines), parse_mode="html", buttons=buttons, link_preview=False)
