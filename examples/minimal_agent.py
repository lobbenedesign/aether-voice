"""Minimal LiveKit Agent using the Moshi realtime plugin.

Prerequisite: a running Moshi-protocol server. Pick one:

    # Apple Silicon (M-series), no discrete GPU needed:
    pip install moshi_mlx
    python -m moshi_mlx.local_web

    # CUDA GPU:
    pip install moshi
    python -m moshi.server

Both expose a websocket at ws://localhost:8998/api/chat by default.

Run this agent against a LiveKit room the usual way:
    python minimal_agent.py dev
"""

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins import moshi


class MoshiAgent(Agent):
    def __init__(self) -> None:
        # No `instructions=` here: Moshi has no instructions channel (see
        # RealtimeModel's docstring). Passing one would be silently misleading,
        # so we don't accept the illusion that it does anything.
        super().__init__(instructions="")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    session = AgentSession(
        llm=moshi.RealtimeModel(
            connect_options=moshi.MoshiConnectOptions(url="ws://localhost:8998/api/chat"),
        ),
    )
    await session.start(agent=MoshiAgent(), room=ctx.room)


if __name__ == "__main__":
    # moshi.prewarm connects once and waits for the first response audio frame,
    # forcing MLX's one-time JIT graph compilation (measured ~3s on an Apple M1)
    # to happen at worker startup instead of during a real user's first turn.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=moshi.prewarm))
