#!/usr/bin/env python3
"""durable-config-guard — PreToolUse(Edit|Write|NotebookEdit) hook.

Asks before a session working inside this repo writes to a config file that outlives
it — `~/.claude/settings.json`, a shell rc, the global CLAUDE.md. These are machine-
wide and permanent, and nothing in the tool call says so: writing `~/.claude/settings.json`
looks exactly like writing `README.md`.

A session asked to change a setting "for a bit" can add it to the user's global
settings.json — a durable, every-project, every-future-session change — and report
success, when setting the variable for one run would have done the job and touched
nothing.

world-scope-guard.py cannot cover this: it returns immediately when WORLD_AGENT is unset, which
is precisely the human's own interactive session — the one with the reach to edit a global
file. Different boundary, different hook (world keeps one enforcement layer per boundary).

The verdict is `ask`, never `deny`. The user edits these files on purpose all the time
(token storage, permissions). What went wrong was not the write; it was the write happening
silently, unlabeled, on behalf of a request that never implied permanence. So the hook makes
the permanence visible and lets the human answer. Exit 0 always: `ask` is carried in the
JSON, and a hook that crashed the tool call would be worse than the bug it prevents.
"""

# Hooks may run under the macOS system Python 3.9; defer annotation evaluation.
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path.home()

# Durable, machine-wide, and edited-by-mistake in the same shape as the case above.
# Not a general "outside the repo" rule: a session legitimately writes scratch files,
# clones, and other repos. It is *config that changes future sessions* that needs a human.
DURABLE = {
    HOME / ".claude" / "settings.json": "every Claude Code session on this machine",
    HOME / ".claude" / "settings.local.json": "every Claude Code session on this machine",
    HOME / ".claude" / "CLAUDE.md": "every Claude Code session on this machine",
    HOME / ".claude" / "keybindings.json": "every Claude Code session on this machine",
    HOME / ".zshrc": "every new shell",
    HOME / ".zshenv": "every new shell",
    HOME / ".bashrc": "every new shell",
    HOME / ".bash_profile": "every new shell",
    HOME / ".profile": "every new shell",
}


def ask(target: Path, blast_radius: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": (
                        f"{target} is durable config — this change persists for "
                        f"{blast_radius}, not just this session. If the need is "
                        f"temporary, an env prefix (`VAR=value claude`) or "
                        f"`claude --settings '{{...}}'` does the same job and touches "
                        f"no file. Confirm only if a permanent change is intended."
                    ),
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    # An world poll-agent's writes are world-scope-guard's boundary, and it denies outright.
    # Also: a spawned agent is headless and cannot answer an `ask`.
    if os.environ.get("WORLD_AGENT"):
        sys.exit(0)

    try:
        payload = json.load(sys.stdin.buffer)
    except ValueError:  # JSONDecodeError, or non-UTF-8 bytes
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not raw:
        sys.exit(0)

    target = Path(raw)
    if not target.is_absolute():
        target = Path.cwd() / target
    # A symlink from inside the repo to ~/.claude/settings.json is still a write to
    # ~/.claude/settings.json. resolve() before comparing, strict=False for new files.
    target = target.resolve(strict=False)

    for path, blast_radius in DURABLE.items():
        if target == path.resolve(strict=False):
            ask(target, blast_radius)

    sys.exit(0)


if __name__ == "__main__":
    main()
