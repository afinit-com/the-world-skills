"""Run log rolling and log timestamps (issue #4).

Before: Harness.log printed local time with no offset, so a launcher added a second
timestamp to every line, and logs/harness.jsonl grew forever.
"""

import contextlib
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import Harness, Persona  # noqa: E402
from world.process import runlog  # noqa: E402
from world.process.orchestrator import append_run_log  # noqa: E402


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
        self.assertFalse(self.path.exists())
        with gzip.open(rolled, "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"a": 1}\n')
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()), [rolled.name])

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
        lines = h.run_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["agent"], "x")
        self.assertEqual(len(list(h.logs_dir.glob("harness.*.jsonl.gz"))), 1)


if __name__ == "__main__":
    unittest.main()
