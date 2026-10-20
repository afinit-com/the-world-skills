from __future__ import annotations

import json
import tempfile
import time
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.process import orchestrator
from world.process.ledger import (
    LedgerFabricationError,
    append_ledger,
    mark_decisions_live,
    recent_rejections_snippet,
)
from world.process.state import load_state, prune_stale_grants
from world.slack.approvals import ApprovalMixin
from world.types import Agent, ApprovalPhrases, PollTrigger, RunResult, Signal, SlackConfig


class StateAndBackoffTests(unittest.IsolatedAsyncioTestCase):
    def test_state_upgrade_keeps_existing_and_adds_schema(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness = SimpleNamespace(
                agents=[Agent("watch", PollTrigger(lambda *_: None))],
                state_path=root / "state.json",
            )
            harness.state_path.write_text(json.dumps({"agents": {"old": {"x": 1}}, "custom": 2}))
            state = load_state(harness)
            self.assertEqual(state["agents"]["old"]["x"], 1)
            self.assertIn("watch", state["agents"])
            self.assertIn("pending_approvals", state)
            self.assertEqual(state["custom"], 2)

    def test_blocked_result_sets_retry_backoff_without_marking_done(self):
        calls = []
        harness = SimpleNamespace(log=lambda text: calls.append(text))
        state = {"recent_reports": []}
        slot = {}
        signal = Signal("work", on_done=lambda _result: calls.append("done"))
        orchestrator._apply_agent_result(
            harness,
            state,
            SimpleNamespace(name="watch"),
            slot,
            signal,
            RunResult("blocked", "missing finding"),
            100.0,
        )
        self.assertEqual(slot["blocked_until"], 100.0 + orchestrator.BLOCKED_BACKOFF_SECONDS)
        self.assertNotIn("done", calls)

    async def test_tick_enqueues_without_awaiting_a_model(self):
        """The tick's job is noticing work. It must not be the thing that blocks."""
        first = SimpleNamespace(name="first")
        second = SimpleNamespace(name="second")
        harness = SimpleNamespace(
            tick_tasks=[], system_agents=[first, second], log=lambda _text: None
        )
        state = {
            "agents": {"worktree-sweep": {"last_run": time.time()}},
            "rate_limited_until": time.time() + 999,
        }
        with (
            patch.object(orchestrator, "_run_tick_tasks", new=AsyncMock()),
            patch.object(orchestrator, "save_state"),
            patch.object(orchestrator, "_enqueue_due_agent", new=AsyncMock(return_value=False)) as enq,
        ):
            await orchestrator.run_tick(
                harness, state, None, dry_run=True, scheduler=SimpleNamespace()
            )
        # Every agent is still considered while rate limited: enqueueing costs nothing
        # and the queue is where work waits it out. Only STARTING a run is gated.
        self.assertEqual(enq.await_count, 2)

    def test_stale_unspent_grant_is_pruned(self):
        state = {"approved_threads": {"old": {"at": 1}, "new": {"at": 99}}}
        self.assertEqual(prune_stale_grants(state, now=100, ttl_s=10), 1)
        self.assertEqual(set(state["approved_threads"]), {"new"})


class LedgerTests(unittest.TestCase):
    def test_ledger_refuses_fabrication_then_records_live_decision(self):
        with tempfile.TemporaryDirectory() as raw:
            harness = SimpleNamespace(ledger_path=Path(raw) / "ledger.jsonl")
            with self.assertRaises(LedgerFabricationError):
                append_ledger(harness, {"decision": "denied"})
            mark_decisions_live(harness)
            append_ledger(harness, {"decision": "denied", "requester": "watch", "summary": "no"})
            self.assertIn("watch", recent_rejections_snippet(harness))


class FakeApprovalGateway(ApprovalMixin):
    def __init__(self, root: Path):
        self.state = {
            "threads": {"t": {"persona": "main"}},
            "pending_approvals": [
                {"thread_ts": "t", "requester": "main", "summary": "publish", "msg_ts": "m", "ts": 1}
            ],
            "approved_threads": {},
            "relay_nonces": [],
        }
        self.harness = SimpleNamespace(
            approval_phrases=ApprovalPhrases(),
            ledger_path=root / "ledger.jsonl",
            log=lambda _text: None,
        )
        self.posts = []
        self.resumed = []
        self.saved = 0

    def save_state(self):
        self.saved += 1

    async def post(self, text, **kwargs):
        self.posts.append((text, kwargs))

    async def _record_decision(self, thread_ts, entry):
        self.decision = (thread_ts, entry)

    async def _handle_mention(self, thread_ts, text, event, identity):
        self.resumed.append(identity)


class SlackApprovalContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_gateway_grants_once_and_resumes_requester(self):
        with tempfile.TemporaryDirectory() as raw:
            gateway = FakeApprovalGateway(Path(raw))
            handled = await gateway.on_approver_message("t", "approve", {"user": "U1"})
            self.assertTrue(handled)
            self.assertTrue(gateway.thread_approved("t"))
            self.assertEqual(gateway.resumed, ["main"])
            gateway.consume_thread_approval("t")
            self.assertFalse(gateway.thread_approved("t"))


class FakeHopGateway(ApprovalMixin):
    """Minimal stand-in for the listener, for the over-cap handoff release path."""

    def __init__(self, root: Path, *, parked: dict | None = None):
        meta = {"persona": "keeper"}
        if parked is not None:
            meta["pending_hop"] = parked
            meta["bot_hops"] = 4
            meta["hop_limit_notified"] = True
        self.state = {
            "threads": {"t": meta},
            "pending_approvals": [],
            "approved_threads": {},
            "relay_nonces": [],
        }
        self.harness = SimpleNamespace(
            approval_phrases=ApprovalPhrases(),
            ledger_path=root / "ledger.jsonl",
            log=lambda _text: None,
            slack=SimpleNamespace(
                channel="C1", approver="U1", co_approver=None,
                addressee="U1", is_approver=lambda who: who == "U1",
            ),
        )
        self.identities = {
            "keeper": {"display": "Obs-Keeper"},
            "watcher": {"display": "Obs-Watcher"},
        }
        self.web = SimpleNamespace(chat_update=AsyncMock(), chat_postMessage=AsyncMock())
        self.posts = []
        self.dispatched = []
        self.notes = []
        self.decisions = []

    def save_state(self):
        pass

    async def post(self, text, **kwargs):
        self.posts.append(text)

    async def _record_decision(self, thread_ts, entry):
        self.decisions.append(entry)

    async def _handle_mention(self, thread_ts, text, event, identity, *, note=""):
        self.dispatched.append((identity, text, event))
        self.notes.append(note)


def _hop_click(action_id: str) -> dict:
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": "t"}],
        "user": {"id": "U1"},
        "message": {
            "ts": "m2", "thread_ts": "t",
            "blocks": [{"type": "section"}, {"type": "actions"}],
        },
    }


