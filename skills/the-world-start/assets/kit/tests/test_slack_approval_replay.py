"""A button click with no open ask behind it decides nothing.

Found in the live run: the "기존 승인 카드" pointer's link preview carried the original
card's buttons after the card was answered. _on_interactive fell back to an empty entry,
so such a click wrote a fresh grant, rewrote the message it came from to 승인됨, and woke
the requester — the tool that had already run on its one approval could run again.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from world.process.ledger import mark_decisions_live
from world.slack.approvals import APPROVAL_STALE, ApprovalMixin
from world.types import ApprovalPhrases


class FakeListener(ApprovalMixin):
    def __init__(self, root: Path):
        self.state = {
            "threads": {"t": {"persona": "keeper"}},
            "pending_approvals": [{
                "msg_ts": "card", "thread_ts": "t", "requester": "keeper",
                "summary": "jot_memo", "reply_channel": "C1", "reply_thread_ts": "t", "ts": 1,
            }],
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
        self.identities = {}
        self.web = SimpleNamespace(chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
        self.posts = []
        self.notified = []
        self.resumed = []
        self.decisions = []

    def save_state(self):
        pass

    def thread_worktree(self, thread_ts):
        return None

    async def post(self, text, **kwargs):
        self.posts.append(text)

    async def notify_thread(self, thread_ts, text, *, channel=None):
        self.notified.append((thread_ts, text, channel))

    async def _record_decision(self, thread_ts, entry):
        self.decisions.append(entry)

    async def _handle_mention(self, thread_ts, text, event, identity, *, note=""):
        self.resumed.append(identity)


def _click(action_id: str, msg_ts: str = "card", value: str = "C1:t|keeper") -> dict:
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": value}],
        "user": {"id": "U1"},
        "message": {"ts": msg_ts, "thread_ts": "t",
                    "blocks": [{"type": "section"}, {"type": "actions"}]},
    }


class ApprovalReplayTests(unittest.IsolatedAsyncioTestCase):
    def listener(self, raw: str) -> FakeListener:
        listener = FakeListener(Path(raw))
        mark_decisions_live(listener.harness)
        return listener

    def assert_decided_nothing(self, listener: FakeListener) -> None:
        self.assertEqual(listener.state["approved_threads"], {})
        self.assertEqual(listener.resumed, [])
        self.assertEqual(listener.decisions, [])
        self.assertEqual(listener.notified, [])
        listener.web.chat_update.assert_not_awaited()
        # Only the clicker hears about it.
        self.assertEqual(listener.posts, [])
        listener.web.chat_postEphemeral.assert_awaited_once()
        kwargs = listener.web.chat_postEphemeral.await_args.kwargs
        self.assertEqual((kwargs["user"], kwargs["text"]), ("U1", APPROVAL_STALE))

    async def test_approve_with_no_pending_ask_grants_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            listener = self.listener(raw)
            listener.state["pending_approvals"] = []
            await listener._on_interactive(_click("world_approve"))
            self.assert_decided_nothing(listener)

    async def test_deny_with_no_pending_ask_decides_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            listener = self.listener(raw)
            listener.state["pending_approvals"] = []
            await listener._on_interactive(_click("world_deny"))
            self.assert_decided_nothing(listener)

    async def test_a_click_from_the_pointer_message_preview_grants_nothing(self):
        # The unfurl's buttons report the pointer message's ts, not the card's.
        with tempfile.TemporaryDirectory() as raw:
            listener = self.listener(raw)
            await listener._on_interactive(_click("world_approve", msg_ts="pointer"))
            self.assert_decided_nothing(listener)
            # The real card's ask is untouched and still answerable.
            self.assertEqual([p["msg_ts"] for p in listener.state["pending_approvals"]], ["card"])

    async def test_the_same_card_clicked_twice_runs_once(self):
        with tempfile.TemporaryDirectory() as raw:
            listener = self.listener(raw)
            await listener._on_interactive(_click("world_approve"))
            self.assertTrue(listener.thread_approved("t"))
            self.assertEqual(listener.resumed, ["keeper"])
            listener.consume_thread_approval("t")  # the gated tool ran

            await listener._on_interactive(_click("world_approve"))
            self.assertFalse(listener.thread_approved("t"))
            self.assertEqual(listener.resumed, ["keeper"])
            self.assertEqual(len(listener.decisions), 1)
            listener.web.chat_update.assert_awaited_once()
            listener.web.chat_postEphemeral.assert_awaited_once()

    async def test_routing_comes_from_the_ask_not_the_click_value(self):
        # A value naming another requester and a foreign thread does not redirect the
        # decision: the pending entry this process wrote does.
        with tempfile.TemporaryDirectory() as raw:
            listener = self.listener(raw)
            await listener._on_interactive(_click("world_approve", value="C9:elsewhere|scout"))
            self.assertEqual(listener.resumed, ["keeper"])
            self.assertEqual(listener.notified[0][0], "t")
            self.assertEqual(listener.notified[0][2], "C1")
            self.assertEqual(listener.decisions[0]["requester"], "keeper")


if __name__ == "__main__":
    unittest.main()
