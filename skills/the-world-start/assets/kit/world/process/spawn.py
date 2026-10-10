"""Provider-neutral one-shot agent execution and completion enforcement.

The provider runner decides how a model is invoked and normalizes its result. This module
owns the WORLD contract that is independent of any provider: dry-run behavior, prompt
composition, the rule that exit success is not completion when an agent promised a
finding file, and the rule that a mutates:true agent runs in its own worktree or not at
all.

``_build_options`` remains as a compatibility import for existing smoke checks; new code
must configure a runner instead of reaching into provider options.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from . import worktrees
from .state import save_state
from .task import read as read_task
from .task import task_work_dir
from ..context import current_task, reset_current_worktree, set_current_worktree
from ..runners.base import ToolCallback
from ..types import Agent, RunResult, voice_clause

if TYPE_CHECKING:
    from ..harness import Harness


def _snapshot_outputs(harness: "Harness", agent: Agent) -> set[str]:
    """Snapshot every file satisfying a writes-finding completion contract.

    The task's own work/ is the place a run writes now; logs/ and findings/ stay
    readable so a harness that has not moved its agents' write_scope yet still
    completes. Both are relative to the harness home, so the paths returned here are
    the same ones RunResult.artifacts carries to the announcer.
    """

    names: set[str] = set()
    task_id = current_task()
    if task_id:
        directory = task_work_dir(harness.home, task_id)
        if directory is not None and directory.exists():
            rel = directory.relative_to(harness.home)
            names |= {f"{rel}/{path.name}" for path in directory.iterdir()}
    for root in (harness.logs_dir, harness.findings_dir):
        directory = root / agent.name
        if directory.exists():
            names |= {f"{root.name}/{path.name}" for path in directory.iterdir()}
    return names


def compose_prompt(harness: "Harness", agent: Agent, trigger_prompt: str, context: str) -> str:
    """Compose standing instructions, persona, trigger payload, and recent context."""

    parts: list[str] = []
    if agent.prompt_file:
        path = harness.home / agent.prompt_file
        if path.exists():
            parts.append(path.read_text(encoding="utf-8").strip())
    if agent.persona and agent.persona.flavor:
        parts.append(f"You speak as {agent.persona.flavor}")
    parts.append(voice_clause(agent.persona))
    parts.append(trigger_prompt)
    parts.append(context)
    return "\n\n".join(part for part in parts if part)


def enforce_completion(
    harness: "Harness",
    agent: Agent,
    result: RunResult,
    before: set[str] | None,
) -> RunResult:
    """Apply WORLD's file-based completion and permission-denial contract."""

    if result.status != "ok":
        return result

    produced: set[str] = set()
    if before is not None:
        produced = _snapshot_outputs(harness, agent) - before
        result = replace(result, artifacts=tuple(sorted(produced)))
    wrote = bool(produced)
    if result.permission_denials and not wrote:
        denied = ", ".join(result.permission_denials[:3])
        return replace(
            result,
            status="blocked",
            result_text=f"permission denials: {denied} | {result.result_text[:1000]}",
        )
    if before is not None and not wrote:
        return replace(
            result,
            status="blocked",
            result_text=(
                f"exited ok but wrote no file under this task's work/ (nor "
                f"logs/{agent.name}/ or findings/{agent.name}/) | {result.result_text[:1000]}"
            ),
        )
    if result.permission_denials:
        harness.log(
            f"{agent.name} completed with {len(result.permission_denials)} permission "
            f"denial(s) — allowlist tuning candidates: "
            f"{', '.join(result.permission_denials[:5])}"
        )
    return result


async def run_agent_oneshot(
    harness: "Harness",
    agent: Agent,
    prompt: str,
    *,
    dry_run: bool = False,
    on_tool: ToolCallback | None = None,
    state: dict | None = None,
) -> RunResult:
    """Run one agent through its harness runner and verify real completion.

    `state` is needed only by a mutates:true agent, whose worktree is recorded there so
    the sweep and the keep/remove buttons can reach it. Without it such a run refuses.
    """

    if dry_run:
        where = " in its own worktree" if agent.mutates else ""
        harness.log(f"[dry-run] would spawn {agent.name}{where}: {prompt.splitlines()[0]!r}")
        return RunResult(status="skipped")

    # mutates:true is enforced HERE, above the provider, so it holds for every runner:
    # the run is handed its own worktree as cwd, or it does not start. There is no third
    # outcome where it runs at git_root — see worktrees.run_worktree.
    run_kwargs: dict = {}
    worktree_token = None
    if agent.mutates:
        task_id = current_task()
        record = read_task(harness.home, task_id) if task_id else None
        try:
            workdir = await worktrees.run_worktree(
                harness, state, agent.name, task_id, record.thread if record else ""
            )
        except worktrees.WorktreeRefusedError as exc:
            harness.log(f"{agent.name}: refused to spawn (mutates=True): {exc}")
            return RunResult(
                status="blocked",
                result_text=f"refused to run {agent.name} outside its own worktree: {exc}",
            )
        finally:
            # Saved now, not after the run: a crash mid-run must not leave a worktree the
            # sweep has never heard of.
            if state is not None and state.get("worktrees"):
                save_state(harness, state)
        run_kwargs["cwd"] = str(workdir)
        worktree_token = set_current_worktree(str(workdir))

    try:
        before = _snapshot_outputs(harness, agent) if agent.writes_report else None
        # `cwd` is passed only when there is one. A runner that does not take it fails
        # with a TypeError — loud, and still not in the main checkout — instead of
        # silently spawning at git_root, which is what ignoring it would amount to.
        result = await harness.agent_runner().run(agent, prompt, on_tool=on_tool, **run_kwargs)
    finally:
        if worktree_token is not None:
            reset_current_worktree(worktree_token)
    # Stamped here, not in the runner: how many tools the agent HOLDS is a property of
    # the spawn surface (agent.tools is the whole of it — the base tool set is empty by
    # design), while a runner's job is reading the outcome.
    result = replace(result, tools_available=len(agent.tools))
    return enforce_completion(harness, agent, result, before)


def _build_options(harness: "Harness", agent: Agent):
    """Compatibility shim for the former Claude-specific private helper."""

    from ..runners.claude import _build_options as build_claude_options

    return build_claude_options(harness, agent)


__all__ = [
    "_build_options",
    "compose_prompt",
    "enforce_completion",
    "run_agent_oneshot",
]
