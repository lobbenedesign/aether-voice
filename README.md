# livekit-plugins-moshi

A [LiveKit Agents](https://github.com/livekit/agents) `RealtimeModel` plugin that
wraps a running [kyutai-labs/moshi](https://github.com/kyutai-labs/moshi) server
— genuine full-duplex speech-to-speech, not a pipelined STT→LLM→TTS chain.

> **This replaces the previous "Aether-Voice" project at this path.** v1 of that
> project (commits `21b39c6`..`0680efe`) claimed a "140ms full-duplex neural
> voice engine" that beat Moshi, OpenAI Realtime, and ElevenLabs on every
> metric. It didn't run a model: text replies were keyword-matched strings,
> audio was macOS `say` or a 440Hz sine beep, and the "140ms" was the time to
> run that string match plus a subprocess call. See `CHANGELOG.md`. This
> rewrite exists specifically to not repeat that.

## What this actually is

```
LiveKit Room (48kHz Opus, WebRTC)
        │
        ▼
 RealtimeSession.push_audio()  ── resample to 24kHz mono, 80ms frames ──┐
        │                                                               │
        │                                              ┌────────────────▼───────────────┐
        │                                              │ moshi-protocol server            │
        │                                              │ ws://.../api/chat                │
        │                                              │                                   │
        │                                              │  moshi.server        (PyTorch,   │
        │                                              │  moshi_mlx.local_web (MLX, M-ser.)│
        │                                              │  rust server                      │
        │                                              └────────────────┬───────────────┘
        │                                                               │
        ▼                                                               │
 audio_ch / text_ch  ◄── decoded Opus audio + Moshi's own text tokens ──┘
        │
        ▼
LiveKit AgentSession (plays audio back into the room)
```

`livekit/plugins/moshi/realtime_model.py` implements `llm.RealtimeModel` /
`llm.RealtimeSession` from `livekit-agents` (verified against the interface in
`livekit/agents/llm/realtime.py`, current as of this writing) and speaks the
same binary websocket protocol Moshi's own reference client uses — verified by
reading `moshi/moshi/client.py`, `moshi/moshi/server.py`, and
`moshi_mlx/moshi_mlx/local_web.py` directly, not assumed. That protocol is
intentionally minimal:

| Byte 0 | Payload | Direction | Meaning |
|---|---|---|---|
| `0x01` | Opus-encoded audio | both | 24kHz mono PCM, Opus-coded, ~80ms frames |
| `0x02` | UTF-8 text | server → client | Moshi's own text token, time-aligned to *its own* speech |

There is no JSON control channel, no session-config message, no cancel
message. That shapes everything below.

## What this plugin honestly does NOT do

The temptation with a "competitor benchmark" project is to round every gap up
to a checkmark. This one rounds down instead — `RealtimeCapabilities` is set
to match what the wire protocol can actually carry, verified in
`tests/test_capabilities.py`:

| Capability | Value | Why |
|---|---|---|
| `audio_output` | **True** | The one thing the protocol is built for |
| `turn_detection` | False | No "user started/stopped speaking" message exists; layer a VAD (e.g. `livekit-plugins-silero`) in front if your `AgentSession` needs turn boundaries |
| `user_transcription` | False | The text stream is Moshi's own inner monologue, not an ASR transcript of the user |
| `mutable_instructions` | False | No system-prompt message type. Persona is fixed by the checkpoint you load |
| `mutable_chat_context` | False | No way to inject history mid-session |
| `mutable_tools` / `auto_tool_reply_generation` / `manual_function_calls` | False | Moshi is not an LLM with tool calling — it's a raw speech-to-speech dialogue model |
| `message_truncation` | False | No server-side cancel/truncate message |
| `supports_say` | False | No text-to-speech-only mode |

`generate_reply()`, `commit_audio()`, `clear_audio()`, `truncate()`,
`update_instructions()`, `update_chat_ctx()`, and `update_tools()` all either
no-op with a logged warning or raise `RealtimeError` with an explanation,
rather than silently pretending to work. `interrupt()` mutes local playout of
already-generated audio (the only thing this protocol lets a client do) — it
does **not** stop the model from generating server-side, because there is no
message to ask it to.

This is a materially thinner integration than the OpenAI Realtime or Gemini
Live plugins in the same `livekit-agents` repo, because Moshi's actual
protocol is thinner. Anyone telling you otherwise about a Moshi integration is
describing a project they haven't shipped yet.

## Reconnection

Websocket connections drop — server restarts, a reverse-proxy idle timeout,
a network blip. Real competitor realtime plugins handle this: LiveKit's own
`livekit-plugins-openai` reconnects the OpenAI Realtime websocket with
exponential backoff and emits `session_reconnected` (confirmed via
[PR #925](https://github.com/livekit/agents/pull/925) and
[issue #2341](https://github.com/livekit/agents/issues/2341) in
`livekit/agents`, plus [issue #3145](https://github.com/livekit/agents/issues/3145)
on capping retries so a permanently-dead server doesn't loop forever). The
first version of this plugin didn't do any of that — a dropped websocket
ended the session outright, with no way back short of the caller tearing
down and recreating the whole `RealtimeSession`.

`RealtimeSession` now reconnects automatically with exponential backoff
(`MoshiConnectOptions.reconnect_backoff_base` / `reconnect_backoff_max`,
default 0.5s → 10s cap) and gives up after `max_reconnect_attempts` (default
5) unexpected drops, at which point it emits a normal non-recoverable
`error` event same as before. A connection that stayed up at least
`reconnect_stable_after` seconds (default 5.0) before dropping resets the
attempt counter, so an otherwise-healthy server surviving an occasional blip
doesn't get penalized by attempts it made days ago. On a successful
reconnect, the session rebuilds its Opus encoder/decoder pair (a fresh
websocket means a fresh, un-primed Opus stream — reusing the old codec state
across a new connection produces garbage) and emits `session_reconnected`,
matching the event `RealtimeSession` already defines in `livekit-agents` for
exactly this.

**What this does not do**, in keeping with this plugin's existing honesty
policy: Moshi's protocol has no session-resume mechanism, so a reconnect is
a new connection and, semantically, a new generation — audio and `chat_ctx`
in flight at the moment of the drop are lost. This recovers *connectivity*,
not *conversation continuity*. Set `max_reconnect_attempts=0` to restore the
old fail-once behavior if that's what you want instead.

Verified with `tests/test_reconnect.py`, against a real local
`aiohttp.web` websocket server speaking the same wire format (handshake
byte, binary framing) — not a mocked `RealtimeSession`: one test drops the
first connection and asserts the session reconnects, emits
`session_reconnected`, and resumes receiving real messages on the second
connection; a second test makes the server drop every connection
immediately and asserts the plugin gives up after the configured attempt
budget rather than retrying forever, and that the resulting `error` event
is `recoverable=False`. Also regression-checked against the live
`moshi_mlx.local_web` server used for the numbers below
(`scripts/check_moshi_server.py`, unaffected by this change).

## Metrics

Real competitor realtime plugins in `livekit-agents` (`livekit-plugins-openai`,
`livekit-plugins-gemini`) emit `metrics_collected` events carrying a
`RealtimeModelMetrics` payload after each response, which is what powers
`AgentSession`'s built-in `UsageCollector`/logging hooks. This plugin
previously emitted only the connection-acquire metric the base
`llm.RealtimeSession` class reports for free (`acquire_time`, via
`_report_connection_acquired`) — nothing about the generation itself. It now
also reports one `RealtimeModelMetrics` event per generation, on
`session.on("metrics_collected", ...)`, when that generation ends (session
close, server-initiated drop, or reconnect):

- `ttft` — real measured time from generation start to the first decoded
  audio frame off the wire, or `-1.0` if none arrived (matches the documented
  meaning of the field). Not turn-taking latency — see the caveat in "What's
  verified vs. what isn't" above.
- `duration` — how long the generation was open.
- `session_duration` — how long the underlying websocket connection stayed up.
- `input_tokens` / `output_tokens` / `total_tokens` — honestly `0`. Moshi's
  wire protocol has no token concept and nothing is billed or reported in
  tokens; a fabricated number here would be worse than an honest zero.
- `cancelled` — whether `interrupt()` was called on this generation (local
  mute only, per this plugin's existing honesty policy — see above).

Verified against a real, live `moshi_mlx.local_web` server
(2026-08-25, same Apple M1, same `kyutai/moshiko-mlx-q4` checkpoint as the
numbers above), streaming synthetic silence for 3.0s and reading back the
actual emitted events (`scripts/check_metrics_live.py`, reproducible):

```
request_id=''                    ttft=-1.0000s duration=0.0000s session_duration=0.0000s acquire_time=0.0033s
request_id='MOSHI_030f7616fe05'  ttft=0.0021s  duration=3.0134s session_duration=3.0133s acquire_time=0.0000s
```

That `ttft=2.1ms` is consistent with the warm-server 20-44ms range measured
above (same warm process, lower because this was silence into an already
fully warmed-up model with no JIT cost at all in this particular run — expect
it to vary run to run same as the warm numbers above do). Also
unit-tested against a real local `aiohttp.web` websocket server in
`tests/test_metrics.py`: one test asserts a real non-trivial `ttft` is
reported when audio does arrive, the other asserts `ttft=-1.0` (not `0.0`)
when a generation closes having received none.

## Setup

Pick a backend and run its server — this repo does not bundle model weights
or start one for you:

```bash
# Apple Silicon (M1/M2/M3/M4), no discrete GPU required
pip install moshi_mlx
python -m moshi_mlx.local_web
# downloads the model checkpoint (several GB) from Hugging Face on first run

# CUDA GPU
pip install moshi
python -m moshi.server
```

Both default to `ws://localhost:8998/api/chat`. Then:

```bash
pip install -e .
python scripts/check_moshi_server.py   # real handshake check, prints round-trip time or the real error
python examples/minimal_agent.py dev
```

### Prewarming

The MLX backend lazily compiles its computation graph on first inference —
measured at **2-4 seconds on every fresh server process** (see below; this
recurs on each restart, it is not a one-time-per-machine cost). Call
`moshi.prewarm` once at worker startup, via `livekit-agents`' own
`prewarm_fnc` hook, so that cost lands before a real user connects, not
during their first turn:

```python
from livekit.plugins import moshi
cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=moshi.prewarm))
```

`examples/minimal_agent.py` already does this.

## What's verified vs. what isn't

Verified by reading the source:
- The wire protocol (byte-for-byte, from `client.py`/`server.py`/`local_web.py`)
- The `livekit-agents` `RealtimeModel`/`RealtimeSession` ABC this plugin implements

Verified by running it end-to-end on an Apple M1, 2026-08-25 — real
`moshi_mlx.local_web` server (`kyutai/moshiko-mlx-q4` checkpoint, 4-bit
quantized, ~5GB download), real websocket connection, this plugin's actual
`push_audio()`/`RealtimeSession` code path, no mocks:

- `scripts/check_moshi_server.py`: websocket handshake succeeded, **8.2ms**.
- Full plugin round-trip, driven by a script that streams synthetic audio
  (1s silence + a quiet 200Hz tone, paced in real time at 10ms/frame — a
  live human mic was not available in the environment this was run in) and
  times the first response frame the plugin emits:
  - **Every fresh server process pays a 2-4 second JIT/graph-compile cost
    on its first inference.** Measured across 4 independent clean restarts
    (process confirmed killed via `lsof`/`ps` before each one, including one
    after a full OS reboot that also cleared any OS-level shader cache):
    **4043.6ms, 1938.9ms, 2229.7ms**, and an earlier run at **3037ms**. This
    is a real, recurring per-process cost, not a one-time-per-machine cost —
    an earlier draft of this README claimed the latter after seeing 336ms
    and 9.8ms on what were meant to be fresh restarts; those turned out to
    be confounded by leftover MLX worker subprocesses (`multiprocessing`
    children of `moshi_mlx.local_web`) that a plain `kill` on the parent PID
    didn't reap, so those two "restarts" were quietly reusing an
    already-warm worker. Corrected here after re-testing with explicit
    `lsof`-verified-clean kills before each restart. This is exactly the
    kind of number `moshi.prewarm()` exists to move out of a user's first
    turn — see Prewarming above.
  - **Warm within a long-running process: 20–44ms** to first response audio
    frame across 4 further in-process runs (20.0, 21.7, 21.6, 43.8ms).
  - Text tokens (Moshi's own inner monologue) arrived inconsistently across
    runs — 0, 10, 21, or 46 characters over the ~7s window — which tracks
    with Moshi being a proactive, not purely reactive, speaker: it doesn't
    only "answer," it also speaks unprompted, so how much text shows up in
    a fixed window depends on what it was already saying.
  - This measures **streaming latency once the model is warm and running**,
    not conversational turn-taking latency (how fast it responds to the
    *content* of something a person said) — the input was a tone, not
    speech, so there's nothing for it to respond to contentwise. Kyutai's
    own **160–200ms** figure is specifically about turn-taking and remains
    their claim, not independently reproduced here; what was reproduced is
    that the pipe itself, once warm, moves audio in tens of milliseconds,
    not the ~700–1200ms of a cascaded STT→LLM→TTS setup.
  - One discovery this run surfaced: the live server sends an undocumented
    (in `client.py`) `0x00` handshake byte on connect — see `models.py` for
    where that's now handled instead of logged as an unknown message kind.

Not verified:
- Turn-taking latency against real speech content (no live mic in this
  environment — reproduce it yourself with `examples/minimal_agent.py`
  against a real LiveKit room and a microphone)
- Audio quality / MOS
- Behavior under real network jitter, or over a longer session than ~7s
- Anything about the PyTorch/CUDA backend (`moshi.server`) specifically —
  only the MLX backend was run

Re-run `scripts/check_moshi_server.py` and `examples/minimal_agent.py`
yourself before trusting any number here for a decision that matters —
including these.

## Why nobody had built this already

Checked before starting: LiveKit's own `livekit-plugins/` directory ships 60+
provider integrations (OpenAI, Google, Anthropic, ElevenLabs, Ultravox,
Cartesia, etc.) as of this writing, and none of them is Kyutai/Moshi. The
likely reason, now that the protocol has been read end-to-end: Moshi's wire
protocol is architecturally thinner than every other provider LiveKit
integrates against (no control channel, no tools, no instructions), so
wrapping it as a `RealtimeModel` means implementing an interface where most of
the advanced surface area is honestly unsupported — less attractive to build
than a provider where every capability flag can be `True`.

## License

MIT, consistent with the rest of the suite this project lives in.
