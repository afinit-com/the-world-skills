"""Small channel boundary used by the orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol


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
