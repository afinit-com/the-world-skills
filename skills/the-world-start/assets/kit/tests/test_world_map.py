from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from world import Agent, Harness, Persona, PollTrigger, Relationship, ToolSpec
from world.world_map import world_map_data, write_world_map


async def unused_handler(_args):
    raise AssertionError("Export must not execute a tool")


class WorldMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.reader = ToolSpec("read_map", "Read the expedition map", {}, unused_handler)
        self.main = Persona("captain", "Captain")
        self.scout = Persona("scout", "Scout", thread_tools=[self.reader])
        self.harness = Harness(
            "Crew", home=self.home, provider="codex", main_persona=self.main,
            extra_personas=[self.scout], mention_tools=[self.reader],
            relationships=[Relationship("captain", "scout", "Explore", "Look for a path")],
        )

    def test_export_preserves_relationship_direction_and_tool_contexts(self):
        writer = ToolSpec("save_notes", "Save notes", {}, unused_handler)
        self.harness.register(Agent(
            "route", PollTrigger(lambda *_args: self.fail("Must not poll")),
            persona=self.scout, tools=[writer], write_scope=("logs/route",),
        ))
        data = world_map_data(self.harness)
        self.assertEqual(["captain", "scout"], [item["key"] for item in data["characters"]])
        self.assertEqual("captain", data["relationships"][0]["source"])
        self.assertEqual("scout", data["relationships"][0]["target"])
        scout = data["characters"][1]
        self.assertEqual(["logs/route"], scout["jobs"][0]["write_scope"])
        self.assertEqual([], scout["jobs"][0]["tools"]["tools"][0]["write_roots"])
        self.assertEqual("read_map", scout["tool_groups"][2]["tools"][0]["name"])
        self.assertEqual("read_map", scout["tool_groups"][0]["tools"][0]["name"])
        self.assertEqual([], scout["tool_groups"][1]["tools"])

    def test_dynamic_tools_are_unknown_not_executed(self):
        def factory(*_args):
            self.fail("Must not invoke factories")

        self.scout.mention_tools = factory
        self.scout.thread_tools = factory
        groups = world_map_data(self.harness)["characters"][1]["tool_groups"]
        self.assertTrue(groups[1]["dynamic"])
        self.assertTrue(groups[2]["dynamic"])
        self.assertEqual([], groups[1]["tools"])

    def test_unknown_or_self_relationship_is_rejected(self):
        for relation in (Relationship("scout", "missing", "help"),
                         Relationship("scout", "scout", "help"),
                         Relationship("scout", "captain", " ")):
            with self.subTest(relation=relation):
                self.harness.relationships = [relation]
                with self.assertRaises(ValueError):
                    world_map_data(self.harness)

    def test_script_payload_cannot_escape_json_block_or_leak_env_values(self):
        payload = '</script><script>alert("unsafe")</script>'
        self.scout.display = payload
        self.scout.bot_token_env = "TEST_WORLD_MAP_SECRET"
        with patch.dict("os.environ", {"TEST_WORLD_MAP_SECRET": "not-for-export"}):
            output = write_world_map(self.harness, self.home / "WORLD.html").read_text()
        self.assertNotIn(payload, output)
        self.assertNotIn("not-for-export", output)
        data = output.split('<script type="application/json" id="world-data">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(payload, json.loads(data)["characters"][1]["name"])

    def test_portrait_is_embedded_and_cannot_escape_home(self):
        portrait = self.home / "face.png"
        portrait.write_bytes(b"example image bytes")
        self.scout.portrait = "face.png"
        self.assertTrue(world_map_data(self.harness)["characters"][1]["portrait"].startswith("data:image/png;base64,"))
        self.scout.portrait = "../outside.png"
        with self.assertRaises(ValueError):
            world_map_data(self.harness)

    def test_a_url_portrait_falls_back_to_the_name_card_loudly(self):
        # Issue #1: harnesses older than icon_url put the Slack icon URL in portrait.
        self.scout.portrait = "https://x.test/scout.png"
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            data = world_map_data(self.harness)
        self.assertEqual("", data["characters"][1]["portrait"])
        self.assertIn("scout", err.getvalue())
        self.assertIn("icon_url", err.getvalue())

    def test_local_portrait_and_icon_url_together_export(self):
        (self.home / "portraits").mkdir()
        (self.home / "portraits" / "scout.png").write_bytes(b"example image bytes")
        self.scout.portrait = "portraits/scout.png"
        self.scout.icon_url = "https://x.test/scout-icon.png"
        with patch.object(sys, "argv", ["harness.py", "--world-map"]), patch(
            "world.harness.asyncio.run", side_effect=AssertionError("Runtime must not start")
        ), patch("sys.stderr", new_callable=io.StringIO) as err:
            self.harness.main()
        self.assertEqual("", err.getvalue())
        output = (self.home / "WORLD.html").read_text()
        self.assertIn("data:image/png;base64,", output)
        self.assertNotIn("scout-icon.png", output)

    def test_cli_exports_without_starting_runtime(self):
        with patch.object(sys, "argv", ["harness.py", "--world-map"]), patch(
            "world.harness.asyncio.run", side_effect=AssertionError("Runtime must not start")
        ):
            self.harness.main()
        self.assertTrue((self.home / "WORLD.html").is_file())

    def test_unassigned_agent_belongs_to_main_and_no_relations_are_invented(self):
        self.harness.relationships = []
        self.harness.register(Agent("solo", PollTrigger(lambda *_args: None)))
        data = world_map_data(self.harness)
        self.assertEqual("solo", data["characters"][0]["jobs"][0]["name"])
        self.assertEqual([], data["relationships"])
