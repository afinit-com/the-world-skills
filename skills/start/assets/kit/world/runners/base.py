"""Provider-neutral agent runner contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from ..types import Agent, RunResult

ToolCallback = Callable[[str, dict], Awaitable[None]]


class AgentRunner(Protocol):
    """Execute agents for one harness using a concrete model provider."""

    provider: str

    async def run(
        self,
        agent: "Agent",
        prompt: str,
        *,
        on_tool: ToolCallback | None = None,
        cwd: str | None = None,
    ) -> "RunResult":
        """`cwd` is the directory the session starts in; None means the harness's
        git_root. spawn.py passes one only for a mutates:true agent, and it is that run's
        own worktree — a runner given one must start there, never at git_root."""
        ...
