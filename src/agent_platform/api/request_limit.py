"""ASGI request-body limit that rejects oversized payloads incrementally."""

import json
from collections.abc import Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Any]
Send = Callable[[Message], Any]


class RequestSizeLimitMiddleware:
    """Count request body chunks and stop once the configured limit is crossed.

    Declared ``Content-Length`` values that are missing, duplicated, or not
    integers are rejected before the application reads a body. Chunked
    requests are forwarded piece by piece. The middleware keeps only a running
    byte count, discards the chunk that crosses the limit, and does not read
    any further chunks. Response messages are forwarded unchanged so streaming
    responses are not buffered.
    """

    def __init__(self, app: Callable[..., Any], max_bytes: int) -> None:
        """Bind the downstream app and the maximum accepted body size."""
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Apply the limit to HTTP requests and pass other scopes through."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if isinstance(declared, str):
            await _send_json(send, 400, {"detail": declared})
            return
        if declared is not None and declared > self.max_bytes:
            await _send_json(send, 413, {"detail": "request body too large"})
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") != "http.request":
                return message
            body = message.get("body") or b""
            received += len(body)
            if received > self.max_bytes:
                exceeded = True
                return {"type": "http.disconnect"}
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not (exceeded and not response_started):
                raise
        if exceeded and not response_started:
            await _send_json(send, 413, {"detail": "request body too large"})


def _content_length(scope: Scope) -> int | str | None:
    """Return a declared length, ``None`` when absent, or an error detail."""
    values = [
        value
        for key, value in scope.get("headers", [])
        if key.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) > 1:
        return "invalid content-length"
    try:
        decoded = values[0].decode("ascii").strip()
        if decoded == "" or not decoded.isdecimal():
            return "invalid content-length"
        return int(decoded)
    except (UnicodeDecodeError, ValueError):
        return "invalid content-length"


async def _send_json(send: Send, status: int, payload: dict[str, str]) -> None:
    """Send one small JSON response without invoking the downstream app."""
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
