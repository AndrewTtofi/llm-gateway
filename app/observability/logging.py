"""Structured JSON logs with a request id (ADR 0008). Never prompt or completion text."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

request_id: ContextVar[str] = ContextVar("request_id", default="-")
_VALID_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_HANDLER = "gateway-json"


def new_request_id(incoming: str | None) -> str:
    """Reuse a caller's x-request-id if it's sane (for tracing across services)."""
    if incoming and _VALID_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


def _add_request_id(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    rid = request_id.get()
    if rid != "-":
        event.setdefault("request_id", rid)
    return event


def configure(level: str = "INFO") -> None:
    """Route stdlib logging (ours, uvicorn's, libraries') through structlog as JSON."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    handler.set_name(_HANDLER)
    root = logging.getLogger()
    # Replace only our own handler (re-configure is idempotent); leave others alone —
    # e.g. pytest's capture handlers.
    root.handlers[:] = [h for h in root.handlers if h.get_name() != _HANDLER] + [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # Our own access line replaces uvicorn's (it has request id, key, tokens, cost).
    logging.getLogger("uvicorn.access").disabled = True


access = structlog.get_logger("gateway.access")
usage = structlog.get_logger("gateway.usage")
