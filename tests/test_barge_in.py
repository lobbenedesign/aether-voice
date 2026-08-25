"""Exercises real barge-in / interruption handling against a real (local)
websocket server that speaks the Moshi `/api/chat` wire format and streams
continuous audio, like the reference server does — no mocked
`RealtimeSession` internals.

Why this exists: unlike livekit-plugins-openai/gemini, Moshi has exactly one
perpetual generation per connection (see `RealtimeModel`'s docstring), so
`interrupt()` cannot just discard "the current response" and let a fresh one
take over the way turn-based providers do — there is no next response to
switch to. Before this change, `interrupt()` set a sticky mute flag that was
checked before *every* future audio frame for the rest of the connection: the
very first barge-in of a session would silence all of Moshi's output forever
after, which is not what any real "interruption" feature does. This verifies
the actual, fixed behavior instead: interrupt() (1) immediately flushes
audio frames already queued for local playout, (2) drops newly-arrived
frames for a short configurable window (to cover audio the server had
already synthesized before it could react), and (3) lets playout resume
automatically afterwards, matching the standard full-duplex barge-in pattern
described in the LiveKit Agents / Pipecat interruption model, adapted to a
protocol with no server-side cancel message.
"""

import asyncio
import time

import numpy as np
import pytest
import sphn
from aiohttp import web

from livekit.plugins.moshi.models import MoshiConnectOptions
from livekit.plugins.moshi.realtime_model import RealtimeModel


class _StreamingMoshiServer:
    """Sends the handshake, then one real Opus-encoded audio frame every
    `frame_interval` seconds, indefinitely, until the client disconnects —
    simulating Moshi's continuous full-duplex output."""

    def __init__(self, *, frame_interval: float = 0.05) -> None:
        self.frame_interval = frame_interval
        self.runner: web.AppRunner | None = None
        self.port: int | None = None

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(b"\x00")  # handshake

        writer = sphn.OpusStreamWriter(24000)
        silence = np.zeros(1920, dtype=np.float32)  # 80ms @ 24kHz
        try:
            while True:
                opus_bytes = writer.append_pcm(silence)
                if len(opus_bytes) > 0:
                    await ws.send_bytes(b"\x01" + opus_bytes)
                await asyncio.sleep(self.frame_interval)
        except (ConnectionResetError, ConnectionAbortedError, RuntimeError):
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
async def test_interrupt_flushes_queue_and_drops_frames_during_window_then_resumes():
    frame_interval = 0.05
    flush_window = 0.25

    server = _StreamingMoshiServer(frame_interval=frame_interval)
    url = await server.start()
    try:
        model = RealtimeModel(
            connect_options=MoshiConnectOptions(
                url=url, interrupt_flush_window=flush_window
            )
        )
        session = model.session()

        received_at: list[float] = []

        def _on_generation_created(ev):
            async def _drain():
                msg_gen = await ev.message_stream.__anext__()
                async for _ in msg_gen.audio_stream:
                    received_at.append(time.monotonic())

            asyncio.ensure_future(_drain())

        session.on("generation_created", _on_generation_created)

        # Let a few real frames flow before interrupting, so there's
        # something genuinely queued/in-flight to flush.
        await asyncio.sleep(frame_interval * 4)
        assert len(received_at) >= 1, "expected some audio before interrupting"

        session.interrupt()
        interrupt_time = time.monotonic()

        # Keep listening well past the flush window to see relaying resume.
        await asyncio.sleep(flush_window + frame_interval * 6)

        await session.aclose()
        await model.aclose()

        # Nothing should have been delivered during the mute window (allow a
        # small scheduling epsilon either side).
        epsilon = 0.02
        during_window = [
            t
            for t in received_at
            if interrupt_time - epsilon <= t <= interrupt_time + flush_window - epsilon
        ]
        assert during_window == [], (
            f"expected no audio delivered during the {flush_window}s interrupt "
            f"window, got {len(during_window)} frame(s)"
        )

        # Playout must resume on its own afterwards — no manual "resume" call
        # exists, because the model was never told to stop.
        after_window = [t for t in received_at if t > interrupt_time + flush_window]
        assert after_window, "expected audio to resume automatically after the flush window"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_interrupt_marks_generation_cancelled_in_metrics():
    server = _StreamingMoshiServer(frame_interval=0.05)
    url = await server.start()
    try:
        model = RealtimeModel(
            connect_options=MoshiConnectOptions(url=url, interrupt_flush_window=0.1)
        )
        session = model.session()

        metrics_events = []
        session.on("metrics_collected", lambda ev: metrics_events.append(ev))

        got_audio = asyncio.Event()

        def _on_generation_created(ev):
            async def _drain():
                msg_gen = await ev.message_stream.__anext__()
                async for _ in msg_gen.audio_stream:
                    got_audio.set()
                    return

            asyncio.ensure_future(_drain())

        session.on("generation_created", _on_generation_created)

        await asyncio.wait_for(got_audio.wait(), timeout=5.0)
        session.interrupt()

        await session.aclose()
        await model.aclose()

        generation_metrics = [m for m in metrics_events if m.request_id]
        assert generation_metrics
        assert generation_metrics[-1].cancelled is True
    finally:
        await server.stop()
