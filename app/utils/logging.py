"""
Structured logging setup.

IMPORTANT: never log secrets. Call-sites must not pass OTP codes, 2FA
passwords, bot tokens, API hashes, Mongo credentials, or session strings
into log messages. This module does not attempt to scrub log content -
it is the responsibility of every call-site to avoid logging secrets.
"""
import logging
import sys


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = []
        for key in ("job_id", "source_id", "message_id", "retry", "queue_state", "wait"):
            val = getattr(record, key, None)
            if val is not None:
                extras.append(f"{key}={val}")
        if extras:
            base = f"{base} | {' '.join(extras)}"
        return base


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    formatter = ContextFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Quiet down noisy third-party loggers unless we're in DEBUG.
    if level != "DEBUG":
        logging.getLogger("telethon").setLevel(logging.WARNING)
        logging.getLogger("pymongo").setLevel(logging.WARNING)
        logging.getLogger("motor").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
