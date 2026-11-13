"""
Statistics view (point 28).
"""
from telethon import Button, events

from app.bot import owner_only_callback
from app.database import db


def register(client):
    @client.on(events.CallbackQuery(pattern=b"^stats:menu$"))
    @owner_only_callback
    async def _menu(event):
        stats = await db.get_stats()
        counts = await db.queue_counts()
        sources = await db.list_sources()

        lines = [
            "\U0001F4CA <b>Statistics</b>", "",
            f"Total discovered: {stats.get('total_discovered', 0):,}",
            f"Total queued (ever): {stats.get('total_queued', 0):,}",
            f"Total forwarded: {stats.get('total_forwarded', 0):,}",
            f"Total failed: {stats.get('total_failed', 0):,}",
            f"Today forwarded: {stats.get('today_forwarded', 0):,}",
            "",
            f"Live queue: {counts['LIVE']:,}",
            f"Backlog queue: {counts['BACKLOG']:,}",
            f"Failed jobs: {counts['FAILED']:,}",
            "",
            f"Last forward: {stats.get('last_forward_at') or 'never'}",
            f"Last error: {(stats.get('last_error') or 'none')[:80]}",
            "",
            "<b>Per-source:</b>",
        ]
        for s in sources[:10]:
            st = s.get("stats", {})
            lines.append(f"\u2022 {s['title'][:24]}: fwd {st.get('forwarded', 0)}, "
                          f"failed {st.get('failed', 0)}")

        buttons = [[Button.inline("\u2B05\uFE0F Back to Dashboard", b"ctl:refresh")]]
        await event.edit("\n".join(lines), parse_mode="html", buttons=buttons)
