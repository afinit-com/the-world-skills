"""Codex usage in the run footer: cached input is not counted twice, and an unreported
dollar figure says so instead of showing $0."""

import unittest

from world.runners.codex import normalize_usage
from world.slack.run_footer import format_run_footer, run_fields
from world.types import RunResult


class CodexUsageFooterTests(unittest.TestCase):
    def test_cached_codex_input_is_not_counted_twice(self):
        # Codex's input_tokens already includes the cached part.
        fields = normalize_usage({"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 9})
        self.assertEqual(fields["input_tokens"], 20)
        self.assertEqual(fields["cache_read_tokens"], 80)
        footer = format_run_footer(run_fields(RunResult(status="ok", provider="codex", **fields)))
        self.assertIn("⬆ 100 ⬇ 9", footer)
        self.assertNotIn("⬆ 180", footer)

    def test_codex_tokens_and_unreported_dollars_are_visible(self):
        run = RunResult(status="ok", provider="codex", input_tokens=20,
                        cache_read_tokens=80, output_tokens=9)
        footer = format_run_footer(run_fields(run))
        self.assertIn("80 cached (80%)", footer)
        self.assertIn("USD 미제공", footer)
        self.assertNotIn("$0", footer)

    def test_deterministic_post_does_not_claim_measured_usage(self):
        self.assertEqual(format_run_footer(None), "")
        self.assertEqual(format_run_footer(run_fields(RunResult(status="skipped"))), "")


if __name__ == "__main__":
    unittest.main()
