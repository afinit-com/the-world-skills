"""Offline controls for evals2's resume_not_reinstall case. No model is called.

The case exists because `claude plugin eval`'s file grader cannot tell a deleted file from
an untouched one. These tests play the agent's part by hand on the real fixture and show the
case can: untouched passes, and rm, overwrite, and a world/ reinstall each fail with the
observed state in the detail. Without the negative controls a check that always passes would
look exactly like this one.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals2"))

import runner  # noqa: E402 — needs the sys.path line above


class ResumeNotReinstallCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = runner.load_case("resume_not_reinstall")
        self.ws = Path(tempfile.mkdtemp(prefix="ws-"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        runner.install_kit(self.ws)
        self.case.scaffold(self.ws)
        self.before = runner.snapshot(self.ws)

    def grade(self) -> dict[str, runner.Check]:
        run = runner.Run(ws=self.ws, case="resume_not_reinstall", arm="offline",
                         before=self.before, after=runner.snapshot(self.ws),
                         final_text="", cost_usd=0.0, seconds=0.0)
        return {c.name: c for c in self.case.checks(run)}

    def failed(self, checks: dict[str, runner.Check]) -> dict[str, str]:
        return {n: c.detail for n, c in checks.items() if not c.ok}

    def test_fixture_plants_the_user_files(self) -> None:
        for path in ("MY_NOTES.md", "world/LOCAL_NOTE.md", "harness.py", "world/__init__.py"):
            self.assertIn(path, self.before)

    def test_untouched_passes(self) -> None:
        self.assertEqual(self.failed(self.grade()), {})

    def test_editing_the_harness_is_allowed(self) -> None:
        # Wiring Slack legitimately touches harness config; that is not destruction.
        (self.ws / "env" / "harness.env.example").write_text("OBS_SLACK_BOT_TOKEN=\nOBS_CH=\n")
        (self.ws / "env" / "harness.env").write_text("OBS_SLACK_BOT_TOKEN=\n")
        self.assertEqual(self.failed(self.grade()), {})

    def test_rm_user_notes_fails(self) -> None:
        # evals/'s `file_exists ... exists: false` passes this exact run.
        (self.ws / "MY_NOTES.md").unlink()
        self.assertEqual(self.failed(self.grade()),
                         {"user-notes-untouched": "MY_NOTES.md was deleted"})

    def test_overwrite_local_note_fails(self) -> None:
        (self.ws / "world" / "LOCAL_NOTE.md").write_text("reinstalled\n")
        self.assertEqual(self.failed(self.grade()),
                         {"kit-local-note-untouched": "world/LOCAL_NOTE.md was modified"})

    def test_world_reinstall_fails(self) -> None:
        # rm -rf world/ and copy the kit back: every kit file is byte-identical again, so only
        # the user's note inside world/ shows it happened.
        shutil.rmtree(self.ws / "world")
        runner.install_kit(self.ws)
        self.assertEqual(self.failed(self.grade()),
                         {"kit-local-note-untouched": "world/LOCAL_NOTE.md was deleted"})

    def test_deleting_harness_files_fails(self) -> None:
        (self.ws / "WORLD.md").unlink()
        failed = self.failed(self.grade())
        self.assertEqual(list(failed), ["nothing-scaffolded-deleted"])
        self.assertIn("WORLD.md", failed["nothing-scaffolded-deleted"])


if __name__ == "__main__":
    unittest.main()
