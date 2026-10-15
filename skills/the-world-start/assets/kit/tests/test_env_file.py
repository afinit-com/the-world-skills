"""The runtime and ops tools share one env-file grammar without importing world."""
import io
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ENV = Path(__file__).resolve().parents[1] / "world" / "env.py"


class EnvFileTest(unittest.TestCase):
    def test_parser_loader_and_launcher_keys_agree(self):
        env = runpy.run_path(str(ENV))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.env"
            path.write_text(" # comment\n export SPACED_KEY = 'a # b'\n"
                            " PLAIN_KEY = value # comment\nBAD-KEY=no\n=empty\n"
                            "PLAIN_KEY = last\n", encoding="utf-8")
            self.assertEqual(env["parse_env_file"](path),
                             {"SPACED_KEY": "a # b", "PLAIN_KEY": "value"})
            with patch.dict(os.environ, {"WORLD_ENV_FILE": str(path)}, clear=True):
                env["load_harness_env"]()
                self.assertEqual(os.environ["SPACED_KEY"], "a # b")
                self.assertEqual(os.environ["PLAIN_KEY"], "value")
            keys = subprocess.run([sys.executable, "-I", str(ENV), "--keys", str(path)],
                                  check=True, capture_output=True, text=True)
            self.assertEqual(keys.stdout.splitlines(), ["SPACED_KEY", "PLAIN_KEY"])
            self.assertEqual(env["parse_env_file"](path.with_name("missing")), {})

    def test_a_repeated_key_keeps_its_first_value_without_a_shadow_warning(self):
        # The loader before the shared parser set the first value and dropped the later
        # one; harness.env files written since then must keep meaning what they meant.
        env = runpy.run_path(str(ENV))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.env"
            path.write_text("DUP_KEY=first\nDUP_KEY=second\n", encoding="utf-8")
            self.assertEqual(env["parse_env_file"](path), {"DUP_KEY": "first"})
            err = io.StringIO()
            with patch.dict(os.environ, {"WORLD_ENV_FILE": str(path)}, clear=True), \
                    patch("sys.stderr", err):
                env["load_harness_env"]()
                self.assertEqual(os.environ["DUP_KEY"], "first")
            self.assertNotIn("shadowed", err.getvalue(), "a repeated key is not a shadowing export")
