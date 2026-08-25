# Changelog

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
