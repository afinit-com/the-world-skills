#!/usr/bin/env python3
"""Codex PreToolUse backstop for headless WORLD sessions.

The primary boundary is CodexRunner's read-only sandbox plus its per-agent MCP
allowlist. This hook fails closed if a future runner re-enables ``apply_patch`` or Bash.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path.cwd().resolve()
# Kept one-per-column rather than as a sentence to split(): this is an allowlist read by
# eye when deciding whether a subcommand belongs in it, and every entry has to be its own
# thing to scan. Do not let a formatter reflow it onto one line.
READ_ONLY_GIT = frozenset((
    "blame", "cat-file", "check-ignore", "cherry", "count-objects", "describe",
    "diff", "diff-index", "diff-tree", "fsck", "grep", "help", "log", "ls-files",
    "ls-remote", "ls-tree", "merge-base", "name-rev", "rev-list", "rev-parse",
    "shortlog", "show", "show-ref", "status", "var", "verify-pack", "version",
    "whatchanged",
))
GIT = re.compile(r"(?:^|[;&|`]|\$\()\s*git\b(?:\s+-\S+(?:\s+\S+)?)*\s+([a-zA-Z][\w-]*)")
PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
MOVE_PATH = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    raise SystemExit(0)


def tables() -> list[tuple[Path, dict]]:
    candidates = [REPO_ROOT]
    harnesses = REPO_ROOT / "harnesses"
    if harnesses.is_dir():
        candidates.extend(path for path in harnesses.iterdir() if path.is_dir())
    found = []
    for root in candidates:
        try:
            table = json.loads((root / ".the-world" / "agent-scope.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found.append((root, table))
    return found


def patch_paths(command: str) -> list[Path]:
    raw_paths = PATCH_PATH.findall(command) + MOVE_PATH.findall(command)
    result = []
    for raw in raw_paths:
        path = Path(raw.strip())
        result.append((path if path.is_absolute() else REPO_ROOT / path).resolve(strict=False))
    return result


def is_protected(path: Path) -> bool:
    if path.name == "ledger.jsonl":
        return True
    protected = tuple(item.resolve(strict=False) for item in (
        REPO_ROOT / ".codex" / "hooks",
        REPO_ROOT / ".codex" / "hooks.json",
        REPO_ROOT / ".claude" / "hooks",
        REPO_ROOT / ".claude" / "settings.json",
    ))
    return any(path == item or path.is_relative_to(item) for item in protected)


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
    headless = os.environ.get("WORLD_HEADLESS") == "1"
    agent = os.environ.get("WORLD_AGENT")
    try:
        payload = json.load(sys.stdin.buffer)
    except Exception:
        if headless:
            deny("world-guard: malformed hook input in a headless session")
        return
    tool = payload.get("tool_name", "")
    command = str((payload.get("tool_input") or {}).get("command", ""))

    if tool == "Bash" and headless:
        for match in GIT.finditer(command):
            if match.group(1) not in READ_ONLY_GIT:
                deny(f"world-guard: headless git write denied: git {match.group(1)}")
        # Both halves must hit: a path that is enforcement or ledger, AND a verb that
        # could rewrite it. Either alone is a read.
        if re.search(
            r"(?:\.claude|\.codex)/(?:hooks|settings|hooks\.json)|ledger\.jsonl", command
        ) and re.search(r"\b(?:rm|mv|cp|sed|tee|truncate|python|perl|ruby|node)\b|>>?", command):
            deny("world-guard: enforcement or ledger mutation denied")
        return

    if tool != "apply_patch" or not agent:
        return
    targets = patch_paths(command)
    if not targets:
        deny("world-guard: could not determine apply_patch targets; refusing fail-closed")
    scope_tables = tables()
    if not scope_tables:
        deny("world-guard: no generated scope table exists for this WORLD agent")
    scope_root, entry = next(
        ((root, table[agent]) for root, table in scope_tables if agent in table),
        (REPO_ROOT, {"write_scope": []}),
    )
    allowed = [(scope_root / rel).resolve(strict=False)
               for rel in _expand_scope(entry.get("write_scope", []))]
    for target in targets:
        if is_protected(target):
            deny(f"world-guard: protected path mutation denied: {target}")
        if not any(target == root or target.is_relative_to(root) for root in allowed):
            deny(f"world-guard: '{agent}' may not write outside its declared scope: {target}")


if __name__ == "__main__":
    main()
