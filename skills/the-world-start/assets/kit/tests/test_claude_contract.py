from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from world.tools import tool
from world.types import Agent, PollTrigger

KIT = Path(__file__).resolve().parents[1]


@dataclass
class FakeOptions:
    cwd: str = ""
    model: str = ""
    permission_mode: str = ""
    setting_sources: list = field(default_factory=list)
    strict_mcp_config: bool = False
    env: dict = field(default_factory=dict)
    mcp_servers: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    allowed_tools: list = field(default_factory=list)
    disallowed_tools: list = field(default_factory=list)
    plugins: list | None = None


def install_fake_sdk() -> None:
    module = types.ModuleType("claude_agent_sdk")
    module.AssistantMessage = type("AssistantMessage", (), {})
    module.ResultMessage = type("ResultMessage", (), {})
    module.ToolUseBlock = type("ToolUseBlock", (), {})
    module.ClaudeAgentOptions = FakeOptions
    module.create_sdk_mcp_server = lambda **kwargs: kwargs
    module.query = lambda **_kwargs: None

    def fake_tool(name, description, schema):
        def decorate(handler):
            return SimpleNamespace(
                name=name, description=description, input_schema=schema, handler=handler
            )
        return decorate

    module.tool = fake_tool
    sys.modules["claude_agent_sdk"] = module


class ClaudeAdapterContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        install_fake_sdk()
        from world.runners import claude

        cls.adapter = claude

    def harness(self):
        return SimpleNamespace(
            name="h", git_root=Path("/repo"), model="sonnet", plugins=[], log=lambda _: None
        )

    def test_default_surface_is_empty_and_strict(self):
        agent = Agent("watch", PollTrigger(lambda *_: None))
        options = self.adapter._build_options(self.harness(), agent)
        self.assertEqual(options.tools, [])
        self.assertTrue(options.strict_mcp_config)
        self.assertIn("Bash", options.disallowed_tools)

    def test_toolspec_converts_to_in_process_mcp_tool(self):
        @tool("probe", "Probe", {"value": str})
        async def probe(_args):
            return {"content": [], "is_error": False}

        agent = Agent("watch", PollTrigger(lambda *_: None), tools=[probe])
        options = self.adapter._build_options(self.harness(), agent)
        self.assertEqual(options.allowed_tools, ["mcp__h__probe"])
        self.assertEqual(options.mcp_servers["h"]["tools"][0].name, "probe")

    # templates/skill-wrapper-tool.md.tmpl tells the tool word what choosing a skill
    # costs. These pin each runtime fact it states: the poll agent's options here, the
    # mention turn's Agent in MentionTurnContractTests, the log lines in
    # PluginLogContractTests.
    def plugin_surface(self, *, configs, supported=True, **agent_kwargs):
        from world.process import plugins

        harness = self.harness()
        harness.plugins = [SimpleNamespace(name="mp", enabled=("p",))]
        agent = Agent("watch", PollTrigger(lambda *_: None), **agent_kwargs)
        with patch.object(plugins, "sdk_supports_plugins", return_value=supported), \
                patch.object(plugins, "plugin_configs", return_value=configs):
            return self.adapter._build_options(harness, agent)

    def test_skill_needs_allow_plugins_and_a_resolved_plugin(self):
        resolved = [{"type": "local", "path": "/mp/p"}]
        options = self.plugin_surface(configs=resolved, allow_plugins=True)
        self.assertIn("Skill", options.tools)
        self.assertNotIn("Skill", options.disallowed_tools)
        self.assertEqual(options.plugins, resolved)
        for closed in (
            self.plugin_surface(configs=resolved),  # no allow_plugins
            self.plugin_surface(configs=[], allow_plugins=True),  # nothing resolved
            self.plugin_surface(configs=resolved, supported=False, allow_plugins=True),
        ):
            self.assertNotIn("Skill", closed.tools)
            self.assertIn("Skill", closed.disallowed_tools)
            self.assertIsNone(closed.plugins)

    def test_skill_never_brings_read_but_a_bash_agent_keeps_bash(self):
        resolved = [{"type": "local", "path": "/mp/p"}]
        options = self.plugin_surface(configs=resolved, allow_plugins=True)
        self.assertNotIn("Read", options.tools)
        self.assertNotIn("Bash", options.tools)
        options = self.plugin_surface(configs=resolved, allow_plugins=True, allow_bash=True)
        self.assertIn("Bash", options.tools)
        self.assertIn("Skill", options.tools)

    def test_an_sdk_without_plugins_logs_that_the_setting_was_ignored(self):
        logged = []
        harness = self.harness()
        harness.log = logged.append
        harness.plugins = [SimpleNamespace(name="mp", enabled=("p",))]
        from world.process import plugins

        agent = Agent("watch", PollTrigger(lambda *_: None), allow_plugins=True)
        with patch.object(plugins, "sdk_supports_plugins", return_value=False):
            self.adapter._build_options(harness, agent)
        self.assertTrue(any("ignored" in line for line in logged), logged)

    async def test_claude_auth_policy_is_adapter_local(self):
        runner = self.adapter.ClaudeRunner(self.harness())
        agent = Agent("watch", PollTrigger(lambda *_: None))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-used"}):
            result = await runner.run(agent, "hello")
        self.assertEqual(result.status, "error")
        self.assertIn("refuses ANTHROPIC_API_KEY", result.result_text)


