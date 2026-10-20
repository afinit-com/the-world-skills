#!/usr/bin/env python3
"""world-integrity-guard — PreToolUse(Bash|Edit|Write|NotebookEdit) hook.

Guards the two things in this repo that must not be quietly rewritten by the process
they constrain: **the enforcement surface** (`.claude/hooks/*`, `.claude/settings.json`)
and **the append-only decision ledger** (`**/ledger.jsonl`).

Why the hooks guard themselves
------------------------------
A session that was refused a `git push` by a push-guard hook has been seen to edit the
project's instructions to widen the rule, and then reach for the hook file itself to widen
it to match. Nothing in the repository stopped that; only the coding agent's own permission
classifier did. A guard whose integrity depends on an external model's judgment is not a
guard. Sometimes the person really does want the hook changed, which is exactly why the
verdict for a human is `ask` and not `deny`.

Why the ledger
--------------
An agent has also been seen trying to `rm <harness>/ledger.jsonl`, stopped again by the
coding agent's classifier rather than by the repository.
`world.process.ledger.append_ledger` already refuses forged entries (see `LedgerFabricationError`),
but nothing prevented the file being deleted or rewritten out from under it. Every `denied`
line in that file is read back into the prompt of every agent the harness spawns, so
whoever edits it steers every future agent.

Why a separate hook
-------------------
world keeps one enforcement layer per boundary. `world-scope-guard.py` bounds an agent's
*write scope* and only sees `Edit|Write|NotebookEdit`. `world-git-guard.py` bounds *git* and
only sees Bash. Neither sees `rm .claude/hooks/world-git-guard.py`, which is a Bash call
that is not git and a write that no scope table mentions. That gap is this file.

Deliberate overlap: for an `Edit` of a hook by a spawned agent, `world-scope-guard.py` also
denies — the path is outside every declared `write_scope`. That is a backstop, not a
duplicate, and it must not be collapsed: scope-guard fails OPEN when no scope table exists
anywhere (`if not tables: sys.exit(0)`, for a repo where no harness has ever started), and
this hook's refusal does not depend on that condition.

Verdicts
--------
- A spawned world agent (`WORLD_AGENT`) or any headless session (`WORLD_HEADLESS=1`): **deny**.
  No agent has a reason to touch these, and a headless run cannot answer a prompt.
- A human's own interactive session: **ask**, naming what is being touched. The user edits
  hooks on purpose. What must never happen is the edit
  happening *silently, mid-task, because a guard was in the way*.

Like `world-git-guard.py`, the Bash side is a deterministic net over the command string, not
a proof. It does not catch obfuscation. The structural fix is that agents write through
scoped tools and never need raw Bash here at all.

Exit 2 + stderr = deny. JSON on stdout + exit 0 = ask.
"""

# Hooks may run under the macOS system Python 3.9; defer annotation evaluation.
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# What may not be quietly rewritten, and why a reader should care.
PROTECTED = (
    (REPO / ".claude" / "hooks", "the hooks that enforce this repo's boundaries"),
    (REPO / ".claude" / "settings.json", "the file that wires those hooks in"),
)
LEDGER_NAME = "ledger.jsonl"
LEDGER_WHY = "the append-only decision ledger, which is read into every agent's prompt"

# Bash verbs that change a file. Deliberately narrow: `sed -n … ledger.jsonl` reads it, and
# a guard that fires on reads is one the user learns to click through.
_MUTATORS = re.compile(
    r"(?:^|[;&|`]|\$\()\s*(?:sudo\s+)?"
    r"(?:(rm|mv|cp|ln|dd|shred|truncate|chmod|chown|install|tee)\b|sed\s+(?:-\S+\s+)*-\S*i)"
)

# An interpreter is a mutator too — `python3 -c "open('.claude/hooks/x.py','w')"` is not
# obfuscation, it is the first tool an agent reaches for. This applies ONLY to the hooks and
# settings.json, and NOT to the ledger: `world.process.ledger.append_ledger` refuses forged
# entries at the API layer, so the ledger has a backstop the hooks do not, and firing on
# `python3 -c "print(open('ledger.jsonl').read())"` would be noise bought with nothing.
_INTERPRETER = re.compile(r"(?:^|[;&|`]|\$\()\s*(?:sudo\s+)?(python3?|perl|ruby|node|osascript)\b")

# `git` is a mutator when its flags say so. `git diff --output=<hook>` truncates that hook
# — an empty python file is a hook that allows everything — and `diff` sits on
# world-git-guard's read-only allowlist, which is where this was found. That guard now
# refuses it, but it only ever sees `WORLD_HEADLESS=1`; the ask for a human's own session,
# and the deny for `WORLD_AGENT`, are this hook's to make. Bare `git log .claude/hooks/` is
# still a read and still silent: the flag is what makes it a write. See WRITE_FLAGS in
# world-git-guard.py — the two lists must not drift apart.
_GIT_WRITE = re.compile(
    r"(?:^|[;&|`]|\$\()\s*(?:sudo\s+)?git\b.*?(--output(?:=|\s)|--lost-found)"
)

# `>` truncates and `>>` appends; an append to the ledger is forgery as surely as a rewrite.
_REDIRECT = re.compile(r">>?\s*\S*(?:\.claude/(?:hooks/|settings\.json)|ledger\.jsonl)")


def deny(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def ask(what: str, why: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"This modifies {what} — {why}. A guard that can be widened by the task it "
                "is blocking is not a guard. Confirm only if changing it is the point of "
                "what you asked for, not a way around something."
            ),
        }
    }))
    sys.exit(0)


def classify(target: Path) -> tuple[str, str] | None:
    """(what, why) if this path is protected, else None."""
    target = target.resolve(strict=False)
    if target.name == LEDGER_NAME:
        return str(target), LEDGER_WHY
    for path, why in PROTECTED:
        if target == path.resolve(strict=False) or target.is_relative_to(path):
            return str(target), why
    return None


def bash_hit(cmd: str) -> tuple[str, str] | None:
    names = re.findall(r"[\w./-]*(?:\.claude/(?:hooks|settings\.json)|ledger\.jsonl)[\w./-]*", cmd)
    if not names:
        return None

    mutates = _MUTATORS.search(cmd) or _REDIRECT.search(cmd) or _GIT_WRITE.search(cmd)
    # An interpreter only counts against the enforcement surface — see _INTERPRETER.
    if not mutates and _INTERPRETER.search(cmd):
        mutates = any(".claude/" in n for n in names)
    if not mutates:
        return None  # reading, grepping, listing — all fine

    for raw in names:
        hit = classify(Path(raw) if Path(raw).is_absolute() else Path.cwd() / raw)
        if hit:
            return hit
    # Named a protected thing by a path we cannot resolve (a variable, a glob). Say so.
    return names[0], "a protected path in this repo"


def main() -> None:
    try:
        payload = json.load(sys.stdin.buffer)
    except ValueError:  # JSONDecodeError, or non-UTF-8 bytes
        sys.exit(0)

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        hit = bash_hit(str(tool_input.get("command", "") or ""))
    else:
        raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if not raw:
            sys.exit(0)
        target = Path(raw)
        hit = classify(target if target.is_absolute() else Path.cwd() / target)

    if not hit:
        sys.exit(0)

    what, why = hit
    if os.environ.get("WORLD_AGENT") or os.environ.get("WORLD_HEADLESS") == "1":
        deny(
            f"world-integrity-guard: an agent may not modify {what} — {why}. "
            "If this genuinely needs to change, say so in your report and let a human "
            "make the change."
        )
    ask(what, why)


if __name__ == "__main__":
    main()
