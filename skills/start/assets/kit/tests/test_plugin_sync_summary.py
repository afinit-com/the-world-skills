"""sync_all's return value, and the optional post of it to a log channel."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from world.harness import Harness
from world.process import plugins
from world.types import PluginMarketplace, SlackConfig

GIT_URL = "https://deploy:s3cret@git.example/team/registry.git"


def _marketplace(**kwargs) -> PluginMarketplace:
    return PluginMarketplace(name="team-mp", git_url=GIT_URL, **kwargs)


class SyncAllSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.logged: list[str] = []
        self.harness = SimpleNamespace(home=self.home, log=self.logged.append, plugins=[])
        supported = patch.object(plugins, "sdk_supports_plugins", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)

    def write_clone(self, mp: PluginMarketplace, names: list[str]) -> None:
        dest = plugins.cache_dir(self.harness, mp)
        (dest / ".claude-plugin").mkdir(parents=True)
        (dest / ".git").mkdir()
        for name in names:
            (dest / "plugins" / name).mkdir(parents=True)
        (dest / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "plugins": [{"name": n, "source": f"plugins/{n}"} for n in names]}), encoding="utf-8")

    async def test_no_marketplaces_is_an_empty_list(self) -> None:
        self.assertEqual([], await plugins.sync_all(self.harness))

    async def test_a_clean_sync_reports_sha_and_counts(self) -> None:
        mp = _marketplace(enabled=("notes", "deploy"))
        self.harness.plugins = [mp]
        self.write_clone(mp, ["notes", "deploy", "unused"])
        with patch.object(plugins, "sync_marketplace", new=AsyncMock(return_value="abc1234")):
            (summary,) = await plugins.sync_all(self.harness)

        self.assertEqual(
            plugins.SyncSummary("team-mp", "abc1234", resolved=2, enabled=2), summary)
        self.assertTrue(summary.ok)
        self.assertEqual("team-mp @ abc1234 — 2/2 enabled plugin(s) resolved", summary.line())
        # The log line is the one it always was.
        self.assertEqual([f"plugin: {summary.line()}"], self.logged)

    async def test_a_name_that_does_not_resolve_is_an_error_on_the_summary(self) -> None:
        mp = _marketplace(enabled=("notes", "ghost"))
        self.harness.plugins = [mp]
        self.write_clone(mp, ["notes"])
        with patch.object(plugins, "sync_marketplace", new=AsyncMock(return_value="abc1234")):
            (summary,) = await plugins.sync_all(self.harness)

        self.assertEqual((1, 2), (summary.resolved, summary.enabled))
        self.assertFalse(summary.ok)
        self.assertEqual(1, len(summary.errors))
        self.assertIn("'ghost'", summary.errors[0])

    async def test_a_failed_fetch_keeps_the_error_without_the_urls_credentials(self) -> None:
        mp = _marketplace(enabled=("notes",))
        self.harness.plugins = [mp]
        self.write_clone(mp, ["notes"])     # the last good clone stays usable
        failure = AsyncMock(return_value=(1, f"fatal: unable to access '{GIT_URL}'"))
        with patch.object(plugins, "run_cmd", new=failure):
            (summary,) = await plugins.sync_all(self.harness)

        self.assertIsNone(summary.sha)
        self.assertFalse(summary.ok)
        self.assertEqual(1, summary.resolved)
        self.assertIn("fetch/reset failed", summary.errors[0])
        self.assertNotIn("s3cret", " ".join(summary.errors))
        self.assertIn("://[redacted]@git.example", summary.errors[0])
        self.assertEqual("team-mp @ ?? — 1/1 enabled plugin(s) resolved", summary.line())

    async def test_one_summary_per_marketplace_in_order(self) -> None:
        first = PluginMarketplace(name="first", git_url=GIT_URL)
        second = PluginMarketplace(name="second", git_url=GIT_URL)
        self.harness.plugins = [first, second]
        for mp in (first, second):
            self.write_clone(mp, [])
        with patch.object(plugins, "sync_marketplace", new=AsyncMock(side_effect=["aaa", "bbb"])):
            summaries = await plugins.sync_all(self.harness)
        self.assertEqual([("first", "aaa"), ("second", "bbb")],
                         [(s.marketplace, s.sha) for s in summaries])


SUMMARIES = [
    plugins.SyncSummary("team-mp", "abc1234", resolved=2, enabled=2),
    plugins.SyncSummary("other-mp", None, resolved=0, enabled=1,
                        errors=("plugin sync: clone failed for other-mp: timeout",)),
]


class SyncSummaryPostTests(unittest.IsolatedAsyncioTestCase):
    def _harness(self, **slack) -> Harness:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        harness = Harness("t", home=tmp.name, slack=SlackConfig(**slack),
                          plugins=[_marketplace()])
        self.logged: list[str] = []
        harness.log = self.logged.append
        return harness

    async def test_it_is_off_by_default(self) -> None:
        harness = self._harness()
        listener = SimpleNamespace(enabled=True, post=AsyncMock())
        with patch.dict("os.environ", {"T_SYNC_LOG": "C0XXXXXXX"}):
            await harness._post_sync_summary(listener, SUMMARIES, when="startup")
        listener.post.assert_not_awaited()

    async def test_a_named_but_unset_variable_posts_nothing(self) -> None:
        harness = self._harness(sync_log_channel_env="T_SYNC_LOG")
        listener = SimpleNamespace(enabled=True, post=AsyncMock())
        with patch.dict("os.environ", {"T_SYNC_LOG": ""}):
            await harness._post_sync_summary(listener, SUMMARIES, when="startup")
        listener.post.assert_not_awaited()

    async def test_a_configured_channel_gets_one_message_with_every_marketplace(self) -> None:
        harness = self._harness(sync_log_channel_env="T_SYNC_LOG")
        listener = SimpleNamespace(enabled=True, post=AsyncMock())
        with patch.dict("os.environ", {"T_SYNC_LOG": "C0XXXXXXX"}):
            await harness._post_sync_summary(listener, SUMMARIES, when="startup")

        listener.post.assert_awaited_once()
        (text,), kwargs = listener.post.await_args
        self.assertEqual({"channel": "C0XXXXXXX"}, kwargs)
        self.assertEqual([
            "🔄 plugin sync (startup)",
            "• team-mp @ abc1234 — 2/2 enabled plugin(s) resolved",
            "• ⚠️ other-mp @ ?? — 0/1 enabled plugin(s) resolved",
            "    plugin sync: clone failed for other-mp: timeout",
        ], text.splitlines())

    async def test_without_a_live_listener_or_summaries_nothing_is_posted(self) -> None:
        harness = self._harness(sync_log_channel_env="T_SYNC_LOG")
        off = SimpleNamespace(enabled=False, post=AsyncMock())
        on = SimpleNamespace(enabled=True, post=AsyncMock())
        with patch.dict("os.environ", {"T_SYNC_LOG": "C0XXXXXXX"}):
            await harness._post_sync_summary(None, SUMMARIES, when="refresh")
            await harness._post_sync_summary(off, SUMMARIES, when="refresh")
            await harness._post_sync_summary(on, [], when="refresh")
        off.post.assert_not_awaited()
        on.post.assert_not_awaited()

    async def test_a_failed_post_is_logged_and_does_not_raise(self) -> None:
        harness = self._harness(sync_log_channel_env="T_SYNC_LOG")
        listener = SimpleNamespace(
            enabled=True, post=AsyncMock(side_effect=RuntimeError("not_in_channel")))
        with patch.dict("os.environ", {"T_SYNC_LOG": "C0XXXXXXX"}):
            await harness._post_sync_summary(listener, SUMMARIES, when="refresh")
        self.assertEqual(1, len(self.logged))
        self.assertIn("not posted to C0XXXXXXX", self.logged[0])
        self.assertIn("not_in_channel", self.logged[0])

    async def test_the_refresh_tick_posts_what_the_sync_returned(self) -> None:
        harness = self._harness(sync_log_channel_env="T_SYNC_LOG")
        harness._register_plugin_refresh()
        (tick,) = [t for t in harness.tick_tasks if t.name == "plugin_refresh"]
        listener = SimpleNamespace(enabled=True, post=AsyncMock())
        with patch.dict("os.environ", {"T_SYNC_LOG": "C0XXXXXXX"}), \
                patch.object(plugins, "sync_all", new=AsyncMock(return_value=SUMMARIES[:1])):
            await tick.fn({}, listener)
        (text,), _ = listener.post.await_args
        self.assertTrue(text.startswith("🔄 plugin sync (refresh)"), text)


if __name__ == "__main__":
    unittest.main()
