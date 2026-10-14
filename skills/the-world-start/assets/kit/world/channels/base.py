"""Small channel boundary used by the orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from ..env import redact
from ..process import task as tasks

CARD_EXCERPT_CHARS = 300


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
        return [status] + [f"> {item}" for item in evidence]
    if evidence:
        status += " · " + " · ".join(evidence)
    excerpt = redact(text)[:CARD_EXCERPT_CHARS]
    return [status] + [f"> {line}" for line in excerpt.splitlines()]


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
