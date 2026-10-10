from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from world.runners.codex import CodexEvents, CodexRunner, build_command
from world.tools import ToolBridge, tool
from world.tools.mcp_stdio import dispatch
from world.types import Agent, PollTrigger


def agent(**kwargs):
    return Agent("watch", PollTrigger(lambda *_: None), **kwargs)


class CodexContractTests(unittest.TestCase):
    def setUp(self):
        self.harness = SimpleNamespace(
            git_root=Path("/repo").resolve(), home=Path("/repo/harness").resolve()
        )

    def test_command_clears_inherited_mcp_and_disables_broad_tools(self):
        command = build_command(self.harness, agent(), None, executable="codex")
        joined = " ".join(command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("agents.enabled=false", command)
        self.assertIn("features.plugins=false", command)
        self.assertIn("--sandbox read-only", joined)
        self.assertIn("--ignore-user-config", command)

    def test_explicit_capabilities_are_opt_in_but_plugins_stay_closed(self):
        command = build_command(
            self.harness,
            agent(allow_bash=True, task_allowed=True, allow_plugins=True),
            None,
            executable="codex",
        )
        self.assertIn("features.shell_tool=true", command)
        self.assertIn("agents.enabled=true", command)
        self.assertIn("features.plugins=false", command)

    def test_jsonl_events_normalize_text_usage_error_and_tool(self):
        events = CodexEvents()
        events.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
        events.feed({
            "type": "item.completed",
            "item": {"type": "mcp_tool_call", "tool": "write_finding", "arguments": {"x": 1}},
        })
        events.feed({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}})
        self.assertEqual(events.text, "done")
        self.assertEqual(events.tool_calls, [("write_finding", {"x": 1})])
        self.assertEqual(events.usage["input_tokens"], 12)


class CodexRunnerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_cli_contract_normalizes_result(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "fake-codex"
            fake.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                "printf '%s\\n' "
                "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"ok\"}}' "
                "'{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":7,\"output_tokens\":2}}'\n"
            )
            fake.chmod(0o700)
            harness = SimpleNamespace(git_root=root, home=root, codex_model=None)
            result = await CodexRunner(harness, executable=str(fake)).run(agent(), "hello")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.result_text, "ok")
            self.assertEqual((result.input_tokens, result.output_tokens), (7, 2))


class ToolBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_adapter_lists_and_calls_parent_closure(self):
        secret_scope = "findings/watch"

        @tool("probe", "Return bound scope", {"value": str})
        async def probe(args):
            return {
                "content": [{"type": "text", "text": f"{secret_scope}:{args['value']}"}],
                "is_error": False,
            }

        try:
            async with ToolBridge([probe]) as bridge:
                listed = await dispatch(bridge.socket_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                called = await dispatch(bridge.socket_path, {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "probe", "arguments": {"value": "ok"}},
                })
        except PermissionError:
            self.skipTest("current execution sandbox forbids local Unix sockets")
        schema = listed["result"]["tools"][0]["inputSchema"]
        self.assertEqual(schema["properties"]["value"]["type"], "string")
        self.assertEqual(called["result"]["content"][0]["text"], "findings/watch:ok")

    async def test_unknown_tool_fails_closed(self):
        try:
            async with ToolBridge([]) as bridge:
                called = await dispatch(bridge.socket_path, {
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "not_granted", "arguments": {}},
                })
        except PermissionError:
            self.skipTest("current execution sandbox forbids local Unix sockets")
        self.assertTrue(called["result"]["isError"])
