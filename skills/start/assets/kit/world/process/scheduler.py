"""The queue — what may start now, how many at once, and what waits behind what.

Split from orchestrator.py, which used to both notice work and perform it in one serial
pass: `for agent in system_agents: await _poll_one_agent(...)`. One ten-minute
investigation therefore held up every other agent's cheap check for ten minutes, and a
mention waiting on the same tick waited with it. Noticing and running are now two loops.

  orchestrator tick   evaluates triggers, ENQUEUES a task, returns. Never awaits a model.
  scheduler loop      claims queued tasks and runs them as separate asyncio tasks.

Every control here is ordinary code reading ordinary fields off the task record — no
model decides whether something may start:

  queue          status == "queued", oldest first
  dependencies   depends_on, all must be done            (task.ready)
  conflicts      writes exclusive, reads shared          (task.conflicts)
  concurrency    a count checked before each start
  retries        attempts vs retry_budget                (task.record_attempt)
  timeout        lease_until, set at claim               (task.reclaim_expired)
  rate limit     one 429 stops STARTING work, never drops what is queued

The concurrency limit is a plain integer compared against the in-flight map rather than
an asyncio.Semaphore. drain() is the only thing that starts work and the loop is
single-threaded, so the count is exact; a semaphore acquired inside the spawned coroutine
would let drain start more than the limit before any of them acquires.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from . import task as tasks
from . import worktrees
from .ledger import recent_rejections_snippet
from .orchestrator import RATE_LIMIT_BACKOFF_SECONDS, _apply_agent_result, append_run_log
from .spawn import compose_prompt, run_agent_oneshot
from .state import record_result_usage, save_state
from ..context import reset_current_task, set_current_task
from ..types import Signal

if TYPE_CHECKING:
    from .task import Task, TaskType
    from ..harness import Harness

SCHEDULER_INTERVAL_S = 2
DEFAULT_MAX_CONCURRENT = 2

# The completion contract for a plain agent run, and the kit's existing one: a run is
# complete when it finished AND left an artifact. spawn.enforce_completion already
# decides that (exit-0 with nothing written comes back "blocked"), so the predicate reads
# the attempt this scheduler recorded rather than re-deriving it.
AGENT_RUN_TYPE = tasks.AGENT_RUN_TYPE


def _last_attempt(home, task_id: str) -> dict | None:
    return next(
        (e for e in reversed(tasks.read_events(home, task_id)) if e.get("kind") == "attempt"),
        None,
    )


def agent_run_completion(home):
    """Finished, and left something behind. Checks the task's work/ directly rather than
    trusting the recorded outcome — "exit-0 is not completion" applied to the artifact
    itself, which is what this predicate could not do while agents still wrote to
    logs/<agent>/ and the scheduler had only an attempt event to read."""

    def predicate(task: "Task") -> tasks.Verdict:
        last = _last_attempt(home, task.id)
        if last is None:
            return tasks.Verdict.pending("아직 실행된 적이 없음")
        if last.get("outcome") != "ok":
            return tasks.Verdict.pending(
                f"마지막 시도 {last.get('outcome')}: {str(last.get('summary', ''))[:200]}"
            )
        work = tasks.task_work_dir(home, task.id)
        produced = sorted(p.name for p in work.iterdir()) if work and work.is_dir() else []
        if produced:
            return tasks.Verdict.done(
                f"{task.agent} 실행 완료 · work/ 산출물 {', '.join(produced[:5])}"
            )
        # An agent whose write_scope has not moved to the task yet still writes into
        # logs/<agent>/. RunResult.artifacts recorded whatever it produced, so trust that
        # rather than failing a harness mid-migration.
        elsewhere = last.get("artifacts") or []
        if elsewhere:
            return tasks.Verdict.done(
                f"{task.agent} 실행 완료 · 산출물 {', '.join(elsewhere[:5])}"
            )
        return tasks.Verdict.pending("실행은 끝났으나 산출물이 없음")

    return predicate


def default_task_type(home) -> "TaskType":
    return tasks.TaskType(AGENT_RUN_TYPE, completion=agent_run_completion(home))


class Scheduler:
    """Owns the in-flight set. One per harness run."""

    def __init__(self, harness: "Harness", state: dict, listener) -> None:
        self.harness = harness
        self.state = state
        self.listener = listener
        self.limit = max(1, getattr(harness, "max_concurrent_runs", DEFAULT_MAX_CONCURRENT))
        self._inflight: dict[str, asyncio.Task] = {}
        # task_id -> the Signal whose on_done records "this occurrence is handled".
        # In memory only: a closure cannot be written to task.md. A restart therefore
        # loses it and the trigger may fire again — exactly what a restart mid-run does
        # today, so this is not a new hole, but it is why on_done must stay idempotent.
        self._signals: dict[str, Signal] = {}

    # --- enqueue side (called from the tick) ---

    def remember_signal(self, task_id: str, signal: Signal) -> None:
        self._signals[task_id] = signal

    def task_type_for(self, record: "Task") -> "TaskType":
        registry = getattr(self.harness, "task_types", {})
        return registry.get(record.type) or default_task_type(self.harness.home)

    # --- run side ---

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    async def drain(self) -> int:
        """One scheduling pass. Returns how many runs were started.

        Nothing is dropped here: a task that cannot start stays `queued` and is
        reconsidered next pass. "No silent caps" means the queue grows visibly rather
        than requests disappearing.
        """
        home = self.harness.home
        now = time.time()

        for recovered in tasks.reclaim_expired(home, now):
            self.harness.log(f"{recovered.id} lease expired while running — requeued")

        if now < self.state.get("rate_limited_until", 0):
            return 0

        started = 0
        running = tasks.list_tasks(home, status="running")
        for candidate in tasks.list_tasks(home, status="queued"):
            if self.in_flight >= self.limit:
                break
            allowed, why = tasks.ready(home, candidate, running)
            if not allowed:
                continue
            agent = self.harness.agent_by_name(candidate.agent)
            if agent is None:
                tasks.transition(
                    home, candidate, "blocked",
                    reason=f"no registered agent named '{candidate.agent}'",
                )
                continue
            claimed = tasks.claim(home, candidate, self.task_type_for(candidate), now=now)
            running.append(claimed)
            self._start(claimed, agent)
            started += 1
        return started

    def _start(self, record: "Task", agent) -> None:
        runner = asyncio.create_task(self._execute(record, agent), name=f"task:{record.id}")
        self._inflight[record.id] = runner
        runner.add_done_callback(lambda _t, tid=record.id: self._inflight.pop(tid, None))

    async def _execute(self, record: "Task", agent) -> None:
        """Run one task to a recorded outcome. Never raises into the loop."""
        home = self.harness.home
        # Bind the run to its task before anything can write. Scoped write tools resolve
        # `tasks/{task}/work` from here, and the runners pass the same id to the guard
        # hooks as WORLD_TASK — one id, two enforcement layers, neither of them the agent.
        token = set_current_task(record.id)
        try:
            # state goes along so a mutates:true agent's worktree is recorded where the
            # sweep and the keep/remove buttons look (spawn.py refuses the run without it).
            result = await run_agent_oneshot(
                self.harness, agent, self._prompt(record, agent), state=self.state
            )
        except asyncio.CancelledError:
            tasks.append_event(home, record.id, "cancelled")
            with contextlib.suppress(tasks.TaskError):
                tasks.transition(home, record, "queued", reason="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — one task's crash must not stop the queue
            self.harness.log(f"{record.id} crashed: {exc!r}")
            tasks.record_attempt(home, record, outcome="error", summary=repr(exc)[:500])
            return
        finally:
            reset_current_task(token)

        now = time.time()
        if result.status == "rate_limited":
            # A 429 is not the task's fault, so it does not spend retry budget. The
            # circuit breaker lives here rather than in the tick because this is the only
            # place that talks to a provider — the tick's bookkeeping never should stop.
            self.state["rate_limited_until"] = now + RATE_LIMIT_BACKOFF_SECONDS
            tasks.append_event(home, record.id, "rate_limited", backoff_s=RATE_LIMIT_BACKOFF_SECONDS)
            tasks.transition(home, record, "queued", reason="rate limited")
            self.harness.log(f"rate limited — pausing new runs {RATE_LIMIT_BACKOFF_SECONDS}s")
            save_state(self.harness, self.state)
            return

        record_result_usage(self.state, agent.name, result)
        append_run_log(self.harness, {
            "task": record.id, "agent": agent.name, "status": result.status,
            "duration_s": round(result.duration_s, 1), "cost_usd": round(result.cost_usd, 4),
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
        })
        tasks.record_attempt(
            home, record, outcome=result.status, summary=result.result_text[:500],
            artifacts=list(result.artifacts),
        )

        signal = self._signals.pop(record.id, None) or Signal(prompt=record.body)
        agent_state = self.state["agents"].setdefault(agent.name, {})
        _apply_agent_result(self.harness, self.state, agent, agent_state, signal, result, now)

        fresh = tasks.read(home, record.id) or record
        if result.status == "ok" and fresh.status not in tasks.TERMINAL:
            tasks.transition(home, fresh, "verifying", reason="run finished")
            verdict = tasks.verify(home, fresh, self.task_type_for(fresh))
            if verdict.state != "done":
                tasks.transition(home, fresh, "queued", reason="completion not met")
            fresh = tasks.read(home, fresh.id) or fresh
        await self._announce(fresh, result, agent)
        save_state(self.harness, self.state)

    async def _announce(self, record: "Task", result, agent) -> None:
        """Post the task to Slack, once, when it reaches a state a human should see.

        This is where the findings bridge used to be, and the difference is the whole
        point of the move: the bridge learned about work by scanning findings/** a tick
        later, so it never knew which run produced a file, had to batch whatever had
        piled up, and needed a 500-entry seen-file list to avoid re-announcing. Here the
        task and its RunResult are already in hand.

        What is worth announcing is decided by the completion contract and the task's
        state, not by an agent choosing which directory to write to. `done` means the
        contract was met; `blocked` means a human is needed. A run that neither finished
        nor blocked is still in progress and says nothing.

        Once per task, keyed on the "announced" event rather than on `record.thread`: a
        task started from a conversation already carries that conversation's thread, and
        its result belongs there as a reply. Gating on `thread` skipped those tasks; the
        fix that followed posted every result as a new top-level thread instead, so a
        run asked for in a thread answered somewhere else.
        """
        if record.status not in ("done", "blocked"):
            return
        if any(e.get("kind") == "announced" for e in tasks.read_events(self.harness.home, record.id)):
            return
        listener = self.listener
        if not (listener and getattr(listener, "enabled", False)):
            return
        persona = self.harness.persona_for_agent(agent.name)
        try:
            thread_ts = await listener.announce_task(record, result, persona)
        except Exception as exc:  # noqa: BLE001
            # An announce failure must not lose the run. The task record and its event
            # log already hold everything; only the Slack copy is missing, and the next
            # thing to read the record will still find it.
            self.harness.log(f"announcing {record.id} failed ({exc}) — record is intact")
            return
        if thread_ts:
            record.thread = thread_ts
            tasks.save(self.harness.home, record)
            tasks.append_event(self.harness.home, record.id, "announced", thread=thread_ts)
            # A mutates:true task's worktree was cut before it had a thread. Give it this
            # one, so the sweep's cleanup ask and approval pinning land where the work is.
            worktrees.adopt_thread(self.state, record.id, thread_ts)

    def _prompt(self, record: "Task", agent) -> str:
        from .state import recent_context_snippet

        context = recent_context_snippet(self.state)
        rejections = recent_rejections_snippet(self.harness)
        if rejections:
            context = f"{context}\n\n{rejections}"
        return compose_prompt(self.harness, agent, record.body, context)

    async def drain_inflight(self) -> None:
        """Let started runs finish on shutdown instead of leaving them half-recorded."""
        if not self._inflight:
            return
        self.harness.log(f"waiting for {self.in_flight} running task(s)")
        await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)


async def scheduler_loop(scheduler: Scheduler, stop: asyncio.Event, *, once: bool = False) -> None:
    while not stop.is_set():
        try:
            await scheduler.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — pass-level isolation, same as the tick
            scheduler.harness.log(f"scheduler pass crashed (isolated): {exc!r}")
        if once:
            break
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_INTERVAL_S)
    await scheduler.drain_inflight()
