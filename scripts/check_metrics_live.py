"""One-off manual verification script (not part of pytest): drives the real
RealtimeSession against a live Moshi server and prints the real
metrics_collected events it emits, so the numbers in the README/CHANGELOG
can be reproduced rather than taken on faith. Requires a real server at
ws://localhost:8998/api/chat (see README Setup).
"""

import asyncio
import time

from livekit import rtc
from livekit.plugins.moshi.models import MOSHI_CHANNELS, MOSHI_FRAME_SIZE, MOSHI_SAMPLE_RATE
from livekit.plugins.moshi.realtime_model import RealtimeModel


async def main() -> None:
    model = RealtimeModel()
    session = model.session()

    events = []
    session.on("metrics_collected", lambda ev: events.append(ev))

    frame = rtc.AudioFrame(
        data=bytes(MOSHI_FRAME_SIZE * 2),
        sample_rate=MOSHI_SAMPLE_RATE,
        num_channels=MOSHI_CHANNELS,
        samples_per_channel=MOSHI_FRAME_SIZE,
    )
    n_frames = int(3.0 * MOSHI_SAMPLE_RATE / MOSHI_FRAME_SIZE)
    start = time.monotonic()
    for _ in range(n_frames):
        session.push_audio(frame)
        await asyncio.sleep(MOSHI_FRAME_SIZE / MOSHI_SAMPLE_RATE)
    print(f"streamed {n_frames} frames of silence over {time.monotonic() - start:.2f}s")

    await session.aclose()
    await model.aclose()

    print(f"got {len(events)} metrics_collected event(s):")
    for ev in events:
        print(
            f"  request_id={ev.request_id!r} ttft={ev.ttft:.4f}s "
            f"duration={ev.duration:.4f}s session_duration={ev.session_duration:.4f}s "
            f"acquire_time={ev.acquire_time:.4f}s cancelled={ev.cancelled} "
            f"model={ev.metadata.model_name if ev.metadata else None}"
        )


if __name__ == "__main__":
    asyncio.run(main())
