"""The tool trail's timings: the runner's tool-done callback, per-call durations, the
stall ladder pausing while a call is in flight, and the skill-only note."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.process import spawn
from world.runners.base import reports_tool_done
from world.slack import ui
from world.types import Agent, PollTrigger, RunResult

HAVE_SDK = importlib.util.find_spec("claude_agent_sdk") is not None


class Listener:
    """What ToolTrail calls back into, recording instead of talking to Slack."""

    def __init__(self) -> None:
        self.reactions: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.web = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "trail"}), chat_update=AsyncMock())
        self.harness = SimpleNamespace(slack=SimpleNamespace(channel="C0XXXXXXX"))

    async def _react(self, _ts, *, add, remove, identity):
        self.reactions.append((add, remove))

    async def _set_thread_status(self, _thread_ts, status, *, identity):
        self.statuses.append(status)

    def _web_for(self, _identity):
        return self.web

    def _face(self, _identity):
        return {}

    def posted(self) -> list[str]:
        """The audit as it ended up: the edited first message, then any continuation."""
        first = [call.kwargs["text"] for call in self.web.chat_update.await_args_list][-1:]
        rest = [call.kwargs["text"] for call in self.web.chat_postMessage.await_args_list][1:]
        return first + rest


PHRASES = SimpleNamespace(stall_soft="soft", stall_hard="hard", tool_text=lambda name: name)


def _trail(listener: Listener, *, timed: bool) -> ui.ToolTrail:
    return ui.ToolTrail(listener, "main", "T", "M", PHRASES, timed=timed)


class _Clock:
    """time.monotonic for ui.py, moved by hand so durations are exact."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class DurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = _Clock()
        # ui.py's own name `time`, not the time module: the event loop reads that one.
        patcher = patch.object(ui, "time", SimpleNamespace(monotonic=self.clock))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listener = Listener()

    async def test_each_call_shows_how_long_it_took_and_the_head_names_the_slowest(self) -> None:
        trail = _trail(self.listener, timed=True)
        await trail.tool("mcp__world__search", {"q": "open tickets"})
        self.clock.now += 1.25
        await trail.tool_done(0)
        await trail.tool("mcp__world__build_report", {"name": "weekly"})
        self.clock.now += 498
        await trail.tool_done(1)
        await trail.tool("Read", {"path": "notes.md"})
        self.clock.now += 12
        await trail.tool_done(2)
        await trail.finish(True)

        [audit] = self.listener.posted()
        head, *_ = audit.splitlines()
        self.assertEqual("🛠 툴 3회 사용 · 가장 오래 걸린 호출 2. `build_report` 8분 18초:", head)
        self.assertIn("1. `search` · 1.2초", audit)
        self.assertIn("2. `build_report` · 8분 18초", audit)
        self.assertIn("3. `Read` · 12초", audit)
        self.assertIn('"q": "open tickets"', audit)     # full arguments, as before

    async def test_results_out_of_order_land_on_their_own_calls(self) -> None:
        trail = _trail(self.listener, timed=True)
        await trail.tool("first", {})
        await trail.tool("second", {})
        self.clock.now += 2
        await trail.tool_done(1)
        self.clock.now += 5
        await trail.tool_done(0)
        self.assertEqual([7.0, 2.0], trail.durations)

    async def test_a_call_that_came_back_as_an_error_is_marked(self) -> None:
        trail = _trail(self.listener, timed=True)
        await trail.tool("deploy", {"env": "stage"})
        self.clock.now += 3
        await trail.tool_done(0, ok=False)
        await trail.finish(True)
        [audit] = self.listener.posted()
        self.assertIn("1. `deploy` · 3.0초 · 오류", audit)

    async def test_one_call_has_its_time_on_its_line_and_no_slowest_in_the_head(self) -> None:
        trail = _trail(self.listener, timed=True)
        await trail.tool("search", {})
        self.clock.now += 4
        await trail.tool_done(0)
        await trail.finish(True)
        [audit] = self.listener.posted()
        self.assertEqual("🛠 툴 1회 사용:", audit.splitlines()[0])
        self.assertIn("1. `search` · 4.0초", audit)

    async def test_a_call_with_no_reported_return_shows_no_duration(self) -> None:
        trail = _trail(self.listener, timed=True)
        await trail.tool("search", {})
        await trail.finish(False)
        [audit] = self.listener.posted()
        self.assertEqual("⚠️ 툴 1회 사용 (실패로 종료):", audit.splitlines()[0])
        self.assertIn("1. `search`", audit)
        self.assertNotIn("초", audit)

    async def test_a_late_or_unknown_done_changes_nothing(self) -> None:
        trail = _trail(self.listener, timed=True)
        await trail.tool("search", {})
        self.clock.now += 1
        await trail.tool_done(0)
        self.clock.now += 9
        await trail.tool_done(0)      # reported twice
        await trail.tool_done(7)      # never started
        self.assertEqual([1.0], trail.durations)

    async def test_an_untimed_trail_reads_as_it_always_did(self) -> None:
        trail = _trail(self.listener, timed=False)
        await trail.tool("mcp__world__search", {"q": "x"})
        await trail.tool("run_now", {})
        await trail.finish(True)
        [audit] = self.listener.posted()
        self.assertEqual("🛠 툴 2회 사용:", audit.splitlines()[0])
        self.assertIn("1. `search`\n", audit)
        self.assertTrue(audit.endswith("2. `run_now`"), audit)


