from __future__ import annotations

import os
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.process import orchestrator, worktrees


def _harness(logs: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        git_root=Path("."), default_branch="main",
        log=(logs.append if logs is not None else lambda _text: None),
    )


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout=stdout, stderr="")


class GitTimeoutResultTests(unittest.TestCase):
    """A git command that outlives its timeout is a failed command, not an exception:
    every caller already reads returncode and has a path for non-zero."""

    def test_a_timeout_comes_back_as_a_failed_process(self) -> None:
        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch.object(worktrees.subprocess, "run", new=hang):
            proc = worktrees._git(_harness(), "fetch", "--quiet", "origin", "main")

        self.assertEqual(worktrees.GIT_TIMEOUT_RETURNCODE, proc.returncode)
        self.assertNotEqual(0, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("git fetch --quiet origin main", proc.stderr)
        self.assertIn(f"{worktrees.GIT_NETWORK_TIMEOUT_S}s", proc.stderr)


class GitInvocationTests(unittest.TestCase):
    def _call(self, *args: str) -> dict:
        seen: dict = {}

        def run(cmd, **kwargs):
            seen.update(kwargs)
            return _ok()

        with (
            patch.object(worktrees.subprocess, "run", new=run),
            patch.dict(os.environ, {"WORLD_TEST_MARKER": "kept", "GIT_TERMINAL_PROMPT": "1"}),
        ):
            worktrees._git(_harness(), *args)
        return seen

    def test_a_fetch_gets_the_short_timeout_and_cannot_prompt(self) -> None:
        seen = self._call("fetch", "--quiet", "origin", "main")
        self.assertEqual(worktrees.GIT_NETWORK_TIMEOUT_S, seen["timeout"])
        self.assertLessEqual(seen["timeout"], 30)
        self.assertEqual("0", seen["env"]["GIT_TERMINAL_PROMPT"])
        self.assertEqual("kept", seen["env"]["WORLD_TEST_MARKER"])

    def test_a_local_command_keeps_the_long_timeout(self) -> None:
        seen = self._call("-C", "some/path", "status", "--porcelain")
        self.assertEqual(worktrees.GIT_LOCAL_TIMEOUT_S, seen["timeout"])
        self.assertEqual(60, seen["timeout"])
        self.assertEqual("0", seen["env"]["GIT_TERMINAL_PROMPT"])

    def test_a_branch_named_like_a_network_command_is_still_local(self) -> None:
        self.assertEqual(worktrees.GIT_LOCAL_TIMEOUT_S, self._call("branch", "-d", "push")["timeout"])
        self.assertEqual(
            worktrees.GIT_NETWORK_TIMEOUT_S, self._call("-C", "some/path", "fetch")["timeout"])


class TickSurvivesFetchTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """A fetch that hung past its timeout raised out of sync_with_origin, the first thing
    a tick does, so the whole tick was lost: no tick task ran and no trigger was checked."""

    async def test_the_tick_still_evaluates_its_agents(self) -> None:
        logs: list[str] = []
        harness = _harness(logs)
        harness.tick_tasks = []
        harness.system_agents = [SimpleNamespace(name="first"), SimpleNamespace(name="second")]
        state = {"agents": {"worktree-sweep": {"last_run": time.time()}}}

        def run(cmd, **kwargs):
            if cmd[1] == "fetch":
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return _ok({"remote": "origin\n", "rev-parse": "main\n"}.get(cmd[1], ""))

        with (
            patch.object(worktrees.subprocess, "run", new=run),
            patch.object(orchestrator, "save_state"),
            patch.object(orchestrator, "_enqueue_due_agent",
                         new=AsyncMock(return_value=False)) as enqueue,
        ):
            await orchestrator.run_tick(
                harness, state, None, dry_run=False, scheduler=SimpleNamespace())

        self.assertEqual(2, enqueue.await_count)
        self.assertTrue(any("fetch failed" in line and "timed out" in line for line in logs), logs)


class TimeoutIsNotAnAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Callers that turn returncode into yes or no must not read a timeout as "no"."""

    @staticmethod
    def _hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    def test_a_timeout_is_logged_where_it_happens(self) -> None:
        logs: list[str] = []
        with patch.object(worktrees.subprocess, "run", new=self._hang):
            worktrees._git(_harness(logs), "remote")
        self.assertTrue(any("git remote timed out" in line for line in logs), logs)

    def test_branch_exists_does_not_answer_when_git_did_not(self) -> None:
        with patch.object(worktrees.subprocess, "run", new=self._hang):
            self.assertIsNone(worktrees.branch_exists(_harness(), "worktree-fix-1"))
        with patch.object(worktrees.subprocess, "run", new=lambda cmd, **_: _ok()):
            self.assertIs(True, worktrees.branch_exists(_harness(), "worktree-fix-1"))

    async def test_the_sweep_does_not_call_a_branch_deleted_when_it_could_not_check(self) -> None:
        import tempfile

        path = tempfile.mkdtemp()
        now = time.time()
        state = {"worktrees": {path: {
            "branch": "worktree-fix-1", "thread_ts": "1.2",
            "confirm_requested_at": now - worktrees.CONFIRM_GRACE_S - 10, "created": now - 10**7,
        }}, "threads": {}}
        ui = SimpleNamespace(notify_thread=AsyncMock(), ask_worktree_cleanup=AsyncMock())
        with (
            patch.object(worktrees, "_is_merged", return_value=False),
            patch.object(worktrees, "_is_clean", return_value=False),
            patch.object(worktrees, "remove_worktree", return_value=None),
            patch.object(worktrees, "branch_exists", return_value=None),
        ):
            await worktrees.sweep(_harness(), state, now, ui)
        said = ui.notify_thread.await_args.args[1]
        self.assertNotIn("was deleted", said)
        self.assertNotIn("is **kept**", said)
        self.assertIn("worktree-fix-1", said)

    async def test_a_probe_that_timed_out_is_not_reported_as_no_repository(self) -> None:
        with patch.object(worktrees.subprocess, "run", new=self._hang):
            with self.assertRaises(worktrees.WorktreeRefusedError) as caught:
                await worktrees.run_worktree(_harness(), {}, "fixer", "20260713-ab12")
        self.assertNotIn("is not inside a git repository", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))
