from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.process import scheduler as sched
from world.process import task as tasks
from world.types import Agent, PollTrigger, RunResult, Schedule, ScheduleTrigger, Signal


def _harness(*names: str, limit: int = 2) -> SimpleNamespace:
    home = Path(tempfile.mkdtemp())
    (home / "logs").mkdir()
    agents = [Agent(name, PollTrigger(lambda *_: None)) for name in names]
    return SimpleNamespace(
        home=home,
        logs_dir=home / "logs",
        findings_dir=home / "findings",
        run_log_path=home / "logs" / "harness.jsonl",
        ledger_path=home / "ledger.jsonl",
        agents=agents,
        agent_by_name=lambda n: next((a for a in agents if a.name == n), None),
        max_concurrent_runs=limit,
        task_types={},
        log=lambda _text: None,
    )


def _state() -> dict:
    return {"agents": {}, "recent_reports": [], "rate_limited_until": 0}


def _queue(harness, agent: str, **fields) -> tasks.Task:
    return tasks.create(
        harness.home, type=tasks.AGENT_RUN_TYPE, title=f"{agent} 작업",
        slug=tasks.slugify(agent), agent=agent, body="이번 회차를 수행하세요.", **fields,
    )


def _scheduler(harness, state):
    return sched.Scheduler(harness, state, None)


def _producing(home, task_id: str, name: str = "run.md"):
    """A fake run that writes into its task's work/, the way a real agent's scoped
    write tool does. Completion is now checked against the artifact, so a run that
    returns ok and writes nothing is deliberately NOT done."""

    async def run(*_args, **_kwargs):
        work = tasks.task_work_dir(home, task_id)
        work.mkdir(parents=True, exist_ok=True)
        (work / name).write_text("결과")
        return RunResult("ok", "찾은 것", artifacts=(f"tasks/{task_id}/work/{name}",))

    return run


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_caps_starts_and_the_rest_stay_queued(self) -> None:
        harness = _harness("a", "b", "c", limit=2)
        state = _state()
        for name in ("a", "b", "c"):
            _queue(harness, name)
        gate = asyncio.Event()

        async def blocking(*_args, **_kwargs):
            await gate.wait()
            return RunResult("ok", "done")

        scheduler = _scheduler(harness, state)
        with (
            patch.object(sched, "run_agent_oneshot", new=blocking),
            patch.object(sched, "save_state"),
        ):
            started = await scheduler.drain()
            self.assertEqual(2, started)
            self.assertEqual(2, scheduler.in_flight)
            # Nothing was dropped — the third is still queued, not lost.
            self.assertEqual(1, len(tasks.list_tasks(harness.home, status="queued")))
            gate.set()
            await scheduler.drain_inflight()

    async def test_drain_returns_while_a_long_run_is_still_in_flight(self) -> None:
        """The point of the split: noticing work never waits on performing it."""
        harness = _harness("slow")
        state = _state()
        _queue(harness, "slow")
        gate = asyncio.Event()

        async def blocking(*_args, **_kwargs):
            await gate.wait()
            return RunResult("ok", "done")

        scheduler = _scheduler(harness, state)
        with patch.object(sched, "run_agent_oneshot", new=blocking), patch.object(sched, "save_state"):
            await asyncio.wait_for(scheduler.drain(), timeout=1)
            self.assertEqual(1, scheduler.in_flight)
            gate.set()
            await scheduler.drain_inflight()

    async def test_writers_serialize_and_unrelated_targets_run_together(self) -> None:
        harness = _harness("a", "b", "c", limit=3)
        state = _state()
        _queue(harness, "a", writes=("bq://prj/x",))
        _queue(harness, "b", writes=("bq://prj/x",))
        _queue(harness, "c", writes=("bq://prj/y",))
        gate = asyncio.Event()

        async def blocking(*_args, **_kwargs):
            await gate.wait()
            return RunResult("ok", "done")

        scheduler = _scheduler(harness, state)
        with patch.object(sched, "run_agent_oneshot", new=blocking), patch.object(sched, "save_state"):
            # Two slots free, but only ONE writer of bq://prj/x may hold it; the task
            # writing somewhere else is unrelated and goes. Which of the two contenders
            # wins is the queue's business (same-second ties), so assert the invariant
            # rather than the winner.
            self.assertEqual(2, await scheduler.drain())
            running = {t.agent for t in tasks.list_tasks(harness.home, status="running")}
            self.assertIn("c", running)
            self.assertEqual(1, len({"a", "b"} & running))
            self.assertEqual(1, len(tasks.list_tasks(harness.home, status="queued")))
            gate.set()
            await scheduler.drain_inflight()


class OutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def _run_one(self, harness, state, result: RunResult):
        scheduler = _scheduler(harness, state)
        with (
            patch.object(sched, "run_agent_oneshot", new=AsyncMock(return_value=result)),
            patch.object(sched, "save_state"),
        ):
            await scheduler.drain()
            await scheduler.drain_inflight()
        return scheduler

    async def test_ok_run_is_verified_before_it_counts_as_done(self) -> None:
        harness = _harness("watch")
        state = _state()
        made = _queue(harness, "watch")
        scheduler = _scheduler(harness, state)
        with (
            patch.object(sched, "run_agent_oneshot", new=_producing(harness.home, made.id)),
            patch.object(sched, "save_state"),
        ):
            await scheduler.drain()
            await scheduler.drain_inflight()

        final = tasks.read(harness.home, made.id)
        self.assertEqual("done", final.status)
        kinds = [e["kind"] for e in tasks.read_events(harness.home, made.id)]
        # done is reached THROUGH a verify, never assigned by the runner.
        self.assertIn("verify", kinds)
        self.assertLess(kinds.index("verify"), len(kinds) - 1)

    async def test_an_ok_run_that_writes_nothing_is_not_done(self) -> None:
        """exit-0 is not completion, now checked against the artifact itself."""
        harness = _harness("watch")
        state = _state()
        made = _queue(harness, "watch")
        await self._run_one(harness, state, RunResult("ok", "아무것도 안 씀"))
        self.assertEqual("queued", tasks.read(harness.home, made.id).status)

    async def test_blocked_run_spends_budget_and_does_not_finish(self) -> None:
        harness = _harness("watch")
        state = _state()
        made = _queue(harness, "watch", retry_budget=1)
        await self._run_one(harness, state, RunResult("blocked", "wrote no file"))

        final = tasks.read(harness.home, made.id)
        self.assertEqual("blocked", final.status)
        self.assertEqual(1, final.attempts)

    async def test_rate_limit_requeues_without_spending_budget(self) -> None:
        harness = _harness("watch")
        state = _state()
        made = _queue(harness, "watch")
        await self._run_one(harness, state, RunResult("rate_limited", "429"))

        final = tasks.read(harness.home, made.id)
        self.assertEqual("queued", final.status)
        self.assertEqual(0, final.attempts)          # a 429 is not the task's fault
        self.assertGreater(state["rate_limited_until"], time.time())

    async def test_rate_limit_stops_the_next_pass_from_starting_anything(self) -> None:
        harness = _harness("a", "b")
        state = _state()
        _queue(harness, "a")
        _queue(harness, "b")
        scheduler = _scheduler(harness, state)
        with (
            patch.object(sched, "run_agent_oneshot",
                         new=AsyncMock(return_value=RunResult("rate_limited", "429"))),
            patch.object(sched, "save_state"),
        ):
            await scheduler.drain()
            await scheduler.drain_inflight()
            self.assertEqual(0, await scheduler.drain())
        self.assertEqual(2, len(tasks.list_tasks(harness.home, status="queued")))

    async def test_a_crashing_run_is_recorded_and_the_queue_survives(self) -> None:
        harness = _harness("boom")
        state = _state()
        made = _queue(harness, "boom", retry_budget=1)
        scheduler = _scheduler(harness, state)
        with (
            patch.object(sched, "run_agent_oneshot", new=AsyncMock(side_effect=RuntimeError("네트워크"))),
            patch.object(sched, "save_state"),
        ):
            await scheduler.drain()
            await scheduler.drain_inflight()
        self.assertEqual("blocked", tasks.read(harness.home, made.id).status)
        self.assertEqual(0, scheduler.in_flight)

    async def test_unknown_agent_blocks_instead_of_spinning(self) -> None:
        harness = _harness("watch")
        state = _state()
        made = _queue(harness, "ghost")
        scheduler = _scheduler(harness, state)
        self.assertEqual(0, await scheduler.drain())
        self.assertEqual("blocked", tasks.read(harness.home, made.id).status)

    async def test_signal_on_done_fires_only_on_a_completed_run(self) -> None:
        harness = _harness("watch")
        state = _state()
        made = _queue(harness, "watch", retry_budget=1)
        fired = []
        scheduler = _scheduler(harness, state)
        scheduler.remember_signal(made.id, Signal("work", on_done=lambda _r: fired.append(1)))
        with (
            patch.object(sched, "run_agent_oneshot",
                         new=AsyncMock(return_value=RunResult("blocked", "nothing written"))),
            patch.object(sched, "save_state"),
        ):
            await scheduler.drain()
            await scheduler.drain_inflight()
        self.assertEqual([], fired)


class EnqueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_refiring_trigger_does_not_pile_up_duplicates(self) -> None:
        from world.process import orchestrator

        harness = _harness("watch")
        state = _state()
        agent = harness.agents[0]
        agent.trigger = PollTrigger(lambda *_: Signal("계속 참인 조건"))
        scheduler = _scheduler(harness, state)

        now = time.time()
        self.assertTrue(
            await orchestrator._enqueue_due_agent(
                harness, state, agent, now, dry_run=False, scheduler=scheduler
            )
        )
        self.assertFalse(
            await orchestrator._enqueue_due_agent(
                harness, state, agent, now, dry_run=False, scheduler=scheduler
            )
        )
        self.assertEqual(1, len(tasks.list_tasks(harness.home)))

    async def test_dry_run_files_nothing(self) -> None:
        from world.process import orchestrator

        harness = _harness("watch")
        state = _state()
        agent = harness.agents[0]
        agent.trigger = PollTrigger(lambda *_: Signal("무언가 발견"))
        await orchestrator._enqueue_due_agent(
            harness, state, agent, time.time(), dry_run=True, scheduler=_scheduler(harness, state)
        )
        self.assertFalse(tasks.tasks_dir(harness.home).exists())

    async def test_a_blocked_agent_is_not_enqueued(self) -> None:
        from world.process import orchestrator

        harness = _harness("watch")
        state = _state()
        agent = harness.agents[0]
        agent.trigger = PollTrigger(lambda *_: Signal("발견"))
        state["agents"]["watch"] = {"blocked_until": time.time() + 3600}
        self.assertFalse(
            await orchestrator._enqueue_due_agent(
                harness, state, agent, time.time(), dry_run=False,
                scheduler=_scheduler(harness, state),
            )
        )


class TaskTitleTests(unittest.IsolatedAsyncioTestCase):
    """The task title is the headline of its Slack card. It was always the prompt's first
    line, so a scheduled briefing whose prompt opened with an instruction to the model
    showed that instruction to people as the headline (issue #5)."""

    async def _enqueue(self, trigger) -> "tasks.Task":
        from world.process import orchestrator

        harness, state = _harness("watch"), _state()
        agent = harness.agents[0]
        agent.trigger = trigger
        if isinstance(trigger, ScheduleTrigger):
            state["agents"]["watch"] = {"last_fired": 0}
        self.assertTrue(await orchestrator._enqueue_due_agent(
            harness, state, agent, time.time(), dry_run=False,
            scheduler=_scheduler(harness, state),
        ))
        [record] = tasks.list_tasks(harness.home)
        return record

    async def test_a_signal_title_is_the_task_title_and_the_prompt_is_unchanged(self) -> None:
        prompt = "아래 목록대로 report.md 절차에 따라 보고하라.\n- 항목 3건"
        record = await self._enqueue(PollTrigger(lambda *_: Signal(prompt, title="📋 아침 보고")))
        self.assertEqual("📋 아침 보고", record.title)
        self.assertEqual(prompt, record.body.strip())

    async def test_without_a_title_the_first_prompt_line_is_still_used(self) -> None:
        record = await self._enqueue(PollTrigger(lambda *_: Signal("첫 줄\n둘째 줄")))
        self.assertEqual("첫 줄", record.title)

    async def test_a_blank_title_falls_back_rather_than_failing_validation(self) -> None:
        record = await self._enqueue(PollTrigger(lambda *_: Signal("첫 줄", title="  ")))
        self.assertEqual("첫 줄", record.title)

    async def test_a_title_with_line_breaks_is_one_line_and_writes_no_keys(self) -> None:
        # A poll builds titles from outside text (a PR title, a mail subject). task.md has
        # one key per line, so "Daily\nstatus: done" wrote a status line of its own.
        record = await self._enqueue(PollTrigger(
            lambda *_: Signal("p", title="Daily\nstatus: done\r\nid: 20261001-dead"),
        ))
        self.assertEqual("Daily status: done id: 20261001-dead", record.title)
        self.assertEqual("queued", record.status)
        self.assertNotEqual("20261001-dead", record.id)

    async def test_a_schedule_trigger_title_reaches_the_task(self) -> None:
        record = await self._enqueue(ScheduleTrigger(
            Schedule.daily(9, 0, "Asia/Seoul"), prompt="정기 실행", title="정기 보고",
        ))
        self.assertEqual("정기 보고", record.title)
        self.assertEqual("정기 실행", record.body.strip())


