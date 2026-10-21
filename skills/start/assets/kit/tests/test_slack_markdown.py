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

    def test_balanced_parentheses_stay_inside_the_url(self):
        self.assertEqual('<https://en.wikipedia.org/wiki/Foo_(bar)|wiki> 참고',
                         slack_mrkdwn('[wiki](https://en.wikipedia.org/wiki/Foo_(bar)) 참고'))
        self.assertEqual('(<https://example.com/a|a>)',
                         slack_mrkdwn('([a](https://example.com/a))'))


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


class MarkdownBulletTests(unittest.TestCase):
    """`-`/`*`/`+` sent through chat.postMessage stay a literal dash; only Slack's
    composer turns them into bullets. MESSAGE_FORMAT asks for bullets, so the layout
    rule would have produced dashes."""

    def test_markers_become_bullets_and_nested_ones_indent(self):
        source = "결과\n- 배포 완료\n* 모델 v1\n+ 대기 중\n  - 하위 항목\n\t- 탭 하위\n    - 더 깊이"
        self.assertEqual(
            "결과\n• 배포 완료\n• 모델 v1\n• 대기 중\n    ◦ 하위 항목\n        ◦ 탭 하위\n"
            "        ◦ 더 깊이",
            slack_mrkdwn(source))

    def test_bold_rules_and_numbers_are_not_bullets(self):
        self.assertEqual("*굵게* 시작\n*정말 굵게*\n---\n1. 하나\n-붙은 대시\n-",
                         slack_mrkdwn("*굵게* 시작\n**정말 굵게**\n---\n1. 하나\n-붙은 대시\n-"))
        self.assertEqual("- - -\n* * *\n  - - -", slack_mrkdwn("- - -\n* * *\n  - - -"))

    def test_code_keeps_its_dashes(self):
        code = "`- inline`\n```\n- in a fence\n* also\n```"
        self.assertEqual(code, slack_mrkdwn(code))
        self.assertEqual("• `- a`", slack_mrkdwn("- `- a`"))

    def test_bullets_are_idempotent_and_bold_inside_still_converts(self):
        once = slack_mrkdwn("- **중요** 항목\n  - 하위")
        self.assertEqual("• *중요* 항목\n    ◦ 하위", once)
        self.assertEqual(once, slack_mrkdwn(once))