PARKED = {"from": "keeper", "to": "watcher", "text": "<@W> 저장소 원인은 네가 안다", "ts": "b1", "user": "B1"}


class BotHopReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_dispatches_the_parked_handoff_and_rearms_the_ask(self):
        with tempfile.TemporaryDirectory() as raw:
            gateway = FakeHopGateway(Path(raw), parked=dict(PARKED))
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_hop_click("world_hop_allow"))

            # The handoff runs, as the bot it was addressed to, with its original text.
            self.assertEqual(
                gateway.dispatched,
                [("watcher", PARKED["text"], {"ts": "b1", "user": "B1"})],
            )
            meta = gateway.state["threads"]["t"]
            self.assertNotIn("pending_hop", meta)
            # Still over the cap, and re-armed: the next handoff asks again.
            self.assertEqual(meta["bot_hops"], 4)
            self.assertNotIn("hop_limit_notified", meta)
            # The released turn is still over the cap: it is told its next handoff asks too.
            from world.slack.rules import LAST_HOP_NOTE

            self.assertEqual(gateway.notes, [LAST_HOP_NOTE])
            entry = gateway.decisions[0]
            self.assertEqual((entry["kind"], entry["decision"]), ("bot_hop", "allowed"))
            self.assertIn("keeper → watcher", entry["summary"])

    async def test_stop_records_the_attempt_and_dispatches_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            gateway = FakeHopGateway(Path(raw), parked=dict(PARKED))
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_hop_click("world_hop_stop"))

            self.assertEqual(gateway.dispatched, [])
            self.assertNotIn("pending_hop", gateway.state["threads"]["t"])
            self.assertEqual(gateway.decisions[0]["decision"], "stopped")

    async def test_stale_button_replays_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            gateway = FakeHopGateway(Path(raw))  # a human already spoke: nothing parked
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_hop_click("world_hop_allow"))

            self.assertEqual(gateway.dispatched, [])
            self.assertEqual(gateway.decisions, [])

    async def test_release_does_not_open_a_write_grant(self):
        with tempfile.TemporaryDirectory() as raw:
            gateway = FakeHopGateway(Path(raw), parked=dict(PARKED))
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_hop_click("world_hop_allow"))

            # "let the other bot pick this up" is not permission to write.
            self.assertFalse(gateway.thread_approved("t"))


