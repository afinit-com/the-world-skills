from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from world import Harness

KIT = Path(__file__).resolve().parents[1]


class GuardPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / "harness with spaces").resolve()
        self.root.mkdir()
        self.harness = Harness("test", home=self.root, git_root=self.root, provider="codex")
        self.script = self.root / "world-git-guard.py"

    def register(self, body, command=None, copies=1):
        self.script.write_text(body)
        registry = self.root / ".claude/settings.json"
        registry.parent.mkdir(exist_ok=True)
        registry.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": matcher, "hooks": [{
                "type": "command", "command": command or f'"{sys.executable}" "{self.script}"',
            }]} for matcher in ("Read", "Write")[:copies]
        ]}}))

    def test_real_probe_reports_exit_one_but_accepts_allow_and_block(self):
        for code in (0, 2, 1):
            with self.subTest(code=code):
                self.register(
                    "import json, os, sys\n"
                    "payload = json.load(sys.stdin)\n"
                    "assert payload['tool_name'] == 'Read'\n"
                    "assert payload['tool_input']['file_path'].endswith('WORLD.md')\n"
                    "assert payload['cwd'] == os.getcwd()\n"
                    "print('first line\\nlast diagnosis\\n  ', file=sys.stderr)\n"
                    f"sys.exit({code})\n"
                )
                with patch.object(self.harness, "log") as log:
                    problems = self.harness.preflight_guard_hooks()
                if code == 1:
                    self.assertEqual(len(problems), 1)
                    self.assertIn("last diagnosis", problems[0])
                    self.assertIn("exit 1", problems[0])
                    self.assertIn("GUARD HOOKS DISABLED", log.call_args_list[0].args[0])
                else:
                    self.assertEqual(problems, [])
                    log.assert_not_called()

    def test_launch_failure_and_timeout_are_reported_once(self):
        self.register("", copies=2)
        for error in (OSError("cannot launch"), subprocess.TimeoutExpired("guard", 10)):
            with self.subTest(error=error), patch("subprocess.run", side_effect=error) as run:
                problems = self.harness.preflight_guard_hooks()
            self.assertEqual(len(problems), 1)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_relative_script_runs_in_git_root_and_duplicate_matchers_probe_once(self):
        self.register("", command=f'"{sys.executable}" -I world-git-guard.py', copies=2)
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(self.harness.preflight_guard_hooks(), [])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [shutil.which(sys.executable), "-I", str(self.script)])
        self.assertEqual(run.call_args.kwargs["cwd"], self.root)

    def test_missing_interpreter_or_script_is_reported_once(self):
        self.register("", command='missing-world-python "world-git-guard.py"', copies=2)
        self.assertEqual(len(self.harness.preflight_guard_hooks()), 1)
        self.register("", copies=2)
        self.script.unlink()
        self.assertEqual(len(self.harness.preflight_guard_hooks()), 1)

    def test_non_posix_parser_quotes_are_removed_before_execution(self):
        self.register("")
        tokens = [f'"{sys.executable}"', f'"{self.script}"']
        with patch("shlex.split", return_value=tokens), patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run:
            self.assertEqual(self.harness.preflight_guard_hooks(), [])
        self.assertEqual(run.call_args.args[0], [shutil.which(sys.executable), str(self.script)])


class SystemPythonGuardTests(unittest.TestCase):
    def test_bundled_guards_load_under_system_python(self):
        interpreter = Path("/usr/bin/python3")
        if not interpreter.exists():
            self.skipTest("No /usr/bin/python3 on this machine")
        # Every guard the kit ships, and only those. `templates/` once held a second,
        # never-rendered copy of two of them; it was deleted on 2026-09-20 rather than
        # resynced, so there is no third place for the allowlist to drift to.
        scripts = [*KIT.glob(".claude/hooks/*.py"), *KIT.glob(".codex/hooks/*.py")]
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "WORLD.md"},
                              "cwd": str(KIT)})
        for script in scripts:
            with self.subTest(script=script.name):
                result = subprocess.run([str(interpreter), str(script)], input=payload, text=True,
                                        capture_output=True, cwd=KIT, timeout=10)
                self.assertIn(result.returncode, (0, 2), result.stderr)


class TaskTokenScopeTests(unittest.TestCase):
    """The guard hooks resolve `tasks/{task}/work` from WORLD_TASK, the same id the
    write tool resolves from the contextvar. If the two ever disagree the guard stops
    enforcing WITHOUT erroring, which is the failure mode worth a test."""

    def _expander(self, hook: str):
        namespace: dict = {}
        source = (KIT / hook).read_text()
        start = source.index("TASK_TOKEN = ")
        end = source.index("\n\n", source.index("def _expand_scope"))
        exec("import os\n" + source[start:end], namespace)  # noqa: S102
        return namespace["_expand_scope"]

    def test_both_hooks_expand_and_fail_closed(self) -> None:
        for hook in (".claude/hooks/world-scope-guard.py", ".codex/hooks/world-guard.py"):
            expand = self._expander(hook)
            with self.subTest(hook=hook), patch.dict(os.environ):
                os.environ["WORLD_TASK"] = "20260917-a3f2"
                self.assertEqual(
                    ["logs/x", "tasks/20260917-a3f2/work"],
                    expand(["logs/x", "tasks/{task}/work"]),
                )
                # No current task grants NOTHING, never the whole tasks/ tree.
                os.environ.pop("WORLD_TASK", None)
                self.assertEqual(["logs/x"], expand(["logs/x", "tasks/{task}/work"]))
