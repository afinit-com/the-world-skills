from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from world.context import current_asker, reset_current_asker, set_current_asker
from world.harness import Harness
from world.process.spawn import enforce_completion, run_agent_oneshot
from world.slack.mentions import MentionMixin, _report_actions
from world.slack.run_footer import footer_context, format_run_footer
from world.toolkit import make_approval_gated, make_write_report, result, tool
from world.tools.spec import ToolSpec
from world.types import Agent, PollTrigger, RunResult, Schedule


class ScheduleTests(unittest.TestCase):
    def test_daily_previous_occurrence_and_next_day(self) -> None:
        schedule = Schedule.daily(18, 0, "UTC")
        same_day = schedule.previous_occurrence(
            datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)
        )
        previous_day = schedule.previous_occurrence(
            datetime(2026, 7, 12, 17, 59, tzinfo=timezone.utc)
        )
        self.assertEqual("2026-07-12T18:00:00+00:00", same_day.isoformat())
        self.assertEqual("2026-07-11T18:00:00+00:00", previous_day.isoformat())

    def test_weekly_and_monthly_boundaries(self) -> None:
        monday = Schedule.weekly(0, 9, 0, "UTC").previous_occurrence(
            datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        )
        january = Schedule.monthly(31, 0, 0, "UTC").previous_occurrence(
            datetime(2026, 2, 15, tzinfo=timezone.utc)
        )
        self.assertEqual("2026-07-13T09:00:00+00:00", monday.isoformat())
        self.assertEqual("2026-01-31", january.date().isoformat())

    def test_next_occurrence_is_strictly_after_now(self) -> None:
        daily = Schedule.daily(9, 0, "Asia/Seoul").next_occurrence(
            datetime(2026, 10, 3, 11, 22, tzinfo=timezone.utc)  # 20:22 KST
        )
        weekly = Schedule.weekly(0, 9, 0, "UTC").next_occurrence(
            datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)  # exactly Monday 09:00
        )
        monthly = Schedule.monthly(31, 0, 0, "UTC").next_occurrence(
            datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual("2026-10-04T09:00:00+09:00", daily.isoformat())
        self.assertEqual("2026-07-20T09:00:00+00:00", weekly.isoformat())
        self.assertEqual("2026-02-28", monthly.date().isoformat())

    def test_describe_names_kind_time_and_zone(self) -> None:
        self.assertEqual("daily 09:00 Asia/Seoul", Schedule.daily(9, 0, "Asia/Seoul").describe())
        self.assertEqual("weekly Mon 09:30 UTC", Schedule.weekly(0, 9, 30).describe())
        self.assertEqual("monthly day 1 00:00 UTC", Schedule.monthly(1, 0).describe())


class FakeRunner:
    provider = "fake"

    def __init__(self, response: RunResult, write: Path | None = None) -> None:
        self.response = response
        self.write = write
        self.calls = 0

    async def run(self, agent, prompt, *, on_tool=None) -> RunResult:
        self.calls += 1
        if self.write:
            self.write.parent.mkdir(parents=True, exist_ok=True)
            self.write.write_text("finding")
        if on_tool:
            await on_tool("fake_tool", {"prompt": prompt})
        return self.response


class CompletionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.harness = SimpleNamespace(
            home=self.home,
            logs_dir=self.home / "logs",
            findings_dir=self.home / "findings",
            log=lambda _message: None,
        )
        self.agent = Agent(
            name="watcher",
            trigger=PollTrigger(check=lambda _state, _now: None),
            writes_report=True,
            write_scope=("findings/watcher",),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_success_without_promised_file_is_blocked(self) -> None:
        self.harness.agent_runner = lambda: FakeRunner(RunResult(status="ok", result_text="done"))
        outcome = await run_agent_oneshot(self.harness, self.agent, "work")
        self.assertEqual("blocked", outcome.status)
        self.assertIn("wrote no file", outcome.result_text)

    async def test_real_file_satisfies_completion(self) -> None:
        target = self.home / "findings" / "watcher" / "result.md"
        runner = FakeRunner(RunResult(status="ok", result_text="done"), target)
        self.harness.agent_runner = lambda: runner
        outcome = await run_agent_oneshot(self.harness, self.agent, "work")
        self.assertEqual("ok", outcome.status)
        self.assertTrue(target.is_file())

    async def test_permission_denial_without_output_is_blocked(self) -> None:
        result_value = RunResult(
            status="ok", result_text="continued", permission_denials=("Bash:git push",)
        )
        outcome = enforce_completion(self.harness, self.agent, result_value, set())
        self.assertEqual("blocked", outcome.status)
        self.assertIn("git push", outcome.result_text)

    async def test_dry_run_never_calls_provider(self) -> None:
        runner = FakeRunner(RunResult(status="ok"))
        self.harness.agent_runner = lambda: runner
        outcome = await run_agent_oneshot(self.harness, self.agent, "work", dry_run=True)
        self.assertEqual("skipped", outcome.status)
        self.assertEqual(0, runner.calls)


class ToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_decorator_returns_provider_neutral_spec(self) -> None:
        @tool("probe", "Probe one thing", {"value": str})
        async def probe(args):
            return result(0, args["value"])

        self.assertIsInstance(probe, ToolSpec)
        self.assertEqual("probe", probe.name)
        self.assertFalse((await probe.handler({"value": "ok"}))["is_error"])

    async def test_scoped_writer_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            writer = make_write_report(home, ("findings/watcher",))
            denied = await writer.handler({"path": "../escape.md", "content": "no"})
            allowed = await writer.handler(
                {"path": "findings/watcher/{ts}-ok.md", "content": "yes"}
            )
            self.assertTrue(denied["is_error"])
            self.assertFalse(allowed["is_error"])
            self.assertFalse((home.parent / "escape.md").exists())
            self.assertEqual(1, len(list((home / "findings" / "watcher").glob("*-ok.md"))))

    async def test_approval_gate_consumes_only_after_success(self) -> None:
        approved = True
        consumed = 0

        @tool("publish", "Publish", {})
        async def publish(_args):
            return result(0, "published")

        def consume() -> None:
            nonlocal consumed
            consumed += 1

        gated = make_approval_gated(
            publish, is_approved=lambda: approved, consume=consume
        )
        response = await gated.handler({})
        self.assertFalse(response["is_error"])
        self.assertEqual(1, consumed)


class ConversationContextTests(unittest.TestCase):
    def test_asker_context_can_be_scoped_and_reset(self) -> None:
        self.assertEqual("", current_asker())
        token = set_current_asker("U123")
        self.assertEqual("U123", current_asker())
        reset_current_asker(token)
        self.assertEqual("", current_asker())


class ImportBoundaryTests(unittest.TestCase):
    def test_core_imports_without_provider_or_slack_sdks(self) -> None:
        code = r'''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("claude_agent_sdk", "slack_sdk")):
        raise ImportError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import world
import world.harness
import world.process.spawn
import world.tools
print("ok")
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        run = subprocess.run(
            [sys.executable, "-B", "-c", code],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")


class WorldNeutralityTests(unittest.TestCase):
    def test_knowledge_promotion_is_opt_in_and_uses_configured_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mixin = MentionMixin()
            mixin.harness = SimpleNamespace(
                home=home,
                findings_dir=home / "findings",
                knowledge_proposal_agent=None,
            )
            proposal = "findings/knowledge-curator/2026-01-01T00-00-00Z-topic.md"
            self.assertFalse(mixin._is_knowledge_proposal(proposal))
            self.assertNotIn(
                "world_promote_knowledge",
                [button["action_id"] for button in _report_actions(proposal)],
            )

            mixin.harness.knowledge_proposal_agent = "knowledge-curator"
            self.assertTrue(mixin._is_knowledge_proposal(proposal))
            self.assertIn(
                "world_promote_knowledge",
                [
                    button["action_id"]
                    for button in _report_actions(proposal, knowledge_proposal=True)
                ],
            )

    def test_knowledge_proposal_agent_cannot_escape_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "one agent name"):
            Harness("test", home=tmp, knowledge_proposal_agent="../outside")

    def test_shared_kit_contains_no_private_names(self) -> None:
        # Names from the maintainer's private worlds, systems and trackers must not leak
        # into the shipped kit. They are listed by sha256 prefix, not spelled out: a
        # readable list here would publish the very names it keeps out.
        banned = {
            "f59eff44a211fa33", "f81ef6b82cccdd28", "ddf2cd497bbdb7c4", "3ba9802b75483e8f",
            "77d38ea707dfa885", "0e400acf6ad22c08", "462de70082822860", "217eaf48c1f0ab47",
            "9f86326ccc46fdf3", "0f0566a8318ac8eb", "0b5005202223684c", "01a0f2f3c2c13b98",
            "2ed0ec5d408df61f", "9b749047cc5bcce1", "b872e8a15be2653c", "ed67ddd84a83523c",
            "7f27193b4aeaa7ed", "23991c583a8606fb",
        }

        def digest(token: str) -> str:
            return hashlib.sha256(token.encode()).hexdigest()[:16]

        kit = Path(__file__).resolve().parents[1]
        violations = []
        for path in kit.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".tmpl", ".json", ".sh"}:
                continue
            if {".venv", "__pycache__"} & set(path.relative_to(kit).parts):  # not shipped
                continue
            words = re.findall(r"\w+(?:-\w+)*", path.read_text(errors="replace").lower())
            candidates = set(words)
            candidates.update(part for word in words for part in word.split("-"))
            candidates.update(f"{a} {b}" for a, b in zip(words, words[1:]))
            hits = sorted(c for c in candidates if digest(c) in banned)
            violations.extend(f"{path.relative_to(kit)}: {digest(h)}" for h in hits)
        self.assertEqual([], violations)


class RunFooterTest(unittest.TestCase):
    # Real numbers off a live run, not invented ones: input_tokens is the uncached
    # remainder and looks plausible on its own, which is exactly how a footer reading
    # "⬆ 12" for a 116,900-token read gets shipped.
    LIVE = {
        "input_tokens": 12, "cache_creation_tokens": 23283, "cache_read_tokens": 93605,
        "output_tokens": 2900, "cost_usd": 0.0931, "duration_s": 38.4,
        "model": "haiku-4-5+sonnet-5", "tools_available": 3,
    }

    def test_up_arrow_counts_every_token_the_model_read(self) -> None:
        line = format_run_footer(self.LIVE)
        self.assertIn("⬆ 116.9k", line)
        self.assertIn("💾 93.6k cached (80%)", line)
        self.assertIn("🧰 3 tools", line)
        self.assertIn("$0.093", line)

    def test_nothing_to_report_renders_nothing(self) -> None:
        # A deterministic post never reached the API; a footer claiming 0 would be a lie
        # in the shape of a measurement.
        self.assertEqual("", format_run_footer(None))
        self.assertEqual("", format_run_footer({"duration_s": 2.0, "tools_available": 4}))
        self.assertIsNone(footer_context({}))

    def test_absent_fields_are_omitted_not_zeroed(self) -> None:
        # A Codex run: tokens but no billed model and no USD.
        line = format_run_footer(
            {"input_tokens": 1000, "output_tokens": 200, "duration_s": 5.0}
        )
        self.assertEqual("⏱ 5s · ⬆ 1k ⬇ 200", line)



class AnnounceTaskTests(unittest.IsolatedAsyncioTestCase):
    """announce_task is how a worker's result reaches Slack. It had no test that ran it,
    and shipped broken twice in a row: an unimported `read_events`, then a RunResult
    handed to a footer that calls `.get`. Each failure left results only on disk."""

    async def test_a_finished_task_is_posted_with_its_footer(self) -> None:
        from world.process import task as tasks

        home = Path(tempfile.mkdtemp())
        record = tasks.create(home, type="watch", title="OPS-157 diff", agent="jira-watch")
        posted: list[dict] = []

        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(
            home=home, agents=[], run_footer=True, knowledge_proposal_agent=None,
        )
        mixin.state = {"threads": {}}
        mixin.save_state = lambda: None

        async def post(body, **kw):
            posted.append({"body": body, **kw})
            return "111.222"

        mixin.post = post
        run = RunResult(status="ok", result_text="done", input_tokens=1200,
                        output_tokens=300, cost_usd=0.01, duration_s=17.0)
        ts = await mixin.announce_task(record, run, SimpleNamespace(key="lumen"))

        self.assertEqual("111.222", ts)
        [msg] = posted
        self.assertIn("OPS-157 diff", msg["body"])
        self.assertIsNotNone(msg["context"])  # the footer rendered from the RunResult
        self.assertEqual(record.id, mixin.state["threads"]["111.222"]["task_id"])
        self.assertIsNone(msg["thread_ts"])  # a scheduled run opens its own thread

    async def test_a_task_from_a_conversation_replies_in_that_thread(self) -> None:
        from world.process import task as tasks

        home = Path(tempfile.mkdtemp())
        record = tasks.create(home, type="watch", title="OPS-157 diff", agent="jira-watch")
        record.thread = "999.000"
        posted: list[dict] = []
        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(
            home=home, agents=[], run_footer=False, knowledge_proposal_agent=None,
        )
        conversation = {"origin": "mention"}
        mixin.state = {"threads": {"999.000": conversation}}
        mixin.save_state = lambda: None

        async def post(body, **kw):
            posted.append(kw)
            return "999.555"

        mixin.post = post
        ts = await mixin.announce_task(record, RunResult(status="ok"), SimpleNamespace(key="lumen"))
        self.assertEqual("999.000", ts)
        self.assertEqual("999.000", posted[0]["thread_ts"])
        self.assertIs(conversation, mixin.state["threads"]["999.000"])  # left as it was
        self.assertNotIn("999.555", mixin.state["threads"])


EVIDENCE = "watch 실행 완료 · work/ 산출물 report.md"


class AnnounceCardBodyTests(unittest.IsolatedAsyncioTestCase):
    """Issue #5: the default completion check leaves the same evidence on every run, and
    the card showed that evidence INSTEAD of what the agent wrote — a scheduled report
    arrived as "<agent> 실행 완료 · work/ 산출물 <file>" with the report itself hidden."""

    async def _card(self, result_text: str, evidence: str = EVIDENCE, *,
                    title: str = "📋 아침 보고", origin=None) -> str:
        from world.process import task as tasks

        home = Path(tempfile.mkdtemp())
        record = tasks.create(home, type="agent-run", title=title, agent="watch",
                              origin=origin)
        tasks.append_event(home, record.id, "verify", verdict="done",
                           evidence=[evidence])
        posted: list[str] = []
        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(
            home=home, agents=[], run_footer=False, knowledge_proposal_agent=None,
        )
        mixin.state = {"threads": {}}
        mixin.save_state = lambda: None

        async def post(body, **kw):
            posted.append(body)  # the card's text; its blocks are cut from the same lines
            return "111.222"

        mixin.post = post
        await mixin.announce_task(record, RunResult(status="ok", result_text=result_text),
                                  SimpleNamespace(key="lumen"))
        return posted[0]

    async def test_what_the_agent_wrote_leads_and_evidence_moves_to_the_status_line(self) -> None:
        body = await self._card("완료 4건\n실패 5건")
        head, status, *rest = body.splitlines()
        self.assertEqual("*📋 아침 보고*", head)
        self.assertIn("watch 실행 완료 · work/ 산출물 report.md", status)
        self.assertEqual(["> 완료 4건", "> 실패 5건"], rest)

    async def test_with_nothing_written_the_evidence_is_still_the_body(self) -> None:
        body = await self._card("")
        self.assertEqual("> watch 실행 완료 · work/ 산출물 report.md", body.splitlines()[-1])

    async def test_long_evidence_and_a_result_stay_inside_one_slack_section(self) -> None:
        # Slack rejects a section over 3000 characters. Unbounded evidence joined onto the
        # status line plus a 300-char excerpt reached 3151 here.
        evidence = ("실패 로그 한 줄이 아주 길다 " * 20 + "\n") * 10  # ~2800 chars, many lines
        body = await self._card("가" * 300, evidence=evidence[:2800])
        self.assertLessEqual(len(body), 3000)
        head, status, *rest = body.splitlines()
        self.assertTrue(status.endswith("…"), status)
        self.assertEqual(["> " + "가" * 300], rest)

    async def test_the_result_is_slack_mrkdwn_not_github_markdown(self) -> None:
        body = await self._card("## 요약\n**완료** 4건\n`**그대로**`")
        self.assertEqual(["> *요약*", "> *완료* 4건", "> `**그대로**`"], body.splitlines()[2:])

    async def test_card_links_keep_query_parameters(self) -> None:
        body = await self._card("[알림](https://example.com/a?x=1&notify=true&amp;region=kr)")
        self.assertEqual("> <https://example.com/a?x=1&amp;notify=true&amp;region=kr|알림>",
                         body.splitlines()[-1])

    async def test_an_inline_code_fence_stays_in_the_quote(self) -> None:
        # A line that opens and closes ``` is not a block: leaving it unquoted broke the
        # quote bar in the middle of the card.
        body = await self._card("see ```inline``` here\nnext line\n```\ncode\n```")
        self.assertEqual(["> see ```inline``` here", "> next line", "```", "code", "```"],
                         body.splitlines()[2:])

    async def test_broadcast_mentions_in_the_card_do_not_notify_the_channel(self) -> None:
        # A poll run that summarised outside text (a PR body) could carry these through,
        # and Slack notifies the whole channel for them even inside a bot's quote.
        body = await self._card("PR 본문: <!channel> 확인 <!here|here> <!everyone>",
                                evidence="실행 완료 <!here>", title="배포 <!channel>")
        self.assertNotIn("<!", body)
        self.assertIn("&lt;!channel>", body.splitlines()[0])
        self.assertIn("&lt;!here|here>", body)

    async def test_plain_broadcast_words_in_the_card_do_not_notify_the_channel(self) -> None:
        # Slack turns plain @channel/@here in a non-verbatim section into real broadcast
        # mentions (found live). A word joiner after @ stops that and still reads the same.
        import re
        body = await self._card("알림: @channel @Here (@everyone).",
                                evidence="실행 @here", title="배포 @CHANNEL")
        self.assertIsNone(re.search(r"(?i)(?<![\w@])@(?:channel|here|everyone)\b", body))
        self.assertEqual(4 + 1, body.count("@⁠"))
        visible = body.replace("⁠", "")
        for word in ("배포 @CHANNEL", "실행 @here", "알림: @channel @Here (@everyone)."):
            self.assertIn(word, visible)
        # Not a broadcast: an address, a longer word, an ordinary handle.
        body = await self._card("mail ops@here.example @channels @hereford @alice")
        self.assertNotIn("⁠", body)

    async def test_broadcast_defusing_preserves_link_destinations_and_code(self) -> None:
        for source, expected in (
                ("[profile](https://example.com/@here)",
                 "<https://example.com/@here|profile>"),
                ("[@channel](https://example.com/@everyone)",
                 "<https://example.com/@everyone|@\u2060channel>"),
                ("<https://example.com/@channel|profile>",
                 "<https://example.com/@channel|profile>"),
                ("<https://example.com/@here|@everyone>",
                 "<https://example.com/@here|@\u2060everyone>"),
                ("<https://example.com/@everyone>", "<https://example.com/@everyone>"),
                ("`@channel @here` outside @everyone",
                 "`@channel @here` outside @\u2060everyone"),
                ("```\n@channel @here\n```", "```\n@channel @here\n```"),
                ("ops@channel.io", "ops@channel.io")):
            with self.subTest(source=source):
                body = await self._card(source)
                self.assertEqual(expected, "\n".join(body.splitlines()[2:]).removeprefix("> "))

    async def test_an_operator_replay_card_with_long_evidence_stays_inside_one_section(self) -> None:
        from world.process import task as tasks

        body = await self._card("", evidence=("x" * 99 + "\n") * 40, title="t" * 120,
                                origin=tasks.Origin(kind="operator", by="U01", replay=True))
        self.assertTrue(body.startswith("♻️ *운영자 재처리 요청*\n"), body[:40])
        self.assertLessEqual(len(body), 3000)

    async def test_a_result_cut_to_fit_says_so(self) -> None:
        # A briefing cut mid-number ("실패 5" of "실패 57") read as the whole of it.
        body = await self._card("가" * 400)
        last = body.splitlines()[-1]
        self.assertEqual("> " + "가" * 299 + "…", last)

    async def test_a_code_block_cut_in_the_middle_is_closed(self) -> None:
        body = await self._card("```python\n" + "x=1\n" * 200 + "```")
        self.assertEqual(0, body.count("```") % 2, body[-40:])
        self.assertEqual("```", body.splitlines()[-1])
        self.assertNotIn("> x=1", body)
        self.assertNotIn("> ```", body)

    async def test_long_evidence_alone_stays_inside_one_slack_section(self) -> None:
        body = await self._card("", evidence=("x" * 99 + "\n") * 40)
        self.assertLessEqual(len(body), 3000)
        self.assertTrue(body.endswith("…"), body[-20:])

    async def test_a_token_in_the_evidence_is_redacted_on_either_line(self) -> None:
        # The result went through redact() and the evidence did not: a check that quoted
        # a failing command posted its token, as the body and on the status line.
        token = "xoxb-" + "1" * 40
        for result_text in ("", "완료"):
            body = await self._card(result_text, evidence=f"curl -H {token}")
            self.assertNotIn(token, body)
            self.assertIn("[redacted]", body)

    async def test_a_token_cut_by_the_status_line_is_still_redacted(self) -> None:
        # Redacted before the cut: a token cut short may no longer match redact().
        body = await self._card("완료", evidence="e" * 180 + " ghp_" + "a" * 36)
        self.assertNotIn("ghp_aaaa", body)

    async def test_a_code_block_in_evidence_cut_in_the_middle_is_closed(self) -> None:
        body = await self._card("", evidence="```\n" + ("x" * 99 + "\n") * 40 + "```")
        self.assertLessEqual(len(body), 3000)
        self.assertEqual(0, body.count("```") % 2, body[-40:])
        self.assertEqual("```", body.splitlines()[-1])
        self.assertNotIn("> x", body)
        self.assertNotIn("> ```", body)
        self.assertTrue(body.splitlines()[-2].endswith("…"), body[-40:])


class TeammateMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_hands_off_by_name_and_only_to_the_roster(self) -> None:
        from world.toolkit import make_teammate_mention_tool

        mention = make_teammate_mention_tool({"Lumen": "U0LUMEN", "Ember": "U0EMBER"})
        self.assertEqual("mention_teammate", mention.name)
        ok = await mention.handler({"name": "@lumen"})
        self.assertIn("<@U0LUMEN>", ok["content"][0]["text"])
        unknown = await mention.handler({"name": "approver"})
        self.assertTrue(unknown["is_error"])
        self.assertIn("'Lumen'", unknown["content"][0]["text"])

    def test_main_hands_off_to_its_roster_and_others_only_to_main(self) -> None:
        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(main_persona=SimpleNamespace(key="guide", display="Guide"))
        mixin.identities = {
            "guide": {"display": "Guide", "user_id": "U0GUIDE"},
            "lumen": {"display": "Lumen", "user_id": None},
        }
        self.assertEqual(["mention_approver", "hand_off"],
                         [t.name for t in mixin._mention_tools_for("guide", "U0HUMAN", "t1")])
        self.assertEqual(["hand_off"],
                         [t.name for t in mixin._mention_tools_for("lumen", "U0HUMAN", "t1")])
        # a relay turn has no thread to hand within
        self.assertEqual(["mention_approver"],
                         [t.name for t in mixin._mention_tools_for("guide", "U0HUMAN")])
        self.assertEqual([], mixin._mention_tools_for("lumen", "U0HUMAN"))


class FakeHandoffListener(MentionMixin):
    """Just enough listener for the handoff queue, drain and restart resume."""

    def __init__(self, threads=None):
        from world.types import ApprovalPhrases

        self.harness = SimpleNamespace(
            main_persona=SimpleNamespace(key="guide", display="Guide"),
            approval_phrases=ApprovalPhrases(),
        )
        self.identities = {"guide": {"display": "Guide"}, "lumen": {"display": "Lumen"}}
        self.state = {"threads": threads or {}}
        self.saved = 0
        self.posts: list[tuple[str, str | None]] = []
        self.hops: list[tuple[str, str, str]] = []
        self.queued_at_hop: list[int] = []

    def save_state(self):
        self.saved += 1

    async def post(self, text, *, identity=None, thread_ts=None, **_):
        self.posts.append((text, identity))

    async def _dispatch_bot_hop(self, thread_ts, text, event, author_identity, mentioned):
        # the record is still there while the teammate works: a crash now re-runs it
        self.queued_at_hop.append(len(self.state["threads"][thread_ts].get("handoffs", [])))
        self.hops.append((author_identity, mentioned[0], text))


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_hand_off_queues_durably_and_only_to_named_teammates(self) -> None:
        listener = FakeHandoffListener()
        [_, hand_off] = listener._mention_tools_for("guide", "U0HUMAN", "t1")
        ok = await hand_off.handler({"name": "@lumen", "request": "변경 목록 뽑아줘"})
        self.assertFalse(ok["is_error"])
        [queued] = listener.state["threads"]["t1"]["handoffs"]
        self.assertEqual(("guide", "lumen", "변경 목록 뽑아줘"),
                         (queued["from"], queued["to"], queued["text"]))
        self.assertGreater(listener.saved, 0)  # on disk before the tool returns
        bad = await hand_off.handler({"name": "approver", "request": "x"})
        self.assertTrue(bad["is_error"])
        empty = await hand_off.handler({"name": "Lumen", "request": " "})
        self.assertTrue(empty["is_error"])
        self.assertEqual(1, len(listener.state["threads"]["t1"]["handoffs"]))

    async def test_drain_announces_runs_through_the_hop_budget_then_forgets(self) -> None:
        listener = FakeHandoffListener()
        listener._queue_handoff("t1", "guide", "lumen", "변경 목록 뽑아줘")
        await listener._drain_handoffs("t1")
        self.assertEqual([("→ Lumen: 변경 목록 뽑아줘", "guide")], listener.posts)
        self.assertEqual([("guide", "lumen", "변경 목록 뽑아줘")], listener.hops)
        self.assertEqual([1], listener.queued_at_hop)
        self.assertNotIn("handoffs", listener.state["threads"]["t1"])

    async def test_the_record_line_carries_no_mention_and_a_second_drain_waits(self) -> None:
        listener = FakeHandoffListener()
        listener._queue_handoff("t1", "guide", "lumen", "<@U0EMBER> 이거 봐줘")
        listener._draining_threads = {"t1"}  # another turn is draining this thread
        await listener._drain_handoffs("t1")
        self.assertEqual([], listener.hops)
        listener._draining_threads = set()
        await listener._drain_handoffs("t1")
        self.assertEqual([("→ Lumen: 이거 봐줘", "guide")], listener.posts)

    async def test_restart_reruns_a_queued_handoff_without_announcing_it_twice(self) -> None:
        listener = FakeHandoffListener({"t1": {"handoffs": [
            {"id": "h1", "from": "guide", "to": "lumen", "text": "다시", "announced": True}],
            "turn_running": {"identity": "lumen"}}})
        await listener.resume_after_restart()
        self.assertEqual(1, len(listener.posts))
        self.assertIn("Lumen", listener.posts[0][0])  # "이어서 진행합니다 — Lumen에게 …"
        self.assertEqual([("guide", "lumen", "다시")], listener.hops)
        self.assertEqual({}, listener.state["threads"]["t1"])

    async def test_restart_reports_an_interrupted_turn_and_does_not_rerun_it(self) -> None:
        listener = FakeHandoffListener({"t1": {"turn_running": {"identity": "lumen"}}, "t2": {}})
        await listener.resume_after_restart()
        self.assertEqual([(listener.harness.approval_phrases.turn_interrupted, "lumen")],
                         listener.posts)
        self.assertEqual([], listener.hops)
        self.assertNotIn("turn_running", listener.state["threads"]["t1"])


class PersonaChatIconTests(unittest.TestCase):
    def test_icon_url_first_then_a_url_portrait_never_a_local_file(self) -> None:
        from world.types import Persona

        self.assertEqual("https://x.test/i.png",
                         Persona("a", "A", portrait="https://x.test/p.png",
                                 icon_url="https://x.test/i.png").chat_icon)
        self.assertEqual("https://x.test/p.png",
                         Persona("a", "A", portrait="https://x.test/p.png").chat_icon)
        self.assertEqual("", Persona("a", "A", portrait="portraits/a.png").chat_icon)
        self.assertEqual("https://x.test/p.png",
                         Persona("a", "A", portrait="https://x.test/p.png", icon_url=None).chat_icon)
        self.assertEqual("", Persona("a", "A", icon_url=None).icon_url)

    def test_a_url_with_surrounding_space_or_a_capital_scheme_is_still_a_url(self) -> None:
        # Pasted from a settings file or a browser bar: " HTTPS://…\n" is the same URL.
        from world.types import Persona

        self.assertEqual("HTTPS://x.test/i.png",
                         Persona("a", "A", icon_url=" HTTPS://x.test/i.png\n").chat_icon)
        self.assertEqual("Https://x.test/p.png",
                         Persona("a", "A", portrait=" Https://x.test/p.png").chat_icon)
        persona = Persona("a", "A")
        persona.icon_url = "https://x.test/late.png \n"
        self.assertEqual("https://x.test/late.png", persona.chat_icon)

    def test_an_icon_url_that_is_not_a_url_is_refused_whatever_the_portrait(self) -> None:
        from world.types import Persona

        for portrait in ("portraits/a.png", "https://x.test/p.png"):
            with self.subTest(portrait=portrait):
                with self.assertRaisesRegex(ValueError, r"persona 'a': icon_url must be a public http"):
                    Persona("a", "A", portrait=portrait, icon_url="icons/a.png")
        persona = Persona("a", "A", portrait="https://x.test/p.png")
        persona.icon_url = "icons/a.png"
        with self.assertRaises(ValueError):
            persona.chat_icon


class PersonaFaceTests(unittest.TestCase):
    def _poster(self):
        from world.slack.post import PostingMixin
        from world.types import Persona

        poster = PostingMixin()
        main = Persona(key="guide", display="Guide", portrait="https://x.test/guide.png")
        self.logged: list[str] = []
        poster.harness = SimpleNamespace(main_persona=main, log=self.logged.append, personas={
            "guide": main,
            "lumen": Persona(key="lumen", display="Lumen", portrait="https://x.test/lumen.png"),
            "ember": Persona(key="ember", display="Ember", portrait="art/ember.png"),
            "own": Persona(key="own", display="Own", bot_token_env="T_OWN_TOKEN"),
            "beacon": Persona(key="beacon", display="Beacon", portrait="portraits/beacon.png",
                             icon_url="https://x.test/beacon-icon.png"),
        })
        return poster

    def test_icon_url_is_the_chat_icon_and_wins_over_a_url_portrait(self) -> None:
        # Issue #1: a local portrait for the map and an icon URL for Slack, together.
        poster = self._poster()
        self.assertEqual({"username": "Beacon", "icon_url": "https://x.test/beacon-icon.png"},
                         poster._face("beacon"))
        lumen = poster.harness.personas["lumen"]
        lumen.icon_url = "https://x.test/lumen-icon.png"
        self.assertEqual("https://x.test/lumen-icon.png", poster._face("lumen")["icon_url"])

    def test_an_icon_url_broken_after_start_costs_the_icon_not_the_message(self) -> None:
        # A harness that sets icon_url from a settings file after building the persona
        # skips the constructor's check; every post by that persona used to fail instead.
        poster = self._poster()
        poster.harness.personas["lumen"].icon_url = "icons/lumen.png"
        self.assertEqual({"username": "Lumen"}, poster._face("lumen"))
        self.assertEqual({"username": "Lumen"}, poster._face("lumen"))
        [line] = self.logged  # once, not on every post
        self.assertIn("persona 'lumen': icon_url must be a public http", line)

    def test_a_persona_without_its_own_app_posts_under_its_name_and_icon(self) -> None:
        poster = self._poster()
        self.assertEqual({"username": "Lumen", "icon_url": "https://x.test/lumen.png"},
                         poster._face("lumen"))
        self.assertEqual({"username": "Ember"}, poster._face("ember"))  # not a URL: no icon

    def test_the_main_persona_and_a_persona_with_its_own_app_post_as_themselves(self) -> None:
        poster = self._poster()
        self.assertEqual({"icon_url": "https://x.test/guide.png"}, poster._face("guide"))  # name stays the app's
        self.assertEqual({}, poster._face("nobody"))
        with unittest.mock.patch.dict(os.environ, {"T_OWN_TOKEN": "xoxb-own"}):
            self.assertEqual({}, poster._face("own"))


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    """No default buttons: an announcement carries only what its agent declared."""

    def _agent(self, *buttons):
        return Agent(name="jira-watch", trigger=PollTrigger(check=lambda s, n: None),
                     buttons=buttons)

    def test_an_agent_without_buttons_gets_none(self) -> None:
        from world.slack.mentions import task_buttons

        self.assertEqual([], task_buttons(self._agent(), "20261001-abcd", "tasks/x/work/a.md"))
        self.assertEqual([], task_buttons(None, "20261001-abcd", "tasks/x/work/a.md"))

    def test_declared_buttons_render_with_unique_ids_and_no_prompt_in_the_value(self) -> None:
        from world import Button
        from world.slack.mentions import task_buttons

        agent = self._agent(
            Button.ask("원인 더 파봐", "막힌 티켓의 원인을 더 파봐", style="primary"),
            Button.ask("담당자 제안", "담당자 없는 티켓에 담당자를 제안해"),
            Button.link("Jenkins 열기", "https://ci.example/job/api/"),
            Button.show(),
        )
        rendered = task_buttons(agent, "20261001-abcd", "tasks/x/work/a.md")
        ids = [b["action_id"] for b in rendered]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual("jira-watch|0|20261001-abcd", rendered[0]["value"])
        self.assertNotIn("막힌", json.dumps(rendered, ensure_ascii=False))
        self.assertEqual("https://ci.example/job/api/", rendered[2]["url"])
        # show needs a file; without one it is left out rather than rendered dead.
        self.assertEqual(3, len(task_buttons(agent, "20261001-abcd", "")))

    def test_slack_and_reading_limits_are_checked_at_declaration(self) -> None:
        from world import Button

        with self.assertRaises(ValueError):
            Button.ask("x" * 76, "p")
        with self.assertRaises(ValueError):
            Button.ask("빈 요청", " ")
        with self.assertRaises(ValueError):
            self._agent(*(Button.ask(f"b{i}", "p") for i in range(6)))
        with self.assertRaises(ValueError):
            self._agent(Button.ask("a", "p", style="primary"), Button.ask("b", "p", style="primary"))

    async def test_an_ask_click_hands_the_declared_prompt_to_the_persona(self) -> None:
        from world import Button, Persona
        from world.slack.approvals import ApprovalMixin

        agent = self._agent(Button.ask("원인 더 파봐", "막힌 티켓의 원인을 더 파봐"))
        agent.persona = Persona(key="lumen", display="Lumen")
        calls = []
        mixin = ApprovalMixin()
        mixin.harness = SimpleNamespace(agents=[agent], main_persona=Persona(key="guide", display="Guide"))

        async def handle(thread_ts, text, event, identity):
            calls.append((thread_ts, text, event["user"], identity))

        mixin._handle_mention = handle
        await mixin._on_ask_button("jira-watch|0|20261001-abcd", "T1", "M1", "U1")
        await mixin._on_ask_button("jira-watch|9|20261001-abcd", "T1", "M1", "U1")
        await mixin._on_ask_button("other|0|x", "T1", "M1", "U1")
        [(thread, text, user, identity)] = calls
        self.assertEqual(("T1", "U1", "lumen"), (thread, user, identity))
        self.assertIn("막힌 티켓의 원인을 더 파봐", text)
        self.assertIn("20261001-abcd", text)


class StallReactionTests(unittest.IsolatedAsyncioTestCase):
    """The turn-end swap removes whatever progress emoji is showing. It used to remove
    🤔 unconditionally, so a turn that stalled past STALL_SOFT_S kept its 🥱 beside 👍."""

    async def test_a_stalled_turn_ends_without_its_stall_emoji(self) -> None:
        from unittest.mock import patch

        from world.slack import ui

        reactions: list[tuple[str, str]] = []

        class Listener:
            async def _react(self, ts, *, add, remove, identity):
                reactions.append((add, remove))

            async def _set_thread_status(self, *a, **kw):
                pass

        phrases = SimpleNamespace(stall_soft="…", stall_hard="…")
        with patch.object(ui, "STALL_SOFT_S", 0.0), patch.object(ui, "STALL_HARD_S", 60.0):
            trail = ui.ToolTrail(Listener(), "main", "T", "M", phrases)
            self.assertEqual(ui.EMOJI_WORKING, trail.showing)
            await asyncio.sleep(0.01)
            await trail.finish(True)
        self.assertEqual([(ui.EMOJI_STALL_SOFT, ui.EMOJI_WORKING)], reactions)
        self.assertEqual(ui.EMOJI_STALL_SOFT, trail.showing)



class ToolAuditTests(unittest.TestCase):
    """The end-of-turn audit shows every call's FULL arguments, so a reviewer can tell
    from the Slack thread alone what an agent did. Long audits split, never truncate."""

    def test_short_turn_fits_one_message_with_full_arguments(self) -> None:
        from world.slack.ui import audit_messages

        jql = "project = OPS AND status != Done ORDER BY updated DESC"
        [msg] = audit_messages("🛠 툴 2회 사용:", [("jira_search", {"jql": jql}), ("run_now", {})])
        self.assertIn(jql, msg)
        self.assertIn("1. `jira_search`", msg)
        self.assertIn("2. `run_now`", msg)
        self.assertNotIn("미리보기", msg)

    def test_long_audit_splits_without_losing_anything(self) -> None:
        from world.slack.ui import audit_messages

        big = "가" * 9000
        calls = [("write_note", {"name": "a.md", "content": big}), ("run_now", {})]
        msgs = audit_messages("🛠 툴 2회 사용:", calls, limit=1000)
        self.assertTrue(all(len(m) <= 1000 for m in msgs), [len(m) for m in msgs])
        joined = "".join(m.split("```")[1] for m in msgs if "```" in m and "write_note" in m)
        self.assertEqual(big, json.loads(joined)["content"])
        self.assertIn("2. `run_now`", msgs[-1])

    def test_a_fence_in_an_argument_cannot_close_the_block(self) -> None:
        from world.slack.ui import audit_messages

        [msg] = audit_messages("h", [("write_note", {"content": "a ``` b"})])
        self.assertEqual(2, msg.count("```"))


class PersonaVoiceTests(unittest.TestCase):
    """Persona.voice reaches both prompt paths, carries the fact guard, and is a no-op
    when empty so flavor-only harnesses render byte-for-byte as before."""

    VOICE = "반말, 짧게, 문장 끝에 ⚓ 하나"

    def _rules(self, persona):
        from world.slack.mentions import MentionMixin

        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(
            slack=SimpleNamespace(addressee="U1"), pre_approved_tools=[]
        )
        mixin._roster_lines = lambda identity: "ROSTER"
        return mixin._rules_for("lumen", persona)

    def _spawn_prompt(self, persona):
        from world.process.spawn import compose_prompt

        agent = SimpleNamespace(prompt_file=None, persona=persona)
        return compose_prompt(SimpleNamespace(home=Path(".")), agent, "TRIGGER", "CONTEXT")

    def test_voice_reaches_mention_and_spawned_prompts_with_the_guard(self) -> None:
        from world.types import VOICE_GUARD, Persona

        persona = Persona(key="lumen", display="Lumen", flavor="항해사", voice=self.VOICE)
        for prompt in (self._rules(persona), self._spawn_prompt(persona)):
            self.assertIn(self.VOICE, prompt)
            self.assertIn(VOICE_GUARD, prompt)

    def test_the_guard_names_every_fact_the_style_must_not_blur(self) -> None:
        from world.types import VOICE_GUARD

        for fact in ("numbers", '"unverified"', "errors", "APPROVAL_REQUEST"):
            self.assertIn(fact, VOICE_GUARD)

    def test_empty_voice_changes_nothing(self) -> None:
        from world.types import Persona

        plain = Persona(key="lumen", display="Lumen", flavor="항해사")
        blank = Persona(key="lumen", display="Lumen", flavor="항해사", voice="  ")
        self.assertEqual(self._rules(plain), self._rules(blank))
        self.assertTrue(self._rules(plain).startswith("You are 항해사\nYou are answering"))
        from world.types import MESSAGE_FORMAT

        self.assertEqual(
            self._spawn_prompt(plain),
            "You speak as 항해사\n\nYour final message is posted to the team's chat. "
            f"{MESSAGE_FORMAT}\n\nTRIGGER\n\nCONTEXT",
        )

    def test_the_layout_rule_sits_beside_the_voice_in_every_prompt(self) -> None:
        # A persona's run-on line ("좋아, 승인 받았다. … 반영, 모델 … push.") was
        # within its voice; nothing asked for a headline and one fact per line.
        from world.types import MESSAGE_FORMAT, Persona

        for voice in ("", self.VOICE):
            persona = Persona(key="lumen", display="Lumen", flavor="항해사", voice=voice)
            for prompt in (self._rules(persona), self._spawn_prompt(persona)):
                self.assertIn(MESSAGE_FORMAT, prompt)
                if voice:
                    self.assertLess(prompt.index(self.VOICE), prompt.index(MESSAGE_FORMAT))
        for rule in ("headline line", "blank line between", "one fact per line", "backticks",
                     "layout only"):
            self.assertIn(rule, MESSAGE_FORMAT)


if __name__ == "__main__":
    unittest.main()
