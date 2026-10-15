from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
INSTALLER = KIT.parents[1] / "scripts" / "install-kit.py"


class InstallerOwnershipTests(unittest.TestCase):
    def run_installer(self, target: Path, provider: str = "claude") -> None:
        subprocess.run(
            ["python3", str(INSTALLER), str(target), "--provider", provider],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_rerun_replaces_kit_files_and_preserves_harness_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target)
            (target / "harness.py").write_text("# mine\n")
            (target / "world" / "toolkit.py").write_text("# stale kit copy\n")
            pyproject = (target / "pyproject.toml").read_text()

            self.run_installer(target)

            self.assertEqual("# mine\n", (target / "harness.py").read_text())
            self.assertNotEqual("# stale kit copy\n", (target / "world" / "toolkit.py").read_text())
            self.assertEqual(pyproject, (target / "pyproject.toml").read_text())

    def test_new_kit_files_back_up_conflicting_harness_content(self) -> None:
        paths = ("docs/verify.md", "docs/operations.md", "bin/ops-kit.py",
                 "templates/ops/up.ps1.tmpl")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            for rel in paths:
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("original harness content\n")
            command = [sys.executable, str(INSTALLER), str(target), "--provider", "codex"]
            first = subprocess.run(command, check=True, capture_output=True, text=True).stdout
            for rel in paths:
                dest = target / rel
                backup = dest.with_name(dest.name + ".bak")
                self.assertEqual(backup.read_text(), "original harness content\n")
                self.assertEqual(dest.read_bytes(), (KIT / rel).read_bytes())
                self.assertIn(rel + ".bak", first)
                dest.write_text("another edit\n")
            second = subprocess.run(command, check=True, capture_output=True, text=True).stdout
            for rel in paths:
                dest = target / rel
                self.assertEqual(dest.with_name(dest.name + ".bak").read_text(),
                                 "original harness content\n")
                self.assertEqual(dest.with_name(dest.name + ".bak.1").read_text(), "another edit\n")
                self.assertIn(rel + ".bak.1", second)
            third = subprocess.run(command, check=True, capture_output=True, text=True).stdout
            self.assertNotIn("backed up", third)
            self.assertEqual(len(list(target.rglob("*.bak*"))), 2 * len(paths))

    def test_installed_kit_can_render_the_character_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "codex")
            self.assertTrue((target / "docs" / "world-map.md").is_file())
            self.assertTrue((target / "docs" / "integrations.md").is_file())
            subprocess.run(
                [sys.executable, "-c",
                 "import runpy; runpy.run_path('bin/world-map-demo.py', run_name='__main__')",
                 "--world-map"],
                cwd=target, check=True, capture_output=True, text=True,
            )
            output = (target / "WORLD.html").read_text()
            self.assertIn("별빛 탐험대", output)
            self.assertNotIn("__WORLD_DATA__", output)
            self.assertIn("WORLD.html", (target / ".gitignore").read_text())

    def test_installed_kit_prints_a_verification_plan_without_running_anything(self) -> None:
        # Issue #2: the flow past the dry run ships with the harness, and the plan is read
        # from harness.py so it names this harness's cast rather than a generic list.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "codex")
            self.assertTrue((target / "docs" / "verify.md").is_file())
            self.assertTrue((target / "world" / "tool_check.py").is_file())
            plan = subprocess.run(
                [sys.executable, "-c",
                 "import runpy; runpy.run_path('bin/world-map-demo.py', run_name='__main__')",
                 "--verify-plan"],
                cwd=target, check=True, capture_output=True, text=True,
                env={**os.environ, "PYTHONUTF8": "1"},
            ).stdout
            for step in ("## 1. Local checks", "## 2. Tool boundaries", "## 4. A real thread",
                         "## 6. Report"):
                self.assertIn(step, plan)
            self.assertFalse((target / "state.json").exists())
            self.assertFalse((target / "WORLD.html").exists())

    def test_verify_word_scaffolds_its_own_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "codex")
            subprocess.run(
                ["python3", str(target / "bin/scaffold-world-skill.py"), "inspect", "verify",
                 "--host", "codex", "--universe", "Crew", "--description", "Inspect the ship"],
                check=True, capture_output=True,
            )
            body = (target / ".agents/skills/inspect/SKILL.md").read_text()
            self.assertIn("name: inspect", body)
            self.assertIn("harness.py --verify-plan", body)
            self.assertIn("docs/verify.md", body)
            self.assertIn("Never press an approve or deny", " ".join(body.split()))
            self.assertNotIn("{{", body)

    def test_installed_slack_provisioner_renders_from_the_installed_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "codex")
            spec = target / "slack-app" / "slack-apps.json"
            spec.parent.mkdir()
            spec.write_text(json.dumps({"prefix": "Obs", "apps": [
                {"key": "lumen", "display": "Lumen", "main": True,
                 "bot_token_env": "OBS_SLACK_BOT_TOKEN", "app_token_env": "OBS_SLACK_APP_TOKEN"}]}))
            subprocess.run([sys.executable, "bin/slack-provision.py", "--dry-run"],
                           cwd=target, check=True, capture_output=True, text=True)
            manifest = json.loads((spec.parent / "lumen" / "manifest.json").read_text())
            self.assertEqual(manifest["display_information"]["name"], "Obs-Lumen")
            self.assertIn(".slack/", (target / ".gitignore").read_text())

    def test_settings_merge_preserves_unrelated_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            (target / ".claude").mkdir(parents=True)
            existing = {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "python3 mine.py"}]}
                    ]
                }
            }
            (target / ".claude" / "settings.json").write_text(json.dumps(existing))
            self.run_installer(target)
            merged = json.loads((target / ".claude" / "settings.json").read_text())
            self.assertEqual(
                "python3 mine.py", merged["hooks"]["Stop"][0]["hooks"][0]["command"]
            )
            self.assertIn("PreToolUse", merged["hooks"])

    def test_provider_modes_install_only_requested_platform_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "claude"
            codex = root / "codex"
            both = root / "both"
            self.run_installer(claude, "claude")
            self.run_installer(codex, "codex")
            self.run_installer(both, "both")
            self.assertTrue((claude / ".claude" / "settings.json").exists())
            self.assertFalse((claude / ".codex").exists())
            self.assertTrue((codex / ".codex" / "hooks.json").exists())
            self.assertFalse((codex / ".claude").exists())
            self.assertNotIn("claude-agent-sdk", (codex / "pyproject.toml").read_text())
            self.assertTrue((both / ".claude" / "settings.json").exists())
            self.assertTrue((both / ".codex" / "hooks.json").exists())
            self.assertIn("claude-agent-sdk", (both / "pyproject.toml").read_text())

    def test_both_upgrade_preserves_existing_codex_hooks_and_claude_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "claude")
            (target / ".codex").mkdir()
            existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
            (target / ".codex" / "hooks.json").write_text(json.dumps(existing))
            user_file = target / ".claude" / "user-note.md"
            user_file.write_text("mine")
            user_hook = target / ".claude" / "hooks" / "mine.py"
            user_hook.write_text("mine")
            self.run_installer(target, "both")
            merged = json.loads((target / ".codex" / "hooks.json").read_text())
            self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["command"], "mine")
            self.assertEqual(user_file.read_text(), "mine")
            self.assertEqual(user_hook.read_text(), "mine")

    def test_authoring_host_is_independent_of_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "codex")
            command = [
                "python3", str(target / "bin/scaffold-world-skill.py"), "recruit", "agent",
                "--provider", "codex", "--host", "claude",
                "--universe", "Crew", "--description", "Recruit a collaborator",
            ]
            subprocess.run(command, check=True, capture_output=True)
            claude = target / ".claude/skills/recruit/SKILL.md"
            self.assertIn("docs/philosophy.md", claude.read_text())
            self.assertFalse((target / ".agents/skills/recruit/SKILL.md").exists())
            self.assertTrue((target / ".codex/hooks.json").exists())
            claude.write_text("mine")
            command[command.index("--host") + 1] = "all"
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(claude.read_text(), "mine")
            codex = target / ".agents/skills/recruit/SKILL.md"
            self.assertIn("docs/philosophy.md", codex.read_text())
            command[command.index("--host") + 1] = "cursor"  # not an authoring host any more
            self.assertNotEqual(0, subprocess.run(command, capture_output=True).returncode)

    def test_world_skill_scaffolder_writes_both_discovery_paths_and_preserves_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "both")
            script = target / "bin" / "scaffold-world-skill.py"
            command = [
                "python3", str(script), "warp-in", "agent", "--provider", "both",
                "--universe", "Aiur", "--description", "Warp in a scoped watcher",
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            claude = target / ".claude" / "skills" / "warp-in" / "SKILL.md"
            codex = target / ".agents" / "skills" / "warp-in" / "SKILL.md"
            self.assertEqual(claude.read_text(), codex.read_text())
            self.assertIn("docs/philosophy.md", codex.read_text())
            codex.write_text("mine")
            subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertEqual(codex.read_text(), "mine")

    def test_tool_word_proposes_a_placement_before_building(self) -> None:
        # The tool word used to start from the answer ("add the tool the user asked
        # for"), so a request mixing a boundary, a procedure and a changing value all
        # went into one tool. It must split, place and confirm first (issue #3).
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "both")
            script = target / "bin" / "scaffold-world-skill.py"
            for word, flow in (("equip", "tool"), ("recruit", "agent")):
                subprocess.run(
                    ["python3", str(script), word, flow, "--host", "all",
                     "--universe", "Crew", "--description", f"{word} the crew"],
                    check=True, capture_output=True, text=True,
                )
            tool = (target / ".claude/skills/equip/SKILL.md").read_text()
            self.assertEqual(tool, (target / ".agents/skills/equip/SKILL.md").read_text())
            self.assertTrue(tool.startswith("---\nname: equip\n"))
            self.assertNotIn("{{", tool)
            self.assertIn("docs/philosophy.md", tool)
            self.assertIn("wait for the user's confirmation", tool)
            self.assertIn(
                "| Piece | Kind | Where it goes | Why | What enforces it | Approval |", tool)
            for place in ("`Persona.mention_tools`", "`check()`", "prompt file",
                          "`Harness.plugins`", "`knowledge/`", "`settings.json`",
                          "`WORLD.md` |"):
                self.assertIn(place, tool)
            self.assertIn("The `Skill` tool is disallowed by default", tool)
            self.assertIn("equipping the cast", tool)
            self.assertIn("leaves\n`write_scope` empty", tool)
            self.assertNotIn("never empty", tool)
            self.assertIn("short note a person writes", tool)
            agent = (target / ".claude/skills/recruit/SKILL.md").read_text()
            self.assertNotIn("Where it goes", agent)
            self.assertIn("add the agent the user asked for", agent)


    @unittest.skipIf(os.name == "nt", "chmod cannot make a directory unwritable on Windows")
    def test_unwritable_codex_dir_fails_before_writing_anything(self) -> None:
        # Codex's workspace-write sandbox refuses writes under .codex/ and .agents/. The
        # installer copied file by file, so it used to die halfway: world/ and bin/ in
        # place, the guard hooks missing. It must refuse up front instead.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            protected = target / ".codex"
            protected.mkdir(parents=True)
            protected.chmod(0o555)
            try:
                result = subprocess.run(
                    ["python3", str(INSTALLER), str(target), "--provider", "codex"],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn(".codex", result.stderr)
                self.assertIn("sandbox", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual([".codex"], sorted(p.name for p in target.iterdir()))
                self.assertEqual([], list(protected.iterdir()))
            finally:
                protected.chmod(0o755)

    @unittest.skipIf(os.name == "nt", "chmod cannot make a directory unwritable on Windows")
    def test_scaffolder_refuses_unwritable_skill_root_before_writing_any_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.run_installer(target, "codex")
            protected = target / ".agents"
            protected.mkdir()
            protected.chmod(0o555)
            try:
                result = subprocess.run(
                    ["python3", str(target / "bin/scaffold-world-skill.py"), "recruit", "agent",
                     "--host", "all", "--universe", "Crew", "--description", "Recruit"],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn(".agents", result.stderr)
                self.assertIn("sandbox", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((target / ".claude").exists())
                self.assertEqual([], list(protected.iterdir()))
            finally:
                protected.chmod(0o755)


class InstallerProfileTests(unittest.TestCase):
    """--os/--provider/--channel are recorded in project-profile.json, only what they
    select is copied, and the installed runtime reads the same file."""

    def install(self, target: Path, *flags: str) -> str:
        return subprocess.run(["python3", str(INSTALLER), str(target), *flags],
                              check=True, capture_output=True, text=True).stdout

    def test_installed_ops_kit_renders_for_each_os(self) -> None:
        for os_name in ("macos", "linux", "windows"):
            with self.subTest(os=os_name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "harness"
                self.install(target, "--os", os_name, "--provider", "codex")
                # Render using only the installed files, without a CLI, venv or service
                # manager. The pure renderer allows checking every OS on this host.
                code = """
import runpy
from pathlib import Path
ops = runpy.run_path('bin/ops-kit.py')
home = Path.cwd()
files = ops['render'](home, 'installed', OS_NAME, home / '.venv/python', '/bin',
                      user='tester')
ops['write'](home, files, force=False)
assert len(files) == (3 if OS_NAME == 'macos' else 4)
for rel in files:
    assert (home / rel).is_file(), rel
""".replace("OS_NAME", repr(os_name))
                result = subprocess.run([sys.executable, "-I", "-c", code], cwd=target,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_codex_slack_profile_installs_only_what_it_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.install(target, "--os", "windows", "--provider", "codex", "--channel", "slack")
            profile = json.loads((target / "project-profile.json").read_text())
            self.assertEqual({k: profile[k] for k in ("os", "provider", "channel")},
                             {"os": "windows", "provider": "codex", "channel": "slack"})
            for absent in ("world/runners/claude.py", "world/process/plugins.py",
                           "world/discord", "world/channels/discord.py",
                           "bin/discord-setup.py", "docs/discord.md", ".claude"):
                self.assertFalse((target / absent).exists(), absent)
            for present in ("world/runners/codex.py", "world/tools/windows_pipe.py",
                            "bin/codex-spike.py", "bin/slack-provision.py"):
                self.assertTrue((target / present).exists(), present)
            # Seeded files say nothing about what the profile left out.
            example = (target / "env" / "harness.env.example").read_text()
            self.assertNotIn("DISCORD", example)
            self.assertNotIn("[only", example)
            self.assertNotIn(".claude/the-world", (target / "AGENTS.md").read_text())
            readme = (target / "README.md").read_text()
            self.assertTrue(readme.startswith("# harness\n"))
            self.assertIn("Where to change what", readme)
            linux = Path(tmp) / "linux"
            self.install(linux, "--os", "linux", "--provider", "codex", "--channel", "both")
            self.assertFalse((linux / "world" / "tools" / "windows_pipe.py").exists())
            example = (linux / "env" / "harness.env.example").read_text()
            self.assertIn("DISCORD_BOT_TOKEN", example)
            self.assertNotIn("[only", example)
            hooks = (target / ".codex" / "hooks.json").read_text()
            self.assertIn('"python ', hooks)
            self.assertNotIn("python3 ", hooks)

            # The installed copy runs on its own: no kit on sys.path, Codex by default,
            # a Claude or Discord request refused with the reinstall flag named.
            code = """
import argparse, asyncio, os, sys
sys.path.insert(0, os.getcwd())
import world
from world import Agent, Harness, PollTrigger, DiscordConfig
assert all(m.__file__.startswith(os.getcwd()) for n, m in sys.modules.items()
           if n == "world" or n.startswith("world.") and getattr(m, "__file__", None))
h = Harness(name="w", home=os.getcwd())
assert h.provider == "codex" and h.agent_runner().provider == "codex"
for kwargs, flag in ((dict(provider="claude"), "--provider"), (dict(discord=DiscordConfig()), "--channel discord")):
    try:
        Harness(name="x", home=os.getcwd(), **kwargs)
    except ValueError as e:
        assert flag in str(e), e
    else:
        raise AssertionError(kwargs)
h.register(Agent("a", PollTrigger(lambda *_: None), write_scope=("tasks/{task}/work",)))
asyncio.run(h._main_async(argparse.Namespace(once=True, dry_run=True, no_slack=True, tick_interval=60)))
assert os.path.isfile(".the-world/agent-scope.json") and not os.path.exists(".claude")
"""
            env = dict(os.environ, WORLD_ENV_FILE=str(target / "absent.env"))
            env.pop("PYTHONPATH", None)
            run = subprocess.run([sys.executable, "-I", "-c", code], cwd=target, env=env,
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_rerun_keeps_the_recorded_profile_and_a_flag_changes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "harness"
            self.install(target, "--provider", "both", "--channel", "both")
            self.assertTrue((target / "bin" / "discord-setup.py").is_file())
            self.install(target)  # an upgrade: nothing widened or narrowed
            profile = json.loads((target / "project-profile.json").read_text())
            self.assertEqual((profile["provider"], profile["channel"]), ("both", "both"))
            self.assertTrue((target / "world" / "runners" / "claude.py").is_file())
            out = self.install(target, "--channel", "slack")
            self.assertIn("removed  bin/discord-setup.py", out)
            self.assertFalse((target / "world" / "discord").exists())
            self.assertFalse((target / "docs" / "discord.md").exists())
            self.assertTrue((target / "world" / "runners" / "claude.py").is_file())

    def test_a_kit_checkout_without_a_profile_keeps_everything(self) -> None:
        from world import profile
        if profile.PROFILE:
            self.skipTest("running inside an installed harness")
        self.assertEqual(profile.SUPPORTED_PROVIDERS, ("claude", "codex"))
        self.assertEqual(profile.CHANNEL, "both")


if __name__ == "__main__":
    unittest.main()
