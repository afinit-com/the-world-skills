from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.slack.approvals import ApprovalMixin
from world.types import ApprovalPhrases


class ApprovalDedupTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self):
        obj = ApprovalMixin()
        obj.web = object()
        obj.identities = {'main': {'display': 'Keeper'}}
        obj.harness = SimpleNamespace(
            main_persona=SimpleNamespace(key='main'), approval_phrases=ApprovalPhrases(),
            slack=SimpleNamespace(channel='C1', addressee='U1'))
        obj.state = {}
        obj.save_state = lambda: None
        obj.thread_worktree = lambda ts: None
        obj.post = AsyncMock()
        return obj

    async def test_same_pending_request_points_to_existing_card(self):
        obj = self.gateway()
        with patch('world.slack.approvals.slack_call', AsyncMock(return_value={'ts': '1.2'})) as call:
            await obj.post_approval_buttons('t', 'publish')
            await obj.post_approval_buttons('t', 'publish')
        self.assertEqual(1, call.await_count)
        self.assertEqual(1, len(obj.state['pending_approvals']))
        self.assertIn('/C1/p12', obj.post.call_args.args[0])

    async def test_different_summary_thread_requester_route_or_scope_gets_new_card(self):
        obj = self.gateway()
        with patch('world.slack.approvals.slack_call', AsyncMock(return_value={'ts': '1.2'})) as call, \
                patch('world.slack.approvals.approval_scope.fingerprint', return_value=('', [])) as scope:
            await obj.post_approval_buttons('t', 'publish')
            await obj.post_approval_buttons('t', 'publish another')
            await obj.post_approval_buttons('other', 'publish')
            await obj.post_approval_buttons('t', 'publish', requester='scout')
            await obj.post_approval_buttons('t', 'publish', reply_channel='C2')
            scope.return_value = ('changed', ['new.txt'])
            await obj.post_approval_buttons('t', 'publish')
        self.assertEqual(6, call.await_count)
