from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".codex" / "hooks" / "world-guard.py"


class CodexHookFixtureTests(unittest.TestCase):
    def run_hook(self, root: Path, payload: str | dict, *, agent: str = "watch"):
        env = os.environ.copy()
        env.update({"WORLD_HEADLESS": "1", "WORLD_AGENT": agent})
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=body,
            text=True,
            capture_output=True,
            cwd=root,
            env=env,
            check=False,
        )

    def payload(self, tool: str, command: str) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}

    def test_apply_patch_allows_only_declared_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".the-world").mkdir()
            (root / ".the-world" / "agent-scope.json").write_text(
                json.dumps({"watch": {"write_scope": ["findings/watch"]}})
            )
            allowed = self.run_hook(
                root,
                self.payload(
                    "apply_patch",
                    "*** Begin Patch\n*** Add File: findings/watch/ok.md\n+x\n*** End Patch",
                ),
            )
            denied = self.run_hook(
                root,
                self.payload(
                    "apply_patch", "*** Begin Patch\n*** Add File: outside.md\n+x\n*** End Patch"
                ),
            )
            self.assertEqual(allowed.stdout, "")
            self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_malformed_patch_and_input_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            malformed_input = self.run_hook(root, "not-json")
            malformed_patch = self.run_hook(root, self.payload("apply_patch", "not a patch"))
            self.assertEqual(json.loads(malformed_input.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(json.loads(malformed_patch.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_headless_git_write_and_integrity_mutation_are_denied(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            git = self.run_hook(root, self.payload("Bash", "git commit -m nope"))
            integrity = self.run_hook(root, self.payload("Bash", "rm .codex/hooks/world-guard.py"))
            read = self.run_hook(root, self.payload("Bash", "git status"))
            self.assertEqual(json.loads(git.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(json.loads(integrity.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(read.stdout, "")
