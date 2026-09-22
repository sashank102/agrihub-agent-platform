"""Request-size limiting that does not buffer an oversized body."""

import asyncio

import pytest

from agent_platform.api.request_limit import RequestSizeLimitMiddleware
from agent_platform.core.settings import Settings
from agent_platform.main import create_app, run


def _scope(headers: list[tuple[bytes, bytes]], path: str = "/threads") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


def _recording_app(bodies: list[bytes], response_chunks: list[bytes] | None = None):
    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            body = message.get("body") or b""
            if body:
                bodies.append(body)
            if not message.get("more_body", False):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        chunks = response_chunks or [b"ok"]
        for index, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
            )

    return app


def test_chunked_body_is_rejected_without_reading_the_rest():
    async def scenario() -> None:
        seen: list[bytes] = []
        chunks = [b"a" * 60, b"b" * 60, b"c" * 60, b"d" * 60]
        read_count = 0

        async def receive():
            nonlocal read_count
            if read_count >= len(chunks):
                return {"type": "http.request", "body": b"", "more_body": False}
            body = chunks[read_count]
            read_count += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": read_count < len(chunks),
            }

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        app = RequestSizeLimitMiddleware(_recording_app(seen), max_bytes=100)
        await app(_scope([(b"content-type", b"application/json")]), receive, send)
        assert messages[0]["status"] == 413
        assert read_count == 2
        assert b"c" * 60 not in seen
        assert b"d" * 60 not in seen
        assert sum(len(part) for part in seen) <= 100

    asyncio.run(scenario())


def test_missing_content_length_accepts_a_small_chunked_body():
    async def scenario() -> None:
        seen: list[bytes] = []

        async def receive():
            if not getattr(receive, "sent", False):
                receive.sent = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            return {"type": "http.disconnect"}

        receive.sent = False
        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        app = RequestSizeLimitMiddleware(_recording_app(seen), max_bytes=100)
        await app(_scope([]), receive, send)
        assert messages[0]["status"] == 200
        assert seen == [b"{}"]

    asyncio.run(scenario())


def test_invalid_content_length_is_rejected_before_the_body_is_read():
    async def scenario() -> None:
        async def receive():
            raise AssertionError("body should not be read")

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        app = RequestSizeLimitMiddleware(_recording_app([]), max_bytes=100)
        await app(
            _scope([(b"content-length", b"nope")]),
            receive,
            send,
        )
        assert messages[0]["status"] == 400

    asyncio.run(scenario())


def test_declared_content_length_above_the_limit_does_not_read_the_body():
    async def scenario() -> None:
        async def receive():
            raise AssertionError("body should not be read")

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        app = create_app(
            settings=Settings(
                ENVIRONMENT="test",
                API_MAX_REQUEST_BODY_BYTES=32,
                _env_file=None,
            )
        )
        await app(
            _scope([(b"content-length", b"1000"), (b"content-type", b"application/json")]),
            receive,
            send,
        )
        assert messages[0]["status"] == 413

    asyncio.run(scenario())


def test_streaming_response_chunks_are_forwarded_as_they_are_produced():
    async def scenario() -> None:
        release = asyncio.Event()
        first_seen = asyncio.Event()

        async def downstream(scope, receive, send):
            message = await receive()
            assert message["type"] == "http.request"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b"one", "more_body": True}
            )
            await release.wait()
            await send(
                {"type": "http.response.body", "body": b"two", "more_body": False}
            )

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)
            if message.get("body") == b"one":
                first_seen.set()

        app = RequestSizeLimitMiddleware(downstream, max_bytes=100)
        task = asyncio.create_task(app(_scope([(b"content-length", b"2")]), receive, send))
        await first_seen.wait()
        assert [message.get("body") for message in messages if message["type"] == "http.response.body"] == [b"one"]
        release.set()
        await task
        assert [message.get("body") for message in messages if message["type"] == "http.response.body"] == [b"one", b"two"]

    asyncio.run(scenario())


def test_business_routes_return_503_before_lifespan(monkeypatch):
    async def scenario() -> None:
        app = create_app(
            settings=Settings(ENVIRONMENT="test", _env_file=None)
        )
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            assert (await client.get("/health")).status_code == 200
            thread_id = "00000000-0000-4000-8000-000000000099"
            run_id = "00000000-0000-4000-8000-000000000098"
            responses = [
                await client.post("/threads", json={"metadata": {}}),
                await client.post("/threads/search", json={}),
                await client.get(f"/threads/{thread_id}"),
                await client.get(f"/threads/{thread_id}/state"),
                await client.post(f"/threads/{thread_id}/history", json={}),
                await client.post(
                    f"/threads/{thread_id}/runs/stream",
                    json={"assistant_id": "agrihub"},
                ),
                await client.get(f"/threads/{thread_id}/runs/{run_id}/stream"),
                await client.post(f"/threads/{thread_id}/runs/{run_id}/cancel"),
            ]
            assert [response.status_code for response in responses] == [503] * len(responses)

    asyncio.run(scenario())


def test_run_rejects_more_than_one_worker(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="one Uvicorn worker"):
        run()
