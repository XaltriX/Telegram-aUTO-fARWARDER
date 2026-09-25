"""
Centralized configuration loaded from environment variables.
Never hardcode secrets here. All sensitive values come from the process
environment (Heroku config vars / local .env via python-dotenv).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(
            f"Required environment variable '{name}' is not set. "
            f"See .env.example for the full list of required variables."
        )
    return val


def _require_int(name: str) -> int:
    val = _require(name)
    try:
        return int(val)
    except ValueError:
        raise ConfigError(f"Environment variable '{name}' must be an integer, got: {val!r}")


class Config:
    # --- Required ---
    BOT_TOKEN: str = _require("BOT_TOKEN")
    API_ID: int = _require_int("API_ID")
    API_HASH: str = _require("API_HASH")
    MONGO_URI: str = _require("MONGO_URI")
    OWNER_ID: int = _require_int("OWNER_ID")

    # --- Optional (with sane defaults) ---
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    DASHBOARD_UPDATE_INTERVAL: int = int(os.environ.get("DASHBOARD_UPDATE_INTERVAL", "5"))
    RECOVERY_SYNC_INTERVAL: int = int(os.environ.get("RECOVERY_SYNC_INTERVAL", "300"))
    NOTIFICATION_DELETE_SECONDS: int = int(os.environ.get("NOTIFICATION_DELETE_SECONDS", "8"))
    MONGO_DB_NAME: str = os.environ.get("MONGO_DB_NAME", "tg_forwarder")
    PORT: int = int(os.environ.get("PORT", "8000"))

    # Backlog scan pacing (seconds between API calls while scanning history)
    SCAN_BATCH_SIZE: int = int(os.environ.get("SCAN_BATCH_SIZE", "100"))
    SCAN_BATCH_DELAY: float = float(os.environ.get("SCAN_BATCH_DELAY", "1.0"))

    # Retry / backoff
    MAX_ATTEMPTS: int = int(os.environ.get("MAX_ATTEMPTS", "3"))
    RETRY_BACKOFF_BASE: float = float(os.environ.get("RETRY_BACKOFF_BASE", "5.0"))


config = Config()
