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
