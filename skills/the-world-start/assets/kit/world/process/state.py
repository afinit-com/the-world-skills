"""state.json IO, the findings ring buffer, and the prompt snippets rendered from them.

Invariants:
  - state.json is the single source of truth (prompt injection + trigger decisions).
  - HEARTBEAT.md is regenerated wholesale on every save — a human-readable view
    that structurally cannot drift (no separate write path). Never hand-edit.
    The rendering itself lives in heartbeat.py; save_state is its only caller.
  - Schema growth is tolerated on load (setdefault per key) so upgrading world
    never requires wiping a harness's state.

What is deliberately NOT here (split out):
  - heartbeat.py — rendering state into HEARTBEAT.md / STATUS.md.
  - ledger.py — the append-only record of human decisions. state.json holds what is
    currently true and is rewritten every tick; the ledger holds what happened and is
    never rewritten.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .heartbeat import write_heartbeat

if TYPE_CHECKING:
    from ..harness import Harness
    from ..types import RunResult


def default_state(harness: "Harness") -> dict:
    return {
        "agents": {agent.name: {} for agent in harness.agents}
        | {"worktree-sweep": {"last_run": 0}},
        "recent_findings": [],   # [{ts, agent, summary}], most recent last
        "rate_limited_until": 0,
        # thread_ts -> {slug, worktree, branch, created, last_message_ts, origin,
        # retries, session_id, identity} — threads are the unit of work; surviving
        # restarts lets replies to old threads resume via session_id.
        "threads": {},
        # worktree path -> {branch, thread_ts, created, confirm_requested_at}
        "worktrees": {},
        # Live APPROVAL_REQUEST buttons not yet clicked: [{msg_ts, thread_ts,
        # requester, summary, ts}]. Injected into every thread turn so independent
        # sessions stop re-requesting the same action. Removed on approve/deny.
        "pending_approvals": [],
        # GRANTED approvals, thread_ts -> {by, at, summary}. pending_approvals is
        # what was ASKED and not yet decided; this is what was APPROVED and not yet
        # ACTED ON. Each grant authorizes exactly one successful side-effect call:
        # toolkit.make_approval_gated consumes the entry on success, so a second
        # write in the same thread needs a second button click. A denial writes
        # nothing here (and clears any stale grant), so "no entry" is the safe
        # default for every code path that reads it.
        "approved_threads": {},
        # msg_ts of every SIGNED relay decision this harness has already honored. A
        # signature answers exactly one ask, once — without this a captured decision
        # could be re-posted to grant again after the first was consumed.
        "relay_nonces": [],
        # Open PRs a mutates:true agent's tool opened (e.g. a scoped `open_pr` tool the
        # harness defines) that no one has merged yet: [{repo, number, title, url,
        # opened_at}]. Distinct from pending_approvals (Slack APPROVAL_REQUEST
        # buttons, mention-flow) — this is poll-agent-opened PRs, kept in sync
        # with GitHub's actual state by whatever tick_task queries `gh pr list`
        # for the harness project (see refresh_pending_prs in a project's own
        # harness.py) rather than world itself calling `gh` — world core has no
        # opinion on which repos/tools a harness uses.
        "pending_prs": [],
        # Generic "human picks which candidate goes next" primitive (world_select_task
        # button, listener.py's _on_interactive) — a project decides when to ask
        # and what a candidate even means; world core only stores the click result.
        "queue_selected": None,   # the value= of whichever button got clicked, or None
        "queue_asked_ts": None,   # msg ts of the currently-posted selector, or None
    }


def load_state(harness: "Harness") -> dict:
    defaults = default_state(harness)
    if not harness.state_path.exists():
        return defaults
    with harness.state_path.open(encoding="utf-8") as f:
        state = json.load(f)
    for key, value in defaults["agents"].items():
        state.setdefault("agents", {}).setdefault(key, value)
    for key, value in defaults.items():
        if key != "agents":
            state.setdefault(key, json.loads(json.dumps(value)))
    return state


def save_state(harness: "Harness", state: dict) -> None:
    tmp = harness.state_path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(harness.state_path)
    write_heartbeat(harness, state)


def record_finding(state: dict, agent: str, summary: str, limit: int = 20) -> None:
    state["recent_findings"].append({"ts": time.time(), "agent": agent, "summary": summary})
    state["recent_findings"] = state["recent_findings"][-limit:]


# --- state GC ----------------------------------------------------------------
#
# state.json is rewritten WHOLE every tick (save_state), so a key that only grows is a
# file that only grows — rewritten every 60s forever. Most collections here are already
# bounded (recent_findings[-20], pending_approvals[-50], relay_nonces[-200]). Two are
# not, and these prune them:
#   - threads: one entry per mention/finding/relay thread, kept forever with no cleanup.
#   - approved_threads: a grant that is approved but never consumed (a thread that asked,
#     got a yes, and never ran the gated tool) lingers with nothing to spend it.
# Deterministic and non-LLM — meant to be called from a periodic tick_task.


def _live_thread_ts(state: dict) -> set[str]:
    """Threads something still points at — never prune these, however idle. A worktree
    mid-review, a pending approval, or an unspent grant all name a thread
    that is still doing work even if no message has landed in it for a while."""
    live: set[str] = set()
    live |= set(state.get("approved_threads", {}))
    live |= {p.get("thread_ts") for p in state.get("pending_approvals", [])}
    live |= {m.get("thread_ts") for m in state.get("worktrees", {}).values()}
    live.discard(None)
    live.discard("")
    return live


def prune_dead_threads(state: dict, now: float, ttl_s: float) -> int:
    """Drop thread metadata idle past `ttl_s`, unless something live still points at it.
    Returns how many were dropped. Idle age is measured from last_message_ts (falling back
    to created, then to now so a malformed entry with neither is never treated as ancient
    and swept on its first sight)."""
    threads = state.get("threads", {})
    live = _live_thread_ts(state)
    dead = [
        ts
        for ts, meta in threads.items()
        if ts not in live
        and now - meta.get("last_message_ts", meta.get("created", now)) > ttl_s
    ]
    for ts in dead:
        threads.pop(ts, None)
    return len(dead)


def prune_stale_grants(state: dict, now: float, ttl_s: float) -> int:
    """Drop approval grants older than `ttl_s` that were never consumed. A grant is
    single-use and spent on the first successful gated call; one still sitting here past
    the TTL was approved for work that never ran, and leaving it banked lets a much later
    turn in that thread spend a long-forgotten yes. Returns how many were dropped."""
    grants = state.get("approved_threads", {})
    dead = [ts for ts, g in grants.items() if now - g.get("at", 0) > ttl_s]
    for ts in dead:
        grants.pop(ts, None)
    return len(dead)


def record_usage(
    state: dict, key: str, *,
    input_tokens: int = 0, output_tokens: int = 0,
    cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Accumulate one call's token/cost usage into state["agents"][key] — shared
    namespace for both poll agents (orchestrator.py, key=agent name) and Slack
    identities (listener.py, key=persona/identity slug), so STATUS.md renders
    one table for both. setdefault-based so a key with no prior entry just starts from
    zero rather than needing separate init."""
    bucket = state.setdefault("agents", {}).setdefault(key, {})
    bucket["total_input_tokens"] = bucket.get("total_input_tokens", 0) + input_tokens
    bucket["total_output_tokens"] = bucket.get("total_output_tokens", 0) + output_tokens
    bucket["total_cache_creation_tokens"] = bucket.get("total_cache_creation_tokens", 0) + cache_creation_tokens
    bucket["total_cache_read_tokens"] = bucket.get("total_cache_read_tokens", 0) + cache_read_tokens
    bucket["total_cost_usd"] = bucket.get("total_cost_usd", 0.0) + cost_usd
    bucket["run_count"] = bucket.get("run_count", 0) + 1


