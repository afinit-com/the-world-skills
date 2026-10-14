"""The poll loop — system-initiated trigger model. Notices work; does not perform it.

Every tick:
  1. custom tick tasks (direct HTTP/API bookkeeping — keepalives, health pings)
  2. worktree sweep (hourly)
  3. each system agent's trigger — cheap, non-LLM; a Signal ENQUEUES a task

The tick used to run the agent inline, serially, which meant one long investigation held
every other agent's cheap check behind it for as long as it ran. Execution moved to
scheduler.py; what stays here is the part that was always cheap. A tick now never awaits
a model, so it cannot be the thing that blocks.

Required behaviors:
  - blocked backoff: a blocked agent's trigger stays armed (on_done not called,
    so the run is never silently lost) but refires at most hourly — a structurally
    blocked agent once burned ~10min per tick forever.
  - one open task per agent: a poll condition stays true until the work lands, so
    without a duplicate guard the queue fills with copies of one ask.
  - tick-level crash isolation: any exception inside one tick is logged and the
    next tick proceeds; a mid-run network drop once killed the whole process.

The rate-limit circuit breaker moved to scheduler.py with the runs it protects: the
tick's bookkeeping never calls a provider, so it never needed to stop for a 429.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import runlog
from . import task as tasks
from . import worktrees
from .state import record_report, save_state
from ..types import ScheduleTrigger, Signal

if TYPE_CHECKING:
    from ..harness import Harness

RATE_LIMIT_BACKOFF_SECONDS = 300
BLOCKED_BACKOFF_SECONDS = 3600
WORKTREE_SWEEP_INTERVAL_S = 3600


def append_run_log(harness: "Harness", entry: dict) -> None:
    harness.logs_dir.mkdir(parents=True, exist_ok=True)
    # getattr: test stubs and harnesses built before the cap existed carry no settings.
    runlog.maybe_roll(harness.run_log_path,
                      max_bytes=getattr(harness, "run_log_max_bytes", runlog.RUN_LOG_MAX_BYTES),
                      keep_days=getattr(harness, "run_log_keep_days", None),
                      log=getattr(harness, "log", print))
    entry = {"ts": time.time(), **entry}
    with harness.run_log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


async def _run_tick_tasks(harness: "Harness", state: dict, listener, now: float, *,
                          dry_run: bool) -> None:
    """Direct bookkeeping (keepalives, health pings) — unaffected by the Claude
    rate-limit backoff, because none of it calls Claude."""
    for task in harness.tick_tasks:
        last = state.setdefault("tick_tasks", {}).setdefault(task.name, 0)
        if now - last < task.interval_s:
            continue
        if dry_run:
            harness.log(f"[dry-run] would run tick task {task.name}")
            continue
        try:
            await task.fn(state, listener)
            state["tick_tasks"][task.name] = now
        except Exception as exc:  # noqa: BLE001 — a failing keepalive must not stop the tick
            harness.log(f"tick task {task.name} failed (retry next tick): {exc}")


def _apply_agent_result(harness: "Harness", state: dict, agent, agent_state: dict,
                        signal, result, now: float) -> None:
    """Fold one finished run back into state. "rate_limited" is handled by the caller
    (it stops the whole tick, not just this agent) and "skipped" (dry-run) is meant to
    leave state untouched — so neither appears here."""
    if result.status == "ok":
        if signal.on_done:
            signal.on_done(result)
        record_report(state, agent.name, result.result_text[:300])
        harness.log(f"{agent.name} done in {result.duration_s:.0f}s")
    elif result.status == "blocked":
        agent_state["blocked_until"] = now + BLOCKED_BACKOFF_SECONDS
        harness.log(f"{agent.name} BLOCKED (incomplete run, retry in 1h): {result.result_text[:300]}")
        record_report(state, agent.name, f"⚠️ blocked run: {result.result_text[:250]}")
    elif result.status == "error":
        harness.log(f"{agent.name} errored: {result.result_text[:300]}")


def _previous_occurrence_ts(agent, now: float) -> float:
    return agent.trigger.schedule.previous_occurrence(
        datetime.fromtimestamp(now, tz=timezone.utc)
    ).timestamp()


def _due_signal(agent, agent_state: dict, now: float):
    """A ScheduleTrigger's due-check — the wall-clock cron equivalent of a poll check(),
    and deliberately READ-ONLY (a dry-run runs this and must mutate nothing, or it rewrites
    HEARTBEAT.md; see run_tick).

    Dedup is by OCCURRENCE and restart-safe: fire only when `last_fired` predates the most
    recent scheduled time (Schedule.previous_occurrence), and record `last_fired` ONLY in
    on_done — a verified-complete run, the same rule poll checks follow (never on exit-0).
    Because last_fired lives in state.json, a restart at any hour can't re-fire an occurrence
    already recorded — daily, weekly, and monthly alike. The first-sight adoption (so a fresh
    schedule waits for its NEXT time instead of catching up one already passed) happens once
    at startup in Harness._init_schedules, not here, to keep this read-only."""
    due_ts = _previous_occurrence_ts(agent, now)
    if agent_state.get("last_fired", 0) >= due_ts:
        return None

    def on_done(_result) -> None:
        agent_state["last_fired"] = now

    return Signal(prompt=agent.trigger.prompt, on_done=on_done)


async def _enqueue_due_agent(harness: "Harness", state: dict, agent, now: float, *,
                             dry_run: bool, scheduler) -> bool:
    """Evaluate one system agent's trigger and, if it fires, put a task on the queue.

    Returns whether a task was created. Cheap by construction: a PollTrigger's check() is
    the harness author's non-LLM probe and a ScheduleTrigger's due-check is arithmetic.
    Nothing here awaits a model, so a slow agent cannot delay the agent after it.
    """
    agent_state = state["agents"].setdefault(agent.name, {})
    if now < agent_state.get("blocked_until", 0):
        return False

    if isinstance(agent.trigger, ScheduleTrigger):
        view = agent_state
        if dry_run and "last_fired" not in view:
            # A real start adopts the current occurrence as done (Harness._init_schedules)
            # before the first tick; a dry-run skips that write, so mirror it on a copy —
            # otherwise it reports "would enqueue" for a run the real start would not make.
            adopted = _previous_occurrence_ts(agent, now)
            view = {**agent_state, "last_fired": adopted}
        signal = _due_signal(agent, view, now)
        if signal is None and dry_run:
            schedule = agent.trigger.schedule
            nxt = schedule.next_occurrence(datetime.fromtimestamp(now, tz=timezone.utc))
            harness.log(f"[dry-run] {agent.name} not due — next run {nxt:%Y-%m-%d %H:%M} "
                        f"{schedule.tz}")
        origin_kind = "schedule"
    else:
        check = agent.trigger.check  # type: ignore[union-attr] — else-branch is PollTrigger
        signal = check(state, now)
        if inspect.isawaitable(signal):
            signal = await signal
        origin_kind = "collector"
    if signal is None:
        return False

    open_task = tasks.has_open_task(harness.home, agent.name)
    if open_task is not None:
        harness.log(f"{agent.name} fired again while {open_task.id} is {open_task.status} — skipped")
        return False

    if dry_run:
        # tasks/ is committed. A dry run that files one is not a dry run — the same rule
        # that keeps _due_signal read-only and stops save_state rewriting HEARTBEAT.md.
        harness.log(f"[dry-run] would enqueue {agent.name}: {signal.prompt.splitlines()[0][:120]}")
        return False

    record = tasks.create(
        harness.home,
        type=getattr(agent, "task_type", "") or tasks.AGENT_RUN_TYPE,
        title=signal.prompt.splitlines()[0][:120],
        slug=tasks.slugify(agent.name),
        agent=agent.name,
        body=signal.prompt,
        origin=tasks.Origin(kind=origin_kind, by=agent.name),
    )
    scheduler.remember_signal(record.id, signal)
    harness.log(f"queued {record.id} for {agent.name}")
    return True


async def run_tick(harness: "Harness", state: dict, listener, *, dry_run: bool,
                   scheduler=None) -> None:
    now = time.time()

    # Sync local default_branch with origin before anything else runs this tick —
    # a clean fast-forward (e.g. a merged PR) applies automatically; divergence
    # (the normal case, since a harness commits locally but rarely pushes) only
    # gets surfaced (log + HEARTBEAT.md + Slack), never auto-merged.
    if not dry_run:
        await worktrees.sync_with_origin(harness, state, listener)

    await _run_tick_tasks(harness, state, listener, now, dry_run=dry_run)

    sweep_state = state["agents"].setdefault("worktree-sweep", {"last_run": 0})
    if now - sweep_state.get("last_run", 0) >= WORKTREE_SWEEP_INTERVAL_S and not dry_run:
        await worktrees.sweep(
            harness, state, now, listener if listener and listener.enabled else None
        )
        sweep_state["last_run"] = now

    save_state(harness, state)

    if scheduler is None:
        return
    queued = 0
    for agent in harness.system_agents:
        # No early return on a rate limit: enqueueing costs nothing and the queue is
        # exactly where work should wait it out. The scheduler declines to START.
        queued += await _enqueue_due_agent(
            harness, state, agent, now, dry_run=dry_run, scheduler=scheduler
        )
    if queued:
        save_state(harness, state)


async def poll_loop(harness: "Harness", state: dict, listener, stop: asyncio.Event, args,
                    scheduler=None) -> None:
    while not stop.is_set():
        try:
            await run_tick(harness, state, listener, dry_run=args.dry_run, scheduler=scheduler)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — tick-level crash isolation
            harness.log(f"tick crashed (isolated, next tick in {args.tick_interval}s): {exc!r}")
            append_run_log(harness, {"agent": "tick", "status": "crashed", "error": str(exc)[:500]})
        save_state(harness, state)
        if args.once:
            stop.set()
            break
        with contextlib.suppress(asyncio.TimeoutError):  # the timeout IS the tick interval
            await asyncio.wait_for(stop.wait(), timeout=args.tick_interval)
