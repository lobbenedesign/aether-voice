"""Exercises the reconnect-with-backoff path in `RealtimeSession._main_task`
against a real (local) websocket server that speaks the Moshi `/api/chat`
wire format and deliberately drops the first connection — no mocked
`RealtimeSession` internals, no downloaded model weights. This is the
scenario the old implementation could not survive at all: one dropped
connection ended the session for good.
"""

import asyncio

import pytest
from aiohttp import web

from livekit.plugins.moshi.models import MoshiConnectOptions
from livekit.plugins.moshi.realtime_model import RealtimeModel


class _FlakyMoshiServer:
    """A tiny real websocket server: connection #1 sends the handshake byte
    then hangs up abruptly (simulating a server restart / network blip).
    Connection #2 sends the handshake, then a text token, and stays open.
    """

    def __init__(self) -> None:
        self.connection_count = 0
        self.runner: web.AppRunner | None = None
        self.port: int | None = None

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connection_count += 1
        await ws.send_bytes(b"\x00")  # handshake

        if self.connection_count == 1:
            await asyncio.sleep(0.05)
            await ws.close(code=1006, message=b"simulated drop")
            return ws

        # second (and later) connections: send a real text token, then idle
        # until the client disconnects.
        await ws.send_bytes(b"\x02" + "hello after reconnect".encode())
        try:
            async for _ in ws:
                pass
        except Exception:
            pass
        return ws

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/api/chat", self._handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}/api/chat"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


@pytest.mark.asyncio
async def test_session_reconnects_after_dropped_connection():
    server = _FlakyMoshiServer()
    url = await server.start()
    try:
        model = RealtimeModel(
            connect_options=MoshiConnectOptions(
                url=url,
                max_reconnect_attempts=3,
                reconnect_backoff_base=0.05,
                reconnect_backoff_max=0.2,
            )
        )
        session = model.session()

        reconnected = asyncio.Event()
        session.on("session_reconnected", lambda ev: reconnected.set())

        got_text = asyncio.Event()
        received: list[str] = []

        def _on_generation_created(ev):
            async def _drain():
                msg_gen = await ev.message_stream.__anext__()
                async for text in msg_gen.text_stream:
                    received.append(text)
                    got_text.set()
                    return

            asyncio.ensure_future(_drain())

        session.on("generation_created", _on_generation_created)

        await asyncio.wait_for(reconnected.wait(), timeout=5.0)
        await asyncio.wait_for(got_text.wait(), timeout=5.0)

        assert server.connection_count == 2
        assert received == ["hello after reconnect"]

        await session.aclose()
        await model.aclose()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_session_gives_up_after_max_reconnect_attempts():
    """A server that always drops immediately should exhaust the configured
    retry budget and surface a non-recoverable error, not retry forever."""

    class _AlwaysDropServer(_FlakyMoshiServer):
        async def _handler(self, request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.connection_count += 1
            await ws.send_bytes(b"\x00")
            await asyncio.sleep(0.02)
            await ws.close(code=1006, message=b"simulated drop")
            return ws

    server = _AlwaysDropServer()
    url = await server.start()
    try:
        model = RealtimeModel(
            connect_options=MoshiConnectOptions(
                url=url,
                max_reconnect_attempts=2,
                reconnect_backoff_base=0.02,
                reconnect_backoff_max=0.05,
            )
        )
        session = model.session()

        errors = []
        session.on("error", lambda ev: errors.append(ev))

        # 1 initial connection + 2 reconnect attempts = 3 total drops before
        # giving up.
        for _ in range(100):
            if errors:
                break
            await asyncio.sleep(0.05)

        assert errors, "expected a non-recoverable error after exhausting reconnect attempts"
        assert errors[0].recoverable is False
        assert server.connection_count == 3

        await session.aclose()
        await model.aclose()
    finally:
        await server.stop()