def record_result_usage(state: dict, key: str, result: "RunResult") -> bool:
    """record_usage for a whole RunResult — the only shape any caller actually has.
    Returns whether anything was recorded, so a caller can skip persisting state it
    did not change.

    Every call site (orchestrator, both mention paths) was hand-unpacking the same
    five usage fields behind the same `status != "skipped"` guard. Five keyword
    arguments copied three times is five chances to drop one silently: usage would
    simply under-count, and nothing would fail. Adding a sixth usage field should
    mean editing one function, not finding every caller."""
    if result.status == "skipped":  # dry-run — never reached the API, so nothing was spent
        return False
    record_usage(
        state, key,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        cache_read_tokens=result.cache_read_tokens, cost_usd=result.cost_usd,
    )
    # ...and keep THIS run on its own, not just folded into the totals. The Slack footer
    # reports one message's cost, and an announcement is decoupled from the run that
    # produced it (a task is announced by the scheduler that ran it, so the announcer
    # never holds the RunResult). This snapshot is the only thing that can answer "what
    # did this reply cost" at announce time.
    state["agents"][key]["last"] = {
        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
        "cache_creation_tokens": result.cache_creation_tokens,
        "cache_read_tokens": result.cache_read_tokens, "cost_usd": result.cost_usd,
        "duration_s": round(result.duration_s, 1),
        "model": result.model, "tools_available": result.tools_available,
        "provider": result.provider,
    }
    return True


def pending_approvals_snippet(state: dict, limit: int = 10) -> str:
    """Formats the un-clicked APPROVAL_REQUESTs across ALL threads so a session can
    see what has already been put to the approver — the cross-thread dedup signal.
    Empty string when nothing is pending (callers skip injection)."""
    pending = state.get("pending_approvals", [])[-limit:]
    if not pending:
        return ""
    lines = [f"- [{p['requester']} @ thread {p['thread_ts']}] {p['summary']}" for p in pending]
    return (
        "APPROVAL_REQUESTs already awaiting the approver (across all threads). Do "
        "not re-request the same action — reference the existing request instead:\n"
        + "\n".join(lines)
    )


def recent_context_snippet(state: dict, limit: int = 5) -> str:
    """Cross-agent context handoff: newly-spawned agents get the last few findings
    so they can cross-reference known issues instead of rediscovering them."""
    findings = state["recent_findings"][-limit:]
    if not findings:
        return "(no prior findings recorded yet)"
    lines = [f"- [{f['agent']}] {f['summary']}" for f in findings]
    return "Recent harness findings (most recent last):\n" + "\n".join(lines)
