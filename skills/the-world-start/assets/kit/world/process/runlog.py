"""Keep logs/harness.jsonl bounded: one dated gzip per day, and a cap within the day.

The run log used to grow forever (one harness reached 4.8MB in eight weeks), and the
only way to bound it was an external rotator per harness. The kit writes this file, so
the kit bounds it:

  - The first append of a new local day moves the earlier content to
    `harness.<YYYY-MM-DD>.jsonl.gz`, dated by the day it was last written. A person
    looking for "that day's log" finds it by name; a numbered scheme shifts every roll.
  - An append that finds the file over `run_log_max_bytes` rolls it to
    `harness.<YYYY-MM-DD>-<HHMMSS>.jsonl.gz` first.
  - `run_log_keep_days` deletes dated gzips older than that many days. It is None by
    default: a harness keeps every rolled day until someone decides otherwise, and the
    disk grows by roughly one compressed day per day.

Rolling is a rename, not copy-truncate. The only writer opens the file per entry
(append_run_log), so a rename loses no line, while copy-then-truncate could drop one
written between the copy and the truncate. A failed roll is logged and the append goes
ahead on the current file: losing the run record would be worse than an oversized file.
"""

from __future__ import annotations

import gzip
import os
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

RUN_LOG_MAX_BYTES = 10 * 1024 * 1024


def _rolled_path(path: Path, stamp: str) -> Path:
    """`harness.jsonl` -> `harness.<stamp>.jsonl.gz`, never one that already exists."""
    candidate = path.with_name(f"{path.stem}.{stamp}{path.suffix}.gz")
    n = 1
    while candidate.exists() or candidate.with_suffix("").exists():
        n += 1
        candidate = path.with_name(f"{path.stem}.{stamp}.{n}{path.suffix}.gz")
    return candidate


def roll(path: Path, stamp: str) -> Path:
    """Move `path` aside under `stamp` and compress it. Returns the .gz path.

    The rename happens first, so the next append starts a fresh file whatever happens to
    the compression. If compressing fails, the renamed plain file is left in place (the
    lines survive) and the error propagates to the caller, which reports it.
    """
    target = _rolled_path(path, stamp)
    plain = target.with_suffix("")  # harness.<stamp>.jsonl
    os.replace(path, plain)
    with plain.open("rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    plain.unlink()
    return target


def prune(path: Path, keep_days: int, today: date) -> list[Path]:
    """Delete `<stem>.<YYYY-MM-DD>….jsonl.gz` files dated more than keep_days ago."""
    cutoff = today - timedelta(days=keep_days)
    removed = []
    for old in path.parent.glob(f"{path.stem}.*{path.suffix}.gz"):
        day = old.name[len(path.stem) + 1:len(path.stem) + 11]
        try:
            rolled_on = date.fromisoformat(day)
        except ValueError:
            continue  # not one of ours
        if rolled_on < cutoff:
            old.unlink()
            removed.append(old)
    return removed


def maybe_roll(path: Path, *, max_bytes: int | None = RUN_LOG_MAX_BYTES,
               keep_days: int | None = None, now: float | None = None,
               log: Callable[[str], None] = print) -> Path | None:
    """Roll `path` if it belongs to an earlier local day or is over max_bytes.

    Called before each append. Returns the .gz written, or None when nothing rolled.
    """
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    rolled = None
    try:
        st = path.stat()
    except FileNotFoundError:
        st = None
    if st is not None and st.st_size > 0:
        last_day = datetime.fromtimestamp(st.st_mtime).date()
        stamp = None
        if last_day < today:
            stamp = last_day.isoformat()
        elif max_bytes is not None and st.st_size >= max_bytes:
            stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d-%H%M%S")
        if stamp:
            try:
                rolled = roll(path, stamp)
            except OSError as exc:
                log(f"⚠️  could not roll {path.name} ({exc}); still appending to it, "
                    f"which keeps growing until this is fixed")
    if keep_days is not None:
        try:
            for old in prune(path, keep_days, today):
                log(f"run log: deleted {old.name} (older than {keep_days} days)")
        except OSError as exc:
            log(f"⚠️  could not prune old {path.name} rolls ({exc})")
    return rolled
