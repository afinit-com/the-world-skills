"""SlackConfig.guest_channels_env and guest_bots_env: a mention in a guest channel is
answered in its own thread, a guest bot is heard under the driver's rules, and neither
changes who can approve."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.slack.listener import MAX_BOT_HOPS, SlackListener
from world.types import ApprovalPhrases, Persona, SlackConfig

HOME, GUEST, RELAY, OTHER = "C0HOME000", "C0GUEST00", "C0RELAY00", "C0OTHER00"
ENV = {
    "T_CHANNEL": HOME,
    "T_APPROVER": "U0AAAAAAA",
    "T_DRIVER": "U0DRIVERB",
    "T_RELAYS": RELAY,
    "T_GUESTS": f"{GUEST}, {GUEST}",
    "T_GUEST_BOTS": "U0HELPBOT",
}
OURS = "U0OURBOT0"
PERSON = "U0PERSON0"


def _config(**overrides) -> SlackConfig:
    fields = {"channel_env": "T_CHANNEL", "approver_env": "T_APPROVER",
              "driver_env": "T_DRIVER", "relay_channels_env": "T_RELAYS",
              "guest_channels_env": "T_GUESTS", "guest_bots_env": "T_GUEST_BOTS"}
    fields.update(overrides)
    return SlackConfig(**fields)


def _listener(**config) -> SlackListener:
    obj = object.__new__(SlackListener)
    obj.harness = SimpleNamespace(
        main_persona=SimpleNamespace(key="main"), slack=_config(**config),
        approval_phrases=ApprovalPhrases(), log=lambda _t: None)
    obj.state = {"threads": {}}
    obj._seen_msg_ts = {}
    obj._guest_ts = {}
    obj.bot_user_id = OURS
    obj.user_to_identity = {OURS: "main"}
    obj.save_state = lambda: None
    obj.post = AsyncMock()
    obj._handle_mention = AsyncMock()
    obj._handle_relay_mention = AsyncMock()
    obj._escalate_mention = AsyncMock()
    obj.on_approver_message = AsyncMock(return_value=False)
    return obj


class GuestConfigTests(unittest.TestCase):
    def test_off_unless_named_and_set(self) -> None:
        with patch.dict("os.environ", ENV):
            self.assertEqual(((), ()), (SlackConfig().guest_channels, SlackConfig().guest_bots))
            self.assertEqual((GUEST,), _config().guest_channels)
            self.assertEqual(("U0HELPBOT",), _config().guest_bots)
            self.assertEqual(("U0DRIVERB", "U0HELPBOT"), _config().trusted_bots)
        with patch.dict("os.environ", {**ENV, "T_GUESTS": "", "T_GUEST_BOTS": " , "}):
            self.assertEqual(((), ()), (_config().guest_channels, _config().guest_bots))

    def test_a_guest_bot_is_never_an_approver(self) -> None:
        listed_everywhere = {**ENV, "T_APPROVER": "U0HELPBOT", "T_MORE": "U0HELPBOT,U0BBBBBBB",
                             "T_CO": "U0HELPBOT"}
        with patch.dict("os.environ", listed_everywhere):
            slack = _config(approvers_env="T_MORE", co_approver_env="T_CO")
            self.assertFalse(slack.is_approver("U0HELPBOT"))
            self.assertTrue(slack.is_approver("U0BBBBBBB"))


class GuestRoutingTests(unittest.IsolatedAsyncioTestCase):
    """_on_request with the shipped SlackConfig; _handle_mention is a mock here."""

    def setUp(self) -> None:
        env = patch.dict("os.environ", ENV)
        env.start()
        self.addCleanup(env.stop)

    async def deliver(self, obj, **changes) -> None:
        # A guest bot's post as Slack sends it: `message`, bot_id set, never app_mention.
        event = {"type": "message", "channel": GUEST, "user": "U0HELPBOT",
                 "bot_id": "B0HELPBOT", "text": f"<@{OURS}> please run it", "ts": "100.1"}
        event.update(changes)
        event = {k: v for k, v in event.items() if v is not None}
        await obj._on_request(
            SimpleNamespace(send_socket_mode_response=AsyncMock()),
            SimpleNamespace(type="events_api", envelope_id="e", payload={"event": event}))

    async def person(self, obj, **changes) -> None:
        await self.deliver(obj, **{"user": PERSON, "bot_id": None, **changes})

    async def test_a_guest_bots_mention_reaches_the_mention_turn_as_a_bot_hop(self) -> None:
        obj = _listener()
        await self.deliver(obj, thread_ts="90.0")
        obj._handle_mention.assert_awaited_once()
        thread_ts, _text, event, identity = obj._handle_mention.call_args.args
        self.assertEqual(("90.0", "main", GUEST), (thread_ts, identity, event["channel"]))
        self.assertEqual({"bot_hop": True}, obj._handle_mention.call_args.kwargs)
        obj._handle_relay_mention.assert_not_awaited()

    async def test_the_mention_is_remembered_as_being_in_the_guest_channel(self) -> None:
        obj = _listener()
        await self.deliver(obj, thread_ts="90.0")
        self.assertEqual(GUEST, obj.channel_of("100.1"))   # the message, for its reactions
        self.assertEqual(HOME, obj.channel_of("90.0"))     # the thread: once _handle_mention records it
        self.assertEqual(HOME, obj.channel_of(None))

    async def test_a_persons_mention_is_answered_whichever_delivery_comes_first(self) -> None:
        for etype in ("message", "app_mention"):
            with self.subTest(first=etype):
                obj = _listener()
                await self.person(obj, type=etype)
                await self.person(obj, type="app_mention" if etype == "message" else "message")
                obj._handle_mention.assert_awaited_once()
                self.assertEqual({}, obj._handle_mention.call_args.kwargs)  # not a bot hop

    async def test_every_message_needs_the_mention_even_in_a_thread_of_ours(self) -> None:
        obj = _listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "channel": GUEST}
        await self.person(obj, text="and the other repo?", thread_ts="90.0")
        await self.deliver(obj, text="status?", thread_ts="90.0", ts="100.2")
        await self.person(obj, text="hello channel", ts="100.3")
        obj._handle_mention.assert_not_awaited()

    async def test_a_person_speaking_in_our_thread_resets_the_hop_budget(self) -> None:
        obj = _listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "channel": GUEST,
                                        "bot_hops": MAX_BOT_HOPS, "hop_limit_notified": True}
        await self.deliver(obj, thread_ts="90.0")
        obj._handle_mention.assert_not_awaited()          # the budget is spent
        await self.person(obj, text="go on", thread_ts="90.0", ts="100.2")
        await self.deliver(obj, thread_ts="90.0", ts="100.3")
        obj._handle_mention.assert_awaited_once()

    async def test_past_the_hop_budget_the_thread_is_told_once(self) -> None:
        obj = _listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "channel": GUEST,
                                        "bot_hops": MAX_BOT_HOPS}
        await self.deliver(obj, thread_ts="90.0")
        await self.deliver(obj, thread_ts="90.0", ts="100.2")
        obj._handle_mention.assert_not_awaited()
        obj.post.assert_awaited_once()
        self.assertEqual("90.0", obj.post.call_args.kwargs["thread_ts"])

    async def test_a_bot_that_is_not_listed_is_ignored(self) -> None:
        obj = _listener()
        await self.deliver(obj, user="U0STRANGE", bot_id="B0STRANGE")
        obj._handle_mention.assert_not_awaited()

    async def test_our_own_posts_in_a_guest_channel_are_not_routed(self) -> None:
        obj = _listener()
        obj.user_to_identity["U0SCOUT00"] = "scout"
        obj.state["threads"]["90.0"] = {"persona": "main", "channel": GUEST}
        await self.deliver(obj, user=OURS, bot_id="B0OURS", text="<@U0SCOUT00> yours",
                           thread_ts="90.0")
        obj._handle_mention.assert_not_awaited()

    async def test_a_guest_bot_is_heard_in_the_home_channel_too(self) -> None:
        obj = _listener()
        await self.deliver(obj, channel=HOME)
        obj._handle_mention.assert_awaited_once()
        self.assertEqual({"bot_hop": True}, obj._handle_mention.call_args.kwargs)

    async def test_the_driver_is_heard_in_a_guest_channel(self) -> None:
        obj = _listener()
        await self.deliver(obj, user="U0DRIVERB", bot_id="B0DRIVERB")
        obj._handle_mention.assert_awaited_once()

    async def test_channels_that_are_not_listed_stay_closed(self) -> None:
        obj = _listener()
        await self.deliver(obj, channel=OTHER)
        await self.person(obj, channel=OTHER, type="app_mention", ts="100.2")
        await self.deliver(obj, channel=RELAY, ts="100.3")   # a bot's post is no app_mention
        obj._handle_mention.assert_not_awaited()
        obj._handle_relay_mention.assert_not_awaited()

    async def test_without_the_settings_nothing_changes(self) -> None:
        obj = _listener(guest_channels_env=None, guest_bots_env=None)
        await self.deliver(obj)
        await self.deliver(obj, channel=HOME, ts="100.2")
        await self.person(obj, type="app_mention", ts="100.3")
        obj._handle_mention.assert_not_awaited()

    async def test_a_guest_channel_wins_over_a_relay_listing(self) -> None:
        with patch.dict("os.environ", {"T_RELAYS": GUEST}):
            obj = _listener()
            await self.person(obj, type="app_mention")
        obj._handle_mention.assert_awaited_once()
        obj._handle_relay_mention.assert_not_awaited()

    async def test_an_approvers_typed_decision_counts_only_in_a_thread_of_ours(self) -> None:
        obj = _listener()
        obj.on_approver_message = AsyncMock(return_value=True)
        await self.person(obj, user="U0AAAAAAA", text="approve", thread_ts="80.0")
        obj.on_approver_message.assert_not_awaited()       # not a thread we answered in
        obj.state["threads"]["90.0"] = {"persona": "main", "channel": GUEST}
        await self.person(obj, user="U0AAAAAAA", text="approve", thread_ts="90.0", ts="100.2")
        obj.on_approver_message.assert_awaited_once()
        obj._handle_mention.assert_not_awaited()

    async def test_a_guest_bot_typing_a_decision_is_only_a_request(self) -> None:
        obj = _listener()
        obj.state["threads"]["90.0"] = {"persona": "main", "channel": GUEST}
        await self.deliver(obj, text=f"<@{OURS}> approve", thread_ts="90.0")
        obj.on_approver_message.assert_not_awaited()
        obj._handle_mention.assert_awaited_once()          # an ordinary turn; the gate still holds


class GuestThreadTests(unittest.IsolatedAsyncioTestCase):
    """Where a guest thread's posts go, with a recording Slack client."""

    def setUp(self) -> None:
        env = patch.dict("os.environ", ENV)
        env.start()
        self.addCleanup(env.stop)

    def listener(self) -> SlackListener:
        obj = _listener()
        obj.state["threads"] = {"90.0": {"persona": "main", "channel": GUEST},
                                "70.0": {"persona": "main"}}
        obj.state["pending_approvals"] = []
        del obj.post                                       # the real PostingMixin.post
        obj.web = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "200.1"}),
            chat_update=AsyncMock(return_value={}),
            reactions_add=AsyncMock(return_value={}),
            reactions_remove=AsyncMock(return_value={}),
            conversations_replies=AsyncMock(return_value={"messages": []}),
        )
        obj.identities = {"main": {"display": "Main", "web": obj.web, "user_id": OURS}}
        obj.harness.personas = {"main": Persona(key="main", display="Main")}
        obj.harness.home = None
        obj.thread_worktree = lambda _ts: None
        return obj

    async def test_a_reply_goes_to_the_threads_own_channel(self) -> None:
        obj = self.listener()
        await obj.post("in the guest thread", thread_ts="90.0")
        await obj.post("in a home thread", thread_ts="70.0")
        await obj.post("top level")
        channels = [c.kwargs["channel"] for c in obj.web.chat_postMessage.await_args_list]
        self.assertEqual([GUEST, HOME, HOME], channels)

    async def test_the_transcript_is_read_from_the_threads_own_channel(self) -> None:
        obj = self.listener()
        await obj._thread_transcript("90.0")
        self.assertEqual(GUEST, obj.web.conversations_replies.call_args.kwargs["channel"])

    async def test_the_approval_card_is_posted_in_the_guest_thread(self) -> None:
        obj = self.listener()
        await obj.post_approval_buttons("90.0", "open a pull request", requester="main")
        card = obj.web.chat_postMessage.call_args.kwargs
        self.assertEqual((GUEST, "90.0"), (card["channel"], card["thread_ts"]))
        self.assertIn("<@U0AAAAAAA>", card["text"])        # the human approver is the one pinged
        pending = obj.state["pending_approvals"][0]
        self.assertEqual((GUEST, "90.0"), (pending["reply_channel"], pending["reply_thread_ts"]))

    async def test_a_click_by_a_guest_bot_or_a_bystander_grants_nothing(self) -> None:
        for clicker in ("U0HELPBOT", PERSON):
            with self.subTest(clicker=clicker):
                obj = self.listener()
                await obj.post_approval_buttons("90.0", "open a pull request", requester="main")
                obj._handle_mention = AsyncMock()
                await obj._on_interactive({
                    "type": "block_actions", "user": {"id": clicker},
                    "actions": [{"action_id": "world_approve", "value": f"{GUEST}:90.0|main"}],
                    "message": {"ts": "200.1", "thread_ts": "90.0", "blocks": []}})
                self.assertNotIn("90.0", obj.state.get("approved_threads", {}))
                self.assertEqual(1, len(obj.state["pending_approvals"]))
                obj._handle_mention.assert_not_awaited()
                refusal = obj.web.chat_postMessage.call_args.kwargs
                self.assertEqual((GUEST, "90.0"), (refusal["channel"], refusal["thread_ts"]))

    async def test_an_approvers_click_grants_and_resumes_the_guest_thread(self) -> None:
        obj = self.listener()
        await obj.post_approval_buttons("90.0", "open a pull request", requester="main")
        obj._handle_mention = AsyncMock()
        obj._record_decision = AsyncMock()
        await obj._on_interactive({
            "type": "block_actions", "user": {"id": "U0AAAAAAA"},
            "actions": [{"action_id": "world_approve", "value": f"{GUEST}:90.0|main"}],
            "message": {"ts": "200.1", "thread_ts": "90.0", "blocks": []}})
        self.assertEqual("U0AAAAAAA", obj.state["approved_threads"]["90.0"]["by"])
        self.assertEqual(GUEST, obj.web.chat_update.call_args.kwargs["channel"])
        obj._handle_mention.assert_awaited_once()          # the waiting tool gets its turn
        self.assertEqual("90.0", obj._handle_mention.call_args.args[0])

    async def test_a_resumed_turn_reacts_in_the_guest_channel(self) -> None:
        # A turn resumed by a button or a typed approval carries a message the listener
        # never routed. Its reactions go to the thread's channel all the same.
        obj = self.listener()
        del obj._handle_mention                            # the real mention turn
        obj._set_thread_status = AsyncMock()
        obj.harness.status_phrases_for = lambda _identity: SimpleNamespace(queued="queued")
        obj._thread_transcript = AsyncMock(side_effect=RuntimeError("stop after the reaction"))
        with self.assertRaises(RuntimeError):
            await obj._handle_mention("90.0", "go on", {"ts": "300.1", "user": "U0AAAAAAA"})
        self.assertEqual(GUEST, obj.web.reactions_add.call_args.kwargs["channel"])

    async def test_a_hand_off_to_the_face_is_posted_in_the_guest_thread(self) -> None:
        # escalate_human_mentions: the mention is inside somebody else's thread, so the
        # thread ts is one the listener has not seen as a message.
        with patch.dict("os.environ", {"T_CO": "U0FACEBOT"}):
            obj = self.listener()
            obj.harness.slack = _config(co_approver_env="T_CO", escalate_human_mentions=True)
            del obj._escalate_mention                      # the real hand-off
            await obj._escalate_mention(
                "80.0", f"<@{OURS}> what is the status?",
                {"ts": "100.9", "thread_ts": "80.0", "channel": GUEST, "user": PERSON})
        sent = obj.web.chat_postMessage.call_args.kwargs
        self.assertEqual((GUEST, "80.0"), (sent["channel"], sent["thread_ts"]))
        self.assertEqual(GUEST, obj.state["threads"]["80.0"]["channel"])
        self.assertEqual(GUEST, obj.web.reactions_add.call_args.kwargs["channel"])


if __name__ == "__main__":
    unittest.main()
