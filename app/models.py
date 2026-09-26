"""
Shared enums / constants used across the application.
MongoDB documents are plain dicts (schema-less), these constants keep the
field values consistent everywhere.
"""


class JobStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class Priority:
    LIVE = "LIVE"
    BACKLOG = "BACKLOG"


class LoginState:
    IDLE = "LOGIN_IDLE"
    PHONE = "LOGIN_PHONE"
    CODE = "LOGIN_CODE"
    PASSWORD = "LOGIN_2FA"
    COMPLETE = "LOGIN_COMPLETE"
    FAILED = "LOGIN_FAILED"


class SourceMessageGoneError(Exception):
    """Raised when a source message was deleted before it could be copied
    (used by the 'hide forward tag' copy-send path in scheduler.py)."""
    pass


class ScanMode:
    ALL = "ALL"
    LAST_X = "LAST_X"
    FROM_DATE = "FROM_DATE"
    NEW_ONLY = "NEW_ONLY"


# Media type keys used in settings.media_filters and job.media_type
MEDIA_TYPES = ["document", "video", "photo", "audio", "voice", "text"]

MEDIA_TYPE_LABELS = {
    "document": "\U0001F4C4 Documents",
    "video": "\U0001F3AC Videos",
    "photo": "\U0001F5BC\uFE0F Photos",
    "audio": "\U0001F3B5 Audio",
    "voice": "\U0001F399\uFE0F Voice",
    "text": "\U0001F4DD Text",
}

DEFAULT_MEDIA_FILTERS = {m: True for m in MEDIA_TYPES}

# Errors that are worth retrying (transient / network / server-side)
RETRYABLE_ERROR_MARKERS = (
    "timeout",
    "network",
    "connection",
    "TimeoutError",
    "ConnectionError",
    "ServerError",
    "InternalServerError",
)

# Errors that are permanent / configuration issues - never retried
PERMANENT_ERROR_MARKERS = (
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "ChatWriteForbiddenError",
    "UserBannedInChannelError",
    "PeerIdInvalidError",
    "ChannelInvalidError",
    "MessageIdInvalidError",
    "MessageNotModifiedError",
    "UsernameNotOccupiedError",
)
