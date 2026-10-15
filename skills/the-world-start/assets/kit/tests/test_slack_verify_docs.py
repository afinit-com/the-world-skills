from pathlib import Path
import unittest


class SlackVerificationDocsTests(unittest.TestCase):
    def test_browser_verification_addresses_late_redraw_and_secret_cleanup(self):
        text = (Path(__file__).resolve().parents[1] / 'docs/verify.md').read_text()
        self.assertIn('redraw', text)
        self.assertIn('discard', text)
        self.assertIn('delete the as-human input messages', text)

    def test_a_stale_approval_button_is_described_as_changing_nothing(self):
        text = (Path(__file__).resolve().parents[1] / 'docs/verify.md').read_text()
        self.assertIn('no preview under it', text)
        self.assertIn('changes nothing', text)
