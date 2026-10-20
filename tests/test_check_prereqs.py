"""start's Step 0 prerequisite check, with every input injected.

No real interpreter, PATH, or home directory is consulted: the version, `which`, the
environment, and the filesystem probe are all handed in, so the too-old case is a tuple and
not a Python 3.9 that has to exist on the machine running the suite.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/start/scripts/check-prereqs.py"

spec = importlib.util.spec_from_file_location("check_prereqs", SCRIPT)
prereqs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prereqs)

HOME = "/home/someone"
EVERYTHING = {"uv", "git", "claude", "codex", "slack"}


def fake_which(present):
    return lambda name: "/usr/bin/" + name if name in present else None


def run(argv, version=(3, 12, 1), present=EVERYTHING, env=None, files=(), system="Linux"):
    out = io.StringIO()
    code = prereqs.main(argv, out=out, version_info=version, which=fake_which(present),
                        system=system, env=env or {}, home=HOME,
                        exists=lambda path: path in files)
    return code, out.getvalue()


def by_name(output):
    return {entry["name"]: entry for entry in json.loads(output)["items"]}


class CheckPrereqsTests(unittest.TestCase):
    def test_all_present_passes(self) -> None:
        code, output = run(["--json"], env={"ANTHROPIC_API_KEY": "sk-secret-a",
                                            "OPENAI_API_KEY": "sk-secret-b"})
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["ready"])
        items = by_name(output)
        self.assertEqual(list(items), ["python", "uv", "git", "claude", "codex", "slack"])
        for entry in items.values():
            self.assertEqual(entry["status"], "ok")
            self.assertIsNone(entry["next"])
        self.assertNotIn("sk-secret", output)  # a key is never echoed

    def test_too_old_python_blocks_with_an_install_command(self) -> None:
        code, output = run(["--json"], version=(3, 9, 6))
        self.assertEqual(code, 1)
        python = by_name(output)["python"]
        self.assertEqual(python["status"], "too_old")
        self.assertIn("3.9.6", python["detail"])
        self.assertIn("uv python install", python["next"])
        _, text = run([], version=(3, 9, 6))
        self.assertIn("[TOO_OLD] python", text)
        self.assertIn("Install and verification wait on: python", text)

    def test_python_311_is_enough(self) -> None:
        self.assertEqual(by_name(run(["--json"], version=(3, 11, 0))[1])["python"]["status"], "ok")

    def test_missing_uv_or_git_blocks_and_names_the_install(self) -> None:
        for tool, hint in (("uv", "astral.sh/uv/install.sh"), ("git", "xcode-select")):
            with self.subTest(tool=tool):
                code, output = run(["--json"], present=EVERYTHING - {tool}, system="Darwin")
                self.assertEqual(code, 1)
                entry = by_name(output)[tool]
                self.assertEqual(entry["status"], "missing")
                self.assertIn(hint, entry["next"])

    def test_windows_gets_windows_commands(self) -> None:
        _, output = run(["--json"], present=set(), system="Windows")
        items = by_name(output)
        self.assertIn("install.ps1", items["uv"]["next"])
        self.assertIn("winget", items["git"]["next"])

    def test_runtime_and_slack_never_block(self) -> None:
        code, output = run(["--json"], present={"uv", "git"})
        self.assertEqual(code, 0)
        items = by_name(output)
        self.assertEqual(items["claude"]["status"], "missing")
        self.assertIn("/login", items["claude"]["next"])
        self.assertEqual(items["codex"]["status"], "missing")
        self.assertIn("codex login", items["codex"]["next"])
        self.assertEqual(items["slack"]["status"], "info")
        self.assertIn("slack-cli/install.sh", items["slack"]["next"])

    def test_runtime_flag_checks_only_the_chosen_cli(self) -> None:
        self.assertNotIn("codex", by_name(run(["--json", "--runtime", "claude"])[1]))
        self.assertNotIn("claude", by_name(run(["--json", "--runtime", "codex"])[1]))

    def test_login_is_read_from_local_traces(self) -> None:
        items = by_name(run(["--json"])[1])
        self.assertEqual(items["claude"]["status"], "login_unknown")
        self.assertEqual(items["codex"]["status"], "login_unknown")
        files = {os.path.join(HOME, ".claude", ".credentials.json"),
                 os.path.join("/elsewhere", "auth.json")}
        items = by_name(run(["--json"], files=files, env={"CODEX_HOME": "/elsewhere"})[1])
        self.assertEqual(items["claude"]["status"], "ok")
        self.assertEqual(items["codex"]["status"], "ok")

    def test_script_parses_as_python_36(self) -> None:
        # It has to run on the old interpreter it is reporting on, not crash on it.
        source = SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 6))
        futures = [node for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
        self.assertEqual(futures, [])


if __name__ == "__main__":
    unittest.main()
