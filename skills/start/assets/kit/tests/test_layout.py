"""Where the kit writes what it generates, and what it no longer writes.

Two reviews of installed harnesses found the scope table under three names (two written,
one read by each hook), a `findings/` directory every harness had and none used, and a
"current state" sentence in WORLD.md that went stale the moment Slack connected. These
pin the single paths, and that STATUS.md carries the process and channel instead.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from world import Agent, Harness, Persona, PollTrigger
from world.process.heartbeat import render_status
from world.process.state import default_state
from world.world_doc import world_doc_problems

KIT = Path(__file__).resolve().parents[1]


def _agent(name: str) -> Agent:
    return Agent(name=name, trigger=PollTrigger(lambda _s: None),
                 write_scope=("tasks/{task}/work",))


class ScopeFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.harness = Harness("t", home=self.root, git_root=self.root)
        self.harness.log = lambda _msg: None
        self.harness.register(_agent("saebyeok"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_scope_file_and_a_stale_legacy_copy_is_removed(self) -> None:
        legacy = self.root / ".claude" / "world-agent-scope.json"
        legacy.parent.mkdir()
        legacy.write_text('{"old": {}}')
        self.harness.write_agent_scope()
        table = json.loads((self.root / ".the-world" / "agent-scope.json").read_text())
        self.assertEqual(["saebyeok"], list(table))
        self.assertFalse(legacy.exists())

    def test_the_claude_hook_reads_the_file_the_harness_writes(self) -> None:
        hooks = self.root / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy(KIT / ".claude" / "hooks" / "world-scope-guard.py", hooks)
        self.harness.write_agent_scope()
        spec = importlib.util.spec_from_file_location("scope_guard", hooks / "world-scope-guard.py")
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        (root, table), = guard.scope_tables()
        self.assertEqual(self.root, root)
        self.assertIn("saebyeok", table)

    def test_worktrees_live_under_the_kit_directory_not_a_provider_one(self) -> None:
        self.assertEqual(self.root / ".the-world" / "worktrees", self.harness.worktrees_dir)


class StatusTests(unittest.TestCase):
    def test_status_names_the_process_and_whether_the_channel_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = Harness("t", home=Path(raw))
            state = default_state(harness)
            self.assertNotIn("## Process", render_status(harness, state))
            state["process"] = {"pid": 4242, "started": 0, "channel": "slack: connected on C1"}
            text = render_status(harness, state)
            self.assertIn("pid 4242", text)
            self.assertIn("slack: connected on C1", text)
            self.assertIn("logs/harness.jsonl", text)


class NamingTests(unittest.TestCase):
    def test_an_agent_named_apart_from_its_persona_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = Harness("t", home=Path(raw),
                              main_persona=Persona(key="haram", display="등대지기 하람"))
            same = Agent(name="mulgil", trigger=PollTrigger(lambda _s: None),
                         persona=Persona(key="mulgil", display="해도사 물길"))
            harness.register(same)
            self.assertEqual([], harness.naming_problems())
            harness.register(Agent(name="watcher", trigger=PollTrigger(lambda _s: None),
                                   persona=Persona(key="saebyeok", display="관측원 새벽")))
            harness.register(Agent(name="haram2", trigger=PollTrigger(lambda _s: None),
                                   persona=Persona(key="haram", display="다른 하람")))
            found = "\n".join(harness.naming_problems())
            self.assertIn("'watcher' speaks as persona 'saebyeok'", found)
            self.assertIn("persona key 'haram' is both", found)


class ListReportsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_under_task_folders_are_listed_newest_first(self) -> None:
        import os

        from world.toolkit import make_list_reports

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            for i, task in enumerate(("20261009-aaaa", "20261010-bbbb")):
                work = home / "tasks" / task / "work"
                work.mkdir(parents=True)
                (work / "항해일지.md").write_text("x")
                os.utime(work / "항해일지.md", (1000 + i, 1000 + i))
            tool = make_list_reports(home, ("tasks",))
            out = await tool.handler({"directory": "tasks/"})
            text = out["content"][0]["text"]
            self.assertEqual(["[ok]", "20261010-bbbb/work/항해일지.md", "20261009-aaaa/work/항해일지.md"],
                             text.splitlines())


class ReportRenameTests(unittest.IsolatedAsyncioTestCase):
    """A run's output was a "finding" until 0.2.7. Harnesses written then keep working."""

    def test_old_names_still_work(self) -> None:
        import world
        from world.process.state import load_state, record_finding, record_report

        self.assertIs(world.make_write_finding, world.make_write_report)
        self.assertIs(world.make_read_finding, world.make_read_report)
        self.assertIs(world.make_list_findings, world.make_list_reports)
        self.assertIs(record_finding, record_report)
        agent = Agent(name="a", trigger=PollTrigger(lambda _s: None), writes_finding=True)
        self.assertTrue(agent.writes_report)
        tool = world.make_write_finding(Path("."), ("tasks/{task}/work",))
        self.assertEqual("write_report", tool.name)  # the model sees the new name
        with tempfile.TemporaryDirectory() as raw:
            harness = Harness("t", home=Path(raw), finding_footer="old footer")
            self.assertEqual("old footer", harness.report_footer)
            harness.state_path.write_text(json.dumps({"recent_findings": [{"ts": 1, "agent": "a", "summary": "s"}]}))
            state = load_state(harness)
            self.assertEqual("s", state["recent_reports"][0]["summary"])
            self.assertNotIn("recent_findings", state)

    async def test_show_button_opens_a_task_report(self) -> None:
        from world.slack.mentions import MentionMixin

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            report = home / "tasks" / "20261010-8e0b" / "work" / "항해일지.md"
            report.parent.mkdir(parents=True)
            report.write_text("배 4척 입항", encoding="utf-8")
            (home / "secret.md").write_text("no")
            posted: list[str] = []

            class Probe(MentionMixin):
                async def post(self, text, **_kw):
                    posted.append(text)

            probe = Probe()
            probe.harness = Harness("t", home=home)
            probe.state = {}
            await probe.show_report("tasks/20261010-8e0b/work/항해일지.md", "1.0")
            await probe.show_report("secret.md", "1.0")
            await probe.show_report("tasks/20261010-8e0b/task.md", "1.0")
        self.assertIn("배 4척 입항", posted[0])
        self.assertIn("보고서가 아닙니다", posted[1])
        self.assertIn("보고서가 아닙니다", posted[2])


class WorldDocTests(unittest.TestCase):
    def test_the_world_passes_and_an_install_log_does_not(self) -> None:
        world = ("# 등대 관측소\n\n## 사람들\n- 하람: 창구. 새벽이 밤새 본 배를 아침에 전한다.\n"
                 "## 관측소 말\n- 항해일지 = 매일 아침의 요약\n")
        self.assertEqual([], world_doc_problems(world))
        log = world + ("\n## 현재 설치 및 연결 절차\n현재 PID: runtime/harness.pid\n"
                       "```\nuv run python bin/slack-provision.py\n```\n"
                       "대상: /Users/me/site, 토큰 CONTROL_SLACK_BOT_TOKEN\n")
        found = "\n".join(world_doc_problems(log))
        for kind in ("heading", "process id", "code block", "shell command", "absolute path",
                     "env variable"):
            self.assertIn(kind, found)


if __name__ == "__main__":
    unittest.main()
