from __future__ import annotations

from dataclasses import dataclass

# Wire format used by kyutai-labs/moshi's own client/server (moshi/moshi/client.py,
# moshi_mlx/moshi_mlx/local_web.py): a websocket at `<ws|wss>://<host>/api/chat`
# exchanging binary frames prefixed with a 1-byte kind:
#   0x00 <empty>       -> handshake, server -> client only, sent once right after
#                          the websocket opens. Confirmed by running a live
#                          moshi_mlx.local_web server and reading local_web.py's
#                          `await ws.send_bytes(b"\x00")` — it isn't mentioned in
#                          the older moshi/moshi/client.py reference client, which
#                          just logs "unknown message kind" and otherwise ignores
#                          it. Carries no data; safe to ignore.
#   0x01 <opus bytes>  -> audio, both directions
#   0x02 <utf-8 bytes> -> text token, server -> client only
#
# There is no JSON control channel: no session-start message, no cancel/truncate
# message, no way to pass a system prompt or tool schema. Verified against the
# reference client (client.py) and both server implementations (server.py for the
# PyTorch/CUDA backend, local_web.py for the MLX backend) in the kyutai-labs/moshi
# repository, commit range current as of 2026-08, and against a live
# moshi_mlx.local_web server (moshiko-mlx-q4 checkpoint) run end-to-end.
MOSHI_SAMPLE_RATE = 24_000
MOSHI_CHANNELS = 1
MOSHI_FRAME_SIZE = 1_920  # 80ms at 24kHz — matches the reference client's blocksize


@dataclass
class MoshiConnectOptions:
    """Where to reach a running Moshi-protocol server.

    This plugin is backend-agnostic: it only needs a websocket endpoint speaking
    the ``/api/chat`` protocol above. That includes:

    - ``python -m moshi.server`` (official PyTorch backend, needs a CUDA GPU)
    - ``python -m moshi_mlx.local_web`` (Apple Silicon, via MLX — the realistic
      path for a Mac with no discrete GPU)
    - a Rust server built from the ``rust/`` crate in the same repo

    None of these are bundled with this plugin. You run one yourself and point
    this at it.
    """

    url: str = "ws://localhost:8998/api/chat"
    """Full websocket URL to the Moshi server's chat endpoint."""

    connect_timeout: float = 10.0