class StallLadderTests(unittest.IsolatedAsyncioTestCase):
    """STALL_SOFT_S is patched to 20 ms; a 'long' tool is one that outlasts it."""

    async def _run(self, *, timed: bool) -> tuple[Listener, ui.ToolTrail]:
        listener = Listener()
        with patch.object(ui, "STALL_SOFT_S", 0.02), patch.object(ui, "STALL_HARD_S", 60.0):
            trail = _trail(listener, timed=timed)
            await trail.tool("build_report", {})
            await asyncio.sleep(0.08)             # the tool is still running
            self.during = list(listener.reactions)
            await trail.tool_done(0)
            await asyncio.sleep(0.08)             # now the MODEL is quiet
            self.after = list(listener.reactions)
            await trail.finish(True)
        return listener, trail

    async def test_a_tool_in_flight_does_not_escalate(self) -> None:
        _, trail = await self._run(timed=True)
        self.assertEqual([], self.during)
        # Once it returned, silence counts again.
        self.assertEqual([(ui.EMOJI_STALL_SOFT, ui.EMOJI_WORKING)], self.after)
        self.assertEqual(ui.EMOJI_STALL_SOFT, trail.showing)

    async def test_without_a_done_signal_the_ladder_runs_as_before(self) -> None:
        await self._run(timed=False)
        self.assertEqual([(ui.EMOJI_STALL_SOFT, ui.EMOJI_WORKING)], self.during)

    async def test_the_ladder_waits_for_the_last_of_several_calls(self) -> None:
        listener = Listener()
        with patch.object(ui, "STALL_SOFT_S", 0.02), patch.object(ui, "STALL_HARD_S", 60.0):
            trail = _trail(listener, timed=True)
            await trail.tool("a", {})
            await trail.tool("b", {})
            await trail.tool_done(0)
            await asyncio.sleep(0.08)
            self.assertEqual([], listener.reactions)      # b is still running
            await trail.tool_done(1)
            await asyncio.sleep(0.08)
            self.assertEqual([(ui.EMOJI_STALL_SOFT, ui.EMOJI_WORKING)], listener.reactions)
            await trail.finish(True)

    async def test_a_tool_start_still_clears_a_stall_that_was_showing(self) -> None:
        listener = Listener()
        with patch.object(ui, "STALL_SOFT_S", 0.02), patch.object(ui, "STALL_HARD_S", 60.0):
            trail = _trail(listener, timed=True)
            await asyncio.sleep(0.08)                      # the model thought for a while
            await trail.tool("search", {})
            self.assertEqual(
                [(ui.EMOJI_STALL_SOFT, ui.EMOJI_WORKING), (ui.EMOJI_WORKING, ui.EMOJI_STALL_SOFT)],
                listener.reactions)
            await trail.finish(True)

    async def test_finishing_with_a_call_in_flight_leaves_no_timer_behind(self) -> None:
        listener = Listener()
        with patch.object(ui, "STALL_SOFT_S", 0.02), patch.object(ui, "STALL_HARD_S", 60.0):
            trail = _trail(listener, timed=True)
            await trail.tool("search", {})
            await trail.finish(False)
            await trail.tool_done(0)                       # a result after the turn ended
            await asyncio.sleep(0.08)
        self.assertEqual([], listener.reactions)


