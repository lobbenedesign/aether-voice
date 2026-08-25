"""Real end-to-end verification of the moshi RealtimeModel plugin against a live
moshi_mlx.local_web server. No mocked numbers: every figure printed here is a
wall-clock measurement taken during this run.

Input is synthetic audio (silence + a low-level 200Hz tone), not a live human
mic — this validates the plumbing (push_audio -> Opus encode -> websocket ->
real model inference -> Opus decode -> AudioFrame) and measures genuine
time-to-first-response-audio from the loaded model. It does not test whether
Moshi understands speech content, since there is no speech in the input.
"""

import asyncio
import time

import numpy as np
from livekit import rtc
from livekit.plugins import moshi

SAMPLE_RATE = 48_000  # what a real LiveKit room would hand the plugin
DURATION_S = 6.0


async def main():
    model = moshi.RealtimeModel(
        connect_options=moshi.MoshiConnectOptions(url="ws://localhost:8998/api/chat")
    )
    session = model.session()

    first_audio_frame_time = None
    first_text_time = None
    audio_frames_received = 0
    text_chars_received = 0
    errors = []

    def on_error(ev):
        errors.append(ev)
        print(f"[error event] {ev.label}: {ev.error!r} recoverable={ev.recoverable}")

    session.on("error", on_error)

    gen_created = asyncio.Future()

    def on_generation_created(ev):
        if not gen_created.done():
            gen_created.set_result(ev)

    session.on("generation_created", on_generation_created)

    connect_start = time.monotonic()
    ev = await asyncio.wait_for(gen_created, timeout=15.0)
    connect_elapsed = time.monotonic() - connect_start
    print(f"generation_created received {connect_elapsed*1000:.1f}ms after session() call "
          "(includes websocket handshake + this script's own scheduling)")

    msg_gen = await ev.message_stream.__anext__()

    async def drain_output():
        nonlocal first_audio_frame_time, first_text_time, audio_frames_received, text_chars_received
        async def drain_audio():
            nonlocal first_audio_frame_time, audio_frames_received
            async for frame in msg_gen.audio_stream:
                if first_audio_frame_time is None:
                    first_audio_frame_time = time.monotonic()
                audio_frames_received += 1

        async def drain_text():
            nonlocal first_text_time, text_chars_received
            async for tok in msg_gen.text_stream:
                if first_text_time is None:
                    first_text_time = time.monotonic()
                text_chars_received += len(tok)

        await asyncio.gather(drain_audio(), drain_text())

    drain_task = asyncio.create_task(drain_output())

    # Push synthetic audio: 1s silence, then a quiet 200Hz tone, at the frame
    # size a real LiveKit room would use (10ms @ 48kHz = 480 samples/frame).
    push_start = time.monotonic()
    frame_samples = 480
    n_frames = int(DURATION_S * SAMPLE_RATE / frame_samples)
    t = 0
    for i in range(n_frames):
        if i < int(1.0 * SAMPLE_RATE / frame_samples):
            pcm = np.zeros(frame_samples, dtype=np.int16)
        else:
            tt = (np.arange(frame_samples) + t) / SAMPLE_RATE
            pcm = (np.sin(2 * np.pi * 200 * tt) * 3000).astype(np.int16)
            t += frame_samples
        frame = rtc.AudioFrame(
            data=pcm.tobytes(), sample_rate=SAMPLE_RATE, num_channels=1, samples_per_channel=frame_samples
        )
        session.push_audio(frame)
        await asyncio.sleep(frame_samples / SAMPLE_RATE)  # real-time pacing, like a real mic

    push_elapsed = time.monotonic() - push_start
    print(f"pushed {n_frames} audio frames ({push_elapsed:.2f}s of real-time-paced synthetic audio)")

    await asyncio.sleep(2.0)  # let any trailing response finish arriving
    drain_task.cancel()

    print()
    print("=== RESULTS (measured this run, not claimed) ===")
    if first_audio_frame_time is not None:
        print(f"time to first response AUDIO frame: {(first_audio_frame_time - push_start)*1000:.1f}ms "
              f"after synthetic audio started streaming")
    else:
        print("no audio frame received from the model during this run")
    if first_text_time is not None:
        print(f"time to first response TEXT token: {(first_text_time - push_start)*1000:.1f}ms")
    else:
        print("no text token received from the model during this run")
    print(f"total audio frames received: {audio_frames_received}")
    print(f"total text characters received: {text_chars_received}")
    print(f"errors during run: {len(errors)}")

    await session.aclose()
    await model.aclose()


if __name__ == "__main__":
    asyncio.run(main())
