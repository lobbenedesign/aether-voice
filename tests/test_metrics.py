"""Exercises `metrics_collected` emission against a real (local) websocket
server speaking the Moshi `/api/chat` wire format — no mocked
`RealtimeSession` internals. Verifies the plugin reports real, measured
timing (ttft to first decoded audio frame, generation duration, session
duration) the same way livekit-plugins-openai/gemini report
`RealtimeModelMetrics` after each response, rather than reporting nothing at
all (the state before this change) or inventing numbers.
"""

import asyncio
import time

import numpy as np
import pytest
import sphn
from aiohttp import web

from livekit.plugins.moshi.models import MoshiConnectOptions
from livekit.plugins.moshi.realtime_model import RealtimeModel


class _AudioMoshiServer:
    """Sends the handshake, waits a bit (so ttft is measurably > 0), then
    streams one real Opus-encoded audio frame, then idles until the client
    disconnects."""

    def __init__(self, *, delay_before_audio: float = 0.1) -> None:
        self.delay_before_audio = delay_before_audio
        self.runner: web.AppRunner | None = None
        self.port: int | None = None

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(b"\x00")  # handshake
        await asyncio.sleep(self.delay_before_audio)

        writer = sphn.OpusStreamWriter(24000)
        silence = np.zeros(1920, dtype=np.float32)  # 80ms @ 24kHz
        opus_bytes = writer.append_pcm(silence)
        if len(opus_bytes) > 0:
            await ws.send_bytes(b"\x01" + opus_bytes)

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
async def test_metrics_collected_reports_real_ttft_and_session_duration():
    server = _AudioMoshiServer(delay_before_audio=0.15)
    url = await server.start()
    try:
        model = RealtimeModel(connect_options=MoshiConnectOptions(url=url))
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

        start = time.monotonic()
        await asyncio.wait_for(got_audio.wait(), timeout=5.0)
        wall_elapsed = time.monotonic() - start

        await session.aclose()
        await model.aclose()

        # aclose() cancels the recv loop, which ends the generation and
        # triggers the metrics report for the generation that was open.
        generation_metrics = [m for m in metrics_events if m.request_id]
        assert generation_metrics, "expected a RealtimeModelMetrics event for the generation"
        m = generation_metrics[-1]

        assert m.type == "realtime_model_metrics"
        assert m.metadata.model_name == "moshi"
        assert m.metadata.model_provider == "kyutai"
        # ttft should reflect the real server delay (0.15s), not be -1 or 0,
        # and should be bounded by how long the whole exchange actually took.
        assert 0.0 < m.ttft <= wall_elapsed + 0.5
        assert m.duration >= m.ttft
        assert m.session_duration >= 0.0
        # Moshi has no token concept: honestly zero, not invented.
        assert m.input_tokens == 0
        assert m.output_tokens == 0
        assert m.total_tokens == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_metrics_collected_reports_no_audio_as_ttft_minus_one():
    """A generation that closes without ever receiving audio (e.g. an
    immediate disconnect) should report ttft=-1, matching the documented
    meaning of RealtimeModelMetrics.ttft ("-1 if no audio token was sent"),
    not a fabricated 0.0.
    """

    class _SilentServer(_AudioMoshiServer):
        async def _handler(self, request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_bytes(b"\x00")
            await asyncio.sleep(0.05)
            await ws.close()
            return ws

    server = _SilentServer()
    url = await server.start()
    try:
        model = RealtimeModel(
            connect_options=MoshiConnectOptions(url=url, max_reconnect_attempts=0)
        )
        session = model.session()

        metrics_events = []
        done = asyncio.Event()

        def _on_metrics(ev):
            metrics_events.append(ev)
            if ev.request_id:  # ignore the connection-acquired metric (request_id="")
                done.set()

        session.on("metrics_collected", _on_metrics)

        await asyncio.wait_for(done.wait(), timeout=5.0)

        await session.aclose()
        await model.aclose()

        generation_metrics = [m for m in metrics_events if m.request_id]
        assert generation_metrics
        assert generation_metrics[-1].ttft == -1.0
    finally:
        await server.stop()
