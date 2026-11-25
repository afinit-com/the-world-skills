"""SlackConfig.driver_env: one trusted bot whose @-mention is answered like a person's,
that spends the bot-hop budget, and that can never approve."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.slack.listener import MAX_BOT_HOPS, SlackListener
from world.types import ApprovalPhrases, Persona, SlackConfig, StatusPhrases

ENV = {
    "T_CHANNEL": "C0XXXXXXX",
    "T_APPROVER": "U0AAAAAAA",
    "T_DRIVER": "U0DRIVERB",
    "T_RELAYS": "C0YYYYYYY",
}
OURS = "U0OURBOT0"
SCOUT = "U0SCOUT00"


def _config(**overrides) -> SlackConfig:
    fields = {"channel_env": "T_CHANNEL", "approver_env": "T_APPROVER",
              "driver_env": "T_DRIVER", "relay_channels_env": "T_RELAYS"}
    fields.update(overrides)
    return SlackConfig(**fields)


class DriverConfigTests(unittest.TestCase):
    def test_off_unless_named_and_set(self) -> None:
        with patch.dict("os.environ", ENV):
            self.assertIsNone(SlackConfig().driver)
            self.assertEqual("U0DRIVERB", _config().driver)
        with patch.dict("os.environ", {**ENV, "T_DRIVER": ""}):
            self.assertIsNone(_config().driver)

    def test_the_driver_is_never_an_approver(self) -> None:
        with patch.dict("os.environ", ENV):
            slack = _config()
            self.assertFalse(slack.is_approver("U0DRIVERB"))
            self.assertTrue(slack.is_approver("U0AAAAAAA"))

    def test_not_even_when_its_id_is_listed_as_one(self) -> None:
        listed_everywhere = {**ENV, "T_APPROVER": "U0DRIVERB", "T_MORE": "U0DRIVERB,U0BBBBBBB",
                             "T_CO": "U0DRIVERB"}
        with patch.dict("os.environ", listed_everywhere):
            slack = _config(approvers_env="T_MORE", co_approver_env="T_CO")
            self.assertFalse(slack.is_approver("U0DRIVERB"))
            self.assertTrue(slack.is_approver("U0BBBBBBB"))


class DriverRoutingTests(unittest.IsolatedAsyncioTestCase):
    """_on_request with the shipped SlackConfig; _handle_mention is a mock here."""

    def setUp(self) -> None:
        env = patch.dict("os.environ", ENV)
        env.start()
        self.addCleanup(env.stop)

    def listener(self, **config) -> SlackListener:
        obj = object.__new__(SlackListener)
        obj.harness = SimpleNamespace(
            main_persona=SimpleNamespace(key="main"), slack=_config(**config),
            approval_phrases=ApprovalPhrases(), log=lambda _t: None)
        obj.state = {"threads": {}}
        obj._seen_msg_ts = {}
        obj.bot_user_id = OURS
        obj.user_to_identity = {OURS: "main", SCOUT: "scout"}
        obj.save_state = lambda: None
        obj.post = AsyncMock()
        obj._handle_mention = AsyncMock()
        obj._handle_relay_mention = AsyncMock()
        obj._escalate_mention = AsyncMock()
        obj.on_approver_message = AsyncMock(return_value=False)
        return obj

    async def deliver(self, obj, **changes) -> None:
        # What Slack sends for a bot's post: `message`, bot_id set, never app_mention.
        event = {"type": "message", "channel": "C0XXXXXXX", "user": "U0DRIVERB",
                 "bot_id": "B0DRIVERB", "text": f"<@{OURS}> status?", "ts": "100.1"}
        event.update(changes)
        await obj._on_request(
            SimpleNamespace(send_socket_mode_response=AsyncMock()),
            SimpleNamespace(type="events_api", envelope_id="e", payload={"event": event}))

    async def test_a_mention_from_the_driver_reaches_the_mention_turn(self) -> None:
        obj = self.listener()
        await self.deliver(obj)
        obj._handle_mention.assert_awaited_once()
        thread_ts, text, event, identity = obj._handle_mention.call_args.args
        self.assertEqual(("100.1", f"<@{OURS}> status?", "main"), (thread_ts, text, identity))
        self.assertEqual("U0DRIVERB", event["user"])
        self.assertEqual({"bot_hop": True}, obj._handle_mention.call_args.kwargs)

    async def test_it_is_addressed_to_the_persona_it_mentions(self) -> None:
        obj = self.listener()
        await self.deliver(obj, text=f"<@{SCOUT}> look into this")
        self.assertEqual("scout", obj._handle_mention.call_args.args[3])

    async def test_without_our_id_in_the_text_it_is_not_for_us(self) -> None:
        obj = self.listener()
        obj.state["threads"]["90.0"] = {"persona": "main"}
        for changes in ({"text": "status?"},                          # no mention at all
                        {"text": "<@U0SOMEONE> status?"},             # someone else's id
                        {"text": "status?", "thread_ts": "90.0"},     # a thread we track
                        {"text": "status?", "type": "app_mention"}):  # the event type proves nothing
            with self.subTest(changes=changes):
                await self.deliver(obj, **changes)
                obj._handle_mention.assert_not_awaited()

    async def test_one_delivery_is_answered_once(self) -> None:
        obj = self.listener()
        await self.deliver(obj)
        await self.deliver(obj)       # Slack's at-least-once retry
        obj._handle_mention.assert_awaited_once()

    async def test_another_bot_is_still_ignored(self) -> None:
        obj = self.listener()
        await self.deliver(obj, user="U0STRANGE", bot_id="B0STRANGE")
        obj._handle_mention.assert_not_awaited()

    async def test_with_no_driver_configured_the_same_bot_is_ignored(self) -> None:
        obj = self.listener(driver_env=None)
        await self.deliver(obj)
        obj._handle_mention.assert_not_awaited()

    async def test_home_channel_only(self) -> None:
        obj = self.listener()
        # A relay channel wakes on app_mention alone, whoever wrote it, and a bot's post
        # never arrives as one. So the driver's message there is not routed at all.
        await self.deliver(obj, channel="C0YYYYYYY")
        await self.deliver(obj, channel="C0ZZZZZZZ", ts="100.3")      # some other channel
        obj._handle_mention.assert_not_awaited()
        obj._handle_relay_mention.assert_not_awaited()

    async def test_its_approval_words_are_just_a_message(self) -> None:
        obj = self.listener()
        obj.state["threads"]["90.0"] = {"persona": "main"}
        await self.deliver(obj, text=f"<@{OURS}> 승인", thread_ts="90.0")
        obj.on_approver_message.assert_not_awaited()      # never offered as a decision
        obj._handle_mention.assert_awaited_once()         # routed as an ordinary ask

    async def test_it_does_not_reset_the_hop_budget_as_a_person_would(self) -> None:
        obj = self.listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "bot_hops": 2,
                                         "pending_hop": {"to": "scout"}}
        await self.deliver(obj, thread_ts="90.0")
        self.assertEqual(2, obj.state["threads"]["90.0"]["bot_hops"])   # the turn adds its own
        self.assertIn("pending_hop", obj.state["threads"]["90.0"])

    async def test_at_the_cap_it_gets_the_stop_notice_once_and_no_turn(self) -> None:
        obj = self.listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "bot_hops": MAX_BOT_HOPS}
        await self.deliver(obj, thread_ts="90.0", ts="100.1")
        await self.deliver(obj, thread_ts="90.0", ts="100.2")

        obj._handle_mention.assert_not_awaited()
        obj.post.assert_awaited_once()
        (notice,), kwargs = obj.post.call_args
        self.assertEqual(
            ApprovalPhrases().hop_limit.format(n=MAX_BOT_HOPS, who="<@U0AAAAAAA>"), notice)
        self.assertEqual({"thread_ts": "90.0"}, kwargs)

    async def test_a_person_speaking_lets_the_driver_in_again(self) -> None:
        obj = self.listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "bot_hops": MAX_BOT_HOPS,
                                         "hop_limit_notified": True}
        await self.deliver(obj, user="U0PERSON0", bot_id=None, type="app_mention",
                           text=f"<@{OURS}> go on", thread_ts="90.0", ts="100.5")
        obj._handle_mention.reset_mock()
        await self.deliver(obj, thread_ts="90.0", ts="100.6")
        obj._handle_mention.assert_awaited_once()

    async def test_it_is_answered_here_even_when_people_are_handed_to_the_face(self) -> None:
        with patch.dict("os.environ", {"T_CO": "U0FACEBOT"}):
            obj = self.listener(co_approver_env="T_CO", escalate_human_mentions=True)
            await self.deliver(obj)
            obj._escalate_mention.assert_not_awaited()
            obj._handle_mention.assert_awaited_once()

    async def test_an_id_without_slacks_bot_flag_still_gets_the_drivers_rules(self) -> None:
        obj = self.listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "bot_hops": 3}
        await self.deliver(obj, bot_id=None, type="app_mention", thread_ts="90.0")
        self.assertEqual(3, obj.state["threads"]["90.0"]["bot_hops"])   # not reset
        self.assertEqual({"bot_hop": True}, obj._handle_mention.call_args.kwargs)


class DriverHopBudgetTests(unittest.IsolatedAsyncioTestCase):
    """The real _handle_mention, with only Slack and the model stubbed: every driver
    turn spends one hop, and the fifth in a thread is refused."""

    def listener(self) -> SlackListener:
        obj = object.__new__(SlackListener)
        main = Persona(key="main", display="Main")
        obj.harness = SimpleNamespace(
            main_persona=main, personas={"main": main}, slack=_config(),
            approval_phrases=ApprovalPhrases(), mention_tools=[], pre_approved_tools=[],
            status_phrases_for=lambda _identity: StatusPhrases(),
            world_text=lambda: "WORLD", log=lambda _t: None)
        obj.state = {"threads": {}, "pending_approvals": []}
        obj._seen_msg_ts = {}
        obj.bot_user_id = OURS
        obj.user_to_identity = {OURS: "main"}
        obj.identities = {"main": {"display": "Main", "web": None, "user_id": OURS}}
        obj.web = None
        obj.save_state = lambda: None
        obj.post = AsyncMock()
        obj._react = AsyncMock()
        obj._set_thread_status = AsyncMock()
        obj._thread_transcript = AsyncMock(return_value="")
        obj._answer = AsyncMock()
        obj._drain_handoffs = AsyncMock()
        obj.on_approver_message = AsyncMock(return_value=False)
        return obj

    async def deliver(self, obj, ts: str, **changes) -> None:
        event = {"type": "message", "channel": "C0XXXXXXX", "user": "U0DRIVERB",
                 "bot_id": "B0DRIVERB", "text": f"<@{OURS}> again", "ts": ts,
                 "thread_ts": "100.0"}
        event.update(changes)
        await obj._on_request(
            SimpleNamespace(send_socket_mode_response=AsyncMock()),
            SimpleNamespace(type="events_api", envelope_id="e", payload={"event": event}))

    async def test_the_driver_cannot_keep_a_thread_going_past_the_cap(self) -> None:
        with patch.dict("os.environ", ENV):
            obj = self.listener()
            for n in range(MAX_BOT_HOPS + 3):
                await self.deliver(obj, ts=f"100.{n + 1}")

            self.assertEqual(MAX_BOT_HOPS, obj._answer.await_count)
            self.assertEqual(MAX_BOT_HOPS, obj.state["threads"]["100.0"]["bot_hops"])
            obj.post.assert_awaited_once()                 # the stop notice, once

            # A person resets it, as for any bot-to-bot exchange.
            await self.deliver(obj, ts="100.90", user="U0PERSON0", bot_id=None,
                               type="app_mention", text=f"<@{OURS}> carry on")
            await self.deliver(obj, ts="100.91")
            self.assertEqual(MAX_BOT_HOPS + 2, obj._answer.await_count)
            self.assertEqual(1, obj.state["threads"]["100.0"]["bot_hops"])

    async def test_the_turn_it_gets_is_the_ordinary_one(self) -> None:
        with patch.dict("os.environ", ENV):
            obj = self.listener()
            await self.deliver(obj, ts="100.1")
        thread_ts, identity, persona, prompt, tools, msg_ts, _phrases = obj._answer.call_args.args
        self.assertEqual(("100.0", "main", "100.1"), (thread_ts, identity, msg_ts))
        self.assertIn(f"<@{OURS}> again", prompt)
        # No approver-only or write tool appears because a bot asked.
        self.assertEqual(["mention_approver"], [getattr(t, "name", "") for t in tools])


if __name__ == "__main__":
    unittest.main()
