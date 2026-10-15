from __future__ import annotations

import unittest

from world.slack.rules import slack_mrkdwn


class MarkdownLinkTests(unittest.TestCase):
    def test_links_become_slack_links_and_stay_idempotent(self):
        for source in ('[관측소 안내](https://example.com/obs)',
                       '[관측소 안내](<https://example.com/obs>)'):
            rendered = slack_mrkdwn(source)
            self.assertEqual('<https://example.com/obs|관측소 안내>', rendered)
            self.assertEqual(rendered, slack_mrkdwn(rendered))

    def test_code_is_untouched_and_link_labels_cannot_break_out(self):
        code = '`[label](https://example.com)`\n```\n[label](https://example.com)\n```'
        self.assertEqual(code, slack_mrkdwn(code))
        self.assertEqual('<https://example.com?a=1&amp;b=2|a｜b &gt; &lt; &amp;>',
                         slack_mrkdwn('[a|b > < &](https://example.com?a=1&b=2)'))

    def test_unclosed_code_blocks_are_also_protected(self):
        code = '```python\n[label](https://example.com)'
        self.assertEqual(code, slack_mrkdwn(code))

    def test_code_in_a_link_label_cannot_restore_slack_delimiters(self):
        self.assertEqual('<https://example.com|see `a｜b &lt;c&gt; &amp;`>',
                         slack_mrkdwn('[see `a|b <c> &`](https://example.com)'))

    def test_query_parameters_that_look_like_entities_keep_their_address(self):
        # html.unescape read `&not`, `&para`, `&reg`, `&copy` without a semicolon, so a
        # link went to `?x=1¬ify=true` instead of where the model pointed it.
        for query in ('x=1&notify=true', 'x=1&param=2', 'x=1&region=kr&copy=1'):
            with self.subTest(query=query):
                escaped = query.replace('&', '&amp;')
                rendered = slack_mrkdwn(f'[docs &notes](https://example.com/a?{query})')
                self.assertEqual(f'<https://example.com/a?{escaped}|docs &amp;notes>', rendered)
                self.assertEqual(rendered, slack_mrkdwn(rendered))

    def test_already_escaped_entities_are_not_escaped_twice(self):
        self.assertEqual('<https://example.com?a=1&amp;b=2|a &amp; b &lt;c&gt;>',
                         slack_mrkdwn('[a &amp; b &lt;c&gt;](https://example.com?a=1&amp;b=2)'))


class RelayReplyMarkdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_relay_answer_links_to_the_address_the_model_wrote(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from world.slack.mentions import MentionMixin

        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(
            main_persona=SimpleNamespace(key='lumen'), world_text=lambda: '',
            mention_tools=[], slack=SimpleNamespace(addressee=None))
        mixin.web = SimpleNamespace(chat_getPermalink=AsyncMock(return_value={}))
        mixin._thread_transcript = AsyncMock(return_value='')
        mixin._rules_for = lambda identity, persona: ''
        mixin._mention_tools_for = lambda identity, addressee: []
        mixin._run_mention_turn = AsyncMock(return_value=SimpleNamespace(
            failed=False, approval_summary=None,
            reply='[알림 설정](https://example.com/a?x=1&notify=true)'))
        mixin.post = AsyncMock(return_value='')
        mixin.state = {'threads': {}}
        await mixin._handle_relay_mention('ORIGIN', '1.0', 'hi', {'ts': '1.0'})
        body = mixin.post.await_args.args[0]
        self.assertTrue(body.endswith('<https://example.com/a?x=1&amp;notify=true|알림 설정>'), body)
