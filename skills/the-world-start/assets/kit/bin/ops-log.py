#!/usr/bin/env python3
"""ops-log — the one writer of a console log, which rolls the file itself.

    <harness> 2>&1 | python bin/ops-log.py --file logs/console.out [--keep-days N]
    python bin/ops-log.py --file logs/console.out --check     # exit 0 if a writer is alive
    python bin/ops-log.py --file logs/watchdog.log --roll-only [--keep-days N]

scripts/<name>-up pipes the harness's stdout and stderr through this. Each line is
written as it arrives, prefixed with an ISO time and UTC offset unless it already starts
with one (Harness.log lines do; a traceback does not). The file rolls exactly like
logs/harness.jsonl (world/process/runlog.py): to `<stem>.<YYYY-MM-DD><ext>.gz` on the
first line of a new day, and early past 10MB.

Why a writer and not the watchdog: rolling from outside means copy-then-truncate, and a
line appended after the copy read end-of-file and before the truncate is in neither the
copy nor the file. Here the process that rolls is the process that writes, so the rename
happens between two lines and no line is in flight.

One writer per file. It holds an flock on `.<name>.writer` (its pid inside) for its whole
life, so a second writer exits at once instead of rolling the same file, and `--check`
tells the launcher whether one is alive without trusting a pid file.

It never stops reading. A line it cannot write (disk full, the file made read-only) is
dropped, counted in `.<name>.dropping` beside the log and reported on stderr; once writes
work again, the log gets one line saying how many were lost. Stopping would close the
pipe, and the harness would die of EPIPE at whatever line it printed next.

`--roll-only` rolls a file whose writers take turns under the ops lock (the watchdog's
own log) and exits. Standard library only; runlog.py is loaded by path, not through the
`world` package, so a broken harness import cannot take its console log down with it.
POSIX only, like the scripts that start it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: the ops scripts that run this are POSIX only
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
STAMPED = re.compile(rb"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _runlog():
    spec = importlib.util.spec_from_file_location("_ops_runlog",
                                                  ROOT / "world" / "process" / "runlog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(line: bytes) -> bytes:
    if STAMPED.match(line):
        return line
    return datetime.now().astimezone().isoformat(timespec="seconds").encode() + b" " + line


def hold_writer_lock(path: Path, wait_s: float = 0) -> int | None:
    """The fd holding this file's writer lock, or None if another writer has it.

    A writer starting up waits a moment (wait_s): `--check` takes the lock for an instant
    to see whether anyone holds it, and that instant must not turn a writer away."""
    fd = os.open(path.with_name(f".{path.name}.writer"), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def writer_pid(path: Path) -> str | None:
    """The live writer's pid, or None when no writer holds the lock."""
    lock = path.with_name(f".{path.name}.writer")
    if not lock.exists():
        return None
    fd = hold_writer_lock(path)
    if fd is not None:
        os.close(fd)
        return None
    return lock.read_text(encoding="utf-8").strip() or "?"


class Dropping:
    """Count the lines that could not be written, in `.<name>.dropping` (best effort:
    on a full disk that file cannot be written either, so stderr says it too)."""

    def __init__(self, path: Path):
        self.name = path.name
        self.state = path.with_name(f".{path.name}.dropping")
        self.count = 0
        self.since = ""

    def failed(self, exc: OSError) -> None:
        if not self.count:
            self.since = datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"ops-log: cannot write {self.name} ({exc}); dropping lines until it can",
                  file=sys.stderr, flush=True)
        self.count += 1
        try:
            self.state.write_text(f"{self.count} line(s) dropped since {self.since}: {exc}\n",
                                  encoding="utf-8")
        except OSError:
            pass

    def note(self) -> bytes:
        """The line to write ahead of the next one, saying what was lost."""
        if not self.count:
            return b""
        return stamp(f"ops-log: {self.count} line(s) were dropped between {self.since} and "
                     "now because the log could not be written\n".encode())

    def cleared(self) -> None:
        if self.count:
            print(f"ops-log: writing {self.name} again after dropping {self.count} line(s)",
                  file=sys.stderr, flush=True)
            self.count = 0
            self.state.unlink(missing_ok=True)


def main(argv: list[str] | None = None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="Write stdin to a log file that rolls itself.")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--max-bytes", type=int, default=None)
    ap.add_argument("--keep-days", type=int, default=None)
    ap.add_argument("--roll-only", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="print the live writer's pid and exit 0, or exit 1 if there is none")
    args = ap.parse_args(argv)
    runlog = _runlog()
    max_bytes = args.max_bytes if args.max_bytes is not None else runlog.RUN_LOG_MAX_BYTES

    def log(msg: str) -> None:
        print(f"ops-log: {msg}", file=sys.stderr, flush=True)

    args.file.parent.mkdir(parents=True, exist_ok=True)
    if args.check:
        pid = writer_pid(args.file)
        if pid:
            print(pid)
        return 0 if pid else 1
    if args.roll_only:
        runlog.maybe_roll(args.file, max_bytes=max_bytes, keep_days=args.keep_days, log=log)
        return 0
    if hold_writer_lock(args.file, wait_s=2) is None:
        log(f"another writer (pid {writer_pid(args.file)}) already writes {args.file}; exiting")
        return 1
    # The terminal that ran `up` may close; the harness is nohup'd and so is its log.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    stdin = stdin or sys.stdin.buffer
    dropping = Dropping(args.file)
    for line in iter(stdin.readline, b""):
        data = stamp(line)
        try:
            runlog.append(args.file, dropping.note() + data, max_bytes=max_bytes,
                          keep_days=args.keep_days, log=log)
        except OSError as exc:
            dropping.failed(exc)
        else:
            dropping.cleared()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
