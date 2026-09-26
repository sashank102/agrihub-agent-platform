"""JSON logs with correlation ids and no request bodies."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from agent_platform.api.request_context import (
    request_id_var,
    run_id_var,
    thread_id_var,
    user_id_var,
)

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record after secret redaction has already run."""
        event = getattr(record, "event", None)
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": "agent_platform",
            "logger": record.name,
            "event": event or "log",
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
            "thread_id": thread_id_var.get(),
            "run_id": run_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            {key: value for key, value in payload.items() if value is not None},
            default=str,
        )


def configure_logging() -> None:
    """Attach one JSON handler to the platform logger."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    platform_logger = logging.getLogger("agent_platform")
    platform_logger.setLevel(logging.INFO)
    platform_logger.addHandler(handler)
    platform_logger.propagate = False
    _CONFIGURED = True