class ScheduledDryRunTests(unittest.IsolatedAsyncioTestCase):
    """`--once --dry-run` must say what a real start would do. A real start adopts the
    current occurrence as done (Harness._init_schedules) and waits for the next one; a
    dry-run skips that adoption to stay read-only, so it used to report a first-start
    scheduled agent as due — "would enqueue" at 20:22 for a 09:00 job."""

    def _scheduled(self, harness, logs: list[str]):
        agent = harness.agents[0]
        agent.trigger = ScheduleTrigger(Schedule.daily(9, 0, "Asia/Seoul"), prompt="정기 실행")
        harness.log = logs.append
        return agent

    async def test_first_start_dry_run_reports_the_next_run_not_an_enqueue(self) -> None:
        from world.process import orchestrator

        harness, state, logs = _harness("daily-digest"), _state(), []
        agent = self._scheduled(harness, logs)
        now = datetime(2026, 10, 3, 11, 22, tzinfo=timezone.utc).timestamp()  # 20:22 KST
        queued = await orchestrator._enqueue_due_agent(
            harness, state, agent, now, dry_run=True, scheduler=_scheduler(harness, state)
        )
        self.assertFalse(queued)
        self.assertFalse(any("would enqueue" in line for line in logs), logs)
        self.assertTrue(any("2026-10-04 09:00" in line for line in logs), logs)
        self.assertNotIn("last_fired", state["agents"]["daily-digest"])  # still read-only

    async def test_dry_run_still_enqueues_an_occurrence_that_is_really_due(self) -> None:
        from world.process import orchestrator

        harness, state, logs = _harness("daily-digest"), _state(), []
        agent = self._scheduled(harness, logs)
        yesterday = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp()  # 02 09:00 KST
        state["agents"]["daily-digest"] = {"last_fired": yesterday}
        now = datetime(2026, 10, 3, 11, 22, tzinfo=timezone.utc).timestamp()
        await orchestrator._enqueue_due_agent(
            harness, state, agent, now, dry_run=True, scheduler=_scheduler(harness, state)
        )
        self.assertTrue(any("would enqueue daily-digest" in line for line in logs), logs)


class StatusCadenceTests(unittest.TestCase):
    def _status(self, agent: Agent) -> str:
        from world.process.heartbeat import render_status

        harness = SimpleNamespace(name="h", agents=[agent])
        return render_status(harness, _state())

    def test_a_scheduled_agent_shows_its_schedule(self) -> None:
        agent = Agent("daily-digest", ScheduleTrigger(Schedule.daily(9, 0, "Asia/Seoul")))
        text = self._status(agent)
        self.assertIn("- **daily-digest**: daily 09:00 Asia/Seoul", text)
        self.assertNotIn("no fixed cadence", text)

    def test_a_poll_agent_stays_event_driven(self) -> None:
        text = self._status(Agent("watch", PollTrigger(lambda *_: None)))
        self.assertIn("- **watch**: event-driven (no fixed cadence)", text)


if __name__ == "__main__":
    unittest.main()


