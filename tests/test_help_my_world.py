"""help-my-world's collector: what a maintainer needs, and nothing that is a secret."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "skills/help-my-world/collect_env.py"
TOKEN = "xoxb-1111-2222-SECRETVALUE"


class CollectEnvTests(unittest.TestCase):
    def test_reports_the_harness_and_never_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "harness"
            (home / "env").mkdir(parents=True)
            (home / "env" / "harness.env").write_text(f"OBS_SLACK_BOT_TOKEN={TOKEN}\n")
            (home / "project-profile.json").write_text(json.dumps(
                {"schema_version": 1, "os": "windows", "provider": "codex", "channel": "slack"}))
            (home / "WORLD.md").write_text("# 비밀 세계\n고객사 이름\n")
            (home / "runtime").mkdir()
            (home / "runtime" / "harness.log").write_text(
                f"started\nslack auth failed with {TOKEN}\nxapp-1-A0-zzz leaked\n")
            out = subprocess.run([sys.executable, str(COLLECTOR), str(home)], capture_output=True,
                                 encoding="utf-8", check=True).stdout
        self.assertIn("the-world-skills: ", out)
        self.assertIn("profile: os=windows, provider=codex, channel=slack", out)
        self.assertIn("slack auth failed with [redacted]", out)
        self.assertNotIn("SECRETVALUE", out)
        self.assertNotIn("xapp-1", out)
        self.assertNotIn("고객사", out)  # WORLD.md is the user's, never collected
        self.assertNotIn(str(Path.home()) + "/", out)

    def test_world_shape_names_characters_and_agents_without_their_words(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            (home / "harness.py").write_text(
                "from world import Agent, Harness, Persona, PollTrigger\n"
                "keeper = Persona(key='haram', display='하람', voice='VOICE-TEXT')\n"
                "h = Harness(name='t', main_persona=keeper)\n"
                "h.register(Agent(name='saebyeok', trigger=PollTrigger(check=c), prompt_file='p.md',\n"
                "                 persona=Persona(key='saebyeok', display='새벽'), writes_finding=True))\n")
            out = subprocess.run([sys.executable, str(COLLECTOR), str(home)], capture_output=True,
                                 encoding="utf-8", check=True).stdout
        self.assertIn("personas: haram (하람), saebyeok (새벽)", out)
        self.assertIn("agent `saebyeok`", out)
        self.assertIn("PollTrigger(check=c)", out)
        self.assertNotIn("VOICE-TEXT", out)

    def test_filing_sends_a_utf8_file_not_command_line_text(self) -> None:
        skill = (ROOT / "skills/help-my-world/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--input issue.json", skill)
        self.assertIn("nothing is sent before a yes", skill)

    def test_the_offline_headings_are_the_help_forms_labels(self) -> None:
        # Without gh the skill drafts from a list it carries; that list must be the form.
        import re

        form = (ROOT / ".github/ISSUE_TEMPLATE/1-help.yml").read_text(encoding="utf-8")
        labels = re.findall(r"^      label: (.+)$", form, re.MULTILINE)
        skill = " ".join((ROOT / "skills/help-my-world/SKILL.md").read_text(encoding="utf-8").split())
        self.assertIn("draft under the headings " + ", ".join(labels) + ".", skill)
        for name, label in (("1-help.yml", "question"), ("2-bug.yml", "bug"), ("3-feature.yml", "enhancement")):
            text = (ROOT / ".github/ISSUE_TEMPLATE" / name).read_text(encoding="utf-8")
            self.assertIn(f"labels: [{label}]", text)  # GitHub's default labels: they exist


if __name__ == "__main__":
    unittest.main()
