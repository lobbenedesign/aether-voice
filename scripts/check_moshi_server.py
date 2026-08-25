#!/usr/bin/env python3
"""Real connectivity check for a Moshi-protocol server. No mocked numbers, no
fake dashboard: this either connects to `/api/chat` and reports the actual
handshake result, round-trip time, and any I/O error verbatim, or it fails
and says so. If no server is running, it says exactly that instead of
inventing one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import aiohttp


async def check(url: str, timeout: float) -> int:
    print(f"connecting to {url} ...")
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, timeout=timeout) as ws:
                elapsed_ms = (time.monotonic() - start) * 1000
                print(f"connected in {elapsed_ms:.1f}ms (websocket handshake only, "
                      "not a measure of model inference latency)")
                await ws.close()
        return 0
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        print(f"FAILED after {elapsed_ms:.1f}ms: {type(e).__name__}: {e}", file=sys.stderr)
        print(
            "no Moshi server reachable at this URL. start one with either:\n"
            "  python -m moshi_mlx.local_web      # Apple Silicon, MLX backend\n"
            "  python -m moshi.server             # CUDA GPU, PyTorch backend\n",
            file=sys.stderr,
        )
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8998/api/chat")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    sys.exit(asyncio.run(check(args.url, args.timeout)))


if __name__ == "__main__":
    main()
