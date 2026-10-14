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

The .gz is written under a temp name and renamed into place, and the plain file is
removed only after that. A roll cut short (a crash, a full disk) therefore leaves the
plain `harness.<date>.jsonl`, never a truncated .gz under the final name, and the next
write compresses it (recover()). bin/ops-log.py rolls the console log with this module.

Rolling, recovering and appending happen under an flock on `.<name>.lock` beside the
file, and the temp .gz carries the pid that writes it. Two processes on one file (an old
console writer still draining while a new one starts, a recovery overlapping a roll)
then take turns instead of writing one shared temp file into a corrupt gzip and deleting
the original. Windows has no fcntl; there the one harness process is the only writer.
"""

from __future__ import annotations

import contextlib
import gzip
import os
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

RUN_LOG_MAX_BYTES = 10 * 1024 * 1024


@contextlib.contextmanager
def _locked(path: Path):
    """Hold the per-file roll lock. Blocking: every holder is quick (one append, or one
    roll of at most max_bytes)."""
    if fcntl is None:
        yield
        return
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


def _rolled_path(path: Path, stamp: str) -> Path:
    """`harness.jsonl` -> `harness.<stamp>.jsonl.gz`, never one that already exists."""
    candidate = path.with_name(f"{path.stem}.{stamp}{path.suffix}.gz")
    n = 1
    while candidate.exists() or candidate.with_suffix("").exists():
        n += 1
        candidate = path.with_name(f"{path.stem}.{stamp}.{n}{path.suffix}.gz")
    return candidate


class CompressError(OSError):
    """The file was moved aside but not compressed; its lines are in `plain`."""

    def __init__(self, plain: Path, cause: BaseException):
        super().__init__(f"{plain.name} left uncompressed ({cause})")
        self.plain = plain


def _compress(plain: Path) -> Path:
    """`x.jsonl` -> `x.jsonl.gz`, written to a temp name and renamed into place, so a
    crash leaves either no .gz or a complete one, and the plain file until the end."""
    target = plain.with_name(plain.name + ".gz")
    tmp = plain.with_name(f"{plain.name}.gz.{os.getpid()}.tmp")
    with plain.open("rb") as src, gzip.open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, target)
    plain.unlink()
    return target


def roll(path: Path, stamp: str) -> Path:
    """Move `path` aside under `stamp` and compress it. Returns the .gz path.

    The rename happens first, so the next append starts a fresh file whatever happens to
    the compression. If compressing fails, the renamed plain file stays (the lines
    survive), CompressError says so, and recover() finishes it on a later call.
    """
    target = _rolled_path(path, stamp)
    plain = target.with_suffix("")  # harness.<stamp>.jsonl
    os.replace(path, plain)
    try:
        return _compress(plain)
    except OSError as exc:
        raise CompressError(plain, exc) from exc


def recover(path: Path) -> list[Path]:
    """Compress any roll a crash or an error left half-done: a renamed plain
    `<stem>.<date…><suffix>` whose .gz is missing, partial (only a .gz.tmp), or complete
    but not yet followed by removing the plain file. The plain file is the truth in all
    three, so it is compressed again."""
    done = []
    # Called under the roll lock, so any temp .gz left here belongs to a process that died.
    for tmp in path.parent.glob(f"{path.stem}.*{path.suffix}.gz.*.tmp"):
        tmp.unlink()
    for plain in path.parent.glob(f"{path.stem}.*{path.suffix}"):
        if not _dated(path, plain.name):
            continue
        done.append(_compress(plain))
    return done


def _dated(path: Path, name: str) -> bool:
    try:
        date.fromisoformat(name[len(path.stem) + 1:len(path.stem) + 11])
    except ValueError:
        return False
    return True


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

    Returns the .gz written, or None when nothing rolled.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        return _maybe_roll(path, max_bytes=max_bytes, keep_days=keep_days, now=now, log=log)


def append(path: Path, data: bytes, *, max_bytes: int | None = RUN_LOG_MAX_BYTES,
           keep_days: int | None = None, log: Callable[[str], None] = print) -> None:
    """Roll if due, then append `data`, as one step under the roll lock, so no other
    process can rename and compress the file between this open and this write. A failed
    roll is logged and the append still happens; a failed append raises."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        _maybe_roll(path, max_bytes=max_bytes, keep_days=keep_days, now=None, log=log)
        with path.open("ab") as f:
            f.write(data)


def _maybe_roll(path: Path, *, max_bytes: int | None, keep_days: int | None,
                now: float | None, log: Callable[[str], None]) -> Path | None:
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    rolled = None
    try:
        st = path.stat()
    except FileNotFoundError:
        st = None
    try:
        for gz in recover(path):
            log(f"run log: finished an interrupted roll, {gz.name}")
    except OSError as exc:
        log(f"⚠️  could not finish an earlier roll of {path.name} ({exc}); its lines are "
            f"kept uncompressed in {path.parent}")
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
            except CompressError as exc:
                log(f"⚠️  rolled {path.name} but {exc}; retried on the next write")
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
