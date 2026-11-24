"""HEARTBEAT.md is committed, so a routine run must not change it.

Every ok run used to add its summary to "Recent reports" there, which made a deployed
checkout dirty after each scheduled run. Routine summaries now go to STATUS.md."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.process import heartbeat, orchestrator
from world.process.state import (
    BOARD_KINDS,
    recent_context_snippet,
    record_finding,
    record_report,
)
from world.types import Agent, PollTrigger, RunResult, Schedule, ScheduleTrigger, Signal


def _harness(*agents: Agent) -> SimpleNamespace:
    home = Path(tempfile.mkdtemp())
    return SimpleNamespace(
        name="demo", home=home, agents=list(agents), provider="claude",
        heartbeat_path=home / "HEARTBEAT.md", status_path=home / "STATUS.md",
        run_log_path=home / "logs" / "harness.jsonl", log=lambda _text: None,
    )


def _state() -> dict:
    return {"agents": {}, "recent_reports": [], "pending_approvals": [], "threads": {}}


def _finish(harness, state, agent: Agent, result: RunResult) -> None:
    """What the scheduler does with a finished run, then the save that renders."""
    slot = state["agents"].setdefault(agent.name, {})
    orchestrator._apply_agent_result(
        harness, state, agent, slot, Signal("work"), result, time.time())
    heartbeat.write_heartbeat(harness, state)


REMINDER = Agent("reminder", ScheduleTrigger(Schedule.daily(9)))
DIGEST = Agent("digest", ScheduleTrigger(Schedule.daily(8)), writes_report=True)


class RoutineRunTests(unittest.TestCase):
    def test_a_posts_only_agent_finishing_ok_leaves_the_board_byte_identical(self) -> None:
        harness, state = _harness(REMINDER, DIGEST), _state()
        _finish(harness, state, DIGEST, RunResult("ok", "weekly digest written"))
        before = harness.heartbeat_path.read_bytes()

        for n in range(60):       # well past the 20-entry window
            _finish(harness, state, REMINDER, RunResult("ok", f"reminder {n} posted"))

        self.assertEqual(before, harness.heartbeat_path.read_bytes())
        self.assertIn(b"weekly digest written", before)

    def test_on_a_fresh_harness_it_stays_at_none_yet(self) -> None:
        harness, state = _harness(REMINDER), _state()
        heartbeat.write_heartbeat(harness, state)
        before = harness.heartbeat_path.read_bytes()
        for n in range(5):
            _finish(harness, state, REMINDER, RunResult("ok", f"reminder {n} posted"))
        self.assertEqual(before, harness.heartbeat_path.read_bytes())
        self.assertIn(b"(none yet)", before)

    def test_status_md_shows_the_routine_runs(self) -> None:
        harness, state = _harness(REMINDER), _state()
        for n in range(3):
            _finish(harness, state, REMINDER, RunResult("ok", f"reminder {n} posted"))
        status = harness.status_path.read_text(encoding="utf-8")
        self.assertIn("## Recent runs", status)
        for n in range(3):
            self.assertIn(f"**[reminder]** reminder {n} posted", status)
        self.assertNotIn("reminder", harness.heartbeat_path.read_text(encoding="utf-8"))

    def test_the_prompt_context_still_sees_every_run(self) -> None:
        harness, state = _harness(REMINDER), _state()
        _finish(harness, state, REMINDER, RunResult("ok", "reminder posted"))
        self.assertIn("[reminder] reminder posted", recent_context_snippet(state))
        self.assertEqual("run", state["recent_reports"][-1]["kind"])


class BoardEntryTests(unittest.TestCase):
    def board(self, harness) -> str:
        return harness.heartbeat_path.read_text(encoding="utf-8")

    def test_a_report_agents_run_is_on_the_board(self) -> None:
        harness, state = _harness(DIGEST), _state()
        _finish(harness, state, DIGEST, RunResult("ok", "three stale tickets found"))
        self.assertIn("**[digest]** three stale tickets found", self.board(harness))
        self.assertEqual("report", state["recent_reports"][-1]["kind"])
        self.assertNotIn("## Recent runs", harness.status_path.read_text(encoding="utf-8"))

    def test_a_blocked_run_is_on_the_board_whatever_the_agent(self) -> None:
        harness, state = _harness(REMINDER), _state()
        _finish(harness, state, REMINDER, RunResult("blocked", "permission denials: post"))
        self.assertIn("**[reminder]** ⚠️ blocked run: permission denials: post",
                      self.board(harness))
        self.assertEqual("blocked", state["recent_reports"][-1]["kind"])

    def test_an_origin_sync_notice_is_on_the_board(self) -> None:
        from world.process import worktrees

        def git(_harness, *args):      # on main, 2 commits ahead and 2 behind: diverged
            out = {"rev-parse": "main", "rev-list": "2"}.get(args[0], "")
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        harness, state = _harness(REMINDER), _state()
        harness.default_branch = "main"
        listener = SimpleNamespace(enabled=True, post=AsyncMock())

        async def ticks(n: int) -> None:
            for _ in range(n):
                await worktrees.sync_with_origin(harness, state, listener)

        with patch.object(worktrees, "_git", new=git), \
                patch.object(worktrees, "_has_origin_remote", new=AsyncMock(return_value=True)):
            asyncio.run(ticks(1))
            self.assertEqual(["notice"], [e["kind"] for e in state["recent_reports"]])
            heartbeat.write_heartbeat(harness, state)
            first = harness.heartbeat_path.read_bytes()
            self.assertIn("**[origin-sync]**", self.board(harness))

            # Still diverged on the next ticks: the same notice is not recorded again,
            # or its new timestamp would rewrite the board every tick.
            asyncio.run(ticks(3))
            heartbeat.write_heartbeat(harness, state)
        self.assertEqual(1, len(state["recent_reports"]))
        self.assertEqual(first, harness.heartbeat_path.read_bytes())

    def test_routine_runs_do_not_push_a_report_off_the_board(self) -> None:
        harness, state = _harness(REMINDER, DIGEST), _state()
        _finish(harness, state, DIGEST, RunResult("ok", "the one finding"))
        for n in range(200):
            record_report(state, "reminder", f"run {n}")
        heartbeat.write_heartbeat(harness, state)
        self.assertIn("the one finding", self.board(harness))
        # Each side keeps its own last 20, so the list stays bounded.
        kinds = [e["kind"] for e in state["recent_reports"]]
        self.assertEqual(20, kinds.count("run"))
        self.assertEqual(1, kinds.count("report"))

    def test_the_board_itself_keeps_only_its_last_twenty(self) -> None:
        state = _state()
        for n in range(25):
            record_report(state, "digest", f"finding {n}", kind="report")
        summaries = [e["summary"] for e in state["recent_reports"]]
        self.assertEqual([f"finding {n}" for n in range(5, 25)], summaries)


class EntryKindTests(unittest.TestCase):
    def test_an_entry_from_before_kinds_existed_shows_in_status_only(self) -> None:
        harness, state = _harness(REMINDER), _state()
        state["recent_reports"] = [{"ts": 1_700_000_000, "agent": "old", "summary": "from 0.5"}]
        heartbeat.write_heartbeat(harness, state)
        self.assertNotIn("from 0.5", harness.heartbeat_path.read_text(encoding="utf-8"))
        self.assertIn("**[old]** from 0.5", harness.status_path.read_text(encoding="utf-8"))

    def test_record_report_without_a_kind_is_a_routine_entry(self) -> None:
        state = _state()
        record_report(state, "tick", "keepalive ok")
        record_finding(state, "tick", "old name, same thing")
        self.assertEqual(["run", "run"], [e["kind"] for e in state["recent_reports"]])

    def test_a_harness_can_put_its_own_notice_on_the_board(self) -> None:
        harness, state = _harness(REMINDER), _state()
        record_report(state, "quota", "monthly quota at 90%", kind="notice")
        heartbeat.write_heartbeat(harness, state)
        self.assertIn("**[quota]** monthly quota at 90%",
                      harness.heartbeat_path.read_text(encoding="utf-8"))

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            record_report(_state(), "x", "y", kind="important")
        self.assertEqual(("report", "blocked", "notice"), BOARD_KINDS)


if __name__ == "__main__":
    unittest.main()
