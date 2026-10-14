#!/usr/bin/env python3
"""ops-log — the one writer of a console log, which rolls the file itself.

    <harness> 2>&1 | python bin/ops-log.py --file logs/console.out [--keep-days N]
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

`--roll-only` rolls a file whose writers take turns under the ops lock (the watchdog's
own log) and exits. Standard library only; runlog.py is loaded by path, not through the
`world` package, so a broken harness import cannot take its console log down with it.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

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


def main(argv: list[str] | None = None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="Write stdin to a log file that rolls itself.")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--max-bytes", type=int, default=None)
    ap.add_argument("--keep-days", type=int, default=None)
    ap.add_argument("--roll-only", action="store_true")
    args = ap.parse_args(argv)
    runlog = _runlog()
    max_bytes = args.max_bytes if args.max_bytes is not None else runlog.RUN_LOG_MAX_BYTES

    def log(msg: str) -> None:
        print(f"ops-log: {msg}", file=sys.stderr, flush=True)

    def roll() -> None:
        runlog.maybe_roll(args.file, max_bytes=max_bytes, keep_days=args.keep_days, log=log)

    args.file.parent.mkdir(parents=True, exist_ok=True)
    if args.roll_only:
        roll()
        return 0
    # The terminal that ran `up` may close; the harness is nohup'd and so is its log.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    stdin = stdin or sys.stdin.buffer
    for line in iter(stdin.readline, b""):
        roll()
        # Opened per line, so no handle is held across a roll (Windows cannot rename an
        # open file) and a line always lands in the file that is current now.
        with args.file.open("ab") as out:
            out.write(stamp(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
