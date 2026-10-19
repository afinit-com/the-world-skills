"""Agent execution backends. Only the ones in project-profile.json are installed."""

from .base import AgentRunner, ToolCallback
from ..profile import SUPPORTED_PROVIDERS

__all__ = ["AgentRunner", "ToolCallback"]
if "codex" in SUPPORTED_PROVIDERS:
    from .codex import CodexRunner

    __all__.append("CodexRunner")