class SkillOnlyNoteTests(unittest.IsolatedAsyncioTestCase):
    def test_the_note_is_for_a_turn_whose_only_tool_was_the_skill_loader(self) -> None:
        self.assertTrue(ui.skill_only_note([("Skill", {"skill": "weekly-report"})]))
        self.assertTrue(ui.skill_only_note([("Skill", {}), ("Skill", {})]))
        self.assertEqual("", ui.skill_only_note([]))
        self.assertEqual("", ui.skill_only_note([("search", {})]))
        self.assertEqual("", ui.skill_only_note([("Skill", {}), ("post_message", {})]))

    async def test_it_closes_the_audit_of_such_a_turn(self) -> None:
        listener = Listener()
        trail = _trail(listener, timed=True)
        await trail.tool("Skill", {"skill": "weekly-report"})
        await trail.tool_done(0)
        await trail.finish(True)
        [audit] = listener.posted()
        self.assertEqual(ui.skill_only_note(trail.calls), audit.splitlines()[-1])
        self.assertIn("`Skill`", audit.splitlines()[-1])

    async def test_a_turn_that_went_on_to_use_a_tool_gets_no_note(self) -> None:
        listener = Listener()
        trail = _trail(listener, timed=True)
        await trail.tool("Skill", {"skill": "weekly-report"})
        await trail.tool("mcp__world__post_message", {"text": "done"})
        await trail.finish(True)
        [audit] = listener.posted()
        self.assertNotIn("스킬 문서만", audit)


class OverflowTests(unittest.TestCase):
    """Durations and the note ride the same split: nothing is dropped, nothing over
    the limit."""

    def test_a_long_audit_still_splits_and_keeps_every_argument(self) -> None:
        big = "가" * 9000
        calls = [("write_note", {"name": "a.md", "content": big}), ("run_now", {})]
        msgs = ui.audit_messages("🛠 툴 2회 사용:", calls, limit=1000,
                                 marks=[" · 2분 3초", " · 1.0초 · 오류"], note="ℹ️ note")
        self.assertTrue(all(len(m) <= 1000 for m in msgs), [len(m) for m in msgs])
        joined = "".join(m.split("```")[1] for m in msgs if "```" in m and "write_note" in m)
        self.assertEqual(big, json.loads(joined)["content"])
        self.assertTrue(all("1. `write_note` · 2분 3초" in m for m in msgs if "write_note" in m))
        self.assertIn("2. `run_now` · 1.0초 · 오류", msgs[-1])
        self.assertTrue(msgs[-1].endswith("ℹ️ note"))

    def test_a_note_that_does_not_fit_gets_a_message_of_its_own(self) -> None:
        calls = [("search", {"q": "x" * 60})]
        note = "ℹ️ " + "n" * 80
        msgs = ui.audit_messages("head:", calls, limit=110, note=note)
        self.assertEqual(note, msgs[-1])
        self.assertIn("1. `search`", msgs[-2])
        self.assertTrue(all(len(m) <= 110 for m in msgs))

    def test_format_duration(self) -> None:
        self.assertEqual("0.4초", ui.format_duration(0.42))
        self.assertEqual("9.9초", ui.format_duration(9.94))
        self.assertEqual("42초", ui.format_duration(42.4))
        self.assertEqual("2분", ui.format_duration(120))
        self.assertEqual("8분 18초", ui.format_duration(498.7))


