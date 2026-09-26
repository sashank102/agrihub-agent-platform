"""Request ids and response logging that omit credentials and bodies."""

import logging
import re
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agent_platform.api.request_context import (
    bounded_request_id,
    request_id_var,
    run_id_var,
    thread_id_var,
    user_id_var,
)
from agent_platform.services.redaction import redact_text

_THREAD_ID = re.compile(
    r"/threads/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_RUN_ID = re.compile(
    r"/runs/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

logger = logging.getLogger("agent_platform.access")


class RedactionFilter(logging.Filter):
    """Strip recognizable secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the message and arguments, then keep the record."""
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_text(item) if isinstance(item, str) else item
                for item in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_text(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a bounded request id and log the completed status line."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Set the request id, call the app, and echo the id on the response."""
        request_id = bounded_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        thread_match = _THREAD_ID.search(request.url.path)
        run_match = _RUN_ID.search(request.url.path)
        thread_token = thread_id_var.set(thread_match.group(1) if thread_match else None)
        run_token = run_id_var.set(run_match.group(1) if run_match else None)
        user_token = user_id_var.set(None)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                request_id,
                extra={"event": "request.failed"},
            )
            raise
        else:
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request completed method=%s path=%s status=%s request_id=%s",
                request.method,
                request.url.path,
                response.status_code,
                request_id,
                extra={"event": "request.completed"},
            )
            return response
        finally:
            request_id_var.reset(token)
            thread_id_var.reset(thread_token)
            run_id_var.reset(run_token)
            user_id_var.reset(user_token)


def install_redaction(platform_prefix: str = "aghub") -> None:
    """Attach the secret filter once and match the configured key prefix."""
    from agent_platform.services.redaction import configure_redaction

    configure_redaction(platform_prefix)
    platform_logger = logging.getLogger("agent_platform")
    if not any(isinstance(item, RedactionFilter) for item in platform_logger.filters):
        platform_logger.addFilter(RedactionFilter())
