"""Private IPC bridge that keeps scoped tool handlers in the harness process."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .spec import ToolResult, ToolSpec


def json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Accept the SDK's historical ``{"name": str}`` shorthand as JSON Schema."""

    if schema.get("type") == "object" and "properties" in schema:
        return schema
    type_names = {
        str: "string", int: "integer", float: "number", bool: "boolean",
        list: "array", dict: "object",
    }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, value in schema.items():
        if isinstance(value, dict):
            properties[name] = value
        else:
            properties[name] = {"type": type_names[value]} if value in type_names else {}
        required.append(name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def wire_result(result: Any) -> dict[str, Any]:
    if isinstance(result, ToolResult):
        result = result.as_dict()
    if not isinstance(result, dict):
        return {"content": [{"type": "text", "text": str(result)}], "isError": False}
    content = result.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return {"content": content, "isError": bool(result.get("is_error", result.get("isError", False)))}


class ToolBridge:
    """Expose exactly one agent's ``ToolSpec`` list over a private local socket."""

    def __init__(self, tools: list[ToolSpec]) -> None:
        if any(not isinstance(item, ToolSpec) for item in tools):
            raise TypeError("CodexRunner requires provider-neutral ToolSpec tools")
        names = [item.name for item in tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        self._tools = {item.name: item for item in tools}
        self._server: asyncio.AbstractServer | None = None
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.socket_path: Path | None = None
        self._pipe = None

    async def __aenter__(self) -> "ToolBridge":
        if os.name == "nt":
            try:
                from .windows_pipe import WindowsPipeServer
            except ModuleNotFoundError as exc:
                if exc.name != f"{__package__}.windows_pipe":
                    raise  # pywin32 missing: say that, not "wrong profile"
                from ..profile import reinstall_hint

                raise RuntimeError(f"no Windows tool bridge: {reinstall_hint(os='windows')}") from exc

            self._pipe = WindowsPipeServer(self._dispatch)
            await self._pipe.start()
            self.socket_path = Path(self._pipe.address)
            return self
        # Unix socket mode and Windows pipe DACL are the authorization boundary.
        if not hasattr(asyncio, "start_unix_server"):
            raise RuntimeError(
                "CodexRunner needs a unix domain socket to keep scoped tool handlers in "
                "this process, and this platform has none. Use provider='claude' (its "
                "SDK runs handlers in-process, no bridge), or run the harness under WSL2."
            )
        self._tmp = tempfile.TemporaryDirectory(prefix="world-mcp-")
        self.socket_path = Path(self._tmp.name) / "tools.sock"
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._pipe:
            await self._pipe.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._tmp:
            self._tmp.cleanup()

    async def _dispatch(self, request: dict) -> dict:
        try:
            if request.get("method") == "list":
                response = {"tools": [
                    {"name": item.name, "description": item.description, "inputSchema": json_schema(item.input_schema)}
                    for item in self._tools.values()
                ]}
            elif request.get("method") == "call":
                name = request.get("name", "")
                if name not in self._tools:
                    raise ValueError(f"unknown tool: {name}")
                response = wire_result(await self._tools[name].handler(request.get("arguments") or {}))
            else:
                raise ValueError("unsupported bridge method")
        except Exception as exc:
            response = {"content": [{"type": "text", "text": f"tool bridge error: {exc}"}], "isError": True}
        return response

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._dispatch(json.loads(await reader.readline()))
        except Exception as exc:
            response = {"content": [{"type": "text", "text": f"tool bridge error: {exc}"}], "isError": True}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
