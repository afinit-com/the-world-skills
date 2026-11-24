"""Provider-neutral agent runner contract."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from ..types import Agent, RunResult

ToolCallback = Callable[[str, dict], Awaitable[None]]
# (call number, ok): a tool call returned. The number is the call's 0-based position
# among this run's on_tool calls, so a listener pairs the two by counting. Paired by
# position rather than by arrival order because one model message can start several
# tools and their results come back in any order.
ToolDoneCallback = Callable[[int, bool], Awaitable[None]]


def reports_tool_done(runner: object) -> bool:
    """Whether `runner.run` takes `on_tool_done`. A runner that cannot tell when a call
    returned (Codex replays its tool calls after the process exits) simply does not
    take the keyword; spawn.py then leaves it out and the caller keeps working without
    durations."""
    try:
        params = inspect.signature(runner.run).parameters  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    return "on_tool_done" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


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
        own worktree — a runner given one must start there, never at git_root.

        Optional: a runner that knows when each tool call returned also takes
        `on_tool_done: ToolDoneCallback | None = None` (see reports_tool_done)."""
        ...
