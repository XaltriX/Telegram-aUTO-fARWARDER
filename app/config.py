"""
Central configuration - EVERYTHING lives in this one file.

HOW IT WORKS
------------
Fill the values in the "YOUR VALUES" block below once. That's all.
Resolution order for each setting:
    1. environment variable (Heroku Config Vars / .env), if set
    2. the value written in this file
    3. built-in default (optional settings only)

So you can keep everything here (no need to re-enter Config Vars every
time you create a new Heroku app), and still override any value from
Heroku later if you ever want to.

!!! SECURITY - READ THIS !!!
If you write real secrets in this file, the GitHub repo MUST BE PRIVATE.
Anyone who sees BOT_TOKEN / API_HASH / MONGO_URI can take over your bot
and read/wipe your database. (Telegram also auto-revokes bot tokens that
get leaked in public GitHub repos.)
"""
import os

from dotenv import load_dotenv

load_dotenv()

# =====================================================================
#                         YOUR VALUES (fill these)
# =====================================================================
BOT_TOKEN_VALUE = ""      # from @BotFather, e.g. "123456789:AAxxxxxxxx"
API_ID_VALUE = ""         # from https://my.telegram.org, e.g. "1234567"
API_HASH_VALUE = ""       # from https://my.telegram.org
MONGO_URI_VALUE = ""      # e.g. "mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority"
OWNER_ID_VALUE = ""       # your numeric Telegram user id (from @userinfobot)

# =====================================================================
#                 OPTIONAL SETTINGS (defaults are fine)
# =====================================================================
OPTIONAL_DEFAULTS = {
    "LOG_LEVEL": "INFO",
    "MONGO_DB_NAME": "tg_forwarder",
    "PORT": "8000",

    # Dashboard / notifications
    "DASHBOARD_UPDATE_INTERVAL": "5",
    "NOTIFICATION_DELETE_SECONDS": "8",

    # Monitoring / scanning
    "RECOVERY_SYNC_INTERVAL": "300",
    "SCAN_BATCH_SIZE": "100",
    "SCAN_BATCH_DELAY": "1.0",

    # Retry / backoff
    "MAX_ATTEMPTS": "3",
    "RETRY_BACKOFF_BASE": "5.0",

    # ---- MongoDB auto-cleanup (keeps the database small) ----
    # Finished (DONE) jobs are useless after forwarding - the totals live
    # in the stats counters - so they are deleted after this many hours.
    "JOB_DONE_RETENTION_HOURS": "6",
    # Permanently FAILED jobs are kept this many days so you can retry
    # them from Queue -> Failed, then they are deleted.
    "JOB_FAILED_RETENTION_DAYS": "7",
    # The duplicate-protection ledger entries are tiny; they are removed
    # after this many days (recovery scans resume from a checkpoint, so
    # old messages are never re-scanned).
    "LEDGER_RETENTION_DAYS": "30",
    # How often the cleanup job runs.
    "CLEANUP_INTERVAL_MINUTES": "60",
}


class ConfigError(Exception):
    pass


def _get(name: str, file_value: str = None) -> str:
    """env var > value in this file > OPTIONAL_DEFAULTS."""
    val = os.environ.get(name)
    if val is None or val == "":
        val = file_value
    if val is None or val == "":
        val = OPTIONAL_DEFAULTS.get(name)
    return val


def _required(name: str, file_value: str) -> str:
    val = _get(name, file_value)
    if not val or not str(val).strip():
        raise ConfigError(
            f"'{name}' is not set. Fill {name}_VALUE in app/config.py "
            f"(or set the {name} environment variable)."
        )
    return str(val).strip()


def _int(name: str, raw: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' must be an integer, got: {raw!r}")


def _float(name: str, raw: str) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' must be a number, got: {raw!r}")


class Config:
    # --- Required ---
    BOT_TOKEN: str = _required("BOT_TOKEN", BOT_TOKEN_VALUE)
    API_ID: int = _int("API_ID", _required("API_ID", API_ID_VALUE))
    API_HASH: str = _required("API_HASH", API_HASH_VALUE)
    MONGO_URI: str = _required("MONGO_URI", MONGO_URI_VALUE)
    OWNER_ID: int = _int("OWNER_ID", _required("OWNER_ID", OWNER_ID_VALUE))

    # --- Optional ---
    LOG_LEVEL: str = _get("LOG_LEVEL").upper()
    MONGO_DB_NAME: str = _get("MONGO_DB_NAME")
    PORT: int = _int("PORT", _get("PORT"))

    DASHBOARD_UPDATE_INTERVAL: int = _int("DASHBOARD_UPDATE_INTERVAL", _get("DASHBOARD_UPDATE_INTERVAL"))
    NOTIFICATION_DELETE_SECONDS: int = _int("NOTIFICATION_DELETE_SECONDS", _get("NOTIFICATION_DELETE_SECONDS"))

    RECOVERY_SYNC_INTERVAL: int = _int("RECOVERY_SYNC_INTERVAL", _get("RECOVERY_SYNC_INTERVAL"))
    SCAN_BATCH_SIZE: int = _int("SCAN_BATCH_SIZE", _get("SCAN_BATCH_SIZE"))
    SCAN_BATCH_DELAY: float = _float("SCAN_BATCH_DELAY", _get("SCAN_BATCH_DELAY"))

    MAX_ATTEMPTS: int = _int("MAX_ATTEMPTS", _get("MAX_ATTEMPTS"))
    RETRY_BACKOFF_BASE: float = _float("RETRY_BACKOFF_BASE", _get("RETRY_BACKOFF_BASE"))

    JOB_DONE_RETENTION_HOURS: float = _float("JOB_DONE_RETENTION_HOURS", _get("JOB_DONE_RETENTION_HOURS"))
    JOB_FAILED_RETENTION_DAYS: float = _float("JOB_FAILED_RETENTION_DAYS", _get("JOB_FAILED_RETENTION_DAYS"))
    LEDGER_RETENTION_DAYS: int = _int("LEDGER_RETENTION_DAYS", _get("LEDGER_RETENTION_DAYS"))
    CLEANUP_INTERVAL_MINUTES: int = _int("CLEANUP_INTERVAL_MINUTES", _get("CLEANUP_INTERVAL_MINUTES"))


config = Config()
