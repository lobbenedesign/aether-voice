# Changelog

## 0.3.0 — per-generation `metrics_collected` (`RealtimeModelMetrics`)

**Gap and source.** `livekit-agents`' own realtime plugins
(`livekit-plugins-openai`, `livekit-plugins-gemini`) emit a
`metrics_collected` event carrying `llm.RealtimeModelMetrics` after each
response — confirmed via WebSearch against LiveKit's own docs
(`docs.livekit.io/reference/recipes/metrics_realtime/`,
`docs.livekit.io/reference/python/livekit/agents/metrics/`), which state
this is what feeds `AgentSession`'s `UsageCollector`/logging hooks. This
plugin only ever emitted the free connection-acquire metric the base
`llm.RealtimeSession` class already reports (`_report_connection_acquired`,
`acquire_time` only) — nothing about the generation itself, so an app
wiring up the standard LiveKit metrics/usage pattern against this plugin got
silence for the parts other providers report.

### Added
- `_Generation` (`realtime_model.py`) now tracks its own creation time and
  the time its first real audio frame was decoded off the wire, and reports
  a `RealtimeModelMetrics` event via `session.emit("metrics_collected", ...)`
  when it ends: `ttft` (time to first audio frame, `-1.0` if none arrived),
  `duration` (generation lifetime), `session_duration` (underlying websocket
  connection lifetime), `cancelled` (whether `interrupt()` was called on it).
  `input_tokens`/`output_tokens`/`total_tokens` are honestly `0` — Moshi's
  wire protocol has no token concept, so there was nothing to report there
  and nothing invented to fill the gap.
- `RealtimeSession.aclose()` now finishes and reports metrics for the
  in-flight generation before tearing down, instead of losing it: cancelling
  `_main_atask` interrupts `_recv_loop` mid-iteration, so the
  `generation.finish()` call that ordinarily follows `_recv_loop()` inside
  `_main_task` was never reached on an explicit local close — the single
  most common way a session actually ends in real usage (the caller shutting
  it down). Fixed by finishing the generation explicitly in `aclose()`.
- `tests/test_metrics.py` — two tests against a real local `aiohttp.web`
  websocket server (not a mocked `RealtimeSession`): one streams a real
  Opus-encoded audio frame after a real 0.15s server-side delay and asserts
  the reported `ttft` reflects that real delay (not 0, not -1); the other
  closes a generation that never received audio and asserts `ttft=-1.0`
  specifically, not a fabricated `0.0`.
- `scripts/check_metrics_live.py` — manual verification script against a
  real Moshi server, used for the numbers below.

### Verified (2026-08-25, same Apple M1 / `kyutai/moshiko-mlx-q4` as prior
entries)
`pytest tests/` — 10/10 pass, including the two new metrics tests, plus the
existing capability/framing/reconnect/prewarm suite (prewarm test ran live
against a real, currently-running `moshi_mlx.local_web` server rather than
being skipped). Also ran `scripts/check_metrics_live.py` against that live
server — streamed 3.0s of synthetic silence through the real plugin and
printed the actual emitted events:

```
request_id=''                    ttft=-1.0000s duration=0.0000s session_duration=0.0000s acquire_time=0.0033s
request_id='MOSHI_030f7616fe05'  ttft=0.0021s  duration=3.0134s session_duration=3.0133s acquire_time=0.0000s
```

`ttft=2.1ms` here is a single one-off local measurement against an
already-warm process (no JIT cost in this run), consistent with — not a
replacement for — the 20-44ms warm range measured across four runs in the
0.1.0 entry; run-to-run variance is expected, same as it was there.

## 0.2.0 — automatic websocket reconnection with backoff

**Gap and source.** LiveKit's own `livekit-plugins-openai` reconnects its
Realtime API websocket with exponential backoff and emits
`session_reconnected` on success (`livekit/agents` PR #925, issue #2341,
and issue #3145 about capping retries on a permanently broken server —
checked via WebSearch against the actual `livekit/agents` GitHub repo, not
assumed from memory). This plugin's `RealtimeSession._main_task` had no
such thing: any dropped websocket — server restart, network blip, proxy
timeout — ended the session for good, with no automatic path back, even
though `llm.RealtimeSession` already defines a `session_reconnected` event
for exactly this that this plugin simply never emitted.

### Added
- `MoshiConnectOptions.max_reconnect_attempts` / `reconnect_backoff_base` /
  `reconnect_backoff_max` / `reconnect_stable_after` (`models.py`)
- Reconnect loop in `RealtimeSession._main_task` (`realtime_model.py`):
  on an unexpected drop, retries the websocket connection with exponential
  backoff, rebuilds the Opus encoder/decoder pair (a fresh connection needs
  an un-primed codec — reusing the old one across a new stream is garbage),
  starts a fresh generation, and emits `session_reconnected`. Gives up and
  emits a normal `recoverable=False` error after the configured attempt
  budget. A connection that stays up at least `reconnect_stable_after`
  seconds before dropping resets the attempt counter, so a healthy server's
  history of blips doesn't count against a later one.
