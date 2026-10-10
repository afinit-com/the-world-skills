"""Worktree lifecycle for mutates:true agents — slug naming, creation, sweep,
origin sync.

Policy:

  merged + clean          -> auto prune (+ notify the owning thread)
  unmerged, idle 7 days   -> ask in the thread (never delete silently)
  asked, +3 days silence  -> force remove (a human reply in between resets the clock)

Caveat: merged-detection is `git log main..branch` ancestry —
squash merges are never detected as merged. Merge commits (or rebase) required for
auto-prune to fire.

sync_with_origin fast-forwards local `default_branch` from origin when that's a
clean, lossless move, and otherwise only surfaces divergence — never auto-merges
(see its own docstring).

run_worktree is where a mutates:true run gets its worktree: spawn.py calls it before
the provider is started, so the boundary is the kit's, not a tool's (see its docstring).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .state import record_finding

if TYPE_CHECKING:
    from ..harness import Harness

IDLE_CONFIRM_S = 7 * 24 * 3600
CONFIRM_GRACE_S = 3 * 24 * 3600

# The guard-hook registries a spawned session loads from its project directory. When the
# cwd moves into a worktree, these are read from the WORKTREE — i.e. from what is
# committed on the base branch — not from the main checkout. See run_worktree.
GUARD_REGISTRIES = (".claude/settings.json", ".codex/hooks.json")


def slugify(text: str, max_words: int = 5) -> str:
    """Deterministic slug from a thread's first message — no LLM call. Non-ASCII
    words drop out; uniqueness comes from the thread_ts suffix, not the slug."""
    words = re.sub(r"[^a-z0-9\s-]", "", text.lower()).split()
    slug = "-".join(words[:max_words])[:40].strip("-")
    return slug if len(slug) >= 3 else "task"


def short_ts(thread_ts: str) -> str:
    return thread_ts.replace(".", "")[-6:]


def _git(harness: "Harness", *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or str(harness.git_root),
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )


async def create_worktree(
    harness: "Harness", slug: str, suffix: str, base_branch: str | None = None
) -> tuple[str, str]:
    """`<worktrees_dir>/<slug>-<suffix>` on branch `worktree-<slug>-<suffix>` off the
    PR base — `origin/<default_branch>`, NOT the local checkout's HEAD. A fix branch
    exists to become a PR against origin/<default_branch>, so that is the base its diff
    must be measured against. Branching off the local default branch instead drags every
    commit the local checkout is ahead of origin into the PR (a worktree forked from a
    local default branch that has drifted ahead of origin sweeps all those commits into
    the fix's diff-vs-origin). A local-only harness (no origin remote) has no remote base,
    so it falls back to the local default branch. Returns (path, branch)."""
    name = f"{slug}-{suffix}"
    path = str(harness.worktrees_dir / name)
    branch = f"worktree-{name}"

    # `base_branch` overrides the default fork point. Needed by any caller whose batches
    # ACCUMULATE on one long-lived branch rather than each becoming an independent PR off
    # the default branch: an agent that pushes every batch to one findings branch needs the
    # second batch to fork off THAT branch, or its push is a non-fast-forward. Proven live:
    # a caller ported from a harness that had this parameter failed with
    # "unexpected keyword argument 'base_branch'" on its first tick.
    ref = base_branch or harness.default_branch
    base = ref
    if await _has_origin_remote(harness):
        # Fetch so the fork point is current, then branch off origin's ref. A fetch
        # failure (offline, transient) degrades to the local branch rather than blocking
        # the repair — a slightly-stale base beats no worktree at all.
        fetch = await asyncio.to_thread(
            _git, harness, "fetch", "--quiet", "origin", ref
        )
        if fetch.returncode == 0:
            base = f"origin/{ref}"
        else:
            harness.log(
                f"worktree: origin fetch failed, basing on local {base}: "
                f"{fetch.stderr.strip()[:150]}"
            )

    def _create() -> None:
        proc = _git(harness, "worktree", "add", "-b", branch, path, base)
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()[:300]}")

    await asyncio.to_thread(_create)
    harness.log(f"worktree created: {path} ({branch}) off {base}")
    return path, branch


class WorktreeRefusedError(RuntimeError):
    """A mutates:true run cannot be given a worktree of its own, so it must not run."""


async def run_worktree(
    harness: "Harness", state: dict | None, agent_name: str, task_id: str = "",
    thread_ts: str = "",
) -> Path:
    """The directory a mutates:true run is spawned in: its own worktree, never git_root.

    `Agent.mutates` used to be a comment. Both runners spawned every agent at
    `harness.git_root`, and a worktree existed only if one of the agent's tools happened
    to call create_worktree() — so a mutates:true agent with Bash ran in the human's
    main checkout, which is the origin incident in docs/philosophy.md (a config-diff
    agent committing a human's unfinished edit) with the flag that was meant to prevent
    it set. The worktree is now cut here, before the provider starts, so no tool and no
    prompt is involved in deciding where the run lands.

    One worktree per TASK, not per spawn: `<agent>-<task_id>` on `worktree-<agent>-<task_id>`.
    A task that is retried (completion not met, a crash, an expired lease) comes back to
    the work it already did instead of starting over on a fresh branch, and two tasks of
    the same agent never share a tree. The name is deterministic so a run that crashed
    before state.json was saved still finds its tree on disk. Without a task (a direct
    run_agent_oneshot call) the suffix is the spawn time — a one-off run gets a one-off
    tree.

    It is registered in state["worktrees"] like any worktree a tool cut, so the hourly
    sweep, the keep/remove buttons (slack/approvals.py) and approval pinning
    (approval_scope.py, via the thread) all apply unchanged. `thread_ts` is usually empty
    at this point — a scheduled task gets its thread when its result is announced — and
    adopt_thread() fills it in then.

    Raises WorktreeRefusedError rather than returning git_root on ANY failure. Falling back to
    the main checkout is the exact bug this replaces; a refused run is visible (blocked,
    with the reason) and costs one retry, a run in the wrong checkout costs whatever it
    wrote there.
    """
    if state is None:
        raise WorktreeRefusedError(
            "no harness state was passed to record the worktree in, so the sweep could "
            "never reclaim it — call run_agent_oneshot(..., state=state)"
        )
    probe = await asyncio.to_thread(_git, harness, "rev-parse", "--show-prefix")
    if probe.returncode != 0:
        raise WorktreeRefusedError(
            f"git_root {harness.git_root} is not inside a git repository, so there is "
            f"nothing to cut a worktree from ({probe.stderr.strip()[:150]}). If "
            f"{agent_name} does not edit a git repository's files (docs, CS replies, "
            "research), set mutates=False and bound its writes with write_scope"
        )
    # git_root may be a subdirectory of the repository (one of several harnesses in a
    # monorepo). The run starts at the same place inside its worktree, so a relative path
    # in a prompt means what it meant before.
    prefix = probe.stdout.strip()

    slug = slugify(agent_name.replace("_", "-"))
    suffix = task_id or str(int(time.time()))
    path = harness.worktrees_dir / f"{slug}-{suffix}"
    branch = f"worktree-{slug}-{suffix}"
    worktrees: dict = state.setdefault("worktrees", {})

    # `.git` is a FILE in a linked worktree. A directory without one is leftover debris,
    # not a tree we cut, and is not something to hand an agent as its checkout.
    if (path / ".git").is_file():
        harness.log(f"worktree reused for {agent_name}: {path}")
    else:
        try:
            created, branch = await create_worktree(harness, slug, suffix)
        except Exception as exc:  # noqa: BLE001 — any failure is a refusal, never a fallback
            raise WorktreeRefusedError(f"could not create worktree {path}: {exc}") from exc
        path = Path(created)

    meta = worktrees.setdefault(str(path), {"branch": branch, "created": time.time()})
    meta.setdefault("thread_ts", thread_ts)
    if thread_ts and not meta["thread_ts"]:
        meta["thread_ts"] = thread_ts
    meta["agent"] = agent_name
    if task_id:
        meta["task"] = task_id

    # A session loads its guard hooks from its project directory, and that is now the
    # worktree: what is COMMITTED on the base branch, not what is on disk in the main
    # checkout. A harness whose hook registry exists locally but was never committed would
    # spawn this run with no git guard and no scope guard at all — quietly weaker than the
    # main-checkout run it replaces. Refuse instead and say what to commit.
    missing = [
        rel for rel in GUARD_REGISTRIES
        if (harness.git_root / rel).is_file() and not (path / prefix / rel).is_file()
    ]
    if missing:
        raise WorktreeRefusedError(
            f"the guard hook registry {', '.join(missing)} exists in the main checkout but "
            f"not in worktree {path} (it is not committed on the base branch), so this run "
            "would start with no guard hooks — commit and push it, then retry"
        )

    workdir = path / prefix
    if not workdir.is_dir():
        raise WorktreeRefusedError(f"{prefix or '.'} does not exist in worktree {path}")
    return workdir


def adopt_thread(state: dict, task_id: str, thread_ts: str) -> None:
    """Point a task's worktree at the thread its result was announced in.

    The sweep asks about an idle unmerged worktree in its thread, and approvals pin to
    a thread's worktree; a worktree with no thread gets neither. A scheduled task has no
    thread until its announcement, which is after the run that cut the tree."""
    if not (task_id and thread_ts):
        return
    for meta in state.get("worktrees", {}).values():
        if meta.get("task") == task_id and not meta.get("thread_ts"):
            meta["thread_ts"] = thread_ts


def _is_merged(harness: "Harness", branch: str) -> bool:
    proc = _git(harness, "log", f"{harness.default_branch}..{branch}", "--oneline")
    return proc.returncode == 0 and not proc.stdout.strip()


def _is_clean(harness: "Harness", path: str) -> bool:
    proc = _git(harness, "-C", path, "status", "--porcelain")
    return proc.returncode == 0 and not proc.stdout.strip()


def branch_exists(harness: "Harness", branch: str) -> bool:
    proc = _git(harness, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return proc.returncode == 0


def _has_staged(harness: "Harness", path: str) -> bool:
    """Is there anything in the index to commit? `diff --cached --quiet` exits 1 when yes."""
    return _git(harness, "-C", path, "diff", "--cached", "--quiet").returncode != 0


def _refuse(path: str, why: str) -> str:
    return (
        f"refusing to remove {path}: could not preserve its uncommitted work ({why}). "
        "The worktree is left alone; nothing was deleted."
    )


def remove_worktree(harness: "Harness", path: str, branch: str, force: bool = False) -> str | None:
    """Reclaim the worktree's directory. Returns an error string, or None on success.

    `force=True` never means "destroy the work". sweep() only reaches its force path
    when the worktree is NOT (merged and clean) — so by construction, force fires
    exactly on the worktrees that hold commits or edits existing nowhere else. This
    used to run `git worktree remove --force` (discarding uncommitted changes) and
    `git branch -D` (discarding unmerged commits), then announce itself as
    "removed worktree `<path>`".

    That is the shape of a classic failure: a `stop` command that actually terminates
    the machines and deletes their disks, then prints "stopped (volumes kept)". A
    destructive act behind a benign message is a lie that a human, and a model reading
    the message, will both believe.

    So: uncommitted changes are committed to the branch before the directory goes, and
    the branch is deleted with `-d`, which git refuses when the branch is unmerged.
    Disk is reclaimed; commits are not destroyed. `branch_exists()` afterwards tells the
    caller whether the branch survived, so the notification can say which happened.
    """
    if force and not _is_clean(harness, path):
        # -A includes untracked files: the point is that nothing in this directory is
        # lost, and `worktree remove` refuses a dirty tree anyway.
        add = _git(harness, "-C", path, "add", "-A")
        # The preserve step is the only thing standing between --force and the work.
        # If it did not happen, do not proceed: a sweep that reclaims disk is worth
        # nothing next to a commit that exists in one place. `git commit` fails for
        # ordinary, invisible reasons — no user.email on the host, a rejecting hook —
        # and its exit code is the only place it says so.
        if add.returncode != 0:
            return _refuse(path, (add.stderr or add.stdout).strip()[:160])

        # `add -A` can stage NOTHING even though `status --porcelain` was not empty, and
        # the two ways that happens need opposite answers:
        #
        #   - A dirty submodule prints ` M sub` while its gitlink is unchanged. The edit
        #     lives inside the submodule's own repo, where a commit here cannot reach it.
        #     `worktree remove --force` would delete it. This must still refuse — but with
        #     a sentence a human can act on, not `git commit`'s "nothing to commit", which
        #     is an exit code 1 that reads like success.
        #   - The tree simply went clean between the two calls. Nothing to preserve, and
        #     nothing to lose: proceed. Refusing here would wedge the sweep forever, since
        #     every later tick would take the same branch and print the same error.
        if _has_staged(harness, path):
            commit = _git(harness, "-C", path, "commit", "-m",
                          f"wip: world sweep preserved this work before removing the worktree for {branch}")
            if commit.returncode != 0:
                return _refuse(path, (commit.stderr or commit.stdout).strip()[:160])
        elif not _is_clean(harness, path):
            leftover = _git(harness, "-C", path, "status", "--porcelain").stdout.strip()
            return _refuse(
                path,
                "`git add -A` staged nothing, yet the tree is still dirty — the changes "
                "are somewhere a commit on this branch cannot reach, most likely inside a "
                f"submodule. Commit them where they live, then let the sweep run: {leftover[:120]}",
            )

    args = ["worktree", "remove", path] + (["--force"] if force else [])
    proc = _git(harness, *args)
    if proc.returncode != 0:
        return proc.stderr.strip()[:300]
    # Always -d, never -D. An unmerged branch survives, and that is the intended outcome.
    _git(harness, "branch", "-d", branch)
    return None


async def _has_origin_remote(harness: "Harness") -> bool:
    proc = await asyncio.to_thread(_git, harness, "remote")
    return proc.returncode == 0 and "origin" in proc.stdout.split()


async def sync_with_origin(harness: "Harness", state: dict, listener) -> None:
    """Fetch origin/<default_branch> and fast-forward local main if that's a
    clean, lossless move (local HEAD is a strict ancestor of origin/<default_branch>
    — e.g. a PR that got merged upstream). If local has *also* diverged (has
    commits origin doesn't have — the normal case, since a harness commits
    locally but never pushes on its own), don't auto-merge: a silent merge/rebase
    mid-tick is exactly the kind of git operation this project's doctrine says to
    avoid automating. Just surface it (log + record_finding + Slack post) so a
    human resolves it. Only runs when the working tree is actually on
    `default_branch` — worktrees/other branches are untouched. A harness with no
    `origin` remote (local-only git_root) is a normal, static configuration, not
    an error — skip quietly instead of retrying/logging a failed fetch every tick
    forever."""
    if not await _has_origin_remote(harness):
        return

    branch = await asyncio.to_thread(_git, harness, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0 or branch.stdout.strip() != harness.default_branch:
        return

    fetch = await asyncio.to_thread(_git, harness, "fetch", "--quiet", "origin", harness.default_branch)
    if fetch.returncode != 0:
        harness.log(f"origin-sync: fetch failed: {fetch.stderr.strip()[:200]}")
        return

    origin_ref = f"origin/{harness.default_branch}"
    ahead = await asyncio.to_thread(_git, harness, "rev-list", "--count", f"HEAD..{origin_ref}")
    behind = await asyncio.to_thread(_git, harness, "rev-list", "--count", f"{origin_ref}..HEAD")
    if ahead.returncode != 0 or behind.returncode != 0:
        harness.log("origin-sync: rev-list failed, skipping this tick")
        return
    n_ahead = int(ahead.stdout.strip() or 0)   # commits on origin we don't have
    n_behind = int(behind.stdout.strip() or 0)  # commits we have that origin doesn't

    if n_ahead == 0:
        return  # already caught up

    if n_behind == 0:
        merge = await asyncio.to_thread(_git, harness, "merge", "--ff-only", origin_ref)
        if merge.returncode == 0:
            harness.log(f"origin-sync: fast-forwarded {n_ahead} commit(s) from {origin_ref}")
        else:
            harness.log(f"origin-sync: expected clean ff-only but it failed: {merge.stderr.strip()[:200]}")
        return

    msg = (
        f"⚠️ origin-sync: {origin_ref} is {n_ahead} commit(s) ahead AND local is "
        f"{n_behind} commit(s) ahead (diverged) — skipping auto-merge, a human needs "
        "to merge/rebase directly."
    )
    harness.log(msg)
    record_finding(state, "origin-sync", msg)
    if listener and listener.enabled:
        await listener.post(msg)


async def sweep(harness: "Harness", state: dict, now: float, ui) -> None:
    """One sweep pass. `ui` is the listener (needs .notify_thread + .ask_worktree_cleanup)
    or None when Slack is disabled — in which case ask-before-delete steps just wait
    (never delete without the ask having been delivered where a human can see it)."""
    notify = ui.notify_thread if ui else None
    worktrees: dict = state.get("worktrees", {})
    threads: dict = state.get("threads", {})

    for path in list(worktrees):
        meta = worktrees[path]
        branch = meta["branch"]
        thread_ts = meta.get("thread_ts", "")

        exists = await asyncio.to_thread(
            lambda p=path: Path(p).exists()  # absolute, so a moved worktrees_dir keeps old trees
        )
        if not exists:
            harness.log(f"sweep: {path} already gone, dropping from state")
            worktrees.pop(path)
            continue

        merged, clean = await asyncio.gather(
            asyncio.to_thread(_is_merged, harness, branch),
            asyncio.to_thread(_is_clean, harness, path),
        )
        if merged and clean:
            err = await asyncio.to_thread(remove_worktree, harness, path, branch, False)
            if err:
                harness.log(f"sweep: prune of merged {path} failed: {err}")
            else:
                harness.log(f"sweep: pruned merged+idle worktree {path}")
                worktrees.pop(path)
                if notify and thread_ts:
                    await notify(
                        thread_ts,
                        f"🧹 Pruned worktree `{path}` (all commits merged to "
                        f"{harness.default_branch}).",
                    )
            continue

        last_activity = threads.get(thread_ts, {}).get("last_message_ts", meta.get("created", now))
        confirm_at = meta.get("confirm_requested_at")

        if confirm_at and last_activity > confirm_at:
            meta.pop("confirm_requested_at", None)  # human replied — keep, reset clock
            continue

        if confirm_at and now - confirm_at > CONFIRM_GRACE_S:
            err = await asyncio.to_thread(remove_worktree, harness, path, branch, True)
            if err:
                harness.log(f"sweep: forced removal of {path} failed: {err}")
            else:
                kept = await asyncio.to_thread(branch_exists, harness, branch)
                harness.log(
                    f"sweep: removed {path} after unanswered confirmation"
                    + (f" (branch {branch} kept — unmerged)" if kept else "")
                )
                worktrees.pop(path)
                if notify and thread_ts:
                    fate = (
                        f"Branch `{branch}` is **kept** — it has commits not in "
                        f"{harness.default_branch}, and any uncommitted edits were "
                        f"committed to it first. Nothing is lost: `git checkout {branch}`."
                        if kept
                        else f"Branch `{branch}` was deleted — everything on it was "
                        f"already merged into {harness.default_branch}."
                    )
                    await notify(
                        thread_ts,
                        f"🧹 No reply for 3 days — removed worktree `{path}`. {fate}",
                    )
            continue

        if not confirm_at and now - last_activity > IDLE_CONFIRM_S and ui and thread_ts:
            meta["confirm_requested_at"] = now
            await ui.ask_worktree_cleanup(thread_ts, path, branch)
