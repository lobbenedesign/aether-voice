from __future__ import annotations

import asyncio
import time
from typing import Literal

import aiohttp
import numpy as np

from livekit import rtc
from livekit.agents import NOT_GIVEN, NotGivenOr, llm, utils

from .log import logger
from .models import MOSHI_CHANNELS, MOSHI_FRAME_SIZE, MOSHI_SAMPLE_RATE, MoshiConnectOptions

try:
    import sphn
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "the `sphn` package is required by livekit-plugins-moshi for streaming "
        "Opus encode/decode (same dependency kyutai-labs/moshi's own client uses). "
        "install it with `pip install sphn`."
    ) from e


async def prewarm_async(
    connect_options: MoshiConnectOptions | None = None,
    *,
    silence_seconds: float = 1.5,
    timeout: float = 30.0,
) -> float:
    """Connects to a Moshi server and streams silence until the first response
    audio frame arrives, then disconnects. Returns the measured elapsed time
    in seconds.

    Exists because MLX backends (moshi_mlx.local_web) lazily compile their
    computation graph on first inference: measured on an Apple M1 with the
    moshiko-mlx-q4 checkpoint, that cost was 2-4s on every fresh server
    process (see README — it recurs on each restart, it's not one-time-ever),
    vs. 20-45ms once warm. Calling this once per worker process, before a
    real user connects, moves that 2-4s out of someone's first turn.

    Intended for `WorkerOptions(prewarm_fnc=...)` via the `prewarm()` wrapper
    below — call this coroutine directly only if you're already in an event
    loop and want to await it yourself (e.g. during your own app startup).
    """
    model = RealtimeModel(connect_options=connect_options)
    session = model.session()
    start = time.monotonic()
    got_audio = asyncio.Event()

    def _on_generation_created(ev: llm.GenerationCreatedEvent) -> None:
        async def _wait_for_first_frame() -> None:
            msg_gen = await ev.message_stream.__anext__()
            async for _ in msg_gen.audio_stream:
                got_audio.set()
                return

        asyncio.ensure_future(_wait_for_first_frame())

    session.on("generation_created", _on_generation_created)

    try:
        frame_samples = MOSHI_FRAME_SIZE
        silent_frame = rtc.AudioFrame(
            data=bytes(frame_samples * 2),  # int16 silence
            sample_rate=MOSHI_SAMPLE_RATE,
            num_channels=MOSHI_CHANNELS,
            samples_per_channel=frame_samples,
        )
        n_frames = int(silence_seconds * MOSHI_SAMPLE_RATE / frame_samples)
        send_task = asyncio.ensure_future(_stream_silence(session, silent_frame, n_frames))
        await asyncio.wait_for(got_audio.wait(), timeout=timeout)
        send_task.cancel()
    finally:
        elapsed = time.monotonic() - start
        await session.aclose()
        await model.aclose()
    return elapsed


async def _stream_silence(session: "RealtimeSession", frame: rtc.AudioFrame, n_frames: int) -> None:
    for _ in range(n_frames):
        session.push_audio(frame)
        await asyncio.sleep(MOSHI_FRAME_SIZE / MOSHI_SAMPLE_RATE)


def prewarm(proc: object = None, *, connect_options: MoshiConnectOptions | None = None) -> None:
    """Sync entry point matching `livekit.agents.WorkerOptions(prewarm_fnc=...)`
    (called once per worker process, before jobs are accepted). Accepts and
    ignores a positional `JobProcess`-like argument so its signature matches
    that hook; pass `connect_options` as a keyword if you're not using the
    default `ws://localhost:8998/api/chat`.

        from livekit.plugins import moshi
        WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=moshi.prewarm)
    """
    elapsed = asyncio.run(prewarm_async(connect_options))
    logger.info(f"moshi prewarm: first response audio frame after {elapsed*1000:.1f}ms")


