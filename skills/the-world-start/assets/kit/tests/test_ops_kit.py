"""The opt-in ops kit (issue #4): bin/ops-kit.py renders a launcher, a watchdog and a
launchd / systemd unit. Nothing here registers a service; the unit files are only parsed.

The launcher and watchdog are also run for real against a stub harness.py, because their
failure modes (a second copy started, ANTHROPIC_API_KEY handed down, a deliberate `down`
undone by the watchdog two minutes later) only show up in a running process.
"""

import configparser
import gzip
import importlib.util
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
POSIX = os.name == "posix" and shutil.which("bash") and shutil.which("pgrep")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), KIT / "bin" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ops_kit = _load("ops-kit")
ops_notify = _load("ops-notify")


def _render(home: Path, os_name: str, **kw) -> dict:
    return ops_kit.render(home, "my harness", os_name, home / ".venv" / "bin" / "python",
                          "/opt/cli/bin:/usr/bin:/bin", **kw)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "a & b"  # XML- and shell-hostile on purpose
        self.home.mkdir()

    def test_macos_renders_scripts_and_a_launchd_plist(self):
        files = _render(self.home, "macos")
        self.assertEqual(sorted(files), [
            "scripts/launchd/local.the-world.my-harness.watchdog.plist",
            "scripts/my-harness-up", "scripts/my-harness-watchdog"])
        plist = plistlib.loads(files["scripts/launchd/local.the-world.my-harness.watchdog.plist"][0]
                               .encode("utf-8"))
        self.assertEqual(plist["Label"], "local.the-world.my-harness.watchdog")
        self.assertEqual(plist["StartInterval"], 120)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["ProgramArguments"],
                         ["/bin/bash", str(self.home.resolve() / "scripts" / "my-harness-watchdog")])
        self.assertIn("CAFFEINATE=1", files["scripts/my-harness-up"][0])

    def test_linux_renders_a_systemd_user_timer(self):
        files = _render(self.home, "linux")
        service = configparser.ConfigParser(interpolation=None)
        service.read_string(files["scripts/systemd/my-harness-watchdog.service"][0])
        self.assertEqual(service["Service"]["Type"], "oneshot")
        watchdog = str(self.home.resolve() / "scripts" / "my-harness-watchdog")
        self.assertEqual(service["Service"]["ExecStart"], f'/bin/bash "{watchdog}"')
        timer = configparser.ConfigParser(interpolation=None)
        timer.read_string(files["scripts/systemd/my-harness-watchdog.timer"][0])
        self.assertEqual(timer["Timer"]["OnUnitActiveSec"], "120s")
        self.assertEqual(timer["Timer"]["Unit"], "my-harness-watchdog.service")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        self.assertIn("CAFFEINATE=0", files["scripts/my-harness-up"][0])

    def test_windows_is_refused_loudly(self):
        with self.assertRaisesRegex(ops_kit.OpsKitError, "Windows Task Scheduler is not supported"):
            _render(self.home, "windows")

    def test_scripts_are_valid_bash_and_keep_the_known_fixes(self):
        if not shutil.which("bash"):
            self.skipTest("no bash")
        files = _render(self.home, "macos", notify=("OBS_SLACK_BOT_TOKEN", "OBS_SLACK_CHANNEL"),
                        keep_days=30)
        for rel in ("scripts/my-harness-up", "scripts/my-harness-watchdog"):
            content, mode = files[rel]
            self.assertEqual(mode, 0o755)
            path = Path(self.tmp.name) / Path(rel).name
            path.write_text(content, encoding="utf-8")
            check = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, f"{rel}: {check.stderr}")
        up = files["scripts/my-harness-up"][0]
        self.assertIn("env -u ANTHROPIC_API_KEY", up)
        self.assertIn("RUN_PATH=/opt/cli/bin:/usr/bin:/bin", up)
        watchdog = files["scripts/my-harness-watchdog"][0]
        self.assertIn("NOTIFY='--token-env OBS_SLACK_BOT_TOKEN --channel-env OBS_SLACK_CHANNEL'",
                      watchdog)
        self.assertIn("KEEP_DAYS=30", watchdog)

    def test_defaults_neither_notify_nor_delete(self):
        watchdog = _render(self.home, "linux")["scripts/my-harness-watchdog"][0]
        self.assertIn("NOTIFY=  ", watchdog)
        self.assertIn("KEEP_DAYS=  ", watchdog)

    def test_next_steps_remove_the_watchdog_before_down(self):
        for os_name, unregister in (("macos", "launchctl bootout"),
                                    ("linux", "systemctl --user disable --now")):
            steps = "\n".join(ops_kit.next_steps(self.home, "my harness", os_name))
            self.assertLess(steps.index(unregister), steps.rindex("my-harness-up down"), os_name)

    def test_existing_scripts_are_not_replaced_without_force(self):
        files = _render(self.home, "linux")
        ops_kit.write(self.home, files, force=False)
        with self.assertRaisesRegex(ops_kit.OpsKitError, "--force"):
            ops_kit.write(self.home, files, force=False)
        ops_kit.write(self.home, files, force=True)

    def test_notify_refused_while_slack_values_are_placeholders(self):
        env = self.home / "harness.env"
        env.write_text("OBS_SLACK_BOT_TOKEN=xoxb-...\nOBS_SLACK_CHANNEL=C0XXXXXXX     # x\n",
                       encoding="utf-8")
        keys = ("OBS_SLACK_BOT_TOKEN", "OBS_SLACK_CHANNEL")
        with self.assertRaisesRegex(ops_kit.OpsKitError, "OBS_SLACK_BOT_TOKEN and OBS_SLACK_CHANNEL"):
            ops_kit.check_notify_env(env, keys)
        env.write_text("OBS_SLACK_BOT_TOKEN=xoxb-real\nOBS_SLACK_CHANNEL=C123  # chan\n",
                       encoding="utf-8")
        ops_kit.check_notify_env(env, keys)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.empty_env = str(Path(tmp.name) / "harness.env")
        Path(self.empty_env).write_text("", encoding="utf-8")

    def test_missing_token_is_reported_not_posted(self):
        called = []
        err = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"WORLD_ENV_FILE": self.empty_env}), \
                unittest.mock.patch("sys.stderr", err):
            code = ops_notify.main(["--token-env", "NOPE_TOKEN", "--channel-env", "NOPE_CHAN", "hi"],
                                   opener=lambda *a, **k: called.append(a))
        self.assertEqual(code, 1)
        self.assertEqual(called, [])
        self.assertIn("NOPE_TOKEN", err.getvalue())

    def test_slack_refusal_is_a_failure(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        env = {"WORLD_ENV_FILE": self.empty_env, "T_TOKEN": "xoxb-1", "T_CHAN": "C1"}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            bad = ops_notify.main(["--token-env", "T_TOKEN", "--channel-env", "T_CHAN", "hi"],
                                  opener=lambda req, timeout: Resp(b'{"ok": false, "error": "x"}'))
            good = ops_notify.main(["--token-env", "T_TOKEN", "--channel-env", "T_CHAN", "hi"],
                                   opener=lambda req, timeout: Resp(b'{"ok": true}'))
        self.assertEqual((bad, good), (1, 0))

    def test_request_carries_the_token_only_in_the_header(self):
        req = ops_notify.build_request("xoxb-secret", "C1", "restarted")
        self.assertEqual(req.get_header("Authorization"), "Bearer xoxb-secret")
        self.assertNotIn(b"xoxb-secret", req.data)
        self.assertEqual(json.loads(req.data), {"channel": "C1", "text": "restarted"})


STUB_HARNESS = """\
import json, os, sys, time
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "env-seen.json"), "w") as f:
    json.dump({"api_key": os.environ.get("ANTHROPIC_API_KEY"),
               "file_key": os.environ.get("STUB_FROM_FILE"), "path": os.environ["PATH"]}, f)
print("stub harness up", flush=True)
time.sleep(120)
"""


@unittest.skipUnless(POSIX, "the launcher and watchdog are bash + pgrep (macOS, Linux)")
class LauncherAndWatchdogTest(unittest.TestCase):
    """Runs the rendered scripts against a stub harness in a real venv."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name).resolve() / "stub"
        cls.home.mkdir()
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.home / ".venv")],
                       check=True)
        (cls.home / "harness.py").write_text(STUB_HARNESS, encoding="utf-8")
        (cls.home / "env").mkdir()
        (cls.home / "env" / "harness.env").write_text("STUB_FROM_FILE=from-file\n# NOT_A_KEY=x\n",
                                                      encoding="utf-8")
        files = ops_kit.render(cls.home, "stub", "linux", cls.home / ".venv" / "bin" / "python",
                               "/usr/bin:/bin")
        ops_kit.write(cls.home, files, force=False)
        cls.up = cls.home / "scripts" / "stub-up"
        cls.watchdog = cls.home / "scripts" / "stub-watchdog"
        cls.state = cls.home / "logs" / ".watchdog"

    @classmethod
    def tearDownClass(cls):
        subprocess.run([str(cls.up), "down"], capture_output=True)
        cls.tmp.cleanup()

    def run_up(self, *args, env=None):
        full_env = {**os.environ, "DOWN_WAIT_S": "20", **(env or {})}
        return subprocess.run([str(self.up), *args], capture_output=True, text=True, env=full_env)

    def pids(self) -> list[str]:
        match = ops_kit.process_match(self.home)
        out = subprocess.run(["pgrep", "-f", match], capture_output=True, text=True).stdout
        return out.split()

    def test_lifecycle(self):
        # up, with a stale shell export of both kinds the launcher must not hand down
        started = self.run_up("up", env={"ANTHROPIC_API_KEY": "sk-shell",
                                         "STUB_FROM_FILE": "stale-shell-value"})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(len(self.pids()), 1)
        seen = json.loads((self.home / "env-seen.json").read_text(encoding="utf-8"))
        self.assertIsNone(seen["api_key"])
        self.assertIsNone(seen["file_key"], "a harness.env key exported in the shell leaked in")
        self.assertEqual(seen["path"], "/usr/bin:/bin")

        # a second up is a no-op, not a second copy
        again = self.run_up("up")
        self.assertIn("already running", again.stdout)
        self.assertEqual(len(self.pids()), 1)
        self.assertEqual(self.run_up("running").returncode, 0)

        # down is deliberate: the watchdog leaves it down
        self.assertEqual(self.run_up("down").returncode, 0)
        self.assertEqual(self.pids(), [])
        self.assertNotEqual(self.run_up("running").returncode, 0)
        subprocess.run([str(self.watchdog)], check=True)
        self.assertEqual(self.pids(), [], "the watchdog undid a deliberate down")

        # a crash (no down marker) is restarted, then backed off
        self.assertEqual(self.run_up("up").returncode, 0)
        subprocess.run(["kill", "-9", *self.pids()], check=True)
        for _ in range(50):
            if not self.pids():
                break
            time.sleep(0.1)
        subprocess.run([str(self.watchdog)], check=True)
        self.assertEqual(len(self.pids()), 1)
        self.assertEqual((self.state / "failures").read_text().strip(), "1")
        self.assertIn("restarted", (self.home / "logs" / "watchdog.log").read_text())
        subprocess.run(["kill", "-9", *self.pids()], check=True)
        time.sleep(0.5)
        subprocess.run([str(self.watchdog)], check=True)
        self.assertEqual(self.pids(), [], "retried inside the backoff window")

        # the next round past the window restarts it, and a later round that finds it
        # running resets the count
        (self.state / "next_try").write_text("0\n")
        subprocess.run([str(self.watchdog)], check=True)
        self.assertEqual(len(self.pids()), 1)
        self.assertEqual((self.state / "failures").read_text().strip(), "2")
        subprocess.run([str(self.watchdog)], check=True)
        self.assertFalse((self.state / "failures").exists())
        self.assertEqual(self.run_up("down").returncode, 0)

    def test_watchdog_rolls_the_console_log_by_date(self):
        logs = self.home / "logs"
        logs.mkdir(exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        (logs / "console.out").write_text("from an earlier day\n", encoding="utf-8")
        (self.state / "rolled-console.out").write_text("2000-01-01\n")
        (self.state / "down").write_text("test\n")  # keep the watchdog from starting it
        subprocess.run([str(self.watchdog)], check=True)
        rolled = logs / "console.2000-01-01.out.gz"
        self.assertTrue(rolled.exists())
        with gzip.open(rolled, "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "from an earlier day\n")
        self.assertEqual((logs / "console.out").read_text(), "")
        self.assertNotEqual((self.state / "rolled-console.out").read_text().strip(), "2000-01-01")


if __name__ == "__main__":
    unittest.main()
