"""Which posts ask Slack for no link preview, and why only those.

Found live: the "기존 승인 카드" pointer unfurled into a copy of the card, buttons and
all, that Slack never redrew after the card was answered; and a task card's Wikipedia
link put a preview bigger than the card under it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.slack.approvals import ApprovalMixin
from world.slack.mentions import MentionMixin
from world.slack.post import PostingMixin
from world.types import ApprovalPhrases, Persona, RunResult

NO_PREVIEW = {"unfurl_links": False, "unfurl_media": False}


class PostUnfurlTests(unittest.IsolatedAsyncioTestCase):
    def poster(self):
        poster = PostingMixin()
        main = Persona(key="main", display="Main")
        poster.harness = SimpleNamespace(
            main_persona=main, personas={"main": main}, log=lambda _t: None,
            slack=SimpleNamespace(channel="C1"),
        )
        poster.identities = {}
        poster.web = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ts": "1.1"}))
        return poster

    async def test_unfurl_false_asks_for_no_preview_on_every_chunk(self):
        poster = self.poster()
        await poster.post("x" * 5000, unfurl=False)
        calls = poster.web.chat_postMessage.await_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertLessEqual(NO_PREVIEW.items(), call.kwargs.items())

    async def test_a_plain_reply_leaves_previews_to_slack(self):
        poster = self.poster()
        await poster.post("https://example.com")
        kwargs = poster.web.chat_postMessage.await_args.kwargs
        self.assertNotIn("unfurl_links", kwargs)
        self.assertNotIn("unfurl_media", kwargs)


class ApprovalPointerUnfurlTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_pointer_to_an_open_card_has_no_preview(self):
        obj = ApprovalMixin()
        obj.web = object()
        obj.identities = {"main": {"display": "Keeper"}}
        obj.harness = SimpleNamespace(
            main_persona=SimpleNamespace(key="main"), approval_phrases=ApprovalPhrases(),
            slack=SimpleNamespace(channel="C1", addressee="U1"))
        obj.state = {}
        obj.save_state = lambda: None
        obj.thread_worktree = lambda ts: None
        obj.post = AsyncMock()
        with patch("world.slack.approvals.slack_call", AsyncMock(return_value={"ts": "1.2"})):
            await obj.post_approval_buttons("t", "publish")
            await obj.post_approval_buttons("t", "publish")
        self.assertIn("기존 승인 카드", obj.post.await_args.args[0])
        self.assertIs(obj.post.await_args.kwargs.get("unfurl"), False)


class TaskCardUnfurlTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_task_card_has_no_preview(self):
        from world.process import task as tasks

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            record = tasks.create(home, type="watch", title="링크 확인", agent="watch")
            mixin = MentionMixin()
            mixin.harness = SimpleNamespace(
                home=home, agents=[], run_footer=False, knowledge_proposal_agent=None,
            )
            mixin.state = {"threads": {}}
            mixin.save_state = lambda: None
            mixin.post = AsyncMock(return_value="1.3")
            run = RunResult(status="ok",
                            result_text="[위키](https://en.wikipedia.org/wiki/Python)")
            await mixin.announce_task(record, run, SimpleNamespace(key="main"))
            self.assertIs(mixin.post.await_args.kwargs.get("unfurl"), False)


if __name__ == "__main__":
    unittest.main()