def mention_agent_calls(rel: str) -> list[ast.Call]:
    """The `Agent(name="mention", ...)` calls a channel builds a mention turn with, read
    from source so this runs without slack_sdk or discord.py."""
    tree = ast.parse((KIT / rel).read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Agent"
        and any(k.arg == "name" and getattr(k.value, "value", None) == "mention"
                for k in node.keywords)
    ]


class MentionTurnContractTests(unittest.TestCase):
    """Review of PR #6: the template says a mention turn has no `Bash` and opts into
    plugins with `Harness.mention_plugins` (on by default) for every persona at once.
    Nothing held the channels to it."""

    def test_every_channels_mention_turn_has_no_bash_and_harness_wide_plugins(self):
        for rel in ("world/slack/mentions.py", "world/discord/gateway.py"):
            calls = mention_agent_calls(rel)
            self.assertTrue(calls, rel)
            for node in calls:
                kwargs = {k.arg: ast.unparse(k.value) for k in node.keywords}
                self.assertEqual("False", kwargs.get("allow_bash"), rel)
                # Read from the harness, never from the persona: one switch for all.
                self.assertEqual("self.harness.mention_plugins and bool(self.harness.plugins)",
                                 kwargs.get("allow_plugins"), rel)

    def test_mention_plugins_is_on_by_default(self):
        from world.harness import Harness

        default = inspect.signature(Harness.__init__).parameters["mention_plugins"].default
        self.assertIs(True, default)


class PluginLogContractTests(unittest.IsolatedAsyncioTestCase):
    """The template's log claims: an unsynced marketplace, a name missing from its
    marketplace.json and a missing source directory are logged by name; an empty
    `enabled` list shows only as `0/0 enabled plugin(s) resolved`."""

    def setUp(self):
        from world.process import plugins

        self.plugins = plugins
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name)
        cache = patch.object(plugins, "cache_dir", return_value=self.dest)
        cache.start()
        self.addCleanup(cache.stop)
        self.logged = []
        self.harness = SimpleNamespace(log=self.logged.append)

    def write_manifest(self, plugins):
        (self.dest / ".claude-plugin").mkdir()
        (self.dest / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"plugins": plugins}), encoding="utf-8")

    def test_what_does_not_resolve_is_logged_by_name(self):
        mp = SimpleNamespace(name="team-mp", enabled=("ghost", "gone"))
        self.assertEqual([], self.plugins.resolve_enabled(self.harness, mp))
        self.assertIn("team-mp", self.logged[-1])
        self.write_manifest([{"name": "gone", "source": "plugins/gone"}])
        self.assertEqual([], self.plugins.resolve_enabled(self.harness, mp))
        self.assertTrue(any("'ghost'" in line for line in self.logged), self.logged)
        self.assertTrue(any("'gone'" in line and "missing" in line for line in self.logged),
                        self.logged)

    async def test_an_empty_enabled_list_shows_only_in_the_sync_summary(self):
        self.write_manifest([])
        self.harness.plugins = [SimpleNamespace(name="team-mp", enabled=())]
        with patch.object(self.plugins, "sdk_supports_plugins", return_value=True), \
                patch.object(self.plugins, "sync_marketplace", return_value="abc1234"):
            await self.plugins.sync_all(self.harness)
        self.assertEqual(["plugin: team-mp @ abc1234 — 0/0 enabled plugin(s) resolved"],
                         self.logged)
