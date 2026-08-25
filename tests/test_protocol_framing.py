"""Unit tests for the /api/chat wire-framing constants and the single-generation
lifecycle, without needing a running Moshi server or downloaded model weights.
"""

import asyncio

import pytest

from livekit.plugins.moshi.models import MOSHI_CHANNELS, MOSHI_FRAME_SIZE, MOSHI_SAMPLE_RATE
from livekit.plugins.moshi.realtime_model import _Generation


def test_frame_constants_match_reference_client():
    # 1920 samples @ 24kHz = 80ms, matching moshi/moshi/client.py's Connection
    # (frame_size=1920, sample_rate=24000).
    assert MOSHI_SAMPLE_RATE == 24_000
    assert MOSHI_CHANNELS == 1
    assert MOSHI_FRAME_SIZE == 1_920
    assert MOSHI_FRAME_SIZE / MOSHI_SAMPLE_RATE == pytest.approx(0.08)


@pytest.mark.asyncio
async def test_generation_is_a_single_perpetual_stream():
    gen = _Generation(session=None)  # session is only used for logging elsewhere
    event = gen.as_event(user_initiated=False)

    msg_gen = await event.message_stream.__anext__()
    assert msg_gen.message_id == gen.response_id

    gen.push_text("bonjour")
    assert await msg_gen.text_stream.__anext__() == "bonjour"

    assert gen.done is False
    gen.finish()
    assert gen.done is True

    with pytest.raises(StopAsyncIteration):
        await msg_gen.text_stream.__anext__()


@pytest.mark.asyncio
async def test_finish_is_idempotent():
    gen = _Generation(session=None)
    gen.finish()
    gen.finish()  # must not raise on a second close
    assert gen.done is True
