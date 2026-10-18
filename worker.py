"""
Main worker entrypoint - runs:
  - the control bot (Telethon, BOT_TOKEN)
  - the personal account client bootstrap (Telethon, StringSession)
  - the single forwarding scheduler
  - live monitoring + periodic recovery sync
  - the throttled dashboard refresh loop

This is the ONLY process that performs forwarding. If a `web` dyno is also
declared in the Procfile, it must run web.py's lightweight health
endpoint ONLY - never a second copy of this file .
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
        """No per-file chat spam: the live card shows progress. Only rare
        events (terminal failures, rate limits) produce a temporary notice."""
        async def _notice(text):
            dm = dashboard_module.dashboard_manager
            state = await db.get_dashboard_state()
            if dm and state.get("chat_id"):
                await dm.notify(state["chat_id"], text)

        async def _refresh(force=False):
            dm = dashboard_module.dashboard_manager
            if dm:
                await dm.refresh(force=force)

        async def on_done(job, forwarded_id):
            await _refresh()

        async def on_failed(job, error):
            await _notice(f"\u26A0\uFE0F <b>FAILED</b> msg #{job['source_message_id']}\n{error[:150]}")
            await _refresh()

        async def on_flood_wait(job, seconds):
            await _notice(f"\U0001F6A6 <b>RATE LIMITED</b> \u2014 waiting {seconds}s")
            await _refresh(force=True)

        scheduler.on_job_started = lambda job: _refresh()
        scheduler.on_job_done = on_done
        scheduler.on_job_failed = on_failed
        scheduler.on_flood_wait = on_flood_wait
        monitor.on_queue_change = _refresh

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
