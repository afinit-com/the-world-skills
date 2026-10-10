"""Provider-neutral tool contract.

Harness projects historically received a Claude Agent SDK object from ``@tool``.
Keeping that object in every agent declaration made the declaration provider-specific.
``ToolSpec`` preserves the public attributes existing helpers use while leaving provider
conversion to the runner adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class ToolResult:
    """Provider-neutral tool response."""

    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"content": list(self.content), "is_error": self.is_error}


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | ToolResult]]


@dataclass(frozen=True)
class RunEvent:
    """Normalized progress event adapters may emit to observers."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """One capability exposed to an agent.

    Scope remains captured by ``handler`` at construction time. A provider adapter may
    change the wire representation, but it must never add parameters or capabilities.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


def tool(name: str, description: str, input_schema: dict[str, Any]):
    """Decorate an async handler as a provider-neutral :class:`ToolSpec`.

    The call shape matches ``claude_agent_sdk.tool`` so existing harness tool modules
    continue to import and declare tools without changes.
    """

    def decorate(handler: ToolHandler) -> ToolSpec:
        return ToolSpec(name, description, dict(input_schema), handler)

    return decorate
