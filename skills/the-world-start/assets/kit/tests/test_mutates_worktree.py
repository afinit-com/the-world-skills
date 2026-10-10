"""Agent.mutates is enforced at spawn: a mutates:true run gets its own worktree as cwd.

The flag was once documented as "runs in its own git worktree (never the main
checkout)", but both runners spawned every agent at harness.git_root and nothing read it.
These tests hold the fixed contract against a real git repository:

  - mutates=True  → the Claude runner's cwd and Codex's -C are a worktree, not git_root
  - mutates=False → both still start at git_root
  - no worktree   → the run is refused (blocked), never moved back to git_root
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from world.context import current_worktree, reset_current_task, set_current_task
from world.harness import Harness
from world.process import task as tasks
from world.process import worktrees
from world.process.spawn import run_agent_oneshot
from world.process.state import default_state
from world.runners.codex import CodexRunner
from world.types import Agent, PollTrigger, RunResult


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def _agent(mutates: bool) -> Agent:
    return Agent("config-diff", PollTrigger(lambda *_: None), mutates=mutates)


class CapturingRunner:
    """Records what spawn handed it. Its signature is the AgentRunner protocol's."""

    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, agent, prompt, *, on_tool=None, cwd=None) -> RunResult:
        self.calls.append({"cwd": cwd, "context": current_worktree()})
        return RunResult(status="ok", result_text="done")


class LegacyRunner:
    """A runner written before `cwd` existed — the shape every mutates=False run must
    keep working with."""

    provider = "legacy"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, agent, prompt, *, on_tool=None) -> RunResult:
        self.calls += 1
        return RunResult(status="ok", result_text="done")


class MutatesWorktreeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        (self.root / "README.md").write_text("committed\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-q", "-m", "init")
        # The origin incident: a human's unfinished edit sitting in the main checkout.
        (self.root / "README.md").write_text("the human's unfinished edit\n")
        self.harness = Harness("t", home=self.root, git_root=self.root)
        self.harness.log = lambda _msg: None
        self.state = default_state(self.harness)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _run(self, agent: Agent, runner, state=...) -> RunResult:
        self.harness.runner = runner
        return await run_agent_oneshot(
            self.harness, agent, "work", state=self.state if state is ... else state
        )

    # --- mutates=True gets a worktree --------------------------------------------------

    async def test_mutates_run_starts_in_its_own_registered_worktree(self) -> None:
        runner = CapturingRunner()
        outcome = await self._run(_agent(True), runner)

        self.assertEqual("ok", outcome.status)
        cwd = Path(runner.calls[0]["cwd"])
        self.assertNotEqual(self.root, cwd)
        self.assertEqual(self.harness.worktrees_dir, cwd.parent)
        self.assertTrue((cwd / ".git").is_file(), "cwd is not a linked worktree")
        # The tree is the committed one, not the human's working copy.
        self.assertEqual("committed\n", (cwd / "README.md").read_text())
        self.assertEqual(str(cwd), runner.calls[0]["context"])
        self.assertEqual("", current_worktree(), "the worktree context leaked past the run")

        meta = self.state["worktrees"][str(cwd)]
        self.assertEqual(f"worktree-{cwd.name}", meta["branch"])
        self.assertEqual("config-diff", meta["agent"])
        self.assertTrue(worktrees.branch_exists(self.harness, meta["branch"]))
        self.assertTrue(self.harness.state_path.is_file(), "the worktree was not saved")

    async def test_claude_runner_cwd_is_the_worktree_only_for_mutates(self) -> None:
        from world.runners import claude

        seen = []

        async def capture(_prompt, options, _on_tool):
            seen.append(options.cwd)
            return None, False

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.object(claude, "_consume_stream", new=capture), \
                patch.dict(os.environ, env, clear=True):
            await self._run(_agent(True), claude.ClaudeRunner(self.harness))
            await self._run(_agent(False), claude.ClaudeRunner(self.harness))

        mutating, plain = map(Path, seen)
        self.assertEqual(self.harness.worktrees_dir, mutating.parent)
        self.assertEqual(self.root, plain)

    async def test_codex_runner_dash_c_is_the_worktree_only_for_mutates(self) -> None:
        argv = self.root.parent / "argv"
        fake = self.root.parent / "fake-codex"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" >> '{argv}'\n"
            "cat >/dev/null\n"
            "printf '%s\\n' "
            "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"ok\"}}'\n"
        )
        fake.chmod(0o700)

        def dash_c() -> list[str]:
            lines = argv.read_text().splitlines()
            return [lines[i + 1] for i, arg in enumerate(lines) if arg == "-C"]

        await self._run(_agent(True), CodexRunner(self.harness, executable=str(fake)))
        await self._run(_agent(False), CodexRunner(self.harness, executable=str(fake)))

        mutating, plain = map(Path, dash_c())
        self.assertEqual(self.harness.worktrees_dir, mutating.parent)
        self.assertEqual(self.root, plain)

    async def test_mutates_false_keeps_git_root_and_passes_no_cwd(self) -> None:
        runner = LegacyRunner()
        outcome = await self._run(_agent(False), runner, state=None)
        self.assertEqual("ok", outcome.status)
        self.assertEqual(1, runner.calls)
        self.assertFalse(self.harness.worktrees_dir.exists())

    async def test_a_task_reuses_its_worktree_and_adopts_its_thread(self) -> None:
        record = tasks.create(self.harness.home, type=tasks.AGENT_RUN_TYPE, title="diff",
                              agent="config-diff")
        runner = CapturingRunner()
        token = set_current_task(record.id)
        try:
            await self._run(_agent(True), runner)
            await self._run(_agent(True), runner)  # a retry of the same task
        finally:
            reset_current_task(token)

        first, second = (call["cwd"] for call in runner.calls)
        self.assertEqual(first, second)
        self.assertEqual(f"config-diff-{record.id}", Path(first).name)
        self.assertEqual(1, len(self.state["worktrees"]))

        worktrees.adopt_thread(self.state, record.id, "1700000000.000100")
        self.assertEqual("1700000000.000100", self.state["worktrees"][first]["thread_ts"])

    async def test_a_harness_in_a_subdirectory_starts_at_the_same_place(self) -> None:
        sub = self.root / "harnesses" / "obs"
        sub.mkdir(parents=True)
        (sub / "WORLD.md").write_text("# obs\n")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "harness")
        self.harness = Harness("t", home=sub, git_root=sub)
        self.harness.log = lambda _msg: None
        runner = CapturingRunner()
        await self._run(_agent(True), runner)
        cwd = Path(runner.calls[0]["cwd"])
        tree = self.harness.worktrees_dir / cwd.relative_to(self.harness.worktrees_dir).parts[0]
        self.assertEqual(tree / "harnesses" / "obs", cwd)
        self.assertTrue((cwd / "WORLD.md").is_file())

    # --- no worktree → no run ----------------------------------------------------------

    async def _assert_refused(self, outcome: RunResult, runner: CapturingRunner, why: str):
        self.assertEqual("blocked", outcome.status)
        self.assertIn("refused", outcome.result_text)
        self.assertIn(why, outcome.result_text)
        self.assertEqual([], runner.calls, "the provider was started anyway")

    async def test_worktree_creation_failure_refuses_the_run(self) -> None:
        runner = CapturingRunner()
        boom = AsyncMock(side_effect=RuntimeError("git worktree add failed: locked"))
        with patch.object(worktrees, "create_worktree", new=boom):
            outcome = await self._run(_agent(True), runner)
        await self._assert_refused(outcome, runner, "locked")

    async def test_a_harness_outside_git_refuses_the_run(self) -> None:
        plain = self.root.parent / "no-git"
        plain.mkdir()
        self.harness = Harness("t", home=plain, git_root=plain)
        self.harness.log = lambda _msg: None
        # GIT_CEILING_DIRECTORIES keeps git from finding a repository above the tempdir.
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            runner = CapturingRunner()
            outcome = await self._run(_agent(True), runner)
        await self._assert_refused(outcome, runner, "not inside a git repository")
        # ...and the refusal says what to do when the worker never needed git.
        await self._assert_refused(outcome, runner, "set mutates=False")

    def test_startup_warns_when_a_mutates_worker_can_never_run(self) -> None:
        # Outside git: said at startup (and so in the dry run), not at the first real run.
        plain = self.root.parent / "no-git-preflight"
        plain.mkdir()
        harness = Harness("t", home=plain, git_root=plain)
        logged: list[str] = []
        harness.log = logged.append
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            harness.register(_agent(True))
            warned = harness.preflight_mutates()
        self.assertEqual(1, len(warned))
        self.assertIn("config-diff", warned[0])
        self.assertIn("mutates=False", warned[0])
        self.assertEqual(warned, logged)

    def test_no_warning_in_a_repository_or_without_a_mutates_worker(self) -> None:
        self.harness.register(_agent(True))
        self.assertEqual([], self.harness.preflight_mutates())  # self.root is a git repo
        plain = self.root.parent / "no-git-docs"
        plain.mkdir()
        docs = Harness("t", home=plain, git_root=plain)
        docs.log = lambda _msg: None
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            docs.register(_agent(False))  # a docs/CS worker: no git needed, nothing to say
            self.assertEqual([], docs.preflight_mutates())

    async def test_no_state_to_record_the_worktree_refuses_the_run(self) -> None:
        runner = CapturingRunner()
        outcome = await self._run(_agent(True), runner, state=None)
        await self._assert_refused(outcome, runner, "state")

    async def test_uncommitted_guard_hooks_refuse_the_run(self) -> None:
        # Hooks registered in the main checkout but never committed would not exist in
        # the worktree, so the run would start with no guards at all.
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "settings.json").write_text('{"hooks": {}}')
        runner = CapturingRunner()
        outcome = await self._run(_agent(True), runner)
        await self._assert_refused(outcome, runner, ".claude/settings.json")

    async def test_dry_run_cuts_nothing(self) -> None:
        runner = CapturingRunner()
        self.harness.runner = runner
        outcome = await run_agent_oneshot(self.harness, _agent(True), "work", dry_run=True,
                                          state=self.state)
        self.assertEqual("skipped", outcome.status)
        self.assertEqual({}, self.state["worktrees"])
        self.assertFalse(self.harness.worktrees_dir.exists())


if __name__ == "__main__":
    unittest.main()
