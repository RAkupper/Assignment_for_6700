"""Structured logs, request IDs and Prometheus metrics for the service.

Logs are one JSON object per line on stderr, so ``docker compose logs app`` can be
piped to ``jq``. Every line written while a request is handled carries that
request's ID, the same one the response sends back in ``X-Request-ID``. Logs name
the route, status, model, timings and token counts; they never contain a
question, an answer, a prompt or a key.
"""

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger
from prometheus_client import Counter, Histogram

if TYPE_CHECKING:
    from loguru import Message, Record

request_id: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"
VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")  # anything else is replaced

# /ask answers that take longer than this count against the SLO (docs/observability.md).
ASK_SLO_SECONDS = 45.0

# Answers take 5 to 50 s on CPU; the top buckets cover the 60 s queue + 120 s model.
SLOW_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)

REQUESTS = Counter(
    "app_requests_total", "HTTP requests by route and status.", ["route", "status"]
)
REQUEST_SECONDS = Histogram(
    "app_request_duration_seconds",
    "Time from request to response, by route.",
    ["route"],
    buckets=SLOW_BUCKETS,
)
ANSWERS = Counter(
    "app_answers_total",
    "/ask answers by outcome: answered, insufficient_evidence (nothing retrieved) "
    "or model_unavailable (passages only).",
    ["status"],
)
ASK_SLO_EVENTS = Counter(
    "app_ask_slo_events_total",
    f"/ask requests judged against the SLO: good means a 2xx written or "
    f"not-covered answer within {ASK_SLO_SECONDS:g} s.",
    ["good"],
)
MODEL_SECONDS = Histogram(
    "app_model_call_duration_seconds",
    "Time spent in the model server per answer, retries included.",
    buckets=SLOW_BUCKETS,
)
MODEL_FAILURES = Counter(
    "app_model_call_failures_total",
    "Model calls that ended without text, by kind of failure.",
    ["kind"],
)
ANSWER_TOKENS = Histogram(
    "app_answer_tokens",
    "Tokens the model wrote per answer, from the server's usage report.",
    buckets=(8, 16, 32, 64, 96, 128, 192, 256, 384, 512, 1024),
)
RETRIEVAL_SECONDS = Histogram(
    "app_retrieval_duration_seconds",
    "Time to embed the question, search Qdrant and rerank.",
    buckets=SLOW_BUCKETS,
)


def new_request_id(sent: str | None) -> str:
    """Keep a caller's ID when it is safe to log; otherwise make a fresh one."""
    if sent and VALID_REQUEST_ID.fullmatch(sent):
        return sent
    return uuid4().hex


def route_label(path_template: str | None, path: str) -> str:
    """Name a request by its route, never by its raw path, so labels stay few."""
    if path_template:
        return path_template
    if path.startswith("/assets/"):
        return "/assets"
    return "unmatched"  # 404s: one label however many paths are probed


def _json_line(message: "Message") -> None:
    record = message.record
    line: dict[str, Any] = {
        "time": record["time"].astimezone(UTC).isoformat(timespec="milliseconds"),
        "level": record["level"].name,
        "logger": record["extra"].get("logger", record["name"]),
        "message": record["message"],
        "request_id": request_id.get(),
    }
    line.update(
        (k, v)
        for k, v in record["extra"].items()
        if k not in {"logger", "request_id"} and v is not None
    )
    exc = record["exception"]
    if exc is not None and exc.type is not None:
        line["exception"] = "".join(
            traceback.format_exception(exc.type, exc.value, exc.traceback)
        )
    sys.stderr.write(json.dumps(line, default=str) + "\n")


def _plain_format(record: "Record") -> str:
    return "{level}: {message}\n{exception}"


class _ToLoguru(logging.Handler):
    """Send records from the standard library (uvicorn, httpx2, ours) to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(exception=record.exc_info).bind(logger=record.name).log(
            level, record.getMessage()
        )


def setup_logging(json_lines: bool = True, level: str = "INFO") -> None:
    """Route every log line through loguru: JSON for the service, plain for CLIs.

    The service calls this with ``json_lines=True`` at import; command-line tools call
    it with ``False`` so their output reads as text in a terminal.
    """
    logger.remove()
    if json_lines:
        logger.add(_json_line, level=level, format="{message}")
    else:  # a command's output on stdout, its warnings and errors on stderr
        logger.add(
            sys.stdout,
            level=level,
            format="{message}",
            filter=lambda r: r["level"].no < logging.WARNING,
            colorize=False,
        )
        logger.add(sys.stderr, level="WARNING", format=_plain_format, colorize=False)
    logging.basicConfig(handlers=[_ToLoguru()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = [_ToLoguru()]
        logging.getLogger(name).propagate = False
    # The middleware logs one line per request with its ID; uvicorn's line has the
    # client address and no ID, so it is turned off.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
