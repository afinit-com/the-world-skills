"""The runtime and ops tools share one env-file grammar without importing world."""
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
                             {"SPACED_KEY": "a # b", "PLAIN_KEY": "last"})
            with patch.dict(os.environ, {"WORLD_ENV_FILE": str(path)}, clear=True):
                env["load_harness_env"]()
                self.assertEqual(os.environ["SPACED_KEY"], "a # b")
                self.assertEqual(os.environ["PLAIN_KEY"], "last")
            keys = subprocess.run([sys.executable, "-I", str(ENV), "--keys", str(path)],
                                  check=True, capture_output=True, text=True)
            self.assertEqual(keys.stdout.splitlines(), ["SPACED_KEY", "PLAIN_KEY"])
            self.assertEqual(env["parse_env_file"](path.with_name("missing")), {})
