"""Structured logging for the service: loguru writing one JSON object per line.

Every line a request produces carries that request's id, so ``grep`` or a log
tool can follow a single request across modules. Standard-library loggers
(uvicorn, httpx, qdrant) are routed through loguru so the output stays one format.

Never log a question, an answer, a prompt or a key: questions can describe a
user's visa status, and keys would leak to anyone who can read the logs.
"""

import json
import logging
import sys
import traceback
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from loguru import Message, Record

REQUEST_ID_HEADER = "X-Request-ID"

# One mutable dict per request. Sync routes run in a worker thread with a *copy*
# of the context, so they cannot rebind the variable, but they share this dict:
# what the model client writes into it is visible to the middleware afterwards.
_request: ContextVar[dict[str, Any] | None] = ContextVar("request", default=None)

# Standard-library loggers that would otherwise repeat a line per HTTP call.
_QUIET = ("httpx", "httpx2", "httpcore", "urllib3", "uvicorn.access")


def new_request_id(incoming: str | None) -> str:
    """Reuse a caller's request id when it looks sane, otherwise make one.

    Args:
        incoming: the ``X-Request-ID`` header sent by Caddy or a client, if any.

    Returns:
        str: at most 64 printable characters, safe to echo in a header and a log.
    """
    if incoming and len(incoming) <= 64 and incoming.isprintable():
        return incoming
    return uuid.uuid4().hex


def start_request(request_id: str) -> dict[str, Any]:
    """Open the per-request context that every log line will be tagged with.

    Args:
        request_id: the id returned in the ``X-Request-ID`` response header.

    Returns:
        dict: the context; the caller reads model and token fields from it later.
    """
    context: dict[str, Any] = {"request_id": request_id}
    _request.set(context)
    return context


def record_model_call(model: str, tokens: int | None, seconds: float) -> None:
    """Note which model answered, its token count and its time, for the request log.

    Called by the model client; does nothing outside a request (CLI, tests).

    Args:
        model: the model name the server was asked for.
        tokens: total tokens the server reported, or None if it did not say.
        seconds: wall time of the model call, retries included.
    """
    context = _request.get()
    if context is None:
        return
    context["model"] = model
    context["model_seconds"] = round(seconds, 3)
    if tokens is not None:
        context["tokens"] = context.get("tokens", 0) + tokens


def _add_request_id(record: "Record") -> None:
    """Loguru patcher: copy the current request id into every record."""
    context = _request.get()
    if context is not None:
        record["extra"].setdefault("request_id", context["request_id"])


def _json_sink(message: "Message") -> None:
    """Write a record as one compact JSON line on stderr."""
    record = message.record
    line: dict[str, Any] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "message": record["message"],
        **record["extra"],
    }
    if record["exception"] is not None:
        exc_type, exc_value, tb = record["exception"]
        line["exception"] = "".join(
            traceback.format_exception(exc_type, exc_value, tb)
        ).rstrip()
    sys.stderr.write(json.dumps(line, default=str) + "\n")
    sys.stderr.flush()


class InterceptHandler(logging.Handler):
    """Send standard-library log records to loguru, keeping level and origin."""

    def emit(self, record: logging.LogRecord) -> None:
        """Re-emit one standard-library record through loguru."""
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # "logger" in extra overrides loguru's own name, which would be this module.
        logger.bind(logger=record.name).opt(exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging(level: str = "INFO") -> None:
    """Replace every handler with loguru's JSON sink. Safe to call more than once.

    Args:
        level: the lowest level written, such as ``"INFO"`` or ``"DEBUG"``.
    """
    logger.remove()
    logger.configure(patcher=_add_request_id)
    logger.add(_json_sink, level=level.upper())
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    # uvicorn installs its own handlers before importing the app; take them over.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        std = logging.getLogger(name)
        std.handlers = []
        std.propagate = True
    for name in _QUIET:  # the request log line below replaces uvicorn's access log
        logging.getLogger(name).setLevel(logging.WARNING)
