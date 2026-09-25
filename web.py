"""
OPTIONAL lightweight health/status endpoint.

This process must NEVER run the forwarding engine (worker.py) - it only
reports status by reading from MongoDB, so it is safe to run this as a
separate Heroku `web` dyno type (e.g. for uptime monitoring / Heroku's
required $PORT binding) without risking a second forwarding worker.
"""
import asyncio
import json
from datetime import datetime, timezone

from aiohttp import web

from app.config import config
from app.database import db


async def health(request):
    try:
        counts = await db.queue_counts()
        stats = await db.get_stats()
        settings = await db.get_settings()
        session = await db.get_session()
        payload = {
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
            "account_connected": session.get("connected", False),
            "paused": settings.get("paused", False),
            "queue": counts,
            "total_forwarded": stats.get("total_forwarded", 0),
            "total_failed": stats.get("total_failed", 0),
        }
        return web.json_response(payload)
    except Exception as e:
        return web.json_response({"status": "error", "detail": str(e)}, status=503)


async def root(request):
    return web.Response(text="Telegram Forwarder - control interface runs via the bot. See /health.")


async def on_startup(app):
    await db.connect()


async def on_cleanup(app):
    await db.close()


def build_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=config.PORT)
