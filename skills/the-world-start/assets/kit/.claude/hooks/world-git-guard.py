#!/usr/bin/env python3
"""world-git-guard — PreToolUse(Bash) hook.

Blocks git write subcommands run via Bash in ANY headless world
session (poll agent or Slack mention) — WORLD_HEADLESS=1 is set for both by
world/process/spawn.py, regardless of which specific agent is running.

This is a deterministic net, not a proof: literal-pattern matching over the
Bash command string. It does NOT catch obfuscation (base64, a different
interpreter shelling out to git, a helper script). The real structural fix is
giving mutates=True agents scoped git/PR tools (a harness defines them with
ToolSpec, like any other tool) and never letting them need raw git via Bash at all;
this hook is the safety net for the gap between "the tool exists" and "the
agent always uses it."

A human's interactive session is exempt: every git write is theirs to make. Asking a
human before each `git commit` onto the default branch was tried and dropped: committing
to the trunk is a normal workflow, and a prompt on every commit is noise people learn to
click through.

Exit 2 + stderr = block.
"""
# Hooks may run under the macOS system Python 3.9; defer annotation evaluation.
from __future__ import annotations

import json
import os
import re
import sys

# An ALLOWLIST of subcommands that do not change anything by themselves.
#
# This used to be a denylist — `add|commit|push|rm|mv|reset|checkout|restore|
# clean|merge|rebase|cherry-pick|revert|tag|stash|submodule|filter-branch|worktree` — and
# a probe walked straight through ten holes it never named, `git branch -D main` and
# `git update-ref refs/heads/main HEAD~5` among them. Both destroy history; neither is an
# exotic command.
#
# spawn.py had already learned this and written it down (see BASE_TOOLS): "A denylist
# cannot satisfy 'no built-in tools' — it is a promise to have enumerated a set that grows.
# Do not reintroduce a denylist as the primary control." It was reintroduced here.
#
# So: unknown subcommand = write = blocked. `git` grows a new verb, and the default is no.
# Entries that read by default but write under a flag — `branch -D`, `remote add`,
# `config --global`, `reflog delete`, `bisect start`, `notes add`, `symbolic-ref` — are
# deliberately ABSENT. An agent needing one of those needs a human.
# SIM905 (a list literal instead of .split()) would turn this into twenty-odd quoted
# strings and a comma per line. The point of this block is that a human can read the
# whole allowlist at a glance and see what is NOT in it.
READ_ONLY_SUBCOMMANDS = frozenset("""
    blame cat-file check-ignore cherry count-objects describe diff diff-index diff-tree
    fsck grep help log ls-files ls-remote ls-tree merge-base name-rev rev-list rev-parse
    shortlog show show-ref status var verify-pack version whatchanged
""".split())  # noqa: SIM905

# This comment used to read "…that cannot change anything, ever, under any flag." It was
# false, and the sentence is what stopped anyone from checking. `git diff --output=FILE`
# writes FILE — with no diff to render it writes an empty one — and `diff` is on the list
# above. So a headless agent could truncate `.claude/hooks/world-git-guard.py`, this file,
# the one refusing it a `git commit` a moment earlier. `show` and `log -p` take the same
# diff option; `fsck --lost-found` writes into `.git/`; `grep -O` runs a pager.
#
# An allowlist over verbs is not an allowlist over commands. A verb is read-only; an
# invocation is read-only only if its flags are too.
WRITE_FLAGS = re.compile(
    r"--output(?:=|\s|$)"       # diff/show/log/format-patch: writes the named file
    r"|--lost-found"            # fsck: writes .git/lost-found/
    r"|--open-files-in-pager"   # grep: runs a program
    r"|(?<!\S)-O(?:\s|=|$)"     # grep -O: same, short form
)

# Every `git …` invocation in the command string, with its subcommand. The leading anchor
# keeps `grep "git push" f` and `echo "run git commit"` from matching: a quote is not a
# command separator.
GIT_INVOCATION_RE = re.compile(
    r"(?:^|[;&|`]|\$\()\s*git\b(?:\s+-\S+(?:\s+\S+)?)*\s+([a-zA-Z][\w-]*)"
)
SEPARATOR_RE = re.compile(r"[;&|`\n]")


def _arguments_of(cmd: str, after: int) -> str:
    """Everything the invocation starting at `after` passes to git, up to the next
    command separator. The flags live here, and so does the hole this guard missed."""
    end = SEPARATOR_RE.search(cmd, after)
    return cmd[after : end.start() if end else len(cmd)]


def first_write(cmd: str) -> str | None:
    """Why the first non-read-only `git …` in `cmd` is a write. None when all are reads."""
    for m in GIT_INVOCATION_RE.finditer(cmd):
        sub = m.group(1)
        if sub not in READ_ONLY_SUBCOMMANDS:
            return f"`git {sub}` is not one of them"
        flag = WRITE_FLAGS.search(_arguments_of(cmd, m.end()))
        if flag:
            return (
                f"`git {sub}` reads, but `{flag.group(0).strip()}` makes it write — "
                "a read-only subcommand is not a read-only command"
            )
    return None


def block(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def strip_nonexec_text(cmd: str) -> str:
    """Drop heredoc bodies and -m/--message string contents — a commit message
    that happens to mention 'git commit' in prose must not trigger the guard."""
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?\s*\n.*?\n\s*\1\b", " HEREDOC ", cmd, flags=re.S)
    cmd = re.sub(r"(-m|--message)(\s+|=)(\"(\\.|[^\"\\])*\"|'[^']*')", r"\1 MSG", cmd)
    return cmd


def main() -> None:
    try:
        data = json.load(sys.stdin.buffer)
    except Exception:
        sys.exit(0)
    cmd = strip_nonexec_text((data.get("tool_input") or {}).get("command", "") or "")

    if os.environ.get("WORLD_HEADLESS") != "1":
        sys.exit(0)  # human's own interactive session — not this hook's business

    reason = first_write(cmd)
    if reason:
        block(
            f"world-git-guard: headless world sessions can only run git commands that cannot "
            f"change anything, and {reason}. Use the git/PR tool this harness gave you "
            "instead — if you don't have one for this, report it as a finding and let a "
            "human make the change."
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
