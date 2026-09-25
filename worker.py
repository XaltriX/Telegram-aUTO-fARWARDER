"""
Main worker entrypoint - runs:
  - the control bot (Telethon, BOT_TOKEN)
  - the personal account client bootstrap (Telethon, StringSession)
  - the single forwarding scheduler
  - live monitoring + periodic recovery sync
  - the throttled dashboard refresh loop

This is the ONLY process that performs forwarding. If a `web` dyno is also
declared in the Procfile, it must run app/web.py's lightweight health
endpoint ONLY - never a second copy of this file (point 32/36).
"""
import asyncio
import logging
import signal

from app.config import config
from app.database import db
from app.dashboard import DashboardManager
import app.dashboard as dashboard_module
from app.monitor import monitor
from app.scheduler import scheduler
from app.telegram_client import account_manager
from app.utils.logging import setup_logging
from app import bot as bot_module

logger = logging.getLogger(__name__)


class Application:
    def __init__(self):
        self.bot_client = None
        self._tasks = []
        self._shutting_down = False

    async def start(self):
        setup_logging(config.LOG_LEVEL)
        logger.info("Starting Telegram Forwarder...")

        await db.connect()

        self.bot_client = await bot_module.create_bot_client()
        bot_module.register_handlers(self.bot_client)

        dashboard_module.dashboard_manager = DashboardManager(lambda: self.bot_client)

        await account_manager.bootstrap()
        if account_manager.is_connected():
            await monitor.install_handlers()

        self._wire_notifications()

        self._tasks = [
            asyncio.create_task(self.bot_client.run_until_disconnected(), name="bot"),
            asyncio.create_task(scheduler.run(), name="scheduler"),
            asyncio.create_task(dashboard_module.dashboard_manager.start_loop(), name="dashboard"),
            asyncio.create_task(monitor.recovery_loop(), name="recovery"),
        ]

        logger.info("All subsystems started. Send /start to the bot as OWNER_ID=%s.", config.OWNER_ID)
        await asyncio.gather(*self._tasks)

    def _wire_notifications(self):
        async def on_started(job):
            pass  # dashboard already reflects "current job" via DB read

        async def on_done(job, forwarded_id):
            from app.dashboard import dashboard_manager
            state = await db.get_dashboard_state()
            if state and state.get("chat_id"):
                await dashboard_manager.notify(
                    state["chat_id"],
                    f"\u2705 <b>FORWARDED</b>\n\U0001F4E6 msg #{job['source_message_id']} ({job['media_type']})",
                )
            await dashboard_manager.refresh()

        async def on_failed(job, error):
            from app.dashboard import dashboard_manager
            state = await db.get_dashboard_state()
            if state and state.get("chat_id"):
                await dashboard_manager.notify(
                    state["chat_id"],
                    f"\u26A0\uFE0F <b>RETRY/FAILED</b>\n\U0001F4E6 msg #{job['source_message_id']}\n{error[:150]}",
                )
            await dashboard_manager.refresh()

        async def on_flood_wait(job, seconds):
            from app.dashboard import dashboard_manager
            state = await db.get_dashboard_state()
            if state and state.get("chat_id"):
                await dashboard_manager.notify(
                    state["chat_id"],
                    f"\U0001F6A6 <b>RATE LIMITED</b>\nWaiting {seconds}s as requested by Telegram\u2026",
                )
            await dashboard_manager.refresh(force=True)

        async def on_new_live_item(source, message, job):
            from app.dashboard import dashboard_manager
            from app.utils.helpers import get_filename
            state = await db.get_dashboard_state()
            if state and state.get("chat_id"):
                await dashboard_manager.notify(
                    state["chat_id"],
                    f"\U0001F6A8 <b>NEW FILE</b>\n\U0001F4E1 {source['title']}\n"
                    f"\U0001F4E6 {get_filename(message)}\n\u26A1 Added to live queue",
                )

        scheduler.on_job_started = on_started
        scheduler.on_job_done = on_done
        scheduler.on_job_failed = on_failed
        scheduler.on_flood_wait = on_flood_wait
        monitor.on_new_live_item = on_new_live_item

    async def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Graceful shutdown initiated...")

        await scheduler.stop()

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        await account_manager.disconnect()
        if self.bot_client:
            await self.bot_client.disconnect()
        await db.close()
        logger.info("Shutdown complete.")


async def main():
    app = Application()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Termination signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows fallback - not expected on Heroku

    async def _watch_stop():
        await stop_event.wait()
        await app.shutdown()

    watcher = asyncio.create_task(_watch_stop())
    try:
        await app.start()
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        await watcher


if __name__ == "__main__":
    asyncio.run(main())
