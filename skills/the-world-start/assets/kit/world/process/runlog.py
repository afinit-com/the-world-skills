"""Keep logs/harness.jsonl bounded: one dated gzip per day, and a cap within the day.

The run log used to grow for as long as the harness ran, and the
only way to bound it was an external rotator per harness. The kit writes this file, so
the kit bounds it:

  - The first append of a new local day moves the earlier content to
    `harness.<YYYY-MM-DD>.jsonl.gz`, dated by the day it was last written. A person
    looking for "that day's log" finds it by name; a numbered scheme shifts every roll.
  - An append that finds the file over `run_log_max_bytes` rolls it to
    `harness.<YYYY-MM-DD>-<HHMMSS>.jsonl.gz` first.
  - `run_log_keep_days` deletes dated gzips older than that many days, each time the
    file rolls. It is None by default: a harness keeps every rolled day until someone
    decides otherwise, and the disk grows by roughly one compressed day per day.

Rolling is a rename, not copy-truncate. The only writer opens the file per entry
(append_run_log), so a rename loses no line, while copy-then-truncate could drop one
written between the copy and the truncate. A failed roll is logged and the append goes
ahead on the current file: losing the run record would be worse than an oversized file.

An append that has nothing to roll only stats the file. Scanning the directory for an
interrupted roll (recover()) happens on a process's first write to the file and after
a roll of its own left work behind; pruning happens when the file rolls.

The .gz is written under a temp name and renamed into place, and the plain file is
removed only after that. A roll cut short (a crash, a full disk) therefore leaves the
plain `harness.<date>.jsonl`, never a truncated .gz under the final name, and the next
write compresses it (recover()). bin/ops-log.py rolls the console log with this module.

Rolling, recovering and appending happen under an flock on `.<name>.lock` beside the
file, and the temp .gz carries the pid that writes it. Two processes on one file (an old
console writer still draining while a new one starts, a recovery overlapping a roll)
then take turns instead of writing one shared temp file into a corrupt gzip and deleting
the original. On Windows the same lock is a byte-range lock (msvcrt.locking), which the
OS likewise drops when its holder closes the file or dies.

Windows cannot rename a file another process holds open without FILE_SHARE_DELETE, which
neither Python nor PowerShell's Get-Content asks for. A roll that meets one (someone
following the console log) fails, is logged once, and is not tried again for
ROLL_RETRY_S; the lines keep going to the current file meanwhile. The same warning is
not repeated until a roll has worked, so a log followed for a day gets one line about
it, not one per line written.
"""

from __future__ import annotations

import contextlib
import gzip
import os
import shutil
import stat
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

RUN_LOG_MAX_BYTES = 10 * 1024 * 1024
# How long a failed roll or recovery waits before this process tries it again.
ROLL_RETRY_S = 300
# Windows locks are mandatory: a locked byte cannot be read by anyone else. Lock one far
# past the end, so a lock file can still carry its holder's pid at the start.
_WIN_LOCK_OFFSET = 0x7FFFFFF0


def open_lock_file(path: Path) -> int:
    """An fd on a lock file (created if missing), not inherited by child processes."""
    return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)


def try_lock(fd: int) -> bool:
    """Take an exclusive lock on the whole of `fd`'s file without waiting. The OS drops
    it when the fd is closed or the process dies, however it dies."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def lock(fd: int) -> None:
    """try_lock, waiting as long as it takes."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while not try_lock(fd):
        time.sleep(0.01)


def write_pid(fd: int, pid: int) -> None:
    """Replace the lock file's content with `pid` (for a message naming the holder)."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{pid}\n".encode())


@contextlib.contextmanager
def _locked(path: Path):
    """Hold the per-file roll lock. Blocking: every holder is quick (one append, or one
    roll of at most max_bytes)."""
    fd = open_lock_file(path.with_name(f".{path.name}.lock"))
    try:
        lock(fd)
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
    mode = stat.S_IMODE(plain.stat().st_mode)
    # Set the mode before gzip writes even its header; the umask must never broaden a
    # private log during compression, including recovery after an interrupted roll.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as raw:
        tmp.chmod(mode)
        with plain.open("rb") as src, gzip.GzipFile(fileobj=raw, mode="wb") as dst:
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
    mode = stat.S_IMODE(path.stat().st_mode)
    os.replace(path, plain)
    try:
        # Leave an empty current log with the old permissions even if compression
        # fails, or the caller only rolls now and appends in a later process.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.close(fd)
        path.chmod(mode)
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


class _RollState:
    """What this process knows about rolling one file. Read and changed only under the
    file's roll lock, which also keeps two threads of one process apart."""

    def __init__(self) -> None:
        self.recover_due = True  # first write in this process, or a roll left work behind
        self.retry_at = 0.0  # no roll or recovery before this, after one failed
        self.warned: str | None = None  # the last warning, said once until a roll works

    def warn(self, log: Callable[[str], None], msg: str) -> None:
        if msg != self.warned:
            log(msg)
            self.warned = msg

    def failed(self, now: float) -> None:
        self.retry_at = now + ROLL_RETRY_S


_states: dict[str, _RollState] = {}


def _state(path: Path) -> _RollState:
    return _states.setdefault(os.path.abspath(path), _RollState())


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
    state = _state(path)
    if now < state.retry_at:
        return None  # backing off after a failure; the line goes to the current file
    today = datetime.fromtimestamp(now).date()
    rolled = None
    try:
        st = path.stat()
    except FileNotFoundError:
        st = None
    if state.recover_due:
        try:
            for gz in recover(path):
                log(f"run log: finished an interrupted roll, {gz.name}")
        except OSError as exc:
            state.failed(now)
            state.warn(log, f"⚠️  could not finish an earlier roll of {path.name} ({exc}); its "
                            f"lines are kept uncompressed in {path.parent}")
        else:
            state.recover_due = False
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
                # The current file is fresh already; only the compression is left over.
                state.recover_due = True
                state.warn(log, f"⚠️  rolled {path.name} but {exc}; retried on the next write")
            except OSError as exc:
                state.failed(now)
                state.warn(log, f"⚠️  could not roll {path.name} ({exc}); still appending to "
                                f"it, which keeps growing until this is fixed")
            else:
                state.warned = None
                if keep_days is not None:
                    _prune(path, keep_days, today, log)
    return rolled


def _prune(path: Path, keep_days: int, today: date, log: Callable[[str], None]) -> None:
    try:
        for old in prune(path, keep_days, today):
            log(f"run log: deleted {old.name} (older than {keep_days} days)")
    except OSError as exc:
        log(f"⚠️  could not prune old {path.name} rolls ({exc})")
