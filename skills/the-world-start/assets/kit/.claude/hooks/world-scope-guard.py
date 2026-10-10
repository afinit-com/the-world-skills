#!/usr/bin/env python3
"""world-scope-guard — PreToolUse(Edit|Write|NotebookEdit) hook.

BACKSTOP ONLY. world/process/spawn.py sets ClaudeAgentOptions(tools=[]) — an
empty BASE TOOL SET — so a spawned session is offered no built-in tools at all, Write
and Edit included. Verified live by asking a spawned model to enumerate its own
tools. In normal operation this hook never fires: an agent writes through a scoped
tool (make_write_finding and the like in world/toolkit.py) whose roots are bound at
construction, or it doesn't write.

Keep it anyway: it costs nothing, and it still catches the case where someone adds a
built-in back into BASE_TOOLS without thinking it through. Same relationship
world-git-guard.py has to Bash. If you find yourself relying on this hook's denials,
that means the tool surface is wrong — fix the tools, not the hook.

Enforces Write/Edit/NotebookEdit only inside the spawning agent's declared
`write_scope` (see world.types.Agent.write_scope).

Task/Agent (sub-agent spawn) gating is a SEPARATE enforcement layer, on
purpose (world invariant: one enforcement layer per boundary — don't duplicate
the same rule across a hook and an SDK-options callback, since some paths
never invoke the hook at all): `Agent.task_allowed` is enforced directly in
world/process/spawn.py's ClaudeAgentOptions (allowed_tools/disallowed_tools), the same
place `allow_bash` already lives. This hook does not need to know about it.

This hook does NOT import any Python module to learn the scope table. It reads a plain
JSON file (`<harness>/.the-world/agent-scope.json`, shared with the Codex hook)
that Harness.write_agent_scope() regenerates from the live Agent declarations
on every startup. That sidesteps venv import mismatches: Claude
Code invokes hooks with the system `python3`, not the harness's own venv, so a
hook could never `import world` (or `claude_agent_sdk`) even if it wanted to —
stdlib-only JSON avoids the question altogether.

Several harnesses may share one repository, so "the scope file" is not a fixed path.
The hook finds the table that DECLARES this agent, and resolves that agent's
relative scopes against the directory holding it. Keying on the agent rather than on
the hook's own location means it stays correct whichever harness's settings.json
Claude Code decided to load — a thing that depends on cwd and git-root discovery,
and is not worth betting a write boundary on.

If no table anywhere declares the agent, that is not a harness this hook knows and
it BLOCKS: an unrecognized agent name has no declared scope, and "no scope declared"
has always meant "writes nothing". Fail open only when there are no tables at all
(no harness has ever started here).

WORLD_AGENT is set once by the trusted orchestrator (world/process/spawn.py:
run_agent_oneshot) via ClaudeAgentOptions.env at spawn time — nothing inside
the spawned session can rewrite it from the outside (a Bash `export
WORLD_AGENT=x` only affects that Bash call's own child processes, never the CLI
process this hook's env belongs to). No WORLD_AGENT set = not an world poll-agent
session (e.g. a Slack mention session, or a human's own interactive Claude Code
session in this repo) = this hook gets out of the way entirely.

Exit 2 + stderr = block (message is fed back to the agent).
"""
# Hooks may run under the macOS system Python 3.9; defer annotation evaluation.
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIRS = REPO_ROOT / "harnesses"


def scope_tables() -> list[tuple[Path, dict]]:
    """Every harness's scope table, as (harness_dir, table).

    One directory per harness under `harnesses/`, each with its own
    `.the-world/agent-scope.json` that its Harness rewrites on startup. REPO_ROOT itself is
    included for a single-harness project scaffolded straight from the templates,
    where the harness IS the repo."""
    found = []
    candidates = [REPO_ROOT]
    if HARNESS_DIRS.is_dir():
        candidates += [p for p in HARNESS_DIRS.iterdir() if p.is_dir()]
    for harness_dir in candidates:
        path = harness_dir / ".the-world" / "agent-scope.json"
        try:
            found.append((harness_dir, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return found


def block(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


# A scope entry containing this token is per-run: the agent may write only inside the
# work/ of the task it is currently running. WORLD_TASK is injected by the runner from
# the parent process (world.context.current_task) alongside WORLD_AGENT, so it is as
# trustworthy as the agent name this hook already keys off. No WORLD_TASK means no task
# is current, and a task-scoped entry then grants NOTHING rather than the whole tasks/
# tree — the safe direction.
TASK_TOKEN = "{task}"


def _expand_scope(entries):
    task = os.environ.get("WORLD_TASK", "")
    out = []
    for rel in entries:
        if TASK_TOKEN not in rel:
            out.append(rel)
        elif task:
            out.append(rel.replace(TASK_TOKEN, task))
    return out


def main() -> None:
    agent = os.environ.get("WORLD_AGENT")
    if not agent:
        sys.exit(0)  # not an world poll-agent session — nothing to enforce here

    try:
        payload = json.load(sys.stdin.buffer)
    except ValueError:  # JSONDecodeError, or non-UTF-8 bytes
        sys.exit(0)

    tables = scope_tables()
    if not tables:
        sys.exit(0)  # no harness has ever started here — fail open

    # The harness that declares this agent owns it, and its directory is what the
    # agent's relative scopes are relative to. Not declared anywhere = not ours =
    # no scope = writes nothing (the same rule an agent with `write_scope=()` gets).
    scope_root, entry = next(
        ((d, t[agent]) for d, t in tables if agent in t),
        (REPO_ROOT, {"write_scope": [], "task_allowed": False}),
    )
    tool_input = payload.get("tool_input") or {}

    raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not raw:
        sys.exit(0)

    target = Path(raw)
    if not target.is_absolute():
        target = Path.cwd() / target
    # resolve(strict=False): the file may not exist yet (a fresh Write), but any
    # symlink component that DOES exist gets resolved before the containment
    # check — a symlink pointing out of scope can't be used to write through it.
    target = target.resolve(strict=False)

    allowed_rel = _expand_scope(entry.get("write_scope", []))
    for rel in allowed_rel:
        scope_abs = (scope_root / rel).resolve(strict=False)
        if target == scope_abs or target.is_relative_to(scope_abs):
            sys.exit(0)

    scope_desc = ", ".join(allowed_rel) or "(no scope declared)"
    block(
        f"world-scope-guard: '{agent}' may only write inside {scope_desc} — "
        f"denied path: {target}. If a change outside that scope is genuinely "
        "needed, report it as a finding instead and let a human make the change."
    )


if __name__ == "__main__":
    main()
