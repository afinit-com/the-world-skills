"""HEARTBEAT.md and STATUS.md rendering — the two human-readable views of `state`.

Split out of state.py. Nothing here mutates state or touches state.json;
these are pure functions of (harness, state) plus one guarded write. The split matters
because the two files answer different questions and have opposite git contracts:

  - HEARTBEAT.md is COMMITTED. It may hold only things that change when something
    happens — what is pending, what is open, what was found. A clock reading here makes
    the working tree dirty a minute after every commit, forever.
  - STATUS.md is GITIGNORED and regenerated every tick. Clocks, spend, live threads.

Both are regenerated wholesale on every save (state.save_state), so neither can drift
from state.json — there is no separate write path. Never hand-edit either.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..types import Schedule, ScheduleTrigger

if TYPE_CHECKING:
    from pathlib import Path

    from ..harness import Harness


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "never"
    delta = now - ts
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta / 60)}m ago"
    return f"{delta / 3600:.1f}h ago"


def _cadence(schedule: Schedule, now: float) -> str:
    """A scheduled agent's pulse: its fixed cadence and when it next fires, in the
    schedule's own zone (the zone its author wrote the time in)."""
    nxt = schedule.next_occurrence(datetime.fromtimestamp(now, tz=timezone.utc))
    return f"{schedule.describe()} (next {nxt:%Y-%m-%d %H:%M})"


def _utc(ts: float) -> str:
    """Absolute UTC, never 'N minutes ago'.

    HEARTBEAT.md is committed. A relative timestamp makes the file differ on every
    tick, so the working tree would be dirty a minute after any commit, forever, and
    the diff would never mean anything. Absolute times change only when the underlying
    event does — which is exactly when a watcher wants to see a diff."""
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts)) if ts else "—"


def _headline(text: str, limit: int = 140) -> str:
    """The first line that carries meaning. Findings hold whole markdown documents in
    their summary; a watchboard wants the title, not the document.

    "First non-empty line" is not enough: agents open with `---` frontmatter fences,
    horizontal rules, and bare `#`. A board whose every row reads `---` says nothing."""
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence  # skip the whole block, not just its fence line
            continue
        if in_fence or not line:
            continue
        if set(line) <= set("-=_*#"):
            continue  # a rule or a decoration, not a title
        line = line.lstrip("#*->` ").rstrip("*` ").strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return "(no title)"


def render_heartbeat(harness: "Harness", state: dict) -> str:
    """The WATCHBOARD — what a human watching this harness needs, and nothing else.

    Committed to git, so it must hold only things that change when something happens:
    what the harness subscribes to, what is waiting on the approver, what is open, what
    was found. No clocks, no cost tables, no "3m ago" — those live in STATUS.md, which
    is regenerated every tick and gitignored.

    The rule for adding a section here: would a watcher act on it? Agent uptime and
    token spend are things you look up when something is wrong; they are not the board.
    """
    lines = [f"# {harness.name} — watchboard", ""]

    pending = state.get("pending_approvals", [])
    lines += ["## Awaiting the approver", ""]
    if not pending:
        lines.append("(nothing pending)")
    for p in pending:
        lines.append(
            f"- **[{p['requester']}]** {_headline(p['summary'])} — asked {_utc(p.get('ts', 0))}"
        )

    pending_prs = state.get("pending_prs", [])
    if pending_prs:
        lines += ["", "## Open pull requests", ""]
        for p in pending_prs:
            lines.append(
                f"- [{p['repo']}#{p['number']}]({p['url']}) {p['title']} "
                f"— opened {_utc(p.get('opened_at', 0))}"
            )

    watched = [(a.name, w) for a in harness.agents for w in a.watches]
    if watched:
        lines += ["", "## Subscriptions", "",
                  "What each agent polls. Declared on the `Agent`, executed by its `check` —",
                  "if these two disagree, the code wins and this line is a lie.", ""]
        for name, w in watched:
            lines.append(f"- **{name}** — {w}")

    lines += ["", "## Recent findings", ""]
    findings = list(reversed(state.get("recent_findings", [])))
    if not findings:
        lines.append("(none yet)")
    for f in findings:
        lines.append(f"- `{_utc(f['ts'])}` **[{f['agent']}]** {_headline(f['summary'])}")

    return "\n".join(lines) + "\n"


