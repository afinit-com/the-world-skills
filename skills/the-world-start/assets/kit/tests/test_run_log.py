"""Run log rolling and log timestamps (issue #4).

Before: Harness.log printed local time with no offset, so a launcher added a second
timestamp to every line, and logs/harness.jsonl grew forever.
"""

import asyncio
import contextlib
import gzip
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import Harness, Persona  # noqa: E402
from world.process import runlog  # noqa: E402
from world.process.orchestrator import append_run_log, flush_run_log  # noqa: E402


def _at(day_offset: int, hour: int = 12) -> float:
    base = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=day_offset)).timestamp()


class HarnessLogTimestampTest(unittest.TestCase):
    def test_log_line_carries_iso_time_with_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(name="t", home=Path(tmp), main_persona=Persona("main", "Main", ""))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                h.log("hello")
        line = out.getvalue().strip()
        m = re.match(r"^(\S+) \[t\] hello$", line)
        self.assertIsNotNone(m, line)
        stamp = datetime.fromisoformat(m.group(1))
        self.assertIsNotNone(stamp.tzinfo, f"no UTC offset in {m.group(1)!r}")
        self.assertLess(abs(stamp.timestamp() - time.time()), 60)

    def test_harness_defaults_cap_but_never_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(name="t", home=Path(tmp), main_persona=Persona("main", "Main", ""))
        self.assertEqual(h.run_log_max_bytes, runlog.RUN_LOG_MAX_BYTES)
        self.assertIsNone(h.run_log_keep_days)


class RunLogRollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)
        self.path = self.logs / "harness.jsonl"
        self.messages: list[str] = []

    def _write(self, text: str, mtime: float) -> None:
        self.path.write_text(text, encoding="utf-8")
        os.utime(self.path, (mtime, mtime))

    def test_earlier_day_rolls_to_dated_gzip(self):
        yesterday = _at(-1)
        self._write('{"a": 1}\n', yesterday)
        rolled = runlog.maybe_roll(self.path, now=_at(0), log=self.messages.append)
        day = datetime.fromtimestamp(yesterday).date().isoformat()
        self.assertEqual(rolled, self.logs / f"harness.{day}.jsonl.gz")
        self.assertEqual(self.path.read_bytes(), b"")
        with gzip.open(rolled, "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"a": 1}\n')
        visible = sorted(p.name for p in self.logs.iterdir() if not p.name.startswith("."))
        self.assertEqual(visible, sorted([self.path.name, rolled.name]))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_roll_preserves_modes_before_writing_any_content(self):
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                self._write("private\n", _at(-1))
                self.path.chmod(mode)
                previous = os.umask(0o022)
                self.addCleanup(os.umask, previous)
                real_copy = runlog.shutil.copyfileobj

                def check_mode(src, dst):
                    tmp = next(self.logs.glob("*.tmp"))
                    self.assertEqual(stat.S_IMODE(tmp.stat().st_mode), mode)
                    return real_copy(src, dst)

                with mock.patch.object(runlog.shutil, "copyfileobj", side_effect=check_mode):
                    runlog.append(self.path, b"next\n", max_bytes=1)
                gz = max(self.logs.glob("*.gz"), key=lambda p: p.stat().st_mtime_ns)
                self.assertEqual(stat.S_IMODE(gz.stat().st_mode), mode)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_failed_compression_and_recovery_preserve_modes(self):
        self._write("private\n", _at(-1))
        self.path.chmod(0o600)
        with mock.patch.object(runlog.shutil, "copyfileobj", side_effect=OSError("disk full")):
            runlog.append(self.path, b"after failure\n", max_bytes=1, log=self.messages.append)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        runlog.append(self.path, b"after recovery\n", max_bytes=None, log=self.messages.append)
        for gz in self.logs.glob("*.gz"):
            self.assertEqual(stat.S_IMODE(gz.stat().st_mode), 0o600)

    def test_same_day_under_cap_is_left_alone(self):
        self._write('{"a": 1}\n', _at(0, hour=9))
        self.assertIsNone(runlog.maybe_roll(self.path, max_bytes=1000, now=_at(0, hour=10)))
        self.assertTrue(self.path.exists())

    def test_over_cap_rolls_with_time_in_name(self):
        self._write("x" * 100 + "\n", _at(0, hour=9))
        rolled = runlog.maybe_roll(self.path, max_bytes=50, now=_at(0, hour=10))
        self.assertIsNotNone(rolled)
        self.assertRegex(rolled.name, r"^harness\.\d{4}-\d{2}-\d{2}-100000\.jsonl\.gz$")

    def test_a_second_roll_with_the_same_name_does_not_overwrite(self):
        self._write("first\n", _at(-1))
        first = runlog.maybe_roll(self.path, now=_at(0))
        self._write("second\n", _at(-1))
        second = runlog.maybe_roll(self.path, now=_at(0))
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_nothing_is_deleted_by_default(self):
        old = self.logs / "harness.2000-01-01.jsonl.gz"
        old.write_bytes(b"")
        runlog.maybe_roll(self.path, now=_at(0))
        self.assertTrue(old.exists())

    def test_keep_days_prunes_only_old_dated_rolls(self):
        today = datetime.fromtimestamp(_at(0)).date()
        old = self.logs / f"harness.{(today - timedelta(days=40)).isoformat()}.jsonl.gz"
        recent = self.logs / f"harness.{(today - timedelta(days=3)).isoformat()}-101010.jsonl.gz"
        foreign = self.logs / "harness.notes.jsonl.gz"
        for p in (old, recent, foreign):
            p.write_bytes(b"")
        self._write("yesterday\n", _at(-1))
        runlog.maybe_roll(self.path, keep_days=30, now=_at(0), log=self.messages.append)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(any(old.name in m for m in self.messages))

    def test_failed_roll_is_loud_and_append_still_lands(self):
        home = Path(self.tmp.name)
        h = SimpleNamespace(logs_dir=home / "logs", run_log_path=home / "logs" / "harness.jsonl",
                            run_log_max_bytes=1, run_log_keep_days=None,
                            log=self.messages.append)
        h.logs_dir.mkdir()
        h.run_log_path.write_text("old\n", encoding="utf-8")
        real_replace = os.replace

        def refuse(*_args):
            raise PermissionError("denied")
        os.replace = refuse
        try:
            append_run_log(h, {"agent": "x", "status": "ok"})
            flush_run_log()
        finally:
            os.replace = real_replace
        self.assertTrue(any("could not roll" in m for m in self.messages), self.messages)
        lines = h.run_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "old")
        self.assertEqual(json.loads(lines[1])["agent"], "x")

    def test_a_crash_while_compressing_loses_nothing(self):
        """Compression dies halfway (the process was killed, the disk filled): the next
        write finishes the roll from the renamed plain file and appends as usual."""
        lines = "".join(f'{{"n": {n}}}\n' for n in range(5000))
        self._write(lines, _at(-1))
        real_copy = runlog.shutil.copyfileobj

        def die_halfway(src, dst):
            dst.write(src.read(1000))
            raise OSError("killed mid-compression")
        runlog.shutil.copyfileobj = die_halfway
        try:
            self.assertIsNone(runlog.maybe_roll(self.path, now=_at(0), log=self.messages.append))
        finally:
            runlog.shutil.copyfileobj = real_copy
        self.assertTrue(any("left uncompressed" in m for m in self.messages), self.messages)
        day = datetime.fromtimestamp(_at(-1)).date().isoformat()
        plain = self.logs / f"harness.{day}.jsonl"
        self.assertTrue(plain.exists())
        self.assertFalse((self.logs / f"harness.{day}.jsonl.gz").exists(),
                         "a partial .gz must never carry the final name")

        h = SimpleNamespace(logs_dir=self.logs, run_log_path=self.path, log=self.messages.append)
        append_run_log(h, {"n": "after"})
        flush_run_log()
        self.assertFalse(plain.exists())
        self.assertEqual(list(self.logs.glob("*.tmp")), [])
        with gzip.open(self.logs / f"harness.{day}.jsonl.gz", "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), lines)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["n"], "after")

    def test_a_crash_after_compressing_but_before_cleanup_is_finished(self):
        day = datetime.fromtimestamp(_at(-1)).date().isoformat()
        plain = self.logs / f"harness.{day}.jsonl"
        plain.write_text("kept\n", encoding="utf-8")
        with gzip.open(self.logs / f"harness.{day}.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("kept\n")
        runlog.maybe_roll(self.path, now=_at(0), log=self.messages.append)
        self.assertFalse(plain.exists())
        with gzip.open(self.logs / f"harness.{day}.jsonl.gz", "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "kept\n")

    def test_append_run_log_rolls_before_writing(self):
        home = Path(self.tmp.name)
        h = SimpleNamespace(logs_dir=home / "logs", run_log_path=home / "logs" / "harness.jsonl",
                            log=self.messages.append)
        h.logs_dir.mkdir()
        h.run_log_path.write_text('{"old": true}\n', encoding="utf-8")
        os.utime(h.run_log_path, (_at(-2), _at(-2)))
        append_run_log(h, {"agent": "x", "status": "ok"})
        flush_run_log()
        lines = h.run_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["agent"], "x")
        self.assertEqual(len(list(h.logs_dir.glob("harness.*.jsonl.gz"))), 1)


class RollCostTest(unittest.TestCase):
    """Issue #8: every append used to glob the directory (recover, prune) and retry a
    failing roll, with a warning, on every line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)
        self.path = self.logs / "console.out"
        self.messages: list[str] = []

    def test_an_append_that_rolls_nothing_scans_nothing(self):
        runlog.append(self.path, b"first\n", keep_days=30, log=self.messages.append)
        with mock.patch.object(runlog, "recover", wraps=runlog.recover) as recover, \
                mock.patch.object(runlog, "prune", wraps=runlog.prune) as prune:
            for n in range(5):
                runlog.append(self.path, f"{n}\n".encode(), keep_days=30,
                              log=self.messages.append)
        self.assertEqual(recover.call_count, 0)
        self.assertEqual(prune.call_count, 0)

    def test_the_first_append_in_a_process_finishes_an_interrupted_roll(self):
        day = datetime.fromtimestamp(_at(-1)).date().isoformat()
        plain = self.logs / f"console.{day}.out"
        plain.write_text("kept\n", encoding="utf-8")
        runlog.append(self.path, b"next\n", log=self.messages.append)
        self.assertFalse(plain.exists())
        with gzip.open(self.logs / f"console.{day}.out.gz", "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "kept\n")

    def test_a_failing_roll_backs_off_and_warns_once(self):
        """Windows: someone follows the console log, so every rename fails. One warning
        and one attempt per ROLL_RETRY_S, not one of each per line."""
        clock = [time.time()]
        real_replace = os.replace
        attempts = []

        def refuse(src, dst):
            if Path(src) == self.path:
                attempts.append(clock[0])
                raise PermissionError("in use by another process")
            return real_replace(src, dst)

        with mock.patch.object(runlog.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(runlog.os, "replace", side_effect=refuse):
            for n in range(5):
                runlog.append(self.path, f"{n}\n".encode(), max_bytes=1, log=self.messages.append)
            self.assertEqual(len(attempts), 1)
            clock[0] += runlog.ROLL_RETRY_S + 1
            for n in range(5, 8):
                runlog.append(self.path, f"{n}\n".encode(), max_bytes=1, log=self.messages.append)
            self.assertEqual(len(attempts), 2)
        warnings = [m for m in self.messages if "could not roll" in m]
        self.assertEqual(len(warnings), 1, self.messages)
        self.assertEqual(self.path.read_text(encoding="utf-8").split(),
                         [str(n) for n in range(8)])

        clock[0] += runlog.ROLL_RETRY_S + 1
        with mock.patch.object(runlog.time, "time", side_effect=lambda: clock[0]):
            runlog.append(self.path, b"8\n", max_bytes=1, log=self.messages.append)
        self.assertEqual(self.path.read_bytes(), b"8\n")
        self.assertEqual(len(list(self.logs.glob("console.*.out.gz"))), 1)

    def test_a_warning_comes_back_after_the_roll_has_worked_once(self):
        clock = [time.time()]
        fail = [True]
        real_replace = os.replace

        def flaky(src, dst):
            if fail[0] and Path(src) == self.path:
                raise PermissionError("in use by another process")
            return real_replace(src, dst)

        with mock.patch.object(runlog.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(runlog.os, "replace", side_effect=flaky):
            runlog.append(self.path, b"a\n", max_bytes=1, log=self.messages.append)
            runlog.append(self.path, b"b\n", max_bytes=1, log=self.messages.append)
            fail[0] = False
            clock[0] += runlog.ROLL_RETRY_S + 1
            runlog.append(self.path, b"c\n", max_bytes=1, log=self.messages.append)
            fail[0] = True
            clock[0] += 1
            runlog.append(self.path, b"d\n", max_bytes=1, log=self.messages.append)
        warnings = [m for m in self.messages if "could not roll" in m]
        self.assertEqual(len(warnings), 2, self.messages)


class RunLogOffTheLoopTest(unittest.TestCase):
    """R5: append_run_log ran runlog.append (an flock, and a roll that gzips up to 10MB)
    inside async code, so a roll held up the event loop and with it Slack's 3s ack."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.h = SimpleNamespace(logs_dir=home / "logs", run_log_path=home / "logs" / "harness.jsonl",
                                 log=lambda _m: None)
        self.h.logs_dir.mkdir()

    def test_a_slow_write_does_not_hold_the_event_loop_and_keeps_order(self):
        real_append = runlog.append
        written: list[int] = []
        busy = [0, 0]  # writers now, most at once
        lock = threading.Lock()

        def slow_append(path, data, **kw):
            with lock:
                busy[0] += 1
                busy[1] = max(busy)
            time.sleep(0.2)
            written.append(json.loads(data)["n"])
            real_append(path, data, **kw)
            with lock:
                busy[0] -= 1

        async def scenario():
            start = time.monotonic()
            for n in range(4):
                append_run_log(self.h, {"n": n})
            returned = time.monotonic() - start
            await asyncio.sleep(0)
            await asyncio.to_thread(flush_run_log)
            return returned

        with mock.patch.object(runlog, "append", side_effect=slow_append):
            returned = asyncio.run(scenario())
        self.assertLess(returned, 0.1, "append_run_log waited for the write")
        self.assertEqual(written, [0, 1, 2, 3])
        self.assertEqual(busy[1], 1, "two writes ran at once")
        lines = self.h.run_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["n"] for line in lines], [0, 1, 2, 3])

    def test_a_failed_write_is_logged_not_raised(self):
        messages: list[str] = []
        self.h.log = messages.append
        with mock.patch.object(runlog, "append", side_effect=OSError("disk full")):
            append_run_log(self.h, {"agent": "x", "status": "ok"})
            flush_run_log()
        self.assertTrue(any("disk full" in m for m in messages), messages)

    def test_entries_still_queued_at_exit_are_written(self):
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from world.process import orchestrator, runlog\n"
            "real = runlog.append\n"
            "def slow(*a, **k):\n"
            "    time.sleep(0.2)\n"
            "    real(*a, **k)\n"
            "runlog.append = slow\n"
            "p = Path(sys.argv[1])\n"
            "h = SimpleNamespace(run_log_path=p, log=print)\n"
            "for n in range(3):\n"
            "    orchestrator.append_run_log(h, {'n': n})\n"
        )
        done = subprocess.run([sys.executable, "-c", script, str(self.h.run_log_path)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = self.h.run_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["n"] for line in lines], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
