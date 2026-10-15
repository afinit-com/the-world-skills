"""Small channel boundary used by the orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from ..env import redact
from ..process import task as tasks

CARD_EXCERPT_CHARS = 300
# The status line carries the evidence as one line, so it gets a line's worth of room.
CARD_EVIDENCE_CHARS = 200
# Everything under the headline. Slack rejects a section over 3000 characters and the
# chunking in post() cannot shorten a block, and Discord splits a message at 2000; a
# 120-char headline plus this plus a run footer stays inside both.
CARD_BODY_CHARS = 1600


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fit(lines: list[str], limit: int) -> list[str]:
    """Keep lines while they fit; cut the first that does not, marked, and stop."""
    out: list[str] = []
    used = 0
    for line in lines:
        room = limit - used - (1 if out else 0)
        if len(line) <= room:
            out.append(line)
            used += len(line) + (1 if len(out) > 1 else 0)
            continue
        if room > 1:
            out.append(line[: room - 1] + "…")
        break
    return out


def _excerpt(text: str) -> str:
    """The head of what a run said, marked when it is not all of it. A cut inside a code
    block closes that block again: Slack shows an unclosed ``` as literal backticks, and
    on Discord it would run on into the footer under the card. Redacted before the cut,
    so a secret is never cut short of what redact() recognises."""
    text = redact(text)
    if len(text) <= CARD_EXCERPT_CHARS:
        return text
    cut = text[: CARD_EXCERPT_CHARS - 1] + "…"
    return cut + "\n```" if cut.count("```") % 2 else cut


def task_card_lines(home: Path, record: Any, result: Any) -> list[str]:
    """The lines under a task card's headline: a status line, then what the run said.

    What the agent wrote leads. The default completion check records the same evidence
    for every run ("<agent> 실행 완료 · work/ 산출물 <file>"), and when that evidence was
    the body, a scheduled briefing reached the channel as "it finished" with the briefing
    itself one button away. So when the run left a result_text, the evidence shrinks onto
    the status line, still visible; only a run with nothing to say gets it as the body.
    """
    status = f"`{record.id}` · {record.status} · {record.agent}"
    verdict = next(
        (e for e in reversed(tasks.read_events(home, record.id)) if e.get("kind") == "verify"),
        None,
    )
    evidence = [str(item) for item in (verdict or {}).get("evidence") or []][:4]
    text = (result.result_text or "").strip() if result is not None else ""
    if not text:
        quoted = [f"> {line}" for item in evidence for line in item.splitlines()]
        return _fit([status, *quoted], CARD_BODY_CHARS)
    if evidence:
        status += " · " + _one_line(" · ".join(evidence), CARD_EVIDENCE_CHARS)
    excerpt = _excerpt(text)
    return _fit([status, *(f"> {line}" for line in excerpt.splitlines())], CARD_BODY_CHARS)


class ConversationGateway(Protocol):
    enabled: bool
    ready: asyncio.Event

    async def start(self, stop: asyncio.Event) -> None: ...
    async def post(self, text: str, **kwargs: Any) -> Any: ...
    async def announce_task(self, record: Any, result: Any, persona: Any) -> Any: ...


class NullGateway:
    """Poll-only gateway used without importing a channel SDK."""

    enabled = False

    def __init__(self) -> None:
        self.ready = asyncio.Event()

    async def start(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def post(self, text: str, **kwargs: Any) -> None:
        return None

    async def announce_task(self, record: Any, result: Any, persona: Any) -> None:
        return None
