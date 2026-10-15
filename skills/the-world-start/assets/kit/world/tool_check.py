"""Call a thread-bound tool factory outside Slack, to test the boundary it claims to have.

A `Persona.mention_tools` or `Persona.thread_tools` factory is called by the listener as
`await factory(listener, thread_ts)`, so its tools only exist inside a live Slack turn.
Testing that a tool refuses what it must refuse (another host, a repo outside the
allowlist, an action its verdict does not allow) used to mean hand-building the same fake
listener in every harness. This is that fake, once:

    from world.tool_check import FakeListener, bind, refuses

    listener = FakeListener(state={"worktrees": {...}})
    tools = await bind(PERSONA.mention_tools, listener, "1700000000.000100")
    await refuses(tools, "github_pr_comment", {"number": 999999, "body": "x"},
                  outward=["abilities.reviewer.tools.gh_write"])

A boundary check must never be able to write. The tool handler runs for real, so a write
tool whose boundary turns out to be open would comment, push, or merge from inside the
check. `refuses` therefore stubs every outward call named in `outward` for the length of
the call, and fails if any stub was reached, whatever the tool returned. It refuses to
run without at least one, and `call` makes the same choice explicit: `outward=()` is a
declaration that the tool writes nothing outside, not a default.

Name the function the tool looks up when it writes, as the tool's module sees it. A
module that did `from tools.github import gh_write` holds its own name for the function,
and patching `tools.github.gh_write` alone would leave that name pointing at the real
one. So every other module-level name bound to the same function is stubbed too. A
reference kept anywhere else (a closure, a default argument, a dict of handlers) is not
found; stub that name instead.

What else is faked:

- `post()` records the message and returns a made-up ts. Nothing reaches Slack. It takes
  the same keyword arguments as the real listener's `post`, and nothing else, so a typo
  fails here rather than in production.
- `save_state()` counts calls. Nothing is written to `state.json`.
- `bind()` returns the tools as the factory made them, without the approval gate the
  listener wraps around `mention_tools`. What this checks is the tool's own code, not the
  gate; the gate is the kit's and has its own tests.

No Slack SDK import: a harness installed with `--channel none` can run this too.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import pkgutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from unittest import mock

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

    # Same signature as world/slack/post.py's PostingMixin.post; tests/test_verify_flow.py
    # holds the two together.
    async def post(
        self,
        text: str,
        *,
        identity: str = "main",
        thread_ts: str | None = None,
        blocks: list[dict] | None = None,
        channel: str | None = None,
        context: dict | None = None,
    ) -> str | None:
        ts = f"0000000000.{next(self._ts):06d}"
        self.posted.append({"text": text, "ts": ts, "identity": identity,
                            "thread_ts": thread_ts, "blocks": blocks, "channel": channel,
                            "context": context})
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


def _owner(target: Any) -> tuple[Any, str]:
    """The object and attribute a stub target names: a dotted path as `unittest.mock.patch`
    takes it ("abilities.reviewer.tools.gh_write", "subprocess.run"), or an (object,
    attribute) pair."""
    if isinstance(target, str):
        owner, _, attribute = target.rpartition(".")
        return pkgutil.resolve_name(owner), attribute
    return target


def _patch(stack: contextlib.ExitStack, target: Any) -> Any:
    """Stub one outward call where it was named, and under every other module-level name
    bound to the same object, so a `from x import f` in the tool's module is covered too.
    Returns the stub."""
    owner, attribute = _owner(target)
    original = getattr(owner, attribute)
    stub = stack.enter_context(mock.patch.object(owner, attribute, autospec=True))
    for module in list(sys.modules.values()):
        names = getattr(module, "__dict__", None)
        if not isinstance(names, dict):
            continue
        for name, value in list(names.items()):
            if value is original and not (module is owner and name == attribute):
                stack.enter_context(mock.patch.object(module, name, stub))
    return stub


async def call(tools: list[ToolSpec], name: str, args: dict[str, Any], *,
               outward: Iterable[Any]) -> dict[str, Any]:
    """Run the tool called `name` with `args`, every call in `outward` stubbed, and return
    its result as a dict (`content`, `is_error`). Raises AssertionError if a stub was
    reached. `outward=()` declares the tool writes nothing outside; pass it only for a
    tool that reads. A tool name that is not there is an error, not a skip."""
    found = [t for t in tools if t.name == name]
    if not found:
        raise LookupError(f"no tool named {name!r}; bound: {[t.name for t in tools]}")
    with contextlib.ExitStack() as stack:
        stubs = {repr(t): _patch(stack, t) for t in outward}
        out = await found[0].handler(dict(args))
    reached = [t for t, stub in stubs.items() if stub.called]
    if reached:
        raise AssertionError(f"{name} reached outward call(s) {reached}; stubbed, so nothing "
                             "was written, but this boundary is open")
    return out.as_dict() if isinstance(out, ToolResult) else dict(out)


async def refuses(tools: list[ToolSpec], name: str, args: dict[str, Any], *,
                  outward: Iterable[Any]) -> dict[str, Any]:
    """`call` for an input the tool must refuse: its outward calls stubbed and none of
    them reached, and the result an error. Returns the result, for its reason."""
    outward = list(outward)
    if not outward:
        raise ValueError(f"refuses({name!r}) needs the outward call(s) to stub; without one "
                         "an open boundary would write for real")
    out = await call(tools, name, args, outward=outward)
    if not out.get("is_error"):
        raise AssertionError(f"{name} accepted {args!r}: {text(out)}")
    return out


def text(result: dict[str, Any]) -> str:
    """The text a tool result carries, joined, for asserting on a refusal's reason."""
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def run(coro):
    """`asyncio.run`, for a check script that is not itself async."""
    return asyncio.run(coro)
