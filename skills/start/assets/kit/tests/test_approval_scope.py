from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from world.process import approval_scope
from world.slack.approvals import ApprovalMixin
from world.toolkit import make_approval_gated, result, tool


def _repo() -> Path:
    root = Path(tempfile.mkdtemp())
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True, capture_output=True)
    return root


class ChangedFilesTests(unittest.TestCase):
    def test_reports_modified_and_untracked(self) -> None:
        root = _repo()
        (root / "README.md").write_text("edited\n")
        (root / "migrations").mkdir()
        (root / "migrations" / "001.sql").write_text("ALTER TABLE t ADD COLUMN c STRING;")
        self.assertEqual(
            ["README.md", "migrations/001.sql"], approval_scope.changed_files(root)
        )

    def test_a_rename_does_not_corrupt_the_next_entry(self) -> None:
        """A rename spends a second NUL field on the source path. Read as an entry, its
        first three characters get sliced off as a status code."""
        root = _repo()
        (root / "a.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "a"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "mv", "a.txt", "b.txt"], check=True, capture_output=True)
        (root / "zzz.txt").write_text("later\n")
        files = approval_scope.changed_files(root)
        self.assertIn("b.txt", files)
        self.assertIn("zzz.txt", files)
        self.assertNotIn(".txt", files)     # "a.txt" mangled by a 3-char slice

    def test_no_repo_and_no_changes_both_pin_nothing(self) -> None:
        self.assertEqual(("", []), approval_scope.fingerprint(None))
        self.assertEqual(("", []), approval_scope.fingerprint(Path(tempfile.mkdtemp())))
        self.assertEqual(("", []), approval_scope.fingerprint(_repo()))

    def test_fingerprint_follows_content_not_filenames(self) -> None:
        root = _repo()
        target = root / "m.sql"
        target.write_text("ALTER TABLE t ADD COLUMN c STRING;")
        first, files = approval_scope.fingerprint(root)
        self.assertEqual(["m.sql"], files)
        target.write_text("DROP TABLE t;")
        second, _ = approval_scope.fingerprint(root)
        self.assertNotEqual(first, second)

    def test_describe_names_the_files(self) -> None:
        self.assertEqual("", approval_scope.describe([]))
        self.assertIn("`m.sql`", approval_scope.describe(["m.sql"]))
        self.assertIn("외 2개", approval_scope.describe([f"f{i}" for i in range(10)]))


class _Listener(ApprovalMixin):
    """The mixin under test with only what thread_approved touches."""

    def __init__(self, worktree: Path) -> None:
        self.state = {"approved_threads": {}, "worktrees": {str(worktree): {"thread_ts": "T"}}}
        self.harness = SimpleNamespace(log=lambda _t: None)
        self.saved = 0

    def save_state(self) -> None:
        self.saved += 1


class GateTests(unittest.TestCase):
    def test_grant_survives_an_unrelated_edit_it_already_covered(self) -> None:
        root = _repo()
        (root / "m.sql").write_text("ALTER TABLE t ADD COLUMN c STRING;")
        listener = _Listener(root)
        scope_hash, _ = approval_scope.fingerprint(root)
        listener.state["approved_threads"]["T"] = {"by": "U1", "scope_hash": scope_hash}
        self.assertTrue(listener.thread_approved("T"))
        self.assertTrue(listener.thread_approved("T"))  # checking twice must not consume

    def test_editing_the_approved_file_drops_the_grant(self) -> None:
        root = _repo()
        target = root / "m.sql"
        target.write_text("ALTER TABLE t ADD COLUMN c STRING;")
        listener = _Listener(root)
        scope_hash, _ = approval_scope.fingerprint(root)
        listener.state["approved_threads"]["T"] = {"by": "U1", "scope_hash": scope_hash}

        target.write_text("DROP TABLE t;")
        self.assertFalse(listener.thread_approved("T"))
        # Dropped, not just reported: a later call must not find it again.
        self.assertNotIn("T", listener.state["approved_threads"])
        self.assertIn("changed after the approval", listener.last_refusal("T"))
        self.assertEqual("", listener.last_refusal("T"))  # consumed on read

    def test_a_new_file_appearing_also_invalidates(self) -> None:
        root = _repo()
        (root / "m.sql").write_text("x")
        listener = _Listener(root)
        scope_hash, _ = approval_scope.fingerprint(root)
        listener.state["approved_threads"]["T"] = {"by": "U1", "scope_hash": scope_hash}
        (root / "sneaky.sql").write_text("DROP TABLE t;")
        self.assertFalse(listener.thread_approved("T"))

    def test_an_unpinned_grant_behaves_as_before(self) -> None:
        """Granted before this check existed, or with no worktree to pin against."""
        listener = _Listener(Path(tempfile.mkdtemp()))
        listener.state["approved_threads"]["T"] = {"by": "U1"}
        self.assertTrue(listener.thread_approved("T"))

    def test_no_grant_is_still_just_no(self) -> None:
        listener = _Listener(_repo())
        self.assertFalse(listener.thread_approved("T"))
        self.assertEqual("", listener.last_refusal("T"))


class RefusalMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_agent_is_told_which_refusal_this_is(self) -> None:
        @tool("do_it", "writes something outward", {})
        async def do_it(_args: dict) -> dict:
            return result(0, "done")

        async def run(refusal):
            gated = make_approval_gated(
                do_it, is_approved=lambda: False, consume=lambda: None, refusal=refusal
            )
            return (await gated.handler({}))["content"][0]["text"]

        self.assertIn("no approval is on record", await run(None))
        self.assertIn("files changed", await run(lambda: "the approved files changed"))


if __name__ == "__main__":
    unittest.main()
