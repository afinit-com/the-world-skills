"""The Codex tool bridge on native Windows: a named pipe only the current user can open.

Runs only on Windows; macOS/Linux use the 0600 unix socket covered in test_codex.py."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from world.tools import ToolBridge, tool
from world.tools.mcp_stdio import dispatch


@unittest.skipUnless(os.name == "nt", "Windows named pipe contract")
class WindowsPipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_parent_handlers_and_unknown_tool(self):
        calls = []

        @tool("probe", "Only granted capability", {"value": str})
        async def probe(args):
            calls.append(args["value"])
            return {"content": [{"type": "text", "text": args["value"]}]}

        async with ToolBridge([probe]) as bridge:
            results = await asyncio.gather(*[
                dispatch(bridge.socket_path, {"id": i, "method": "tools/call",
                    "params": {"name": "probe", "arguments": {"value": str(i)}}})
                for i in range(5)
            ])
            denied = await dispatch(bridge.socket_path, {"id": 9, "method": "tools/call",
                "params": {"name": "not_granted"}})
        self.assertEqual(set(calls), {str(i) for i in range(5)})
        self.assertTrue(all(not r["result"]["isError"] for r in results))
        self.assertTrue(denied["result"]["isError"])

    async def test_pipe_acl_and_remote_rejection(self):
        import win32pipe
        import win32security

        async with ToolBridge([]) as bridge:
            pipe = bridge._pipe
            flags, *_ = win32pipe.GetNamedPipeInfo(pipe._handle)
            # GetNamedPipeInfo does not expose the remote-client mode; creation uses 0x08.
            self.assertTrue(flags & win32pipe.PIPE_TYPE_MESSAGE)
            sd = win32security.GetSecurityInfo(pipe._handle, win32security.SE_KERNEL_OBJECT,
                win32security.DACL_SECURITY_INFORMATION)
            acl = sd.GetSecurityDescriptorDacl()
            self.assertEqual(acl.GetAceCount(), 1)
            token = win32security.OpenProcessToken(__import__('win32api').GetCurrentProcess(), 8)
            try:
                sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            finally:
                token.Close()
            self.assertEqual(acl.GetAce(0)[2], sid)

    async def test_stdio_adapter_in_separate_process(self):
        @tool("unicode", "Korean and emoji", {})
        async def unicode_tool(args):
            return {"content": [{"type": "text", "text": "우라! 🐰"}]}

        async with ToolBridge([unicode_tool]) as bridge:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "world.tools.mcp_stdio", "--socket", str(bridge.socket_path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            request = json.dumps({"id": 1, "method": "tools/call", "params": {"name": "unicode"}}) + "\n"
            stdout, stderr = await asyncio.wait_for(process.communicate(request.encode()), 15)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(json.loads(stdout)["result"]["content"][0]["text"], "우라! 🐰")

    async def test_idle_close_releases_pipe(self):
        from world.tools.windows_pipe import exchange
        bridge = ToolBridge([])
        async with bridge:
            address = str(bridge.socket_path)
        with self.assertRaises(OSError):
            await asyncio.to_thread(exchange, address, {"method": "list"})

    async def test_remote_address_is_rejected_before_connection(self):
        from world.tools.windows_pipe import exchange
        with self.assertRaises(ValueError):
            exchange("\\\\remote\\pipe\\world-mcp-test", {"method": "list"})

    async def test_early_disconnect_does_not_kill_listener(self):
        from multiprocessing.connection import PipeClient
        async with ToolBridge([]) as bridge:
            connection = await asyncio.to_thread(PipeClient, str(bridge.socket_path))
            connection.close()
            await asyncio.sleep(0.05)
            result = await dispatch(bridge.socket_path, {"id": 1, "method": "tools/list"})
            self.assertEqual(result["result"]["tools"], [])

    async def test_second_server_cannot_claim_existing_pipe(self):
        import pywintypes

        from world.tools.windows_pipe import new_handle, pipe_security
        async with ToolBridge([]) as bridge:
            with self.assertRaises(pywintypes.error):
                new_handle(str(bridge.socket_path), pipe_security(), first=True)

    async def test_scoped_finding_preserves_korean_and_emoji(self):
        from world.toolkit import make_write_report
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            writer = make_write_report(home, ("work",))
            async with ToolBridge([writer]) as bridge:
                written = await dispatch(bridge.socket_path, {"id": 1, "method": "tools/call",
                    "params": {"name": "write_report", "arguments": {
                        "path": "work/result.md", "content": "우라! 🐰\n원인: 미확인"}}})
                denied = await dispatch(bridge.socket_path, {"id": 2, "method": "tools/call",
                    "params": {"name": "write_report", "arguments": {
                        "path": "outside.md", "content": "must not write"}}})
            self.assertFalse(written["result"]["isError"])
            self.assertEqual((home / "work/result.md").read_text(encoding="utf-8"),
                "우라! 🐰\n원인: 미확인")
            self.assertTrue(denied["result"]["isError"])
            self.assertFalse((home / "outside.md").exists())


if __name__ == "__main__":
    unittest.main()