class NoApproverGateTests(unittest.IsolatedAsyncioTestCase):
    """Slack connected, approver left empty: the gate fails closed."""

    def _gateway(self, root: Path) -> FakeHopGateway:
        gateway = FakeHopGateway(root, parked=dict(PARKED))
        # The real SlackConfig, so is_approver is the shipped check, not a stand-in.
        gateway.harness.slack = SlackConfig(approver_env="T_UNSET_APPROVER")
        return gateway

    async def test_a_click_is_refused_and_the_operator_is_told_what_to_set(self):
        with tempfile.TemporaryDirectory() as raw, \
                patch.dict("os.environ", {"T_UNSET_APPROVER": "", "SLACK_CHANNEL": "C1"}):
            gateway = self._gateway(Path(raw))
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_hop_click("world_hop_allow"))

            self.assertEqual(gateway.dispatched, [])
            self.assertEqual(gateway.decisions, [])
            self.assertIn("pending_hop", gateway.state["threads"]["t"])
            self.assertEqual(len(gateway.posts), 1)
            self.assertIn("T_UNSET_APPROVER", gateway.posts[0])

    async def test_a_configured_approver_still_decides(self):
        with tempfile.TemporaryDirectory() as raw, \
                patch.dict("os.environ", {"T_UNSET_APPROVER": "U1", "SLACK_CHANNEL": "C1"}):
            gateway = self._gateway(Path(raw))
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_hop_click("world_hop_allow"))

            self.assertEqual(len(gateway.dispatched), 1)


class FakePromptBuilder:
    """Duck-typed MentionMixin for _build_prompt, called unbound."""

    def __init__(self):
        self.state = {"pending_approvals": []}
        self.harness = SimpleNamespace(world_text=lambda: "WORLD")

    def _rules_for(self, identity, persona):
        return "RULES"


class BuildPromptNoteTests(unittest.TestCase):
    def test_a_note_lands_right_before_the_incoming_message(self):
        from world.slack.mentions import MentionMixin

        prompt = MentionMixin._build_prompt(
            FakePromptBuilder(), "watcher", None, "<@W> 네가 안다", "", note="NOTE"
        )
        self.assertTrue(prompt.endswith("NOTE\n\n방금 온 메시지:\n<@W> 네가 안다"))

    def test_no_note_adds_nothing(self):
        from world.slack.mentions import MentionMixin

        prompt = MentionMixin._build_prompt(FakePromptBuilder(), "watcher", None, "hi", "")
        self.assertEqual(prompt, "RULES\n\nWORLD\n\n방금 온 메시지:\nhi")


class FakeHopDispatcher:
    """Duck-typed listener for SlackListener._dispatch_bot_hop, called unbound."""

    def __init__(self, *, hops: int, addressee: str | None = "U1"):
        meta = {"persona": "keeper"}
        if hops:
            meta["bot_hops"] = hops
        self.state = {"threads": {"t": meta}}
        self.harness = SimpleNamespace(
            approval_phrases=ApprovalPhrases(),
            slack=SimpleNamespace(addressee=addressee),
        )
        self.asked = []
        self.posts = []
        self.dispatched = []
        self.notes = []

    def save_state(self):
        pass

    async def post_hop_buttons(self, thread_ts, from_identity, to_identity, summary, n):
        self.asked.append((thread_ts, from_identity, to_identity, summary, n))

    async def post(self, text, **kwargs):
        self.posts.append(text)

    async def _handle_mention(self, thread_ts, text, event, identity, *, note=""):
        self.dispatched.append(identity)
        self.notes.append(note)


