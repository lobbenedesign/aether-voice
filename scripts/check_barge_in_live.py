"""One-off manual verification script (not part of pytest): drives the real
RealtimeSession against a live Moshi server, streams real audio in, waits for
real response audio out, then calls interrupt() mid-stream and prints the
actual timestamps of every audio frame received before/after — so the
flush-then-resume barge-in behavior can be seen against genuine model output
rather than the synthetic server used in tests/test_barge_in.py. Requires a
real server at ws://localhost:8998/api/chat (see README Setup).
"""

import asyncio
import time

from livekit import rtc
from livekit.plugins.moshi.models import (
    MOSHI_CHANNELS,
    MOSHI_FRAME_SIZE,
    MOSHI_SAMPLE_RATE,
    MoshiConnectOptions,
)
from livekit.plugins.moshi.realtime_model import RealtimeModel


async def main() -> None:
    flush_window = 0.4
    model = RealtimeModel(connect_options=MoshiConnectOptions(interrupt_flush_window=flush_window))
    session = model.session()

    received_at: list[float] = []

    def _on_generation_created(ev):
        async def _drain():
            msg_gen = await ev.message_stream.__anext__()
            async for _ in msg_gen.audio_stream:
                received_at.append(time.monotonic())

        asyncio.ensure_future(_drain())

    session.on("generation_created", _on_generation_created)

    frame = rtc.AudioFrame(
        data=bytes(MOSHI_FRAME_SIZE * 2),
        sample_rate=MOSHI_SAMPLE_RATE,
        num_channels=MOSHI_CHANNELS,
        samples_per_channel=MOSHI_FRAME_SIZE,
    )
    n_frames_pre = int(3.0 * MOSHI_SAMPLE_RATE / MOSHI_FRAME_SIZE)
    start = time.monotonic()
    for _ in range(n_frames_pre):
        session.push_audio(frame)
        await asyncio.sleep(MOSHI_FRAME_SIZE / MOSHI_SAMPLE_RATE)
    print(f"streamed {n_frames_pre} frames of silence, got {len(received_at)} response frame(s) so far")

    session.interrupt()
    interrupt_t = time.monotonic()
    print(f"called interrupt() at t={interrupt_t - start:.3f}s (flush_window={flush_window}s)")

    n_frames_post = int(2.0 * MOSHI_SAMPLE_RATE / MOSHI_FRAME_SIZE)
    for _ in range(n_frames_post):
        session.push_audio(frame)
        await asyncio.sleep(MOSHI_FRAME_SIZE / MOSHI_SAMPLE_RATE)

    await session.aclose()
    await model.aclose()

    during = [t for t in received_at if interrupt_t <= t <= interrupt_t + flush_window]
    after = [t for t in received_at if t > interrupt_t + flush_window]
    print(f"total response frames received: {len(received_at)}")
    print(f"frames delivered during the {flush_window}s mute window (expect 0): {len(during)}")
    print(f"frames delivered after the window (expect >0 if the model kept speaking): {len(after)}")
    if after:
        print(f"first frame after window resumed at t={after[0] - start:.3f}s (interrupt was at {interrupt_t - start:.3f}s)")


if __name__ == "__main__":
    asyncio.run(main())
