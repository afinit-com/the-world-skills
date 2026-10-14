from __future__ import annotations

import os
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from world.tools import tool
from world.types import Agent, PollTrigger


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
    # costs. These pin each runtime fact it states to the options the runner builds.
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

    async def test_claude_auth_policy_is_adapter_local(self):
        runner = self.adapter.ClaudeRunner(self.harness())
        agent = Agent("watch", PollTrigger(lambda *_: None))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-used"}):
            result = await runner.run(agent, "hello")
        self.assertEqual(result.status, "error")
        self.assertIn("refuses ANTHROPIC_API_KEY", result.result_text)