class RealtimeModel(llm.RealtimeModel):
    """LiveKit RealtimeModel backed by a Moshi-protocol server.

    Wraps a running instance of kyutai-labs/moshi (PyTorch, MLX, or Rust backend —
    see :class:`MoshiConnectOptions`) as a LiveKit Agents realtime speech-to-speech
    model.

    Read this before wiring it into an AgentSession — Moshi's wire protocol is
    much thinner than OpenAI's or Gemini's realtime APIs, and several
    :class:`~livekit.agents.llm.RealtimeCapabilities` are honestly ``False``
    because the underlying protocol has no mechanism for them:

    - No tool / function calling. Moshi is a raw speech-to-speech dialogue model
      with no text-conditioning channel, so ``mutable_tools``,
      ``auto_tool_reply_generation`` and ``manual_function_calls`` are all False.
    - No instructions / system prompt. There is no message type to send one, so
      ``mutable_instructions`` is False. The model's persona is baked into the
      checkpoint you load.
    - No mid-session chat context injection (``mutable_chat_context`` is False).
    - No server-side turn-detection events. The wire protocol never sends a
      "user started/stopped speaking" signal — Moshi's full-duplex design means
      it is always listening and always (potentially) speaking, frame by frame.
      ``turn_detection`` is False; if your AgentSession needs discrete turn
      boundaries (for transcript UI, interruption metrics, etc.), run a VAD
      (e.g. ``livekit-plugins-silero``) in front of this model rather than
      relying on it to tell you.
    - No user-audio transcription. The one text stream the server sends back is
      Moshi's *own* inner monologue, timestamp-aligned to its own speech — not
      an ASR transcript of what the user said. ``user_transcription`` is False.
    - No response cancellation. Because the model runs continuously, there is no
      wire message to tell it "stop, discard that". ``interrupt()`` on this
      session stops relaying already-generated audio into the room; it does not
      (and structurally cannot, over this protocol) stop the model from having
      generated it.

    What *is* real: genuine full-duplex, single-model speech-to-speech audio,
    with the low latency that architecture implies (Kyutai's own published
    figure is a ~160-200ms theoretical/practical range for the reference
    deployment — not independently re-measured here; see the README for what
    was and wasn't verified before shipping this).
    """

    def __init__(self, *, connect_options: MoshiConnectOptions | None = None) -> None:
        super().__init__(
            capabilities=llm.RealtimeCapabilities(
                message_truncation=False,
                turn_detection=False,
                user_transcription=False,
                auto_tool_reply_generation=False,
                audio_output=True,
                manual_function_calls=False,
                can_disable_turn_detection=False,
                mutable_chat_context=False,
                mutable_instructions=False,
                mutable_tools=False,
                per_response_tool_choice=False,
                supports_say=False,
            )
        )
        self._connect_options = connect_options or MoshiConnectOptions()

    @property
    def model(self) -> str:
        return "moshi"

    @property
    def provider(self) -> str:
        return "kyutai"

    def session(self, *, turn_detection_disabled: bool = False) -> "RealtimeSession":
        # turn_detection_disabled is a no-op: the wire protocol has no server-side
        # turn detection to disable in the first place (capabilities.turn_detection
        # is already False).
        return RealtimeSession(self)

    async def aclose(self) -> None:
        pass


