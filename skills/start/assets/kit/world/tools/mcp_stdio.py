"""Stdio MCP adapter for the parent-process scoped-tool bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


async def bridge(socket_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if os.name == "nt":
        from .windows_pipe import exchange

        return await asyncio.to_thread(exchange, str(socket_path), payload)
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line)


async def dispatch(socket_path: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method", "")
    request_id = request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        requested_version = (request.get("params") or {}).get("protocolVersion")
        result = {
            # MCP clients require the server to negotiate the version they offered.
            # Codex releases do not all advertise the same revision.
            "protocolVersion": requested_version or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "world-scoped-tools", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = await bridge(socket_path, {"method": "list"})
    elif method == "tools/call":
        params = request.get("params") or {}
        result = await bridge(socket_path, {
            "method": "call", "name": params.get("name"), "arguments": params.get("arguments"),
        })
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def main(socket_path: Path) -> None:
    # MCP stdio is newline-delimited JSON-RPC. Never log to stdout.
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            response = await dispatch(socket_path, json.loads(line))
            if response is not None:
                sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
        except Exception as exc:
            sys.stderr.write(f"world MCP adapter error: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.socket))
