"""env/harness.env on native Windows: written as UTF-8 and readable by the current user only.

os.chmod(0o600) is a no-op on Windows, so the token file used to stay readable by every
local account. Runs only on Windows."""

import importlib.util
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows file ACL contract")
class WindowsEnvFileTests(unittest.TestCase):
    def test_env_write_preserves_unicode_and_user_only_permissions(self):
        import win32api
        import win32con
        import win32security

        from world.env import _warn_if_readable

        spec = importlib.util.spec_from_file_location("provision", KIT / "bin" / "slack-provision.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "harness.env"
            module.upsert_env(path, {"PLACEHOLDER": "관측원 — 🔭"})
            module.upsert_env(path, {"OTHER_PLACEHOLDER": "우라!!"})
            self.assertEqual(module.read_env(path)["PLACEHOLDER"], "관측원 — 🔭")
            descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
            self.assertTrue(descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
            dacl = descriptor.GetSecurityDescriptorDacl()
            self.assertEqual(dacl.GetAceCount(), 1)
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            finally:
                token.Close()
            self.assertEqual(dacl.GetAce(0)[2], sid)
            output = StringIO()
            with redirect_stderr(output):
                _warn_if_readable(path)
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
