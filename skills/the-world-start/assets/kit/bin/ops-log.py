#!/usr/bin/env python3
"""ops-log — the one writer of a console log, which rolls the file itself.

    <harness> 2>&1 | python bin/ops-log.py --file logs/console.out [--keep-days N]
    python bin/ops-log.py --file logs/console.out --check     # exit 0 if a writer is alive
    python bin/ops-log.py --file logs/watchdog.log --roll-only [--keep-days N]
    python bin/ops-log.py --file logs/console.out --detach -- <harness command...>
    ... --err-file logs/ops-log.err   # its own warnings go there instead of stderr

scripts/<name>-up pipes the harness's stdout and stderr through this. Each line is
written as it arrives, prefixed with an ISO time and UTC offset unless it already starts
with one (Harness.log lines do; a traceback does not). The file rolls exactly like
logs/harness.jsonl (world/process/runlog.py): to `<stem>.<YYYY-MM-DD><ext>.gz` on the
first line of a new day, and early past 10MB.

Why a writer and not the watchdog: rolling from outside means copy-then-truncate, and a
line appended after the copy read end-of-file and before the truncate is in neither the
copy nor the file. Here the process that rolls is the process that writes, so the rename
happens between two lines and no line is in flight.

One writer per file. It holds a lock on `.<name>.writer` (its pid inside; flock, or a
byte-range lock on Windows) for its whole life, so a second writer exits at once instead
of rolling the same file, and `--check` tells the launcher whether one is alive without
trusting a pid file.

It never stops reading. A line it cannot write (disk full, the file made read-only) is
dropped, counted in `.<name>.dropping` beside the log and reported once; once writes
work again, the log gets one line saying how many were lost. Stopping would close the
pipe, and the harness would die of EPIPE at whatever line it printed next.

Its own warnings (a line it had to drop, a roll that failed) go to stderr, or with
`--err-file` to that file, opened for each warning and closed again under the same lock
and rolling as the log itself (at ERR_MAX_BYTES). A launcher that redirected stderr to a
file instead held it open for the writer's whole life, and an open file can neither be
rolled from outside nor, on Windows, renamed. An unexpected traceback still goes to
stderr.

`--roll-only` rolls a file whose writers take turns under the ops lock (the watchdog's
own log) and exits.

`--detach -- COMMAND` is how scripts/<name>-up.ps1 starts a harness on Windows, where a
shell pipe between two background processes is not to be had from PowerShell 5.1. It
starts a writer in the background (no window, its own process group, out of the
caller's job when the job allows it, so it outlives the scheduled task that started it),
prints the writer's pid and returns. Its stderr is `ops-log.crash` beside the log, for a
traceback; its warnings go to `--err-file` when given. That writer (`--run`) takes the writer lock, then
starts COMMAND with its stdout and stderr on a pipe it reads, so the harness is the
writer's child and the same rules hold: the writer exits on EOF, and a harness whose
writer died gets EPIPE at its next line.

Standard library only; runlog.py is loaded by path, not through the `world` package, so
a broken harness import cannot take its console log down with it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMPED = re.compile(rb"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# The warnings file rolls early: a few lines a day is normal, a megabyte is a loop.
ERR_MAX_BYTES = 1024 * 1024


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


def hold_writer_lock(path: Path, wait_s: float = 0, runlog=None) -> int | None:
    """The fd holding this file's writer lock, or None if another writer has it.

    A writer starting up waits a moment (wait_s): `--check` takes the lock for an instant
    to see whether anyone holds it, and that instant must not turn a writer away."""
    runlog = runlog or _runlog()
    fd = runlog.open_lock_file(path.with_name(f".{path.name}.writer"))
    deadline = time.monotonic() + wait_s
    while not runlog.try_lock(fd):
        if time.monotonic() >= deadline:
            os.close(fd)
            return None
        time.sleep(0.05)
    runlog.write_pid(fd, os.getpid())
    return fd


def writer_pid(path: Path, runlog=None) -> str | None:
    """The live writer's pid, or None when no writer holds the lock."""
    lock = path.with_name(f".{path.name}.writer")
    if not lock.exists():
        return None
    fd = hold_writer_lock(path, runlog=runlog)
    if fd is not None:
        os.close(fd)
        return None
    return lock.read_text(encoding="utf-8").strip() or "?"


# Windows creation flags: no console window (the harness's own children inherit the
# hidden console instead of each opening one), a process group of its own (a Ctrl-C in
# the terminal that ran `up` does not reach it), and out of the caller's job object, so
# a scheduled task's round ending does not take the harness with it.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW = 0x08000000