class RealtimeSession(llm.RealtimeSession):
    def __init__(self, realtime_model: RealtimeModel) -> None:
        super().__init__(realtime_model)
        self._opts = realtime_model._connect_options
        self._chat_ctx = llm.ChatContext.empty()
        self._tools = llm.ToolContext.empty()

        self._http_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._main_atask: asyncio.Task | None = None

        self._opus_writer = sphn.OpusStreamWriter(MOSHI_SAMPLE_RATE)
        self._opus_reader = sphn.OpusStreamReader(MOSHI_SAMPLE_RATE)

        self._input_resampler: rtc.AudioResampler | None = None
        self._input_bstream = utils.audio.AudioByteStream(
            MOSHI_SAMPLE_RATE, MOSHI_CHANNELS, samples_per_channel=MOSHI_FRAME_SIZE
        )

        # Moshi has exactly one perpetual "generation" for the life of a
        # connection: there is no server-side concept of discrete turns.
        self._generation: _Generation | None = None
        self._closed = False

        self._main_atask = asyncio.create_task(self._main_task())

    # -- required overrides ------------------------------------------------

    @property
    def chat_ctx(self) -> llm.ChatContext:
        return self._chat_ctx

    @property
    def tools(self) -> llm.ToolContext:
        return self._tools

    async def update_instructions(self, instructions: str) -> None:
        raise llm.RealtimeError(
            "Moshi has no instructions/system-prompt channel in its wire protocol; "
            "the persona is fixed by the loaded checkpoint. update_instructions() "
            "is not supported by this plugin.",
            code="unsupported",
        )

    async def update_chat_ctx(self, chat_ctx: llm.ChatContext) -> None:
        raise llm.RealtimeError(
            "Moshi has no mechanism to inject chat history mid-session. "
            "update_chat_ctx() is not supported by this plugin.",
            code="unsupported",
        )

    async def update_tools(self, tools: list[llm.Tool]) -> None:
        if tools:
            raise llm.RealtimeError(
                "Moshi does not support tool/function calling. update_tools() "
                "with a non-empty tool list is not supported by this plugin.",
                code="unsupported",
            )

    def update_options(
        self, *, tool_choice: NotGivenOr[llm.ToolChoice | None] = NOT_GIVEN
    ) -> None:
        if utils.is_given(tool_choice) and tool_choice is not None:
            logger.warning("tool_choice is ignored: Moshi does not support tool calling")

    def push_audio(self, frame: rtc.AudioFrame) -> None:
        if self._ws is None or self._closed:
            return
        for f in self._resample(frame):
            for chunk in self._input_bstream.write(f.data.tobytes()):
                pcm_i16 = np.frombuffer(chunk.data, dtype=np.int16)
                pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
                opus_bytes = self._opus_writer.append_pcm(pcm_f32)
                if len(opus_bytes) > 0:
                    self._send_bytes(b"\x01" + opus_bytes)

    def push_video(self, frame: rtc.VideoFrame) -> None:
        logger.warning("Moshi is audio-only; push_video() is a no-op")

    def generate_reply(
        self,
        *,
        instructions: NotGivenOr[str] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        tools: NotGivenOr[list[llm.Tool]] = NOT_GIVEN,
    ) -> asyncio.Future[llm.GenerationCreatedEvent]:
        if utils.is_given(instructions):
            logger.warning("generate_reply(instructions=...) is ignored: see update_instructions()")

        fut: asyncio.Future[llm.GenerationCreatedEvent] = asyncio.Future()
        if self._generation is not None and not self._generation.done:
            fut.set_result(self._generation.as_event(user_initiated=True))
        else:
            fut.set_exception(
                llm.RealtimeError(
                    "Moshi has no request/response cycle to trigger: it is always "
                    "listening and generating for the lifetime of the connection. "
                    "generate_reply() only resolves once, against the session's "
                    "single ongoing generation; call it after the session connects.",
                    code="unsupported",
                )
            )
        return fut

    def commit_audio(self) -> None:
        logger.warning(
            "commit_audio() is a no-op: Moshi streams audio continuously frame by "
            "frame, it has no input buffer to commit"
        )

    def clear_audio(self) -> None:
        logger.warning(
            "clear_audio() is a no-op: Moshi streams audio continuously frame by "
            "frame, it has no input buffer to clear"
        )

    def interrupt(self) -> None:
        # There is no cancel/truncate message in the wire protocol. Best-effort
        # local interruption: stop relaying already-generated audio into the
        # room. The model itself keeps generating server-side — this plugin
        # cannot stop that over this protocol, and does not claim to.
        if self._generation is not None:
            self._generation.local_mute = True
        logger.debug(
            "interrupt(): muted local playout of Moshi's output; the model "
            "itself is not stopped (no cancel message exists in its protocol)"
        )

    def truncate(
        self,
        *,
        message_id: str,
        modalities: list[Literal["text", "audio"]],
        audio_end_ms: int,
        audio_transcript: NotGivenOr[str] = NOT_GIVEN,
    ) -> None:
        logger.warning("truncate() is a no-op: Moshi's protocol has no server-side truncate message")

    async def aclose(self) -> None:
        self._closed = True
        if self._main_atask is not None:
            self._main_atask.cancel()
            try:
                await self._main_atask
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            await self._ws.close()
        if self._http_session is not None:
            await self._http_session.close()

    # -- connection + recv loop ---------------------------------------------

    def _resample(self, frame: rtc.AudioFrame):
        if self._input_resampler is not None and frame.sample_rate != self._input_resampler._input_rate:
            self._input_resampler = None

        if self._input_resampler is None and (
            frame.sample_rate != MOSHI_SAMPLE_RATE or frame.num_channels != MOSHI_CHANNELS
        ):
            self._input_resampler = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=MOSHI_SAMPLE_RATE,
                num_channels=MOSHI_CHANNELS,
            )

        if self._input_resampler is not None:
            yield from self._input_resampler.push(frame)
        else:
            yield frame

    def _send_bytes(self, data: bytes) -> None:
        if self._ws is None:
            return
        asyncio.ensure_future(self._safe_send(data))

    async def _safe_send(self, data: bytes) -> None:
        try:
            assert self._ws is not None
            await self._ws.send_bytes(data)
        except Exception as e:
            self._emit_error(e, recoverable=False)

    def _start_generation(self) -> None:
        self._generation = _Generation(self)
        self.emit("generation_created", self._generation.as_event(user_initiated=False))

    def _emit_error(self, error: Exception, recoverable: bool) -> None:
        self.emit(
            "error",
            llm.RealtimeModelError(
                timestamp=time.time(),
                label=self._realtime_model.label,
                error=error,
                recoverable=recoverable,
            ),
        )

    async def _main_task(self) -> None:
        """Connect, run the recv loop, and — unlike a one-shot connection —
        reconnect with exponential backoff if the websocket drops
        unexpectedly (server restart, network blip, reverse-proxy idle
        timeout), instead of ending the session on the first hiccup.

        Moshi's protocol has no session-resume mechanism (see models.py):
        each reconnect is a brand-new websocket, a brand-new Opus
        encoder/decoder pair (Opus is a stateful codec — reusing one across
        a fresh connection produces garbage), and, semantically, a new
        "generation" — Moshi has no server-side concept of resuming the old
        one. `chat_ctx`/audio already in flight before the drop is lost;
        this recovers the *connection*, not conversation continuity. That
        matches what the wire protocol can actually offer, same honesty
        policy as the rest of this plugin.
        """
        attempt = 0
        first_connect = True
        while not self._closed:
            acquire_start = time.time()
            try:
                self._http_session = self._http_session or aiohttp.ClientSession()
                self._ws = await self._http_session.ws_connect(
                    self._opts.url, timeout=self._opts.connect_timeout
                )
            except Exception as e:
                if attempt < self._opts.max_reconnect_attempts:
                    delay = self._reconnect_delay(attempt)
                    logger.warning(
                        f"could not connect to Moshi server at {self._opts.url} "
                        f"(attempt {attempt + 1}/{self._opts.max_reconnect_attempts}): {e}. "
                        f"retrying in {delay:.1f}s"
                    )
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
                self._emit_error(
                    ConnectionError(
                        f"could not connect to Moshi server at {self._opts.url}: {e}. "
                        "did you start one? see MoshiConnectOptions. "
                        f"gave up after {attempt} reconnect attempt(s)."
                    ),
                    recoverable=False,
                )
                return

            self._report_connection_acquired(time.time() - acquire_start)

            if first_connect:
                first_connect = False
            else:
                # A fresh websocket needs a fresh, un-primed Opus codec pair —
                # the old ones hold state from the dead connection's stream.
                self._opus_writer = sphn.OpusStreamWriter(MOSHI_SAMPLE_RATE)
                self._opus_reader = sphn.OpusStreamReader(MOSHI_SAMPLE_RATE)
                self.emit("session_reconnected", llm.RealtimeSessionReconnectedEvent())
                logger.info(f"Moshi session reconnected after {attempt} attempt(s)")

            self._start_generation()
            connected_at = time.monotonic()
            clean_close = await self._recv_loop()
            connection_lifetime = time.monotonic() - connected_at

            if self._generation is not None:
                self._generation.finish()

            if self._closed or clean_close:
                return

            if connection_lifetime >= self._opts.reconnect_stable_after:
                # It stayed up a while before dropping — treat the server/network
                # as healthy and don't let this drop count against the retry
                # budget, same idea OpenAI's realtime plugin backoff uses.
                attempt = 0

            if attempt >= self._opts.max_reconnect_attempts:
                self._emit_error(
                    ConnectionError(
                        f"Moshi websocket at {self._opts.url} dropped and "
                        f"reconnect gave up after {attempt} attempt(s)."
                    ),
                    recoverable=False,
                )
                return

            delay = self._reconnect_delay(attempt)
            logger.warning(
                f"Moshi websocket dropped unexpectedly after {connection_lifetime:.2f}s; "
                f"reconnecting in {delay:.1f}s "
                f"(attempt {attempt + 1}/{self._opts.max_reconnect_attempts})"
            )
            attempt += 1
            await asyncio.sleep(delay)

    def _reconnect_delay(self, attempt: int) -> float:
        return min(
            self._opts.reconnect_backoff_base * (2**attempt),
            self._opts.reconnect_backoff_max,
        )

    async def _recv_loop(self) -> bool:
        """Reads frames off `self._ws` until it closes or errors.

        Returns True for a clean, server-initiated close (nothing to
        reconnect for) and False for anything that looks like an
        unexpected drop worth retrying.
        """
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.CLOSED:
                    return True
                if message.type == aiohttp.WSMsgType.ERROR:
                    logger.warning(f"Moshi websocket error: {self._ws.exception()}")
                    return False
                if message.type != aiohttp.WSMsgType.BINARY:
                    continue

                data: bytes = message.data
                if len(data) == 0:
                    continue

                kind, payload = data[0], data[1:]
                if kind == 0:  # handshake, sent once on connect, no data
                    continue
                elif kind == 1:  # audio
                    self._handle_audio_payload(payload)
                elif kind == 2:  # Moshi's own text token, not a user transcript
                    self._handle_text_payload(payload)
                else:
                    logger.warning(f"unknown Moshi message kind {kind}")
            # async-for ended without an explicit CLOSED/ERROR message, e.g. the
            # TCP connection was reset out from under aiohttp.
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Moshi websocket recv loop raised: {e}")
            return False

    def _handle_audio_payload(self, payload: bytes) -> None:
        if self._generation is None or self._generation.local_mute:
            return
        pcm_f32 = self._opus_reader.append_bytes(payload)
        if pcm_f32 is None or len(pcm_f32) == 0:
            return
        pcm_i16 = np.clip(pcm_f32, -1.0, 1.0)
        pcm_i16 = (pcm_i16 * 32767.0).astype(np.int16)
        frame = rtc.AudioFrame(
            data=pcm_i16.tobytes(),
            sample_rate=MOSHI_SAMPLE_RATE,
            num_channels=MOSHI_CHANNELS,
            samples_per_channel=len(pcm_i16),
        )
        self._generation.push_audio(frame)

    def _handle_text_payload(self, payload: bytes) -> None:
        if self._generation is None:
            return
        self._generation.push_text(payload.decode("utf-8", errors="ignore"))


class _Generation:
    """The single, perpetual generation of a Moshi session.

    Unlike turn-based providers there is no natural point where a "response" is
    complete, so this stays open for the life of the connection and is only
    closed when the session itself closes.
    """

    def __init__(self, session: RealtimeSession) -> None:
        self._session = session
        self.response_id = utils.shortuuid("MOSHI_")
        self.done = False
        self.local_mute = False

        self._message_ch = utils.aio.Chan[llm.MessageGeneration]()
        self._function_ch = utils.aio.Chan[llm.FunctionCall]()
        self._text_ch = utils.aio.Chan[str]()
        self._audio_ch = utils.aio.Chan[rtc.AudioFrame]()

        modalities = asyncio.Future()
        modalities.set_result(["audio", "text"])

        self._message_ch.send_nowait(
            llm.MessageGeneration(
                message_id=self.response_id,
                text_stream=self._text_ch,
                audio_stream=self._audio_ch,
                modalities=modalities,
            )
        )
        # function_ch is intentionally left open-but-empty: Moshi never calls
        # tools, so nothing will ever be sent on it.

    def as_event(self, *, user_initiated: bool) -> llm.GenerationCreatedEvent:
        return llm.GenerationCreatedEvent(
            message_stream=self._message_ch,
            function_stream=self._function_ch,
            user_initiated=user_initiated,
            response_id=self.response_id,
        )

    def push_audio(self, frame: rtc.AudioFrame) -> None:
        if not self._audio_ch.closed:
            self._audio_ch.send_nowait(frame)

    def push_text(self, text: str) -> None:
        if not self._text_ch.closed:
            self._text_ch.send_nowait(text)

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        if not self._text_ch.closed:
            self._text_ch.close()
        if not self._audio_ch.closed:
            self._audio_ch.close()
        if not self._function_ch.closed:
            self._function_ch.close()
        if not self._message_ch.closed:
            self._message_ch.close()
