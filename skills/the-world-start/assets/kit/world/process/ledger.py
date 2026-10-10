"""The decision ledger — one append-only line per human decision.

Split out of state.py, where it had always been a guest: state.json is
rewritten wholesale every tick and holds only what is CURRENTLY true; the ledger holds
what HAPPENED, and is never rewritten. Those are different jobs, different files, and
different failure modes — losing state.json costs a tick, losing the ledger costs the
only durable record of what a human actually approved or refused.

Kept separate from volatile state so audit history survives state regeneration.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..harness import Harness

MAX_REJECTIONS = 5


class LedgerFabricationError(RuntimeError):
    """Someone tried to record a human decision when no human could have made one."""


def mark_decisions_live(harness: "Harness") -> None:
    """Called once by SlackListener.start(), after auth_test() proves a real connection.

    Until then, nothing can reach a human, so nothing can carry a human's decision.
    """
    harness._decisions_live = True


def append_ledger(harness: "Harness", entry: dict) -> None:
    """One line per human decision (approve/deny buttons, message decisions).
    Append-only ground truth for a retro agent — never rewritten.

    ⚠️ What this writes is READ BACK INTO EVERY SPAWN by recent_rejections_snippet. A
    fabricated `denied` entry therefore appears in the prompt of every agent this harness
    runs, forever, telling them the approver refused something that was never asked. An
    append-only audit trail that feeds prompts is a prompt-injection surface: whatever
    can write it can steer every future agent.

    A fake listener holding a live Harness object could otherwise create invented
    decisions that look well-formed. Documentation cannot enforce this boundary.

    So the rule is executable now. A decision may only be recorded by a harness whose
    Slack listener has actually connected; that is the only path by which a human's
    click or message can arrive. Anything else — a test, a REPL, a migration script —
    gets LedgerFabricationError instead of a line in the audit trail. Tests that need to
    exercise this path build a stub harness with a `ledger_path` under a temp dir and
    call `mark_decisions_live()` on it, which makes the bypass deliberate and visible.
    """
    if not getattr(harness, "_decisions_live", False):
        raise LedgerFabricationError(
            f"refusing to append to {harness.ledger_path}: this harness's Slack listener "
            "never connected, so no human decision could have reached it. If you are "
            "testing, point ledger_path at a temp dir and call mark_decisions_live()."
        )
    entry = {"ts": time.time(), **entry}
    harness.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with harness.ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def approval_subject_tally(harness: "Harness") -> dict:
    """Count ledger decisions by subject (human vs co-approver) and outcome.

    This is the read side of the `by_kind` tag. It reports what the append-only ledger
    recorded and grants no authority.

    Returns e.g. {"human": {"approved": 12, "denied": 3},
                  "co_approver": {"approved": 1, "denied": 0}, "total": 16}.
    Entries written before the tag existed count as "human" (the only decider then).
    """
    path = harness.ledger_path
    tally: dict = {"human": {"approved": 0, "denied": 0},
                   "co_approver": {"approved": 0, "denied": 0}, "total": 0}
    if not path.exists():
        return tally
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            decision = e.get("decision")
            if decision not in ("approved", "denied"):
                continue
            kind = e.get("by_kind", "human")
            if kind not in tally:
                kind = "human"
            tally[kind][decision] += 1
            tally["total"] += 1
    except OSError:
        return tally
    return tally


def recent_rejections_snippet(harness: "Harness", limit: int = MAX_REJECTIONS) -> str:
    """The most recent DENIED decisions, so a spawned session can see what the approver
    already turned down. The cheap half of the feedback loop: an agent that re-pitches a
    rejected class of proposal every tick costs money and trust. Empty when there are
    none — callers skip injection."""
    path = harness.ledger_path
    if not path.exists():
        return ""
    denied = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("decision") == "denied":
                denied.append(e)
    except OSError:
        return ""
    if not denied:
        return ""
    lines = [f"- [{e.get('requester', '?')}] {str(e.get('summary', ''))[:140]}"
             for e in denied[-limit:]]
    return (
        "The approver recently DENIED these. Do not re-propose the same thing without "
        "new evidence:\n" + "\n".join(lines)
    )