class AnnounceTests(unittest.IsolatedAsyncioTestCase):
    """What replaced bridge.py: the scheduler announces the task it just ran."""

    def _listener(self):
        posted = []

        async def announce_task(record, result, persona):
            posted.append((record.id, record.status, result))
            return "1700000000.0001"

        return SimpleNamespace(enabled=True, announce_task=announce_task), posted

    async def _run(self, harness, state, result, listener, *, produces=None):
        scheduler = sched.Scheduler(harness, state, listener)
        runner = produces if produces else AsyncMock(return_value=result)
        with (
            patch.object(sched, "run_agent_oneshot", new=runner),
            patch.object(sched, "save_state"),
        ):
            await scheduler.drain()
            await scheduler.drain_inflight()

    async def test_a_finished_task_is_announced_once(self) -> None:
        harness = _harness("watch")
        harness.persona_for_agent = lambda _n: SimpleNamespace(key="main")
        state = _state()
        made = _queue(harness, "watch")
        listener, posted = self._listener()
        await self._run(harness, state, None, listener,
                        produces=_producing(harness.home, made.id))

        self.assertEqual([(made.id, "done")], [(a, b) for a, b, _ in posted])
        # Announced once and the thread is recorded, so a re-announce is impossible.
        self.assertEqual("1700000000.0001", tasks.read(harness.home, made.id).thread)
        self.assertIn("announced", [e["kind"] for e in tasks.read_events(harness.home, made.id)])

    async def test_a_task_asked_for_in_a_thread_is_announced_there(self) -> None:
        # A run queued from a conversation carries that thread. It used to be skipped
        # for having one, and when it wasn't, the result opened a new top-level thread.
        harness = _harness("watch")
        harness.persona_for_agent = lambda _n: SimpleNamespace(key="main")
        state = _state()
        made = _queue(harness, "watch")
        made.thread = "1699999999.0001"
        tasks.save(harness.home, made)
        seen = []

        async def announce_task(record, result, persona):
            seen.append(record.thread)
            return record.thread

        listener = SimpleNamespace(enabled=True, announce_task=announce_task)
        await self._run(harness, state, None, listener,
                        produces=_producing(harness.home, made.id))
        self.assertEqual(["1699999999.0001"], seen)
        self.assertEqual("1699999999.0001", tasks.read(harness.home, made.id).thread)

    async def test_a_blocked_task_is_announced_because_a_human_is_needed(self) -> None:
        harness = _harness("watch")
        harness.persona_for_agent = lambda _n: SimpleNamespace(key="main")
        state = _state()
        _queue(harness, "watch", retry_budget=1)
        listener, posted = self._listener()
        await self._run(harness, state, RunResult("blocked", "wrote no file"), listener)
        self.assertEqual(["blocked"], [status for _id, status, _r in posted])

    async def test_a_rate_limited_task_says_nothing(self) -> None:
        harness = _harness("watch")
        harness.persona_for_agent = lambda _n: SimpleNamespace(key="main")
        state = _state()
        _queue(harness, "watch")
        listener, posted = self._listener()
        await self._run(harness, state, RunResult("rate_limited", "429"), listener)
        self.assertEqual([], posted)

    async def test_an_announce_failure_does_not_lose_the_run(self) -> None:
        harness = _harness("watch")
        harness.persona_for_agent = lambda _n: SimpleNamespace(key="main")
        state = _state()
        made = _queue(harness, "watch")

        async def boom(*_a):
            raise RuntimeError("channel_not_found")

        listener = SimpleNamespace(enabled=True, announce_task=boom)
        await self._run(harness, state, None, listener,
                        produces=_producing(harness.home, made.id))
        self.assertEqual("done", tasks.read(harness.home, made.id).status)

    async def test_no_slack_is_not_an_error(self) -> None:
        harness = _harness("watch")
        harness.persona_for_agent = lambda _n: SimpleNamespace(key="main")
        state = _state()
        made = _queue(harness, "watch")
        await self._run(harness, state, None, None,
                        produces=_producing(harness.home, made.id))
        self.assertEqual("done", tasks.read(harness.home, made.id).status)
        self.assertEqual("", tasks.read(harness.home, made.id).thread)
