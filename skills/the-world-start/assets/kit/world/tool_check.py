"""Call a thread-bound tool factory outside Slack, to test the boundary it claims to have.

A `Persona.mention_tools` or `Persona.thread_tools` factory is called by the listener as
`await factory(listener, thread_ts)`, so its tools only exist inside a live Slack turn.
Testing that a tool refuses what it must refuse (another host, a repo outside the
allowlist, an action its verdict does not allow) used to mean hand-building the same fake
listener in every harness. This is that fake, once:

    from world.tool_check import FakeListener, bind, call

    listener = FakeListener(state={"worktrees": {...}})
    tools = await bind(PERSONA.mention_tools, listener, "1700000000.000100")
    result = await call(tools, "github_pr_comment", {"number": 999999, "body": "x"})
    assert result["is_error"], result

What it fakes, and what it does not:

- `post()` records the message and returns a made-up ts. Nothing reaches Slack.
- `save_state()` counts calls. Nothing is written to `state.json`.
- The tool handler itself runs for real. A tool that writes to GitHub writes to GitHub
  unless its own code refuses first, so call a write tool only on a path it must refuse,
  or fake its outward call (`unittest.mock`) as `tests/test_integration_recipes.py` does.
  A boundary that turns out to be open is then the one write this check leaves behind;
  say so in the report rather than hide it.
- `bind()` returns the tools as the factory made them, without the approval gate the
  listener wraps around `mention_tools`. What this checks is the tool's own code, not the
  gate; the gate is the kit's and has its own tests.

No Slack SDK import: a harness installed with `--channel none` can run this too.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .tools.spec import ToolResult, ToolSpec


class FakeListener:
    """The part of `SlackListener` a tool factory reads, with nothing behind it.

    `state`, `save_state`, `post`, `harness` and `thread_worktree` are what factories in
    the kit's own recipes and in running harnesses touch. A factory reaching for
    anything else fails here with an AttributeError naming it, which is the honest
    answer: this fake does not know what that call would have done.
    """

    def __init__(self, state: dict | None = None, harness: Any = None) -> None:
        self.state: dict = state if state is not None else {}
        self.logged: list[str] = []
        self.harness = harness if harness is not None else SimpleNamespace(
            log=self.logged.append, slack=SimpleNamespace(addressee=None, channel=None),
        )
        self.saves = 0
        self.posted: list[dict] = []
        self._ts = itertools.count(1)

    def save_state(self) -> None:
        self.saves += 1

    async def post(self, text: str, **kwargs: Any) -> str:
        ts = f"0000000000.{next(self._ts):06d}"
        self.posted.append({"text": text, "ts": ts, **kwargs})
        return ts

    def thread_worktree(self, thread_ts: str) -> Path | None:
        # Same lookup as ApprovalMixin.thread_worktree, which lives behind the Slack SDK.
        for path, meta in self.state.get("worktrees", {}).items():
            if meta.get("thread_ts") == thread_ts:
                return Path(path)
        return None


async def bind(tools: Any, listener: FakeListener, thread_ts: str) -> list[ToolSpec]:
    """The tools a persona's slot yields in `thread_ts`: a list as-is, a factory called
    the way the listener calls it. A factory that raises raises here. The listener turns
    that into "no tools this turn", which is right for a turn and wrong for a test."""
    if callable(tools):
        tools = await tools(listener, thread_ts)
    return list(tools)


async def call(tools: list[ToolSpec], name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run the tool called `name` with `args` and return its result as a dict
    (`content`, `is_error`). A tool name that is not there is an error, not a skip."""
    found = [t for t in tools if t.name == name]
    if not found:
        raise LookupError(f"no tool named {name!r}; bound: {[t.name for t in tools]}")
    out = await found[0].handler(dict(args))
    return out.as_dict() if isinstance(out, ToolResult) else dict(out)


def text(result: dict[str, Any]) -> str:
    """The text a tool result carries, joined, for asserting on a refusal's reason."""
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def run(coro):
    """`asyncio.run`, for a check script that is not itself async."""
    return asyncio.run(coro)
