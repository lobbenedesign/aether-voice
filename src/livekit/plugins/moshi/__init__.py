from .models import MoshiConnectOptions
from .realtime_model import RealtimeModel, RealtimeSession, prewarm, prewarm_async
from .version import __version__

__all__ = [
    "RealtimeModel",
    "RealtimeSession",
    "MoshiConnectOptions",
    "prewarm",
    "prewarm_async",
    "__version__",
]

from livekit.agents import Plugin

from .log import logger


class MoshiPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(__name__, __version__, __package__, logger)


Plugin.register_plugin(MoshiPlugin())
