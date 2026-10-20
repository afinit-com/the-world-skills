"""Slack progress indicators — the reaction ladder, the thread status banner, and
ToolTrail's updatable "⚙️ working" message.

Split out of listener.py when that file crossed 800 lines. Nothing
here talks to Slack directly: ToolTrail calls back into the listener's `_react` /
`_set_thread_status` / `_web_for`, so this module stays free of the socket
lifecycle and can be read on its own.

The status controller is transport-agnostic except for listener callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING

from .post import slack_call

if TYPE_CHECKING:
    from .listener import SlackListener
    from ..types import StatusPhrases

# Reactions on the triggering message.
EMOJI_QUEUED = "eyes"            # 👀 received
EMOJI_WORKING = "thinking_face"  # 🤔 turn in progress
EMOJI_DONE = "thumbsup"          # 👍 turn ok
EMOJI_ERROR = "scream"           # 😱 turn failed
STATUS_UPDATE_MIN_S = 2.0  # avoid excessive chat_update calls

# Stall escalation — no tool call/update
# for STALL_SOFT_S -> 🥱 + a softer status line; STALL_HARD_S -> 😨 + a more
# urgent one (both the reaction AND assistant.threads.setStatus). Reset by
# every tool call (proof of life); cancelled on finish().
EMOJI_STALL_SOFT = "yawning_face"  # 🥱 no update in a while
EMOJI_STALL_HARD = "fearful"       # 😨 no update in a long while
STALL_SOFT_S = 10.0
STALL_HARD_S = 30.0

TOOL_PREVIEW_CHARS = 20  # the live "working" line only; the final audit shows everything
TRAIL_TEXT_LIMIT = 3800  # Slack's hard limit is 4000 — one audit message never exceeds this


def _tool_preview(tool_input: dict) -> str:
    joined = " ".join(str(v) for v in tool_input.values())
    if len(joined) > TOOL_PREVIEW_CHARS:
        return joined[:TOOL_PREVIEW_CHARS] + "…"
    return joined


def _tool_args(tool_input: dict) -> str:
    """Full arguments, as the agent sent them. A fence inside a value would close the
    code block early, so it is broken with a zero-width space."""
    text = json.dumps(tool_input, ensure_ascii=False, indent=2, default=str)
    return text.replace("```", "`\u200b``")


def audit_messages(head: str, calls: list[tuple[str, dict]],
                   limit: int = TRAIL_TEXT_LIMIT) -> list[str]:
    """The end-of-turn audit as Slack messages, each at most `limit` chars: every tool
    call in order with its FULL arguments, so a reviewer can read from the thread what
    the agent actually did. Nothing is dropped. Calls are packed into as few messages as
    fit, and one whose arguments alone are too long is split across messages, each
    piece in its own code block."""
    messages: list[str] = []
    current = head
    for i, (label, tool_input) in enumerate(calls, 1):
        title = f"{i}. `{label}`"
        body = _tool_args(tool_input) if tool_input else ""
        entry = title + (f"\n```{body}```" if body else "")
        if len(current) + 1 + len(entry) <= limit:
            current += "\n" + entry
            continue
        if len(entry) <= limit:
            messages.append(current)
            current = entry
            continue
        # Too long for any one message: split the arguments themselves.
        messages.append(current)
        room = limit - len(title) - len(" (계속)") - 8  # newline + two fences
        for n, start in enumerate(range(0, len(body), room)):
            piece = body[start:start + room]
            messages.append(f"{title}{' (계속)' if n else ''}\n```{piece}```")
        current = messages.pop()
    messages.append(current)
    return messages


class ToolTrail:
    """One updatable '⚙️ working' message per turn.

    While the turn runs, the message shows the latest tool with a ~20-character input
    preview. When the turn ends it becomes the audit: every call with its full arguments
    (never its output), split across thread messages when it is long, so how an agent
    worked can be reviewed from Slack alone.

    No tool call for STALL_SOFT_S escalates
    the reaction + thread status to 🥱, STALL_HARD_S to 😨. Any tool call is
    proof of life — it cancels + restarts both timers and, if a stall emoji is
    currently showing, swaps the reaction back to "working" first."""

    def __init__(
        self,
        listener: "SlackListener",
        identity: str,
        thread_ts: str,
        msg_ts: str,
        phrases: "StatusPhrases",
    ) -> None:
        self.listener = listener
        self.identity = identity
        self.thread_ts = thread_ts
        self.msg_ts = msg_ts
        self.phrases = phrases
        self.ts: str | None = None
        self.count = 0
        self.last_update = 0.0
        self.calls: list[tuple[str, dict]] = []  # (tool name, full input)
        self._finished = False
        self._stalled_emoji: str | None = None
        self._stall_soft: asyncio.Task | None = None
        self._stall_hard: asyncio.Task | None = None
        self._arm_stall_timers()

    # --- stall escalation ----------------------------------------------------

    def _arm_stall_timers(self) -> None:
        self._stall_soft = asyncio.create_task(
            self._stall(STALL_SOFT_S, EMOJI_STALL_SOFT, self.phrases.stall_soft)
        )
        self._stall_hard = asyncio.create_task(
            self._stall(STALL_HARD_S, EMOJI_STALL_HARD, self.phrases.stall_hard)
        )

    def _disarm_stall_timers(self) -> None:
        for t in (self._stall_soft, self._stall_hard):
            if t and not t.done():
                t.cancel()
        self._stall_soft = self._stall_hard = None

    async def _reset_stall(self) -> None:
        """Called on every tool call — a call in flight is proof the turn isn't
        actually stuck, so cancel/restart the timers and undo any escalation."""
        self._disarm_stall_timers()
        if self._stalled_emoji:
            await self.listener._react(
                self.msg_ts, add=EMOJI_WORKING, remove=self._stalled_emoji, identity=self.identity,
            )
            self._stalled_emoji = None
        self._arm_stall_timers()

    async def _stall(self, delay: float, emoji: str, status: str) -> None:
        try:
            await asyncio.sleep(delay)
            if self._finished:
                return
            await self.listener._react(
                self.msg_ts, add=emoji, remove=self._stalled_emoji or EMOJI_WORKING,
                identity=self.identity,
            )
            self._stalled_emoji = emoji
            await self.listener._set_thread_status(self.thread_ts, status, identity=self.identity)
        except asyncio.CancelledError:
            pass

    @property
    def showing(self) -> str:
        """The progress emoji currently on the message — what the turn-end swap must
        remove. After a stall that is 🥱/😨, not 🤔."""
        return self._stalled_emoji or EMOJI_WORKING

    # --- tool trail -----------------------------------------------------------

    async def tool(self, name: str, tool_input: dict) -> None:
        self.count += 1
        # mcp__toolname__actual_name -> actual_name
        label = name.split("__")[-1] if name.startswith("mcp__") else name
        preview = _tool_preview(tool_input)
        self.calls.append((label, dict(tool_input)))
        line = f"⚙️ 작업 중 (#{self.count}) — `{label}`" + (f" `{preview}`" if preview else "")
        await self._reset_stall()
        await self.listener._set_thread_status(
            self.thread_ts, self.phrases.tool_text(label), identity=self.identity,
        )
        now = time.monotonic()
        web = self.listener._web_for(self.identity)
        channel = self.listener.harness.slack.channel
        if not web:
            return
        try:
            if self.ts is None:
                resp = await slack_call(web, "chat_postMessage",
                    channel=channel, thread_ts=self.thread_ts, text=line,
                    **self.listener._face(self.identity),
                )
                self.ts = resp["ts"]
                self.last_update = now
            elif now - self.last_update >= STATUS_UPDATE_MIN_S:
                await slack_call(web, "chat_update", channel=channel, ts=self.ts, text=line)
                self.last_update = now
        except Exception:
            pass  # progress display only — never fail the turn over it

    async def finish(self, ok: bool) -> None:
        self._finished = True
        self._disarm_stall_timers()
        if self.ts is None or self.count == 0:
            return
        # The final message becomes the in-thread audit — which tools ran, in order,
        # with their full arguments. Outputs never leave the agent process.
        icon, outcome = ("🛠", "") if ok else ("⚠️", " (실패로 종료)")
        head = f"{icon} 툴 {self.count}회 사용{outcome}:"
        first, *rest = audit_messages(head, self.calls)
        web = self.listener._web_for(self.identity)
        channel = self.listener.harness.slack.channel
        if not web:
            return
        # a trail that fails to redraw is cosmetic; the turn it narrates is not
        with contextlib.suppress(Exception):
            await slack_call(web, "chat_update", channel=channel, ts=self.ts, text=first)
            for text in rest:
                await slack_call(web, "chat_postMessage", channel=channel, thread_ts=self.thread_ts, text=text)
