"""Live test: requires a running Moshi server at ws://localhost:8998/api/chat
(e.g. `python -m moshi_mlx.local_web`). Skipped automatically if none is
reachable, so the rest of the suite stays runnable with no server and no
model weights.
"""

import asyncio

import aiohttp
import pytest

from livekit.plugins import moshi

MOSHI_URL = "ws://localhost:8998/api/chat"


async def _server_reachable() -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(MOSHI_URL, timeout=2.0) as ws:
                await ws.close()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_prewarm_against_live_server():
    if not await _server_reachable():
        pytest.skip(f"no Moshi server reachable at {MOSHI_URL}")

    elapsed = await moshi.prewarm_async(timeout=30.0)
    assert elapsed > 0
    assert elapsed < 30.0
