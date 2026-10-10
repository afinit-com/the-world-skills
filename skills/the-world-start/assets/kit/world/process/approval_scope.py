"""What an approval actually covers — the changed files under a thread's worktree.

The gap this closes: a grant used to mean "the approver clicked approve in thread T",
and `thread_approved()` asked exactly one question — is T in the grants dict. Nothing
looked at WHAT was approved. So an approval survived its own subject changing: the
approver says yes to a migration that adds a column, the agent regenerates the file
before the gated tool fires, and the tool runs against whatever is on disk now. The
human approved one thing and the harness did another, with no error anywhere.

A grant is therefore fingerprinted at approve time and re-checked at spend time.

Why the scope is computed HERE and not declared by the tool: the approval is requested
BEFORE any tool is called — the agent ends its turn with `APPROVAL_REQUEST:` and stops,
so at grant time no tool has run and there are no arguments to read. The harness must
derive the scope from something it can see on its own. That is the agent's worktree,
which is also the only place a mutating agent is allowed to write (see worktrees.py).

Deriving it from the repository, rather than from anything the agent says it will do,
is the point: a scope the agent declares is a promise in a prompt, which is the failure
mode this whole kit exists to avoid.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .task import compute_scope_hash

MAX_LISTED = 8
GIT_TIMEOUT_S = 30


def changed_files(worktree: Path) -> list[str]:
    """Paths git reports as changed in `worktree`, relative to it. Untracked included —
    a brand-new migration file is exactly the kind of thing being approved.

    Empty on any failure (not a repo, git missing, timeout). The caller reads an empty
    scope as "nothing to pin", which leaves behavior exactly as it was before this
    module existed rather than blocking work on a git hiccup.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z",
             "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []

    out: list[str] = []
    fields = [f for f in done.stdout.split("\0") if f]
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        out.append(path)
        # A rename or copy spends a SECOND NUL field on the source path. Not consuming
        # it would read that bare path as the next entry and slice its first 3 chars off
        # as a status code, producing a garbage filename in the fingerprint.
        if status[0] in ("R", "C") or status[1] in ("R", "C"):
            index += 1
    return sorted(out)


def fingerprint(worktree: Path | None) -> tuple[str, list[str]]:
    """(scope_hash, changed paths). ("", []) when there is nothing to pin."""
    if worktree is None or not Path(worktree).is_dir():
        return "", []
    files = changed_files(Path(worktree))
    if not files:
        return "", []
    return compute_scope_hash(Path(worktree), files, []), files


def describe(files: list[str]) -> str:
    """The line a human reads under the approval buttons. Names, not a hash: the
    fingerprint is for the code to compare, the file list is what makes the ask
    reviewable."""
    if not files:
        return ""
    shown = ", ".join(f"`{f}`" for f in files[:MAX_LISTED])
    if len(files) > MAX_LISTED:
        shown += f" 외 {len(files) - MAX_LISTED}개"
    return f"대상 {len(files)}개 파일: {shown}"
