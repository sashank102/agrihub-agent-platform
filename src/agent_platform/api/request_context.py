"""Request identifiers shared by middleware, logs, and audit records."""

import re
import uuid
from contextvars import ContextVar

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """Return the request id for the current task, when one is bound."""
    return request_id_var.get()


def bounded_request_id(candidate: str | None) -> str:
    """Accept a caller-supplied id or generate a new bounded one."""
    if candidate and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex
