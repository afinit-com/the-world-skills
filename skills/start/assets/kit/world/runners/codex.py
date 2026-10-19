"""Codex CLI execution adapter with a per-agent scoped MCP tool surface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import ToolCallback
from ..context import current_task
from ..env import child_env
from ..tools.bridge import ToolBridge
from ..types import Agent, RunResult

if TYPE_CHECKING:
    from ..harness import Harness

CODEX_TIMEOUT_SECONDS = 20 * 60
RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "usage limit", "overloaded")


def capability_prompt(agent: Agent, prompt: str) -> str:
    if not agent.tools:
        return prompt
    names = ", ".join(tool.name for tool in agent.tools)
    return (
        f"WORLD MCP tools connected for this turn: {names}.\n"
        "These are real callable tools, not merely names in instructions. If your top-level "
        "tool list shows functions.exec, use it to inspect ALL_TOOLS for these names, read "
        "their descriptions/schemas, and call the matching tools[entry.name] with the required "
        "arguments. Deferred MCP tools may not appear in the top-level namespace. "
        "Do not report a tool unavailable without checking ALL_TOOLS. functions.exec is "
        "JavaScript tool orchestration, not a shell permission. Use only the connected WORLD tools.\n\n"
        + prompt
    )


def normalize_usage(usage: dict) -> dict:
    cached = int(usage.get("cached_input_tokens") or 0)
    written = int(usage.get("cache_write_input_tokens") or 0)
    return {
        "input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached - written),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": cached,
        "cache_creation_tokens": written,
    }


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return "[" + ",".join(_toml_string(value) for value in values) + "]"


@dataclass
class CodexEvents:
    text: str = ""
    errors: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    permission_denials: list[str] = field(default_factory=list)

    def feed(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        item = event.get("item") or {}
        item_type = item.get("type", "")
        if kind == "item.completed" and item_type == "agent_message":
            self.text = item.get("text", "")
        elif kind in {"turn.failed", "error"}:
            error = event.get("error") or event.get("message") or event
            self.errors.append(str(error))
        elif kind == "turn.completed":
            self.usage = event.get("usage") or {}
        elif item_type in {"mcp_tool_call", "tool_call"} and kind in {
            "item.started", "item.completed",
        }:
            # Emit once, when possible. Older CLIs only exposed completed events.
            if kind == "item.completed" or not item.get("status"):
                name = item.get("tool") or item.get("name") or "mcp_tool"
                arguments = item.get("arguments") or item.get("input") or {}
                self.tool_calls.append((name, arguments if isinstance(arguments, dict) else {}))
            if kind == "item.completed" and item.get("status") == "failed":
                message = (item.get("error") or {}).get("message", "tool call failed")
                self.permission_denials.append(f"{item.get('tool') or item.get('name')}: {message}")


def build_command(
    harness: "Harness", agent: Agent, socket_path: Path | None, *, executable: str,
    cwd: str | None = None,
) -> list[str]:
    """Build an isolated, non-interactive invocation.

    Clearing the root ``mcp_servers`` table prevents user/project MCP inheritance. The
    default shell and multi-agent surfaces are disabled unless explicitly opted in.
    ``cwd`` is a mutates:true run's own worktree (spawn.py) and becomes ``-C``; without
    one the session starts at git_root.
    """

    command = [
        executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--sandbox", "read-only", "-C", str(cwd or harness.git_root),
        "-c", "approval_policy=never", "-c", "mcp_servers={}",
        "-c", f"features.shell_tool={'true' if agent.allow_bash else 'false'}",
        "-c", f"agents.enabled={'true' if agent.task_allowed else 'false'}",
        # Codex plugins may bring their own MCP servers and hooks, which cannot be
        # reduced to this agent's ToolSpec allowlist. Keep them off in the MVP.
        "-c", "features.plugins=false",
        "-c", "features.apps=false", "-c", "features.browser_use=false",
        "-c", "features.computer_use=false", "-c", "features.image_generation=false",
        "-c", 'web_search="disabled"',
    ]
    model = agent.model if agent.model and not agent.model.startswith(("sonnet", "haiku", "opus")) else None
    model = model or getattr(harness, "codex_model", None)
    if model:
        command.extend(["--model", model])
    if socket_path is not None:
        server = "world"
        runtime_root = str(Path(__file__).resolve().parents[2])
        command.extend([
            "-c", f"mcp_servers.{server}.command={_toml_string(sys.executable)}",
            "-c",
            f"mcp_servers.{server}.args="
            f"{_toml_array(['-m', 'world.tools.mcp_stdio', '--socket', str(socket_path)])}",
            "-c", f"mcp_servers.{server}.cwd={_toml_string(str(harness.home))}",
            "-c", f"mcp_servers.{server}.env={{PYTHONPATH={_toml_string(runtime_root)}}}",
            "-c", f"mcp_servers.{server}.enabled_tools={_toml_array([tool.name for tool in agent.tools])}",
            "-c", f"mcp_servers.{server}.required=true",
            # Every exposed capability was already selected in Agent.tools; outward
            # side effects remain enforced by the handler's one-shot approval gate.
            "-c", f"mcp_servers.{server}.default_tools_approval_mode=approve",
        ])
    command.append("-")
    return command


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


class CodexRunner:
    provider = "codex"

    def __init__(
        self,
        harness: "Harness",
        *,
        executable: str = "codex",
        timeout_s: int = CODEX_TIMEOUT_SECONDS,
        model: str | None = None,
    ) -> None:
        self.harness = harness
        self.executable = executable
        self.timeout_s = timeout_s
        if model:
            harness.codex_model = model

    async def run(
        self,
        agent: Agent,
        prompt: str,
        *,
        on_tool: ToolCallback | None = None,
        cwd: str | None = None,
    ) -> RunResult:
        started = time.time()
        if agent.allow_plugins:
            return RunResult(
                status="error",
                result_text=(
                    "CodexRunner does not enable plugins: plugin-provided MCP/hooks cannot "
                    "yet be reduced to this agent's scoped ToolSpec surface"
                ),
            )
        try:
            async with contextlib.AsyncExitStack() as stack:
                socket_path = None
                if agent.tools:
                    bridge = await stack.enter_async_context(ToolBridge(agent.tools))
                    socket_path = bridge.socket_path
                command = build_command(
                    self.harness, agent, socket_path, executable=self.executable, cwd=cwd
                )
                env = child_env("codex", {"WORLD_HEADLESS": "1", "WORLD_AGENT": agent.name,
                                          "WORLD_TASK": current_task()})
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(capability_prompt(agent, prompt).encode()), timeout=self.timeout_s
                    )
                except asyncio.TimeoutError:
                    await _terminate(process)
                    return RunResult(
                        status="error", result_text="timed out", duration_s=time.time() - started
                    )
        except asyncio.CancelledError:
            if "process" in locals():
                await _terminate(process)
            raise
        except Exception as exc:
            text = str(exc)
            status = "rate_limited" if any(mark in text.lower() for mark in RATE_LIMIT_MARKERS) else "error"
            return RunResult(status=status, result_text=text[-2000:], duration_s=time.time() - started)

        events = CodexEvents()
        malformed: list[str] = []
        if os.environ.get("WORLD_CODEX_DEBUG") == "1":
            print(stdout.decode(errors="replace"), file=sys.stderr)
            print(stderr.decode(errors="replace"), file=sys.stderr)
        for raw in stdout.decode(errors="replace").splitlines():
            try:
                events.feed(json.loads(raw))
            except json.JSONDecodeError:
                malformed.append(raw)
        if on_tool:
            for name, arguments in events.tool_calls:
                await on_tool(name, arguments)
        error_text = "\n".join(events.errors + malformed + [stderr.decode(errors="replace")]).strip()
        combined = "\n".join(filter(None, [events.text, error_text]))
        duration = time.time() - started
        usage = events.usage
        # Codex input includes cached tokens; WORLD's shared counter is fresh input.
        normalized_usage = normalize_usage(usage)
        if any(mark in combined.lower() for mark in RATE_LIMIT_MARKERS):
            status = "rate_limited"
        elif process.returncode != 0 or events.errors or not events.text:
            status = "error"
        else:
            status = "ok"
        return RunResult(
            status=status,
            result_text=combined[-4000:],
            provider="codex",
            duration_s=duration,
            permission_denials=tuple(events.permission_denials),
            **normalized_usage,
        )


__all__ = ["CODEX_TIMEOUT_SECONDS", "CodexEvents", "CodexRunner", "build_command"]