- `tests/test_reconnect.py` — two tests against a real local `aiohttp.web`
  websocket server (not a mock of `RealtimeSession` itself): one drops the
  first connection and asserts the plugin reconnects, emits
  `session_reconnected`, and keeps receiving real messages afterward; the
  other makes every connection drop immediately and asserts the plugin
  gives up after its configured attempt budget instead of looping forever,
  with a `recoverable=False` error.

### What this does not do
Moshi's wire protocol has no session-resume message. A reconnect is a new
connection and a new generation — in-flight audio/text from the moment of
the drop is lost. This recovers connectivity, not conversation state. Set
`max_reconnect_attempts=0` for the old fail-once behavior.

### Verified (2026-08-25)
`pytest tests/` — 8/8 pass, including the two new reconnect tests, against
a real (if minimal) local websocket server built for the test, exercising
the actual `RealtimeSession._main_task` reconnect loop end to end — no
mocking of the class under test. Also re-ran `scripts/check_moshi_server.py`
against the same live `moshi_mlx.local_web` server (`kyutai/moshiko-mlx-q4`)
used for the 0.1.0 numbers below to confirm the refactor didn't regress the
non-reconnect path: handshake succeeded in 5.3ms.

## 0.1.0 — full rewrite, replaces the v1.0.0 "Aether-Voice" prototype

**What changed and why.** v1.0.0 (`21b39c6`, `4d357e5`, `0680efe`) shipped a
Bun/TypeScript HTTP server that presented itself as a "full-duplex neural
voice engine" with a published benchmark table ranking it above Kyutai Moshi,
OpenAI Realtime, and ElevenLabs on latency, cost, and feature coverage. On
inspection, none of that was backed by real audio ML:

- `generateTextResponse()` was keyword matching on the input string (`"ciao"`
  → a fixed reply), not an LLM call.
- `synthesizeRealAudio()` shelled out to macOS's `/usr/bin/say`, or — if that
  failed — generated a 440Hz sine wave and called it speech.
- The reported "140ms latency" was `performance.now()` measured around that
  keyword match and subprocess call, not a measurement of any speech-to-speech
  pipeline.
- `src/competitor_benchmark.ts` was a hardcoded array where this project's own
  row always won every column.

This version replaces that entirely with a real LiveKit Agents plugin that
speaks Moshi's actual documented wire protocol against a real Moshi server.
It is honest about what that protocol can and can't do (see README), rather
than filling gaps with plausible-sounding numbers. No performance claim in
this version's README is invented; anything not independently measured is
labeled as such, with its source.

### Added
- `livekit/plugins/moshi/realtime_model.py` — real `RealtimeModel` /
  `RealtimeSession` implementation over Moshi's `/api/chat` protocol
- `scripts/check_moshi_server.py` — real connectivity check, no mocked output
- `tests/test_capabilities.py`, `tests/test_protocol_framing.py`
- `examples/minimal_agent.py`

### Removed
- `server.ts`, `src/*.ts`, `public/*` — the TypeScript mock described above

### Verified (2026-08-25, Apple M1)
Ran this plugin end-to-end against a live `moshi_mlx.local_web` server
(`kyutai/moshiko-mlx-q4`, real download, real inference — no mocks). 5/5
`pytest` tests pass. Real measured numbers, cold vs. warm model, are in the
README's "What's verified vs. what isn't" section — including a genuine
3037ms cold-start (MLX graph compilation) that the earlier "not verified"
placeholder text could not have told you about. Also found and fixed: the
live server sends an undocumented `0x00` handshake byte not mentioned in
`moshi/moshi/client.py`; it was being logged as an unknown message kind and
is now handled explicitly (`models.py`, `realtime_model.py`).
Added `scripts/e2e_verify.py`, the script used to produce these numbers, so
they can be reproduced or challenged rather than taken on faith.

### Added — `moshi.prewarm()` / `moshi.prewarm_async()`
Added `prewarm()` matching `livekit-agents`' own
`WorkerOptions(prewarm_fnc=...)` convention, so an app can absorb the
JIT/graph-compile cost at worker startup instead of during a real user's
first turn. `examples/minimal_agent.py` updated to use it.

An earlier version of this entry claimed the 3037ms cold-start was a
one-time cost per machine, based on two restart measurements of 336ms and
9.8ms. That was wrong: re-testing with the server process confirmed dead
(`lsof`/`ps`, not just a `kill` on the PID it was launched with — this
server forks `multiprocessing` workers that a plain kill can leave running)
before each restart gave 4043.6ms, 1938.9ms, and 2229.7ms across three
clean restarts — consistently 2-4s, every time. The 336ms/9.8ms readings
were an already-warm leftover worker process, not a fresh one. Corrected in
the README; this changelog entry is kept, not deleted, as the record of the
mistake and the fix — the same reason this whole rewrite exists.