def render_status(harness: "Harness", state: dict) -> str:
    """The volatile operational view: liveness, spend, live threads. Regenerated every
    tick, gitignored — everything here is a clock reading, useless as a diff."""
    now = time.time()
    lines = [
        f"# {harness.name} status",
        "",
        f"_generated {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} local — "
        "regenerated on every tick, do not hand-edit (world/process/heartbeat.py render_status)_",
        "",
    ]
    process = state.get("process")
    if process:
        lines += ["## Process", "",
                  f"- pid {process.get('pid')}, up since "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(process.get('started', now)))}",
                  f"- channel: {process.get('channel', 'unknown')}",
                  f"- log: {harness.run_log_path.relative_to(harness.home)}", ""]
    lines += ["## Agent pulse", ""]
    for agent in harness.agents:
        agent_state = state["agents"].get(agent.name, {})
        parts = []
        if isinstance(agent.trigger, ScheduleTrigger):
            parts.append(_cadence(agent.trigger.schedule, now))
        elif not agent.heartbeat_fields:
            lines.append(f"- **{agent.name}**: event-driven (no fixed cadence)")
            continue
        parts += [f"{f}={_ago(agent_state.get(f, 0), now)}" for f in agent.heartbeat_fields]
        lines.append(f"- **{agent.name}**: {', '.join(parts)}")

    rate_limited_until = state.get("rate_limited_until", 0)
    if rate_limited_until > now:
        lines.append(f"- ⚠️ rate-limited, backing off until {_ago(rate_limited_until, now)}")

    usage_rows = [(key, s) for key, s in state.get("agents", {}).items() if s.get("run_count")]
    if usage_rows:
        lines += ["", "## Token / cost usage (cumulative, since state.json existed)", "",
                  "| agent/identity | runs | cost (USD) | input | output | cache write | cache read |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for key, s in sorted(usage_rows, key=lambda kv: -kv[1].get("total_cost_usd", 0)):
            dollars = "미제공" if harness.provider == "codex" else f"${s.get('total_cost_usd', 0):.2f}"
            lines.append(
                f"| {key} | {s['run_count']} | {dollars} | "
                f"{s.get('total_input_tokens', 0):,} | {s.get('total_output_tokens', 0):,} | "
                f"{s.get('total_cache_creation_tokens', 0):,} | {s.get('total_cache_read_tokens', 0):,} |"
            )
        total_cost = sum(s.get("total_cost_usd", 0) for _, s in usage_rows)
        dollars = "미제공" if harness.provider == "codex" else f"${total_cost:.2f}"
        lines.append(f"| **total** | | **{dollars}** | | | | |")

    threads = state.get("threads", {})
    if threads:
        lines += ["", "## Slack threads", ""]
        for ts, meta in sorted(threads.items(), key=lambda kv: kv[1].get("last_message_ts", 0)):
            wt = f", worktree `{meta['worktree']}`" if meta.get("worktree") else ""
            lines.append(
                f"- `{meta.get('slug', ts)}` ({meta.get('origin', 'mention')}{wt}) — "
                f"last activity {_ago(meta.get('last_message_ts', 0), now)}"
            )

    return "\n".join(lines) + "\n"


def _write_if_changed(path: "Path", content: str) -> None:
    """Leave mtime and git status alone when nothing changed. HEARTBEAT.md is committed;
    rewriting identical bytes every 60s would make it look perpetually modified to any
    tool that watches mtimes."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    # The temp name is PER PROCESS. It used to be a single shared `<name>.tmp`, which is
    # safe for one harness and fatal for two: on a restart where the old process outlives
    # its tmux pane, both instances write the same temp path, the first rename consumes it,
    # and the second dies on FileNotFoundError. The losing instance takes a whole tick
    # down with it, and the symptom (a rename failing on a file this very function had just written) reads like
    # a filesystem fault rather than "there are two of me".
    #
    # os.replace is atomic, so the last writer simply wins; equivalent content either way.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave a stray temp behind
        raise


def write_heartbeat(harness: "Harness", state: dict) -> None:
    _write_if_changed(harness.heartbeat_path, render_heartbeat(harness, state))
    _write_if_changed(harness.status_path, render_status(harness, state))
