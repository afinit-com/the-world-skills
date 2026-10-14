#!/usr/bin/env python3
"""ops-lock — run a command while holding this harness's ops lock.

    python bin/ops-lock.py logs/.watchdog/lock --wait-s 600 -- <command> [args...]

scripts/<name>-up (up, down, restart) and scripts/<name>-watchdog run themselves under
it, so "is it running / is it marked down" and "start it" happen as one step. Without
it, six `up`s at once each saw no harness and started one, and a watchdog that checked
the down marker just before a `down` finished started the harness right back up.

flock(2) rather than a lock directory: the kernel drops the lock when the last process
holding it exits, however it exits, so there is no stale lock to detect and no race in
reclaiming one.

The command holds the lock too, on fd 9 (LOCK_FD). Were only this process to hold it, a
SIGKILL here would release the lock while the command was still mid-way through its
critical section, and a second `up` would start a second harness. So the lock lives as
long as the command does. The scripts close fd 9 (`9>&-`) on everything they start in
the background, so a long-running harness or console writer never holds it. POSIX only,
like the scripts.

Exit status is the command's, or 75 (EX_TEMPFAIL) when the lock stayed busy for
--wait-s seconds, with the holder's pid on stderr.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

BUSY = 75
LOCK_FD = 9


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="Run a command under the harness's ops lock.",
                                 usage="%(prog)s LOCK [--wait-s S] -- COMMAND [ARGS...]")
    ap.add_argument("lock", type=Path)
    ap.add_argument("--wait-s", type=float, default=600)
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        ap.error("no command after --")
    split = argv.index("--")
    args = ap.parse_args(argv[:split])
    command = argv[split + 1:]

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.lock, os.O_RDWR | os.O_CREAT, 0o644)  # non-inheritable (PEP 446)
    deadline = time.monotonic() + args.wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
                print(f"ops-lock: {args.lock} is held by pid {holder}; gave up after "
                      f"{args.wait_s:g}s", file=sys.stderr)
                return BUSY
            time.sleep(0.1)
    if fd != LOCK_FD:
        os.dup2(fd, LOCK_FD)  # same open file description, so the same lock
    child = subprocess.Popen(command, pass_fds=(LOCK_FD,))
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{child.pid}\n".encode(), 0)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue  # the child got the same SIGINT; its exit status is the answer


if __name__ == "__main__":
    raise SystemExit(main())
