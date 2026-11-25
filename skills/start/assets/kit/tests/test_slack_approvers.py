"""SlackConfig.approvers_env: several human approvers, one addressee."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_state_and_approval import PARKED, FakeHopGateway, _hop_click
from world.process.ledger import mark_decisions_live
from world.types import ApprovalPhrases, SlackConfig

ENV = {
    "T_APPROVER": "U0AAAAAAA",
    "T_APPROVERS": "U0BBBBBBB, U0CCCCCCC,,U0AAAAAAA",
    "T_CO": "U0RELAYBOT",
    "SLACK_CHANNEL": "C0XXXXXXX",
}


def _config(**overrides) -> SlackConfig:
    return SlackConfig(approver_env="T_APPROVER", approvers_env="T_APPROVERS", **overrides)


class ApproverListTests(unittest.TestCase):
    def test_the_list_is_the_primary_then_the_extras_without_duplicates(self):
        with patch.dict("os.environ", ENV):
            self.assertEqual(("U0AAAAAAA", "U0BBBBBBB", "U0CCCCCCC"), _config().approvers)

    def test_everyone_in_the_list_is_an_approver_and_nobody_else(self):
        with patch.dict("os.environ", ENV):
            slack = _config()
            for who in ("U0AAAAAAA", "U0BBBBBBB", "U0CCCCCCC"):
                self.assertTrue(slack.is_approver(who), who)
            self.assertFalse(slack.is_approver("U0STRANGER"))
            self.assertFalse(slack.is_approver(""))

    def test_a_single_approver_config_is_unchanged(self):
        with patch.dict("os.environ", ENV):
            slack = SlackConfig(approver_env="T_APPROVER")
            self.assertEqual(("U0AAAAAAA",), slack.approvers)
            self.assertTrue(slack.is_approver("U0AAAAAAA"))
            self.assertFalse(slack.is_approver("U0BBBBBBB"))

    def test_an_unset_list_variable_adds_nobody(self):
        with patch.dict("os.environ", {"T_APPROVER": "U0AAAAAAA", "T_APPROVERS": ""}):
            self.assertEqual(("U0AAAAAAA",), _config().approvers)

    def test_asks_still_go_to_one_addressee(self):
        with patch.dict("os.environ", ENV):
            self.assertEqual("U0AAAAAAA", _config().addressee)
            self.assertEqual("U0RELAYBOT", _config(co_approver_env="T_CO").addressee)

    def test_the_co_approver_still_counts_and_is_not_listed_as_a_human(self):
        with patch.dict("os.environ", ENV):
            slack = _config(co_approver_env="T_CO")
            self.assertTrue(slack.is_approver("U0RELAYBOT"))
            self.assertNotIn("U0RELAYBOT", slack.approvers)


def _click_by(who: str) -> dict:
    return {**_hop_click("world_hop_allow"), "user": {"id": who}}


class ApproverListGateTests(unittest.IsolatedAsyncioTestCase):
    """The button gate, with the shipped SlackConfig rather than a stand-in."""

    def _gateway(self, root: Path, **overrides) -> FakeHopGateway:
        gateway = FakeHopGateway(root, parked=dict(PARKED))
        gateway.harness.slack = _config(**overrides)
        mark_decisions_live(gateway.harness)
        return gateway

    async def test_an_extra_approvers_click_decides(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict("os.environ", ENV):
            gateway = self._gateway(Path(raw))
            await gateway._on_interactive(_click_by("U0CCCCCCC"))
            self.assertEqual(1, len(gateway.dispatched))
            self.assertEqual([], gateway.posts)

    async def test_a_strangers_click_is_refused_without_paging_the_list(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict("os.environ", ENV):
            gateway = self._gateway(Path(raw), co_approver_env="T_CO")
            await gateway._on_interactive(_click_by("U0STRANGER"))

            self.assertEqual([], gateway.dispatched)
            self.assertIn("pending_hop", gateway.state["threads"]["t"])
            self.assertEqual(1, len(gateway.posts))
            notice = gateway.posts[0]
            # Named as before: the clicker, the primary approver, the co_approver.
            self.assertIn("<@U0STRANGER>", notice)
            self.assertIn("<@U0AAAAAAA>", notice)
            self.assertIn("<@U0RELAYBOT>", notice)
            # Counted, never mentioned: everyone the list added.
            self.assertNotIn("U0BBBBBBB", notice)
            self.assertNotIn("U0CCCCCCC", notice)
            self.assertIn(ApprovalPhrases().more_approvers.format(n=2), notice)

    async def test_a_list_with_no_primary_approver_is_counted_not_named(self):
        env = {**ENV, "T_APPROVER": ""}
        with tempfile.TemporaryDirectory() as raw, patch.dict("os.environ", env):
            gateway = self._gateway(Path(raw))
            await gateway._on_interactive(_click_by("U0STRANGER"))

            notice = gateway.posts[0]
            self.assertIn(ApprovalPhrases().approver_count.format(n=3), notice)
            self.assertNotIn("U0BBBBBBB", notice)
            # Not the "no approver configured" message: three people can still click.
            self.assertNotIn("T_APPROVER", notice)

    async def test_without_a_list_the_notice_reads_as_before(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict("os.environ", ENV):
            gateway = FakeHopGateway(Path(raw), parked=dict(PARKED))
            gateway.harness.slack = SlackConfig(approver_env="T_APPROVER")
            mark_decisions_live(gateway.harness)
            await gateway._on_interactive(_click_by("U0STRANGER"))
            self.assertEqual(
                ApprovalPhrases().not_approver.format(who="U0STRANGER", valid="<@U0AAAAAAA>"),
                gateway.posts[0],
            )


if __name__ == "__main__":
    unittest.main()
