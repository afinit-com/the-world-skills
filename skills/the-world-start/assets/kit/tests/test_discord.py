"""EXPERIMENTAL Discord gateway — fakes only, no network, no token.

The gateway is tested through its plain-dataclass surface (InboundMessage, ButtonClick)
with a fake transport, so nothing here needs discord.py. The one adapter test at the
bottom runs only when discord.py is installed (`uv run --extra dev --extra discord`).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from world.discord.gateway import (
    APPROVE_ID,
    DENY_ID,
    ButtonClick,
    DiscordGateway,
    HistoryLine,
    InboundMessage,
    chunk_text,
)
from world.harness import Harness
from world.process import task as tasks
from world.toolkit import result, tool
from world.types import DiscordConfig, Persona, RunResult

CHANNEL = "100"
APPROVER = "555"
STRANGER = "777"
HUMAN = "888"
BOT = "999"
ENV = {"T_DISCORD_TOKEN": "fake-token", "T_DISCORD_CHANNEL": CHANNEL, "T_DISCORD_APPROVER": APPROVER}


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.threads: list[tuple[str, str, str]] = []
        self.history_lines: list[HistoryLine] = []
        self.events: list[tuple[str, str]] = []  # ("typing+"/"typing-"/"send", channel)
        self.typing_fails = False
        self._n = 0

    @contextlib.asynccontextmanager
    async def typing(self, channel_id):
        if self.typing_fails:
            raise RuntimeError("typing endpoint down")
        self.events.append(("typing+", channel_id))
        try:
            yield
        finally:
            self.events.append(("typing-", channel_id))

    async def run(self, stop):
        await stop.wait()

    async def send(self, channel_id, text, *, buttons=None, as_persona=None):
        self._n += 1
        mid = f"m{self._n}"
        self.events.append(("send", channel_id))
        self.sent.append({"channel": channel_id, "text": text, "buttons": buttons, "id": mid,
                          "as": as_persona})
        return mid

    async def create_thread(self, channel_id, message_id, name):
        self.threads.append((channel_id, message_id, name))
        return f"t-{message_id}"

    async def history(self, channel_id, limit):
        return list(self.history_lines)


class ScriptedRunner:
    """Answers each turn with `reply`, or — when the persona holds a `publish` tool —
    calls it the way a model would, and reports what the gate said."""

    provider = "fake"

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.events: list | None = None  # set to the transport's events to log "run"

    async def run(self, agent, prompt, *, on_tool=None):
        self.prompts.append(prompt)
        if self.events is not None:
            self.events.append(("run", ""))
        publish = next((t for t in agent.tools if t.name == "publish"), None)
        if publish is None:
            return RunResult(status="ok", result_text=self.reply)
        res = await publish.handler({})
        if res["is_error"]:
            return RunResult(status="ok", result_text="brief\nAPPROVAL_REQUEST: publish the post")
        return RunResult(status="ok", result_text="published")


def msg(id, *, channel=CHANNEL, parent=None, author=HUMAN, bot=False, mention=True, content="hi"):
    return InboundMessage(id=id, channel_id=channel, parent_id=parent, author_id=author,
                          author_is_bot=bot, mentions_bot=mention, content=content)


class Click:
    """A ButtonClick plus a record of how it was answered."""

    def __init__(self, custom_id, message_id, user_id, channel_id="t-m0"):
        self.ephemerals: list[str] = []
        self.resolved: list[str] = []

        async def eph(text):
            self.ephemerals.append(text)

        async def res(text):
            self.resolved.append(text)

        self.click = ButtonClick(custom_id, message_id, channel_id, user_id, eph, res)


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.publish_calls = 0

        @tool("publish", "Publish a post somewhere public", {})
        async def publish(_args):
            self.publish_calls += 1
            return result(0, "posted")

        self.publish = publish
        self.runner = ScriptedRunner()
        self.harness = Harness(
            "t", home=self.home, runner=self.runner,
            main_persona=Persona(key="main", display="Main"),
            extra_personas=[Persona(key="scout", display="Scout",
                                    portrait="https://example.test/scout.png"),
                            Persona(key="keeper", display="Keeper", portrait="art/keeper.png"),
                            Persona(key="beacon", display="Beacon", portrait="art/beacon.png",
                                    icon_url="https://example.test/beacon-icon.png")],
            discord=DiscordConfig(bot_token_env="T_DISCORD_TOKEN",
                                  channel_env="T_DISCORD_CHANNEL",
                                  approver_env="T_DISCORD_APPROVER"),
        )
        self.harness.log = lambda _m: None
        self.state = {"threads": {}, "agents": {}}
        self.transport = FakeTransport()
        self.gw = DiscordGateway(self.harness, self.state, lambda: None, transport=self.transport)

    async def asyncSetUp(self) -> None:
        await self.gw.on_ready(BOT)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()


class ChunkTests(unittest.TestCase):
    def test_every_piece_fits_and_nothing_is_lost(self) -> None:
        text = ("line of text\n" * 400) + ("x" * 4500)
        pieces = chunk_text(text)
        self.assertTrue(all(len(p) <= 2000 for p in pieces))
        self.assertEqual(text.replace("\n", ""), "".join(pieces).replace("\n", ""))

    def test_empty_text_still_posts_something(self) -> None:
        self.assertEqual(["(empty)"], chunk_text(""))


class MentionTests(GatewayCase):
    async def test_mention_opens_a_thread_runs_the_turn_and_replies_chunked(self) -> None:
        self.runner.reply = "A" * 4500
        await self.gw.on_message(msg("m0", content="what is up"))

        self.assertEqual([(CHANNEL, "m0", "what is up")], self.transport.threads)
        self.assertEqual(1, len(self.runner.prompts))
        self.assertIn("what is up", self.runner.prompts[0])
        replies = [s for s in self.transport.sent if s["channel"] == "t-m0"]
        self.assertEqual(3, len(replies))
        self.assertTrue(all(len(s["text"]) <= 2000 for s in replies))
        self.assertTrue(replies[0]["text"].startswith("AAAA"))
        self.assertEqual("mention", self.state["threads"]["t-m0"]["origin"])

    async def test_the_prompt_says_to_answer_in_the_humans_language(self) -> None:
        # Live test: an English persona flavor made every reply English to a Korean user.
        await self.gw.on_message(msg("m0", content="안녕, 뭐 할 수 있어?"))
        self.assertIn("same language the human", self.runner.prompts[0])

    async def test_typing_shows_in_the_thread_for_the_whole_turn(self) -> None:
        # The "is typing…" line is the only progress signal Discord has; it must cover
        # the model run and be gone before the reply lands.
        self.runner.events = self.transport.events
        await self.gw.on_message(msg("m0", content="what is up"))
        self.assertEqual(
            [("typing+", "t-m0"), ("run", ""), ("typing-", "t-m0"), ("send", "t-m0")],
            self.transport.events,
        )

    async def test_a_typing_failure_does_not_cost_the_reply(self) -> None:
        self.transport.typing_fails = True
        await self.gw.on_message(msg("m0", content="what is up"))
        self.assertEqual(1, len(self.runner.prompts))
        self.assertEqual(["t-m0"], [s["channel"] for s in self.transport.sent])

    async def test_what_is_not_answered(self) -> None:
        await self.gw.on_message(msg("a", mention=False))                 # chatter in channel
        await self.gw.on_message(msg("b", bot=True))                      # any bot
        await self.gw.on_message(msg("c", author=BOT))                    # ourselves
        await self.gw.on_message(msg("d", channel="200"))                 # another channel
        await self.gw.on_message(msg("e", channel="t-x", parent=CHANNEL, mention=False))  # untracked thread
        self.assertEqual([], self.runner.prompts)
        self.assertEqual([], self.transport.sent)

    async def test_a_reply_in_a_tracked_thread_continues_without_a_mention(self) -> None:
        await self.gw.on_message(msg("m0"))
        self.transport.history_lines = [HistoryLine(BOT, True, "earlier answer")]
        await self.gw.on_message(msg("m9", channel="t-m0", parent=CHANNEL, mention=False,
                                     content="and then?"))
        self.assertEqual(2, len(self.runner.prompts))
        self.assertIn("[main] earlier answer", self.runner.prompts[1])
        self.assertIn("hi", self.runner.prompts[1])  # the starter message, from state

    async def test_a_redelivered_message_runs_once(self) -> None:
        await self.gw.on_message(msg("m0"))
        await self.gw.on_message(msg("m0"))
        self.assertEqual(1, len(self.runner.prompts))


class AnnounceTests(GatewayCase):
    async def test_a_scheduled_result_posts_to_the_channel_and_opens_a_thread(self) -> None:
        record = tasks.create(self.home, type="watch", title="nightly diff", agent="watch")
        run = RunResult(status="ok", result_text="3 changes", input_tokens=900,
                        output_tokens=100, cost_usd=0.01, duration_s=4.0)
        thread = await self.gw.announce_task(record, run, self.harness.main_persona)

        [post] = self.transport.sent
        self.assertEqual(CHANNEL, post["channel"])
        self.assertIn("**nightly diff**", post["text"])
        self.assertIn("-# ", post["text"])  # the run footer, as Discord subtext
        self.assertEqual("t-m1", thread)
        self.assertEqual(record.id, self.state["threads"]["t-m1"]["task_id"])

    async def test_the_result_leads_the_card_over_the_completion_evidence(self) -> None:
        # Issue #5, the Discord twin of the Slack card.
        record = tasks.create(self.home, type="watch", title="nightly diff", agent="watch")
        tasks.append_event(self.home, record.id, "verify", verdict="done",
                           evidence=["watch 실행 완료 · work/ 산출물 diff.md"])
        await self.gw.announce_task(record, RunResult(status="ok", result_text="3 changes"),
                                    self.harness.main_persona)
        lines = self.transport.sent[0]["text"].splitlines()
        self.assertIn("watch 실행 완료 · work/ 산출물 diff.md", lines[1])
        self.assertEqual("> 3 changes", lines[2])

    async def test_a_card_with_long_evidence_is_still_one_discord_message(self) -> None:
        record = tasks.create(self.home, type="watch", title="t" * 120, agent="watch")
        tasks.append_event(self.home, record.id, "verify", verdict="done",
                           evidence=[("e" * 99 + "\n") * 28])
        run = RunResult(status="ok", result_text="r" * 300, input_tokens=900,
                        output_tokens=100, cost_usd=0.01, duration_s=4.0)
        await self.gw.announce_task(record, run, self.harness.main_persona)
        [post] = self.transport.sent
        self.assertLessEqual(len(post["text"]), 2000)

    async def test_a_card_with_only_long_evidence_is_still_one_discord_message(self) -> None:
        # No result_text, so the evidence is the body: up to CARD_BODY_CHARS of it.
        record = tasks.create(self.home, type="watch", title="t" * 120, agent="watch")
        tasks.append_event(self.home, record.id, "verify", verdict="done",
                           evidence=[("e" * 99 + "\n") * 28] * 4)
        run = RunResult(status="ok", input_tokens=900, output_tokens=100, cost_usd=0.01,
                        duration_s=4.0)
        await self.gw.announce_task(record, run, self.harness.main_persona)
        [post] = self.transport.sent
        self.assertLessEqual(len(post["text"]), 2000)
        self.assertIn("-# ", post["text"])

    async def test_a_token_in_the_evidence_never_reaches_discord(self) -> None:
        token = "ghp_" + "a" * 36
        for result_text in ("", "3 changes"):
            record = tasks.create(self.home, type="watch", title="nightly diff", agent="watch")
            tasks.append_event(self.home, record.id, "verify", verdict="done",
                               evidence=[f"git push https://{token}@github.com"])
            await self.gw.announce_task(record, RunResult(status="ok", result_text=result_text),
                                        self.harness.main_persona)
            self.assertNotIn(token, self.transport.sent[-1]["text"])

    async def test_a_code_block_in_evidence_cut_short_is_closed_on_discord(self) -> None:
        record = tasks.create(self.home, type="watch", title="nightly diff", agent="watch")
        tasks.append_event(self.home, record.id, "verify", verdict="done",
                           evidence=["```diff\n" + "+ line\n" * 400 + "```"])
        await self.gw.announce_task(record, RunResult(status="ok"), self.harness.main_persona)
        text = self.transport.sent[0]["text"]
        self.assertLessEqual(len(text), 2000)
        self.assertEqual(0, text.count("```") % 2, text[-60:])

    async def test_a_long_result_is_marked_as_cut_on_discord_too(self) -> None:
        record = tasks.create(self.home, type="watch", title="nightly diff", agent="watch")
        run = RunResult(status="ok", result_text="```diff\n" + "+ line\n" * 100 + "```")
        await self.gw.announce_task(record, run, self.harness.main_persona)
        lines = self.transport.sent[0]["text"].splitlines()
        self.assertEqual("> ```", lines[-1])
        self.assertTrue(lines[-2].endswith("…"), lines[-2])

    async def test_a_result_for_a_conversation_replies_in_its_thread(self) -> None:
        self.state["threads"]["t-m0"] = {"origin": "mention"}
        record = tasks.create(self.home, type="watch", title="asked for", agent="watch")
        record.thread = "t-m0"
        thread = await self.gw.announce_task(record, RunResult(status="ok"), self.harness.main_persona)
        self.assertEqual("t-m0", thread)
        self.assertEqual("t-m0", self.transport.sent[0]["channel"])
        self.assertEqual([], self.transport.threads)

    async def test_the_main_persona_posts_as_the_bot(self) -> None:
        record = tasks.create(self.home, type="watch", title="by main", agent="watch")
        await self.gw.announce_task(record, RunResult(status="ok"), self.harness.main_persona)
        self.assertIsNone(self.transport.sent[0]["as"])

    async def test_another_persona_posts_under_its_own_name_and_avatar(self) -> None:
        record = tasks.create(self.home, type="watch", title="by scout", agent="watch")
        scout = self.harness.personas["scout"]
        thread = await self.gw.announce_task(record, RunResult(status="ok"), scout)
        [post] = self.transport.sent
        self.assertEqual(("Scout", "https://example.test/scout.png"), post["as"])
        self.assertEqual("t-m1", thread)  # a webhook message still opens the thread

    async def test_a_portrait_that_is_not_a_url_is_left_off(self) -> None:
        await self.gw.post("hello", identity="keeper")
        await self.gw.post("hello", identity="nobody")
        self.assertEqual([("Keeper", ""), None], [s["as"] for s in self.transport.sent])

    async def test_an_icon_url_broken_after_start_costs_the_avatar_not_the_message(self) -> None:
        logged: list[str] = []
        self.harness.log = logged.append
        self.harness.personas["keeper"].icon_url = "icons/keeper.png"
        await self.gw.post("hello", identity="keeper")
        await self.gw.post("again", identity="keeper")
        self.assertEqual([("Keeper", "")] * 2, [s["as"] for s in self.transport.sent])
        self.assertEqual(1, len(logged))
        self.assertIn("persona 'keeper'", logged[0])

    async def test_icon_url_is_the_avatar_beside_a_local_portrait(self) -> None:
        await self.gw.post("hello", identity="beacon")
        self.assertEqual(("Beacon", "https://example.test/beacon-icon.png"),
                         self.transport.sent[0]["as"])


class VoiceTests(GatewayCase):
    async def test_a_persona_voice_reaches_the_discord_prompt_with_its_guard(self) -> None:
        from world.types import VOICE_GUARD

        self.harness.main_persona.voice = "반말, 짧게"
        await self.gw.on_message(msg("m0"))
        self.assertIn("Your speaking style: 반말, 짧게", self.runner.prompts[-1])
        self.assertIn(VOICE_GUARD, self.runner.prompts[-1])

    async def test_no_voice_adds_nothing(self) -> None:
        await self.gw.on_message(msg("m0"))
        self.assertNotIn("Your speaking style", self.runner.prompts[-1])


class HandoffRunner:
    """Main hands the first turn to Scout; every other turn just answers."""

    provider = "fake"

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.prompts: list[str] = []

    async def run(self, agent, prompt, *, on_tool=None):
        self.turns.append(agent.persona.key)
        self.prompts.append(prompt)
        hand_off = next((t for t in agent.tools if t.name == "hand_off"), None)
        if agent.persona.key == "main" and len(self.turns) == 1 and hand_off:
            res = await hand_off.handler({"name": "Scout", "request": "변경 목록 뽑아줘"})
            assert not res["is_error"], res
            return RunResult(status="ok", result_text="Scout에게 넘겼어요")
        return RunResult(status="ok", result_text=f"{agent.persona.display} 답")


class HandoffTests(GatewayCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = HandoffRunner()
        self.harness.runner = self.runner

    async def test_main_hands_off_and_the_teammate_answers_under_its_own_name(self) -> None:
        await self.gw.on_message(msg("m0"))
        self.assertEqual(["main", "scout"], self.runner.turns)
        said = [(s["text"].split("\n")[0], s["as"]) for s in self.transport.sent]
        self.assertEqual("Scout에게 넘겼어요", said[0][0])
        self.assertIsNone(said[0][1])
        self.assertEqual(("→ Scout: 변경 목록 뽑아줘", None), said[1])
        self.assertEqual("Scout 답", said[2][0])
        self.assertEqual(("Scout", "https://example.test/scout.png"), said[2][1])
        meta = self.state["threads"]["t-m0"]
        self.assertNotIn("handoffs", meta)
        self.assertNotIn("turn_running", meta)
        self.assertIn("Teammates: Scout, Keeper", self.runner.prompts[0])
        self.assertIn("Teammates: Main", self.runner.prompts[1])  # back to the hub only

    async def test_naming_a_teammate_in_the_text_does_not_make_it_their_turn(self) -> None:
        # A person's message is always the main persona's turn; only hand_off,
        # called by a persona during its own turn, starts a teammate's.
        await self.gw.on_message(msg("m0", content="@Scout Scout: 너가 직접 해"))
        self.assertEqual("main", self.runner.turns[0])

    async def test_past_the_cap_the_handoff_stops_with_a_notice(self) -> None:
        from world.discord.gateway import MAX_HOPS

        self.state["threads"]["t-m0"] = {"channel": "discord", "bot_hops": MAX_HOPS}
        self.gw._queue_handoff("t-m0", "main", "scout", "더 해줘")
        await self.gw._drain_handoffs("t-m0")
        self.assertEqual([], self.runner.turns)
        self.assertIn("멈춥니다", self.transport.sent[-1]["text"])
        self.assertNotIn("handoffs", self.state["threads"]["t-m0"])

    async def test_a_person_speaking_resets_the_handoff_budget(self) -> None:
        self.state["threads"]["t-m0"] = {"channel": "discord", "bot_hops": 4,
                                         "hop_limit_notified": True}
        await self.gw.on_message(msg("m5", channel="t-m0", parent=CHANNEL, mention=False))
        self.assertEqual(["main", "scout"], self.runner.turns)
        self.assertEqual(1, self.state["threads"]["t-m0"]["bot_hops"])

    async def test_restart_reruns_a_queued_handoff_and_reports_a_cut_turn(self) -> None:
        self.state["threads"]["t-a"] = {"channel": "discord", "handoffs": [
            {"id": "h1", "from": "main", "to": "scout", "text": "다시", "announced": True}]}
        self.state["threads"]["t-b"] = {"channel": "discord",
                                        "turn_running": {"identity": "keeper"}}
        await self.gw.resume_after_restart()
        self.assertEqual(["scout"], self.runner.turns)  # the cut person's turn is not re-run
        texts = {(s["channel"], s["text"].split("\n")[0]) for s in self.transport.sent}
        ph = self.harness.approval_phrases
        self.assertIn(("t-a", ph.handoff_resumed.format(to_name="Scout")), texts)
        self.assertIn(("t-b", ph.turn_interrupted), texts)
        self.assertNotIn("turn_running", self.state["threads"]["t-b"])


class ApprovalTests(GatewayCase):
    def setUp(self) -> None:
        super().setUp()
        self.harness.main_persona.mention_tools = [self.publish]

    async def _ask(self) -> dict:
        await self.gw.on_message(msg("m0", content="post it"))
        [ask] = [s for s in self.transport.sent if s["buttons"]]
        return ask

    async def test_the_gate_refuses_then_asks_with_buttons(self) -> None:
        ask = await self._ask()
        self.assertEqual(0, self.publish_calls)
        self.assertEqual("t-m0", ask["channel"])
        self.assertEqual([APPROVE_ID, DENY_ID], [b[0] for b in ask["buttons"]])
        self.assertIn(f"<@{APPROVER}>", ask["text"])
        self.assertEqual(ask["id"], self.state["pending_approvals"][0]["msg_ts"])

    async def test_approver_click_grants_once_and_the_tool_runs_once(self) -> None:
        ask = await self._ask()
        c = Click(APPROVE_ID, ask["id"], APPROVER)
        await self.gw.on_click(c.click)

        self.assertEqual([], c.ephemerals)
        self.assertEqual(1, len(c.resolved))
        self.assertEqual(1, self.publish_calls)                       # ran on resume
        self.assertNotIn("t-m0", self.state.get("approved_threads", {}))  # and spent it
        self.assertEqual([], self.state["pending_approvals"])
        ledger = [json.loads(line) for line in self.harness.ledger_path.read_text().splitlines()]
        self.assertEqual(["approved"], [e["decision"] for e in ledger])
        self.assertEqual(APPROVER, ledger[0]["by"])

        # A second click on the same ask finds nothing to decide.
        again = Click(APPROVE_ID, ask["id"], APPROVER)
        await self.gw.on_click(again.click)
        self.assertEqual(1, len(again.ephemerals))
        self.assertEqual(1, self.publish_calls)

        # And the next attempt in the thread is refused again: one click, one call.
        await self.gw.on_message(msg("m5", channel="t-m0", parent=CHANNEL, content="again"))
        self.assertEqual(1, self.publish_calls)

    async def test_a_non_approver_click_is_refused_privately(self) -> None:
        ask = await self._ask()
        c = Click(APPROVE_ID, ask["id"], STRANGER)
        await self.gw.on_click(c.click)

        self.assertEqual(1, len(c.ephemerals))
        self.assertEqual([], c.resolved)
        self.assertEqual(0, self.publish_calls)
        self.assertNotIn("t-m0", self.state.get("approved_threads", {}))
        self.assertEqual(1, len(self.state["pending_approvals"]))  # still open for the approver

    async def test_typing_approve_grants_nothing(self) -> None:
        await self._ask()
        await self.gw.on_message(msg("m7", channel="t-m0", parent=CHANNEL, author=APPROVER,
                                     content="승인 approve yes"))
        self.assertEqual(0, self.publish_calls)
        self.assertNotIn("t-m0", self.state.get("approved_threads", {}))

    async def test_deny_grants_nothing(self) -> None:
        ask = await self._ask()
        c = Click(DENY_ID, ask["id"], APPROVER)
        await self.gw.on_click(c.click)
        self.assertEqual(0, self.publish_calls)
        self.assertNotIn("t-m0", self.state.get("approved_threads", {}))

    async def test_no_configured_approver_means_no_one_can_approve(self) -> None:
        ask = await self._ask()
        with patch.dict(os.environ, {"T_DISCORD_APPROVER": ""}):
            c = Click(APPROVE_ID, ask["id"], APPROVER)
            await self.gw.on_click(c.click)
        self.assertEqual(1, len(c.ephemerals))
        self.assertEqual(0, self.publish_calls)

    async def test_an_unknown_button_is_not_a_decision(self) -> None:
        ask = await self._ask()
        c = Click("world_something_else", ask["id"], APPROVER)
        await self.gw.on_click(c.click)
        self.assertEqual(1, len(c.ephemerals))
        self.assertNotIn("t-m0", self.state.get("approved_threads", {}))


class HarnessSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_config_selects_the_discord_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs: list[str] = []
            h = Harness("t", home=tmp, discord=DiscordConfig(bot_token_env="T_UNSET_TOKEN"))
            h.log = logs.append
            args = argparse.Namespace(once=True, dry_run=True, no_slack=False,
                                      tick_interval=60, world_map=None)
            await h._main_async(args)
        self.assertTrue(any("discord gateway disabled (tokens missing)" in m for m in logs), logs)
        self.assertTrue(any("discord=off" in m for m in logs), logs)


class ImportBoundaryTests(unittest.TestCase):
    def test_kit_imports_and_builds_a_discord_harness_without_discord_py(self) -> None:
        code = r'''
import builtins
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split(".")[0] in ("discord", "slack_sdk"):
        raise ImportError(name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
import world
import world.channels.discord
from world import DiscordConfig, Harness
h = Harness("t", discord=DiscordConfig())
print("ok")
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        run = subprocess.run([sys.executable, "-B", "-c", code], text=True,
                             capture_output=True, env=env, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual("ok", run.stdout.strip())


@unittest.skipUnless(importlib.util.find_spec("discord"), "discord.py not installed")
class DiscordPyAdapterTests(unittest.IsolatedAsyncioTestCase):
    """The translation is the trust boundary: identity from the interaction's user, the
    mention from Discord's resolved list."""

    async def test_click_identity_comes_from_the_interaction_user(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from world.discord.client import DiscordPyTransport

        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(
            message=SimpleNamespace(id=42, content="ask <@555> approve?"),
            channel_id=7, user=SimpleNamespace(id=777), response=response,
        )
        click = DiscordPyTransport._click(interaction, APPROVE_ID)
        self.assertEqual(("777", "42", "7"), (click.user_id, click.message_id, click.channel_id))
        await click.resolve("done")
        self.assertIsNone(response.edit_message.await_args.kwargs["view"])

    async def test_mention_comes_from_the_resolved_mention_list(self) -> None:
        from types import SimpleNamespace

        import discord

        from world.discord.client import DiscordPyTransport

        transport = DiscordPyTransport(
            "fake", on_ready=None, on_message=None, on_click=None, log=lambda _m: None,
        )
        transport.client._connection.user = SimpleNamespace(id=999)
        channel = SimpleNamespace(id=100)
        author = SimpleNamespace(id=888, bot=False)
        typed = SimpleNamespace(id=1, channel=channel, author=author, webhook_id=None,
                                content="<@999> hi", mentions=[])
        real = SimpleNamespace(id=2, channel=channel, author=author, webhook_id=None,
                               content="<@999> hi", mentions=[SimpleNamespace(id=999)])
        self.assertFalse(transport._inbound(typed).mentions_bot)
        self.assertTrue(transport._inbound(real).mentions_bot)
        self.assertEqual("hi", transport._inbound(real).content)
        self.assertIsNotNone(discord)
        await transport.client.close()

    async def test_mentioning_the_bots_own_role_counts_as_a_mention(self) -> None:
        # Live test: Discord auto-creates a role named after the bot on invite, and the
        # @-autocomplete offers it next to the bot. A person picked the role.
        from types import SimpleNamespace

        from world.discord.client import DiscordPyTransport

        transport = DiscordPyTransport(
            "fake", on_ready=None, on_message=None, on_click=None, log=lambda _m: None,
        )
        transport.client._connection.user = SimpleNamespace(id=999)
        channel = SimpleNamespace(id=100)
        author = SimpleNamespace(id=888, bot=False)
        own = SimpleNamespace(id=55, tags=SimpleNamespace(bot_id=999))
        other_bot = SimpleNamespace(id=66, tags=SimpleNamespace(bot_id=123))
        plain = SimpleNamespace(id=77, tags=None)

        def m(role, content):
            return SimpleNamespace(id=3, channel=channel, author=author, webhook_id=None,
                                   content=content, mentions=[], role_mentions=[role])

        inbound = transport._inbound(m(own, "<@&55> 공지 올려줘"))
        self.assertTrue(inbound.mentions_bot)
        self.assertEqual("공지 올려줘", inbound.content)
        self.assertFalse(transport._inbound(m(other_bot, "<@&66> hi")).mentions_bot)
        self.assertFalse(transport._inbound(m(plain, "<@&77> hi")).mentions_bot)
        await transport.client.close()


def _load_setup_script():
    path = Path(__file__).resolve().parents[1] / "bin" / "discord-setup.py"
    spec = importlib.util.spec_from_file_location("discord_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SetupScriptTests(unittest.TestCase):
    """bin/discord-setup.py against a fake fetch — no network, no token."""

    def setUp(self) -> None:
        self.setup = _load_setup_script()
        self.lines: list[str] = []

    def _run(self, responses: dict, channel: str | None = "100") -> int:
        def fetch(path):
            return responses.get(path, (404, {"message": "Unknown"}))
        return self.setup.run(fetch, channel, out=self.lines.append)

    def _app(self, flags: int) -> dict:
        return {"/applications/@me": (200, {"id": "42", "name": "World", "flags": flags,
                                            "bot": {"id": "43", "username": "world"}}),
                "/channels/100": (200, {"name": "bots"})}

    def test_everything_in_place_prints_the_invite_url_and_exits_0(self) -> None:
        self.assertEqual(0, self._run(self._app(1 << 19)))
        text = "\n".join(self.lines)
        self.assertIn("client_id=42&scope=bot+applications.commands&permissions=309774601216",
                      text)
        self.assertIn("can see channel #bots", text)

    def test_message_content_intent_off_is_reported(self) -> None:
        self.assertEqual(1, self._run(self._app(0)))
        self.assertIn("Message Content intent looks OFF", "\n".join(self.lines))
        self.assertEqual(0, self._run(self._app(1 << 18)))  # the 100+ servers bit counts too

    def test_a_bad_token_stops_early(self) -> None:
        self.assertEqual(1, self._run({"/applications/@me": (401, {})}))
        self.assertIn("token rejected", self.lines[0])

    def test_an_unseen_channel_is_reported(self) -> None:
        responses = self._app(1 << 19)
        responses["/channels/100"] = (403, {"message": "Missing Access"})
        self.assertEqual(1, self._run(responses))
        self.assertIn("cannot see channel 100", "\n".join(self.lines))

    def test_permissions_are_exactly_the_seven_the_gateway_uses(self) -> None:
        self.assertEqual(
            {"View Channel", "Send Messages", "Embed Links", "Read Message History",
             "Manage Webhooks", "Create Public Threads", "Send Messages in Threads"},
            set(self.setup.PERMISSIONS),
        )
        self.assertEqual(309774601216, self.setup.permission_integer())  # 309237730304 + Manage Webhooks (1 << 29)


if __name__ == "__main__":
    unittest.main()
