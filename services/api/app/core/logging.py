"""Logging configuration.

Two output shapes from one switch. ``LOG_JSON=true`` emits one JSON object per
line, which is what a log shipper wants; ``false`` emits a readable line, which is
what a developer with a terminal wants. Both carry the request id, so a 500 and the
three warnings that preceded it can be tied to the same request.

The request id lives in a ``ContextVar`` rather than being threaded through every
call: an async request handler and everything it awaits share the context, so a
log call five layers into the risk engine still knows which request it belongs to
without the engine taking a logging parameter it has no business knowing about.

Uvicorn's own access log is left alone. The middleware in ``app.api.middleware``
therefore logs completed requests only when they are slow or unsuccessful, rather
than printing a second line for every healthy request.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any, Dict

from app.core.config import get_settings

__all__ = ["RequestIdFilter", "configure_logging", "request_id_ctx"]

#: Set by the request-context middleware; ``-`` outside a request (startup, CLI).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

#: Attributes ``logging`` puts on every record. Anything outside this set was added
#: by a caller via ``extra=`` and is worth emitting.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"request_id", "asctime", "message", "taskName"}


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with ``extra=`` fields preserved."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so a UUID or datetime in extra= cannot break the log line.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging() -> None:
    """Install handlers on the root logger. Idempotent.

    Called once from the application factory. Existing handlers are replaced rather
    than added to, so a reload under ``uvicorn --reload`` does not produce every
    line twice.
    """

    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if settings.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # SQLAlchemy's engine logger is controlled by DB_ECHO, not by LOG_LEVEL: a
    # DEBUG level otherwise floods the log with every statement and its parameters,
    # which is also the fastest way to write user data into a log file.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )
