"""Structured JSON logging helpers for runtime and moderation observability."""

import json
import logging
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Serialize log records as compact JSON objects for Docker/stdout consumption."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record as JSON with stable top-level fields."""
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        event = getattr(record, "event", None)
        if event:
            payload["event"] = event

        context = getattr(record, "context", None)
        if isinstance(context, dict) and context:
            payload["context"] = context

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging to emit structured JSON records to stdout."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)


def emit_structured_log(
    event: str,
    *,
    level: int = logging.INFO,
    logger_name: str | None = None,
    message: str = "",
    **context: Any,
) -> None:
    """Emit a structured log event with arbitrary contextual fields."""
    logger = logging.getLogger(logger_name)
    logger.log(level, message or event, extra={"event": event, "context": context})