def detach(argv: list[str], err_file: Path) -> int:
    """Start `ops-log.py --run` in the background and return its pid without waiting."""
    command = [sys.executable, str(Path(__file__).resolve()), *argv]
    with open(err_file, "ab") as err:
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                  close_fds=True)
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, **kw).pid
        flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(command, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB,
                                    **kw).pid
        except PermissionError:  # the caller's job forbids breakaway: stay in it
            return subprocess.Popen(command, creationflags=flags, **kw).pid


class Dropping:
    """Count the lines that could not be written, in `.<name>.dropping` (best effort:
    on a full disk that file cannot be written either, so `log` says it too)."""

    def __init__(self, path: Path, log=None):
        self.log = log or (lambda msg: print(f"ops-log: {msg}", file=sys.stderr, flush=True))
        self.name = path.name
        self.state = path.with_name(f".{path.name}.dropping")
        self.count = 0
        self.since = ""

    def failed(self, exc: OSError) -> None:
        if not self.count:
            self.since = datetime.now().astimezone().isoformat(timespec="seconds")
            self.log(f"cannot write {self.name} ({exc}); dropping lines until it can")
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
            self.log(f"writing {self.name} again after dropping {self.count} line(s)")
            self.count = 0
            self.state.unlink(missing_ok=True)


def main(argv: list[str] | None = None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="Write stdin to a log file that rolls itself.")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--max-bytes", type=int, default=None)
    ap.add_argument("--keep-days", type=int, default=None)
    ap.add_argument("--err-file", type=Path, default=None,
                    help="write this writer's warnings to this file instead of stderr")
    ap.add_argument("--roll-only", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="print the live writer's pid and exit 0, or exit 1 if there is none")
    ap.add_argument("--run", action="store_true",
                    help="start the command after -- and write its stdout and stderr")
    ap.add_argument("--detach", action="store_true",
                    help="--run in the background; print the writer's pid and return")
    argv = sys.argv[1:] if argv is None else list(argv)
    command: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    args = ap.parse_args(argv)
    if (args.run or args.detach) and not command:
        ap.error("--run and --detach need a command after --")
    runlog = _runlog()
    max_bytes = args.max_bytes if args.max_bytes is not None else runlog.RUN_LOG_MAX_BYTES

    def to_stderr(msg: str) -> None:
        print(f"ops-log: {msg}", file=sys.stderr, flush=True)

    def log(msg: str) -> None:
        if args.err_file is None:
            to_stderr(msg)
            return
        try:
            runlog.append(args.err_file, stamp(f"ops-log: {msg}\n".encode()),
                          max_bytes=ERR_MAX_BYTES, keep_days=args.keep_days, log=to_stderr)
        except OSError as exc:
            to_stderr(f"{msg} (and could not write {args.err_file.name}: {exc})")

    args.file.parent.mkdir(parents=True, exist_ok=True)
    if args.check:
        pid = writer_pid(args.file, runlog)
        if pid:
            print(pid)
        return 0 if pid else 1
    if args.roll_only:
        runlog.maybe_roll(args.file, max_bytes=max_bytes, keep_days=args.keep_days, log=log)
        return 0
    if args.detach:
        rest = ["--file", str(args.file.resolve()), "--run"]
        if args.max_bytes is not None:
            rest += ["--max-bytes", str(args.max_bytes)]
        if args.keep_days is not None:
            rest += ["--keep-days", str(args.keep_days)]
        if args.err_file is not None:
            rest += ["--err-file", str(args.err_file.resolve())]
        print(detach([*rest, "--", *command], args.file.parent / "ops-log.crash"), flush=True)
        return 0
    writer_lock = hold_writer_lock(args.file, wait_s=2, runlog=runlog)
    if writer_lock is None:
        log(f"another writer (pid {writer_pid(args.file, runlog)}) already writes {args.file}; "
            "exiting")
        return 1
    try:
        return _write(args, runlog, max_bytes, log, command, stdin)
    finally:
        os.close(writer_lock)


def _write(args, runlog, max_bytes: int, log, command: list[str], stdin) -> int:
    if hasattr(signal, "SIGHUP"):
        # The terminal that ran `up` may close; the harness is nohup'd and so is its log.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    child = None
    if args.run:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, close_fds=True)
        stdin = child.stdout
    stdin = stdin or sys.stdin.buffer
    dropping = Dropping(args.file, log)
    for line in iter(stdin.readline, b""):
        data = stamp(line)
        try:
            runlog.append(args.file, dropping.note() + data, max_bytes=max_bytes,
                          keep_days=args.keep_days, log=log)
        except OSError as exc:
            dropping.failed(exc)
        else:
            dropping.cleared()
    if child is not None:
        child.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