class RunnerContractTests(unittest.IsolatedAsyncioTestCase):
    def test_which_runners_report_a_tool_returning(self) -> None:
        class Plain:
            async def run(self, agent, prompt, *, on_tool=None, cwd=None): ...

        class Timed:
            async def run(self, agent, prompt, *, on_tool=None, cwd=None, on_tool_done=None): ...

        class Open:
            async def run(self, agent, prompt, **kwargs): ...

        self.assertFalse(reports_tool_done(Plain()))
        self.assertTrue(reports_tool_done(Timed()))
        self.assertTrue(reports_tool_done(Open()))
        self.assertFalse(reports_tool_done(object()))

    def test_codex_does_not_claim_timings_it_cannot_have(self) -> None:
        from world.runners.codex import CodexRunner

        # Its tool calls are read back after the process exits, all at once.
        self.assertFalse(reports_tool_done(CodexRunner.__new__(CodexRunner)))

    async def _oneshot(self, runner, **kwargs) -> RunResult:
        harness = SimpleNamespace(agent_runner=lambda: runner, log=lambda _t: None)
        agent = Agent("mention", PollTrigger(lambda *_: None))
        return await spawn.run_agent_oneshot(harness, agent, "hi", **kwargs)

    async def test_spawn_hands_the_callback_only_to_a_runner_that_takes_it(self) -> None:
        seen: dict = {}

        class Plain:
            async def run(self, agent, prompt, *, on_tool=None):
                seen["plain"] = True
                return RunResult("ok", "fine")

        class Timed:
            async def run(self, agent, prompt, *, on_tool=None, on_tool_done=None):
                seen["timed"] = on_tool_done
                return RunResult("ok", "fine")

        done = AsyncMock()
        self.assertEqual("ok", (await self._oneshot(Plain(), on_tool_done=done)).status)
        self.assertEqual("ok", (await self._oneshot(Timed(), on_tool_done=done)).status)
        self.assertIs(done, seen["timed"])
        await self._oneshot(Timed())
        self.assertIsNone(seen["timed"])


def _message(cls, **fields):
    """An SDK message without its constructor: these tests need three attributes, and
    the real classes have required fields that have changed between SDK releases."""
    message = cls.__new__(cls)
    for name, value in fields.items():
        object.__setattr__(message, name, value)
    return message


@unittest.skipUnless(HAVE_SDK, "claude_agent_sdk not installed")
class ClaudeStreamPairingTests(unittest.IsolatedAsyncioTestCase):
    """_consume_stream turns a ToolResultBlock's tool_use_id into the call's number."""

    def setUp(self) -> None:
        from world.runners import claude

        self.claude = claude

    def use(self, block_id: str, name: str):
        return _message(self.claude.ToolUseBlock, id=block_id, name=name, input={"n": name})

    def result(self, block_id: str, is_error=None):
        return _message(self.claude.ToolResultBlock, tool_use_id=block_id, content="out",
                        is_error=is_error)

    def assistant(self, *blocks):
        return _message(self.claude.AssistantMessage, content=list(blocks), error=None)

    def user(self, content):
        return _message(self.claude.UserMessage, content=content)

    async def consume(self, messages, **callbacks):
        async def stream(**_kwargs):
            for message in messages:
                yield message

        with patch.object(self.claude, "query", new=stream):
            return await self.claude._consume_stream("p", None, **callbacks)

    async def test_results_are_paired_by_id_not_by_arrival_order(self) -> None:
        events: list[tuple] = []

        async def on_tool(name, tool_input):
            events.append(("start", name))

        async def on_tool_done(number, ok):
            events.append(("done", number, ok))

        await self.consume(
            [
                self.assistant(self.use("id-a", "search"), self.use("id-b", "build")),
                self.user([self.result("id-b", is_error=True)]),
                self.user([self.result("id-a")]),
                self.user("a plain user turn is not a tool result"),
                self.assistant(self.use("id-c", "post")),
                self.user([self.result("id-c", is_error=False), self.result("id-unknown")]),
            ],
            on_tool=on_tool, on_tool_done=on_tool_done,
        )
        self.assertEqual(
            [("start", "search"), ("start", "build"), ("done", 1, False), ("done", 0, True),
             ("start", "post"), ("done", 2, True)],
            events,
        )

    async def test_without_a_done_callback_the_stream_reads_as_before(self) -> None:
        started: list[str] = []

        async def on_tool(name, tool_input):
            started.append(name)

        result, limited = await self.consume(
            [self.assistant(self.use("id-a", "search")), self.user([self.result("id-a")])],
            on_tool=on_tool,
        )
        self.assertEqual(["search"], started)
        self.assertEqual((None, False), (result, limited))


if __name__ == "__main__":
    unittest.main()
