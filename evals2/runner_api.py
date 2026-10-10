"""The surface a case imports. Kept separate from runner.py so a case module stays
importable on its own (for a syntax check, or for reading it without running anything)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Check:
    """One assertion against what the run produced.

    `detail` is read only when the check fails, so put the OBSERVED value in it, not the
    expectation. "write_scope=('.',) task_allowed=True" tells you what to fix; "scope should
    be narrow" does not.
    """

    name: str
    weight: int
    ok: bool
    detail: str = ""