@unittest.skipUnless(find_spec("slack_sdk"), "world.slack.listener imports slack_sdk at runtime")
class BotHopDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def _hop(self, fake):
        from world.slack.listener import SlackListener

        await SlackListener._dispatch_bot_hop(
            fake, "t", "<@U0WATCHER> 네가 안다", {"ts": "b1", "user": "B1"}, "keeper", ["watcher"],
        )

    @property
    def cap(self) -> int:
        from world.slack.listener import MAX_BOT_HOPS

        return MAX_BOT_HOPS

    async def test_under_the_cap_the_handoff_just_runs(self):
        fake = FakeHopDispatcher(hops=self.cap - 1)
        await self._hop(fake)
        self.assertEqual(fake.dispatched, ["watcher"])
        self.assertEqual(fake.state["threads"]["t"]["bot_hops"], self.cap)
        self.assertEqual(fake.asked, [])

    async def test_the_last_hop_under_the_cap_is_told_it_is_the_last(self):
        # The turn that spends the final hop is the only one that can still act on it:
        # past this, its next @-mention of another bot is held, and it will not hear so.
        from world.slack.rules import LAST_HOP_NOTE

        fake = FakeHopDispatcher(hops=self.cap - 1)
        await self._hop(fake)
        self.assertEqual(fake.notes, [LAST_HOP_NOTE])
        self.assertEqual(len(LAST_HOP_NOTE.splitlines()), 1)
        # It sets expectations, it does not discourage the handoff: the over-cap ask is
        # the harness's job, and it only fires if the bot still hands off.
        self.assertIn("hand_off로 넘기세요", LAST_HOP_NOTE)
        self.assertIn("버튼", LAST_HOP_NOTE)

    async def test_with_no_addressee_the_last_hop_is_told_it_stops(self):
        # Nobody to ask: past the cap the thread only gets the stop notice, so the note
        # must not promise a button.
        from world.slack.rules import LAST_HOP_NOTE_UNATTENDED

        fake = FakeHopDispatcher(hops=self.cap - 1, addressee=None)
        await self._hop(fake)
        self.assertEqual(fake.notes, [LAST_HOP_NOTE_UNATTENDED])
        self.assertEqual(len(LAST_HOP_NOTE_UNATTENDED.splitlines()), 1)
        self.assertNotIn("버튼", LAST_HOP_NOTE_UNATTENDED)

    async def test_earlier_hops_get_no_note_and_no_budget_number(self):
        # The rejected alternative was a remaining-budget count on every turn. Nothing.
        for hops in range(self.cap - 1):
            fake = FakeHopDispatcher(hops=hops)
            await self._hop(fake)
            self.assertEqual(fake.notes, [""], hops)

    async def test_over_the_cap_it_parks_and_asks_instead_of_dispatching(self):
        fake = FakeHopDispatcher(hops=self.cap)
        await self._hop(fake)
        self.assertEqual(fake.dispatched, [])
        self.assertEqual(len(fake.asked), 1)
        parked = fake.state["threads"]["t"]["pending_hop"]
        self.assertEqual((parked["from"], parked["to"], parked["ts"]), ("keeper", "watcher", "b1"))
        # The parked text keeps the mention (it is what gets replayed); the quoted summary
        # must not, or the ask echoes back through this same path as another handoff.
        self.assertIn("<@U0WATCHER>", parked["text"])
        self.assertNotIn("<@", fake.asked[0][3])

    async def test_asking_happens_once_per_thread(self):
        fake = FakeHopDispatcher(hops=self.cap)
        await self._hop(fake)
        await self._hop(fake)
        self.assertEqual(len(fake.asked), 1)

    async def test_with_no_addressee_it_falls_back_to_the_stop_notice(self):
        fake = FakeHopDispatcher(hops=self.cap, addressee=None)
        await self._hop(fake)
        self.assertEqual(fake.asked, [])
        self.assertEqual(fake.dispatched, [])
        self.assertIn("사람", fake.posts[0])
        self.assertNotIn("pending_hop", fake.state["threads"]["t"])


class ForgedPersonaFake:
    """Duck-typed listener for SlackListener._dispatch_human, called unbound."""

    def __init__(self):
        self.state = {"threads": {}}
        self.harness = SimpleNamespace(slack=SimpleNamespace(
            escalate_to=None, respond_to_channel_messages=False))
        self.dispatched = []

    def _mark_handled(self, ts):
        pass

    async def _handle_mention(self, thread_ts, text, event, identity, *, note=""):
        self.dispatched.append(identity)


@unittest.skipUnless(find_spec("slack_sdk"), "world.slack.listener imports slack_sdk at runtime")
class ForgedPersonaRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Whose turn it is comes from the harness, never from the text. With one
    app, a sub-persona has no Slack user, so nothing a person types can address it."""

    async def test_naming_a_sub_persona_in_the_text_still_reaches_the_main_persona(self):
        from world.slack.listener import MENTION_RE, SlackListener

        user_to_identity = {"U0LUMEN": "lumen"}  # one app: only the main bot is a user
        for text in ("<@U0LUMEN> @Scout 이거 해", "<@U0LUMEN> Scout: 이거 해",
                     "<@U0LUMEN> <@scout> 이거 해"):
            mentioned = [user_to_identity[u] for u in MENTION_RE.findall(text)
                         if u in user_to_identity]
            fake = ForgedPersonaFake()
            await SlackListener._dispatch_human(
                fake, "t", text, {"ts": "1"}, "app_mention", "1", "U0HUMAN", mentioned,
            )
            with self.subTest(text=text):
                self.assertEqual(["lumen"], fake.dispatched)

    async def test_the_main_bot_naming_a_sub_persona_hands_nothing_off(self):
        from world.slack.listener import SlackListener

        fake = FakeHopDispatcher(hops=0)
        # the main bot's own message: no other identity is a Slack user to target
        await SlackListener._dispatch_bot_hop(
            fake, "t", "@Scout 이거 해 / Scout: 이거 해", {"ts": "b1"}, "lumen", [],
        )
        self.assertEqual([], fake.dispatched)
