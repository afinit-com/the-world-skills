from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("slack_pair", ROOT / "bin" / "slack-pair.py")
pair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pair)


class PairingTests(unittest.TestCase):
    def test_instructions_are_flushed_before_waiting_on_socket(self) -> None:
        web = SimpleNamespace(auth_test=AsyncMock(return_value={'user_id': 'UBOT', 'user': 'keeper'}))
        socket = SimpleNamespace(socket_mode_request_listeners=[], connect=AsyncMock(),
                                 disconnect=AsyncMock(), close=AsyncMock())
        with patch('slack_sdk.web.async_client.AsyncWebClient', return_value=web), \
                patch('slack_sdk.socket_mode.aiohttp.SocketModeClient', return_value=socket), \
                patch('builtins.print') as output:
            with self.assertRaises(TimeoutError):
                asyncio.run(pair.wait_for_mention('mock', 'mock', 0))
        self.assertEqual(6, output.call_count)
        self.assertTrue(all(call.kwargs.get('flush') for call in output.call_args_list))

    def test_a_person_mentioning_the_bot_names_the_channel_and_the_approver(self) -> None:
        event = {"type": "app_mention", "user": "U0HUMAN", "channel": "C0HOME", "ts": "1.2"}
        self.assertEqual({"channel": "C0HOME", "approver": "U0HUMAN", "ts": "1.2"},
                         pair.pairing_from_event(event, "U0BOT"))

    def test_bots_other_events_and_the_bot_itself_are_not_a_pairing(self) -> None:
        base = {"type": "app_mention", "user": "U0HUMAN", "channel": "C0HOME"}
        self.assertIsNone(pair.pairing_from_event({**base, "bot_id": "B1"}, "U0BOT"))
        self.assertIsNone(pair.pairing_from_event({**base, "type": "message"}, "U0BOT"))
        self.assertIsNone(pair.pairing_from_event({**base, "user": "U0BOT"}, "U0BOT"))
        self.assertIsNone(pair.pairing_from_event({**base, "channel": ""}, "U0BOT"))

    def test_the_ids_land_in_the_env_file_and_other_lines_stay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "harness.env"
            env.write_text("OBS_SLACK_BOT_TOKEN=xoxb-keep\nOBS_SLACK_CHANNEL=C0XXXXXXX\n")
            updates = pair.env_updates({"channel": "C0HOME", "approver": "U0HUMAN"},
                                       "OBS_SLACK_CHANNEL", "APPROVER_SLACK_ID")
            pair._provision_module().upsert_env(env, updates)
            text = env.read_text()
            self.assertIn("OBS_SLACK_BOT_TOKEN=xoxb-keep", text)
            self.assertIn("OBS_SLACK_CHANNEL=C0HOME", text)
            self.assertIn("APPROVER_SLACK_ID=U0HUMAN", text)
            self.assertNotIn("C0XXXXXXX", text)
            if os.name != "nt":  # Windows gets a user-only ACL instead (test_windows_env)
                self.assertEqual(0o600, stat.S_IMODE(os.stat(env).st_mode))


if __name__ == "__main__":
    unittest.main()
