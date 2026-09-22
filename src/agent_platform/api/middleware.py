"""Request ids and response logging that omit credentials and bodies."""

import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agent_platform.api.request_context import bounded_request_id, request_id_var
from agent_platform.services.redaction import redact_text

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
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                request_id,
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
            )
            return response
        finally:
            request_id_var.reset(token)


def install_redaction() -> None:
    """Attach the secret filter once to the platform loggers."""
    platform_logger = logging.getLogger("agent_platform")
    if not any(isinstance(item, RedactionFilter) for item in platform_logger.filters):
        platform_logger.addFilter(RedactionFilter())
