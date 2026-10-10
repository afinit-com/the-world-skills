"""the-world-audit's scanner against harnesses built by the-world-start's installer.

Two fixtures: one built the way the kit intends, which must come back with nothing open,
and one with a known open item planted in each of the audit's seven categories,
each of which must be reported open at the right check. The scanner is also held to its
read-only promise: it never imports the harness, and the tree is byte-identical after.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "skills/the-world-audit/scan.py"
INSTALL = ROOT / "skills/the-world-start/scripts/install-kit.py"

# Built from parts so no token-shaped literal sits in the repository.
FAKE_SLACK_TOKEN = "xo" + "xb-" + "1234567890-" + "abcdefghijklmnop"

HARNESS_PY = '''\
from pathlib import Path

from world import Harness, Persona

{imports}

Path(__file__).with_name("IMPORTED").write_text("the audit imported me")

h = Harness(
    name="obs",
    provider="claude",
    home=Path(__file__).resolve().parent,
    world_doc="WORLD.md",
    main_persona=Persona(key="keeper", display="Obs-Keeper", flavor="관측대를 지킨다"),
)
{registers}

if __name__ == "__main__":
    h.main()
'''

CLEAN_AGENT = '''\
from pathlib import Path

from world import Agent, Persona, PollTrigger, RunResult, Signal
from world.toolkit import make_write_report

HOME = Path(__file__).resolve().parents[2]
SCOPE = ("tasks/{task}/work",)


def check(state: dict, now: float):
    me = state["agents"]["watcher"]
    if now - me.get("last_run", 0) < 86400:
        return None

    def on_done(_result: RunResult) -> None:
        me["last_run"] = now

    return Signal(prompt="어젯밤 무엇이 바뀌었는지 적으세요.", on_done=on_done)


AGENT = Agent(
    name="watcher",
    trigger=PollTrigger(check=check),
    prompt_file="abilities/watcher/prompt.md",
    persona=Persona(key="watcher", display="Obs-Watcher", flavor="지켜본다"),
    writes_report=True,
    write_scope=SCOPE,
    tools=[make_write_report(HOME, SCOPE)],
)
'''

# Two mutates=True workers: one that writes only through its task lane, one with Bash.
FIXER_AGENT = CLEAN_AGENT.replace('name="watcher"', 'name="fixer"').replace(
    'state["agents"]["watcher"]', 'state["agents"]["fixer"]').replace(
    'key="watcher", display="Obs-Watcher"', 'key="fixer", display="Obs-Fixer"').replace(
    "abilities/watcher/", "abilities/fixer/").replace(
    "    writes_report=True,\n", "    writes_report=True,\n    mutates=True,\n")
SHELL_FIXER_AGENT = FIXER_AGENT.replace('"fixer"', '"shellfixer"').replace(
    "abilities/fixer/", "abilities/shellfixer/").replace(
    'key="fixer", display="Obs-Fixer"', 'key="shellfixer", display="Obs-Shellfixer"').replace(
    "    mutates=True,\n", "    mutates=True,\n    allow_bash=True,\n")

# One planted open item per category, and the check each must surface at.
SCOUT_AGENT = '''\
import requests

from world import Agent, Persona, PollTrigger, Signal


def check(state: dict, now: float):
    me = state["agents"]["scout"]
    me["last_seen"] = now                       # silent: marked seen before the run
    issues = requests.get("https://example.invalid/issues").json()   # content: outside
    return Signal(prompt=f"새 이슈를 살펴보세요: {issues}")


AGENT = Agent(
    name="scout",
    trigger=PollTrigger(check=check),
    persona=Persona(key="scout", display="Obs-Scout", flavor="밖을 살핀다"),
    allow_bash=True,                            # authority
    write_scope=(".claude/hooks",),             # self-reconfiguration
)
'''

SCRIBE_AGENT = '''\
from pathlib import Path

from world import Agent, MentionTrigger, Persona
from world.toolkit import make_write_report

from tools.publish import post_issue, read_note

HOME = Path(__file__).resolve().parents[2]

AGENT = Agent(
    name="scribe",
    trigger=MentionTrigger(),
    persona=Persona(key="scribe", display="Obs-Scribe", flavor="기록한다"),
    writes_report=True,
    write_scope=("tasks/{task}/work",),
    tools=[make_write_report(HOME, ("tasks/{task}/work",)), post_issue, read_note],
)
'''

PUBLISH_TOOLS = '''\
import subprocess

import requests

from world.toolkit import result, tool

DENY_FLAGS = ["--force", "--admin"]             # tools: a denylist


@tool("post_issue", "Open an issue.", {"repo": str, "body": str})
async def post_issue(args: dict) -> dict:       # outward, ungated; repo is a free param
    requests.post(f"https://api.github.com/repos/{args['repo']}/issues",
                  json={"body": args["body"]})
    subprocess.run(f"gh issue list --repo {args['repo']}", shell=True)
    subprocess.run(f"gh issue view --repo {args['repo']}".split())   # string-built argv
    return result(0, "posted")


@tool("read_note", "Read a note.", {"path": str})
async def read_note(args: dict) -> dict:        # path escape: no containment
    try:
        return result(0, open(args["path"]).read())
    except OSError:
        pass                                    # silent: swallowed
    return result(1, "unreadable")
'''


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        # Point HOME away from the real one: the scanner reads ~/.claude/settings.json.
        (self.tmp / "home").mkdir()
        self.env = {**os.environ, "HOME": str(self.tmp / "home")}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name: str, agents: dict[str, str], extra: dict[str, str] | None = None) -> Path:
        target = self.tmp / name
        target.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=target, check=True)
        subprocess.run([sys.executable, str(INSTALL), str(target), "--provider", "claude"],
                       check=True, capture_output=True, env=self.env)
        imports, registers = [], []
        (target / "abilities").mkdir()
        (target / "abilities/__init__.py").write_text("")
        for agent, source in agents.items():
            folder = target / "abilities" / agent
            folder.mkdir()
            (folder / "__init__.py").write_text("")
            (folder / "agent.py").write_text(source)
            (folder / "prompt.md").write_text("읽고 적는다.\n")
            imports.append(f"from abilities.{agent} import agent as {agent}")
            registers.append(f"h.register({agent}.AGENT)")
        (target / "harness.py").write_text(HARNESS_PY.format(
            imports="\n".join(imports), registers="\n".join(registers)))
        (target / "WORLD.md").write_text("# WORLD — 관측대\n")
        for relative, text in (extra or {}).items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return target

    def scan(self, harness: Path) -> list[dict]:
        before = tree_digest(harness)
        done = subprocess.run([sys.executable, str(SCAN), str(harness), "--json"],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((harness / "IMPORTED").exists(), "the scanner imported the harness")
        self.assertEqual(tree_digest(harness), before, "the scanner changed the harness")
        return json.loads(done.stdout)["findings"]

    def test_a_harness_built_as_intended_has_nothing_open(self) -> None:
        harness = self.build("clean", {"watcher": CLEAN_AGENT})
        findings = self.scan(harness)
        self.assertEqual([f for f in findings if f["status"] == "open"], [])
        categories = {f["category"] for f in findings}
        self.assertEqual(categories, {"authority", "tools", "outward", "content",
                                      "self_config", "secrets", "silent"})

    def test_a_known_open_item_in_each_category_is_reported_open(self) -> None:
        harness = self.build(
            "open",
            {"scout": SCOUT_AGENT, "scribe": SCRIBE_AGENT},
            {
                "tools/__init__.py": "",
                "tools/publish.py": PUBLISH_TOOLS,
                "abilities/scout/prompt.md": f"Slack token: {FAKE_SLACK_TOKEN}\n",
                "env/harness.env": "OBS_GITHUB_TOKEN=not-a-real-one\n",
            },
        )
        findings = self.scan(harness)
        opened = {(f["category"], f["check"]) for f in findings if f["status"] == "open"}
        expected = {
            ("authority", "allow_bash"),
            ("authority", "write tool bound to scope"),
            ("tools", "shell=True"),
            ("tools", "string-built command"),
            ("tools", "denylist"),
            ("tools", "parameter reopens the boundary"),
            ("tools", "path escape"),
            ("outward", "outward action gated"),
            ("content", "outside input to a privileged bot"),
            ("self_config", "writes its own configuration"),
            ("secrets", "token outside env/harness.env"),
            ("secrets", "credentials a bot can reach"),
            ("silent", "exit 0 counted as done"),
            ("silent", "input marked seen before completion"),
            ("silent", "errors swallowed"),
        }
        self.assertEqual(expected - opened, set())
        for finding in findings:
            if finding["status"] == "open":
                with self.subTest(check=finding["check"]):
                    self.assertTrue(finding["where"], "an open item must name where")
                    self.assertTrue(finding["repro"], "an open item must say how to reproduce")
        rendered = json.dumps(findings)
        self.assertNotIn(FAKE_SLACK_TOKEN, rendered)
        self.assertNotIn("not-a-real-one", rendered)

    def test_an_empty_approver_is_open_only_on_a_kit_that_skips_the_check(self) -> None:
        env = {"env/harness.env": "SLACK_BOT_TOKEN=set-but-not-shown\nAPPROVER_SLACK_ID=\n"}

        def approver_status(harness: Path) -> str:
            return next(f["status"] for f in self.scan(harness)
                        if f["check"] == "approver verification")

        # The current kit refuses every click when no approver is set.
        current = self.build("current", {"watcher": CLEAN_AGENT}, env)
        self.assertEqual(approver_status(current), "closed")

        # A harness still carrying the old gate lets anyone in the channel approve.
        old = self.build("old", {"watcher": CLEAN_AGENT}, env)
        approvals = old / "world/slack/approvals.py"
        approvals.write_text(approvals.read_text().replace(
            "if not slack.is_approver(clicker):",
            "if slack.approver and not slack.is_approver(clicker):"))
        self.assertEqual(approver_status(old), "open")

    def test_an_edited_kit_or_an_unregistered_hook_is_reported(self) -> None:
        harness = self.build("tampered", {"watcher": CLEAN_AGENT})
        approvals = harness / "world/slack/approvals.py"
        approvals.write_text(approvals.read_text().replace(
            "slack.is_approver(clicker)", "True"))
        settings = harness / ".claude/settings.json"
        registered = json.loads(settings.read_text())
        for group in registered["hooks"]["PreToolUse"]:
            group["hooks"] = [h for h in group["hooks"]
                              if "world-git-guard.py" not in h["command"]]
        settings.write_text(json.dumps(registered))
        findings = self.scan(harness)
        opened = {(f["category"], f["check"], tuple(f["where"])) for f in findings
                  if f["status"] == "open"}
        self.assertIn(("outward", "kit invariant", ("world/slack/approvals.py",)), opened)
        self.assertIn(("self_config", "guard hooks registered", (".claude/settings.json",)),
                      opened)
        self.assertTrue(any(f["check"] == "kit files unchanged" and f["status"] == "not_verified"
                            for f in findings))

    def test_mutates_reflects_spawn_time_worktrees_and_an_older_kit_stays_open(self) -> None:
        def mutates(findings: list[dict]) -> dict[str, str]:
            return {name: f["status"] for f in findings if f["check"] == "mutates"
                    for name in ("fixer", "shellfixer") if f"`{name}`" in f["detail"]}

        harness = self.build("mutating", {"fixer": FIXER_AGENT,
                                          "shellfixer": SHELL_FIXER_AGENT})
        # The kit spawns both in their own worktree. Nothing the lane-only one holds
        # writes elsewhere; Bash is not confined by a cwd, so that one stays unverified.
        self.assertEqual({"fixer": "closed", "shellfixer": "not_verified"},
                         mutates(self.scan(harness)))

        # An older world/ spawned every agent at the git root.
        spawn = harness / "world/process/spawn.py"
        spawn.write_text(spawn.read_text().replace("worktrees.run_worktree(", "spawn_here("))
        self.assertEqual({"fixer": "open", "shellfixer": "open"},
                         mutates(self.scan(harness)))

    def test_a_directory_without_harness_py_is_refused(self) -> None:
        done = subprocess.run([sys.executable, str(SCAN), str(self.tmp)],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 2)
        self.assertIn("no harness.py", done.stderr)


if __name__ == "__main__":
    unittest.main()
