"""Claude Agent SDK execution adapter.

All Claude-specific message types, option names, plugin wiring, and tool conversion live
here. The orchestrator sees only the provider-neutral ``AgentRunner`` and ``RunResult``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)
from claude_agent_sdk import (
    tool as claude_tool,
)

from .base import ToolCallback
from ..context import current_task
from ..env import sdk_env
from ..tools.spec import ToolSpec
from ..types import Agent, RunResult

if TYPE_CHECKING:
    from ..harness import Harness

RATE_LIMIT_MARKERS = ("rate_limit", "429", "overloaded_error", "usage limit")
CLAUDE_TIMEOUT_SECONDS = 20 * 60

BASE_TOOLS: list[str] = []
BASH_TOOL = "Bash"
SUBAGENT_TOOL = "Agent"
SKILL_TOOL = "Skill"


def _as_claude_tool(candidate):
    """Convert a provider-neutral tool while accepting legacy SDK tool objects."""

    if not isinstance(candidate, ToolSpec):
        return candidate
    return claude_tool(candidate.name, candidate.description, candidate.input_schema)(
        candidate.handler
    )


def make_claude_tool_server(name: str, tools: list):
    """Build the in-process MCP server used by ``ClaudeAgentOptions``."""

    converted = [_as_claude_tool(candidate) for candidate in tools]
    server = create_sdk_mcp_server(name=name, version="1.0.0", tools=converted)
    allowed = [f"mcp__{name}__{candidate.name}" for candidate in tools]
    return server, allowed


def _fmt_denial(denial) -> str:
    name = getattr(denial, "tool_name", None) or (
        denial.get("tool_name") if isinstance(denial, dict) else "?"
    )
    tool_input = getattr(denial, "tool_input", None) or (
        denial.get("tool_input") if isinstance(denial, dict) else {}
    )
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input)
    return f"{name}:{str(command)[:140]}" if command else str(denial)[:140]


def _usage_fields(result_msg: ResultMessage | None) -> dict:
    if result_msg is None:
        return {}
    usage = getattr(result_msg, "usage", None) or {}
    # model_usage is {model_id: per-model usage}. Read the KEY rather than echoing back
    # agent.model: this is what was billed, so a fallback or an override is visible.
    # More than one key means the run genuinely spanned models — say so.
    billed = sorted((getattr(result_msg, "model_usage", None) or {}).keys())
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cost_usd": float(getattr(result_msg, "total_cost_usd", None) or 0.0),
        "model": "+".join(_short_model(m) for m in billed),
    }


def _short_model(model_id: str) -> str:
    """`claude-sonnet-4-5-20250929` → `sonnet-4-5`. The footer is read at a glance in one
    Slack line; the vendor prefix and date stamp carry nothing a reader wants there."""
    parts = model_id.split("/")[-1].removeprefix("claude-").split("-")
    while parts and parts[-1].isdigit() and len(parts[-1]) == 8:  # trailing YYYYMMDD
        parts.pop()
    return "-".join(parts) or model_id


def _build_options(
    harness: "Harness", agent: Agent, cwd: str | None = None
) -> ClaudeAgentOptions:
    """Build the historical least-privilege Claude tool surface.

    `cwd` is a mutates:true run's own worktree (spawn.py); everything else starts at
    git_root. Project and local settings — the guard hooks among them — are read from
    wherever the session starts, which is why worktrees.run_worktree refuses a worktree
    whose committed copy is missing the hook registry.
    """

    mcp_servers: dict = {}
    allowed_tools: list[str] = []
    if agent.tools:
        server, allowed_tools = make_claude_tool_server(harness.name, agent.tools)
        mcp_servers[harness.name] = server

    base_tools = list(BASE_TOOLS)
    if agent.allow_bash:
        base_tools.append(BASH_TOOL)
    if agent.task_allowed:
        base_tools.append(SUBAGENT_TOOL)

    plugin_kwargs: dict = {}
    if agent.allow_plugins and harness.plugins:
        from ..process import plugins as _plugins

        if _plugins.sdk_supports_plugins():
            configs = _plugins.plugin_configs(harness)
            if configs:
                base_tools.append(SKILL_TOOL)
                plugin_kwargs["plugins"] = configs
        else:
            harness.log(
                f"{agent.name}: allow_plugins set but this SDK has no plugins support — ignored"
            )

    return ClaudeAgentOptions(
        cwd=str(cwd or harness.git_root),
        model=agent.model or harness.model,
        permission_mode="acceptEdits",
        setting_sources=["user", "project", "local"],
        strict_mcp_config=True,
        # The SDK merges this onto os.environ, so sdk_env() blanks every harness
        # secret explicitly — the CLI, its Bash and its plugin MCP servers never see them.
        env=sdk_env("claude", {"WORLD_HEADLESS": "1", "WORLD_AGENT": agent.name,
                               "WORLD_TASK": current_task()}),
        mcp_servers=mcp_servers,
        tools=base_tools,
        allowed_tools=allowed_tools + base_tools,
        disallowed_tools=(
            ([] if agent.allow_bash else [BASH_TOOL])
            + ([] if agent.task_allowed else [SUBAGENT_TOOL])
            + ([] if SKILL_TOOL in base_tools else [SKILL_TOOL])
        ),
        **plugin_kwargs,
    )


async def _consume_stream(
    prompt: str,
    options: ClaudeAgentOptions,
    on_tool: ToolCallback | None,
) -> tuple[ResultMessage | None, bool]:
    result_msg: ResultMessage | None = None
    saw_rate_limit = False
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            if message.error == "rate_limit":
                saw_rate_limit = True
            if on_tool:
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        await on_tool(block.name, block.input or {})
        if isinstance(message, ResultMessage):
            result_msg = message
    return result_msg, saw_rate_limit


class ClaudeRunner:
    """Run agents through the Claude Agent SDK without leaking SDK types to core."""

    provider = "claude"

    def __init__(self, harness: "Harness", *, timeout_s: int = CLAUDE_TIMEOUT_SECONDS) -> None:
        self.harness = harness
        self.timeout_s = timeout_s

    async def run(
        self,
        agent: Agent,
        prompt: str,
        *,
        on_tool: ToolCallback | None = None,
        cwd: str | None = None,
    ) -> RunResult:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return RunResult(
                status="error",
                result_text=(
                    "ClaudeRunner refuses ANTHROPIC_API_KEY: unset it to use the "
                    "Claude CLI subscription login configured for this harness"
                ),
            )
        options = _build_options(self.harness, agent, cwd)
        started = time.time()
        try:
            result_msg, saw_rate_limit = await asyncio.wait_for(
                _consume_stream(prompt, options, on_tool), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            return RunResult(
                status="error", result_text="timed out", duration_s=time.time() - started
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # SDK streams can raise bare Exception
            text = str(exc)
            status = (
                "rate_limited"
                if any(marker in text.lower() for marker in RATE_LIMIT_MARKERS)
                else "error"
            )
            return RunResult(
                status=status, result_text=text[-2000:], duration_s=time.time() - started
            )

        duration = time.time() - started
        text = (result_msg.result or "") if result_msg else ""
        usage = _usage_fields(result_msg)
        if saw_rate_limit or any(marker in text.lower() for marker in RATE_LIMIT_MARKERS):
            return RunResult(
                status="rate_limited", result_text=text[-2000:], duration_s=duration, **usage
            )
        if result_msg is None or result_msg.is_error:
            errors = "; ".join(result_msg.errors or []) if result_msg else "no ResultMessage"
            return RunResult(
                status="error",
                result_text=(text or errors)[-2000:],
                duration_s=duration,
                **usage,
            )
        denials = tuple(_fmt_denial(item) for item in (result_msg.permission_denials or []))
        return RunResult(
            status="ok",
            result_text=text[:4000],
            duration_s=duration,
            permission_denials=denials,
            **usage,
        )


__all__ = [
    "BASE_TOOLS",
    "BASH_TOOL",
    "CLAUDE_TIMEOUT_SECONDS",
    "ClaudeRunner",
    "SKILL_TOOL",
    "SUBAGENT_TOOL",
    "_build_options",
    "make_claude_tool_server",
]
