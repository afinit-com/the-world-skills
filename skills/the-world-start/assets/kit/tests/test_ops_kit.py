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
from datetime import datetime
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
ops_log = _load("ops-log")


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
        self.assertIs(plist["AbandonProcessGroup"], True)  # else the round's exit kills it
        self.assertEqual(plist["ProgramArguments"],
                         ["/bin/bash", str(self.home.resolve() / "scripts" / "my-harness-watchdog")])
        self.assertIn("CAFFEINATE=1", files["scripts/my-harness-up"][0])

    def test_linux_renders_a_systemd_user_timer(self):
        files = _render(self.home, "linux")
        service = configparser.ConfigParser(interpolation=None)
        service.read_string(files["scripts/systemd/my-harness-watchdog.service"][0])
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["KillMode"], "process")  # else the cgroup is stopped
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
import json, os, signal, sys, time
if os.environ.get("STUB_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a run that will not finish in time
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "env-seen.json"), "w") as f:
    json.dump({"api_key": os.environ.get("ANTHROPIC_API_KEY"),
               "file_key": os.environ.get("STUB_FROM_FILE"), "path": os.environ["PATH"],
               "lock_env": os.environ.get("WORLD_OPS_LOCK_HELD")}, f)
print("stub harness up", flush=True)
time.sleep(120)
"""


def _read_rolled(logs: Path, stem: str, ext: str) -> list[str]:
    """Every line in <stem><ext> and its rolls, oldest roll first."""
    lines = []
    for gz in sorted(logs.glob(f"{stem}.*{ext}.gz"), key=lambda p: p.stat().st_mtime_ns):
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            lines += f.read().splitlines()
    current = logs / f"{stem}{ext}"
    if current.exists():
        lines += current.read_text(encoding="utf-8").splitlines()
    return lines


class ConsoleWriterTest(unittest.TestCase):
    """bin/ops-log.py: the console log's only writer, which rolls the file itself."""

    def test_no_line_is_lost_while_it_rolls_under_a_steady_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "console.out"
            writer = subprocess.Popen(
                [sys.executable, str(KIT / "bin" / "ops-log.py"), "--file", str(out),
                 "--max-bytes", "2000"], stdin=subprocess.PIPE)
            try:
                for n in range(3000):
                    writer.stdin.write(f"2026-01-01T00:00:00+00:00 line {n}\n".encode())
                    if n % 100 == 0:
                        writer.stdin.flush()
                writer.stdin.close()
                self.assertEqual(writer.wait(timeout=60), 0)
            finally:
                if writer.poll() is None:
                    writer.kill()
            rolls = list(Path(tmp).glob("console.*.out.gz"))
            self.assertGreater(len(rolls), 10, "never rolled")
            lines = _read_rolled(Path(tmp), "console", ".out")
            self.assertEqual([int(line.rsplit(" ", 1)[1]) for line in lines], list(range(3000)))

    def test_stamps_only_lines_without_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "console.out"
            body = b"2026-10-14T09:00:00+09:00 [h] already stamped\nTraceback (most recent call last):\n"
            ops_log.main(["--file", str(out)], stdin=io.BytesIO(body))
            first, second = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(first, "2026-10-14T09:00:00+09:00 [h] already stamped")
        stamp, rest = second.split(" ", 1)
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)
        self.assertEqual(rest, "Traceback (most recent call last):")

    def test_the_watchdog_no_longer_copies_and_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchdog = _render(Path(tmp), "linux")["scripts/my-harness-watchdog"][0]
        self.assertNotIn("gzip -c", watchdog)
        self.assertNotIn(': >"$f"', watchdog)


@unittest.skipUnless(POSIX, "the launcher and watchdog are bash + pgrep (macOS, Linux)")
class LauncherAndWatchdogTest(unittest.TestCase):
    """Runs the rendered scripts against a stub harness in a real venv."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "stub"
        (self.home / "bin").mkdir(parents=True)
        for script in ("ops-lock.py", "ops-log.py"):
            shutil.copy2(KIT / "bin" / script, self.home / "bin" / script)
        (self.home / "world" / "process").mkdir(parents=True)
        shutil.copy2(KIT / "world" / "process" / "runlog.py", self.home / "world" / "process")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.home / ".venv")],
                       check=True)
        (self.home / "harness.py").write_text(STUB_HARNESS, encoding="utf-8")
        (self.home / "env").mkdir()
        (self.home / "env" / "harness.env").write_text("STUB_FROM_FILE=from-file\n# NOT_A_KEY=x\n",
                                                       encoding="utf-8")
        files = ops_kit.render(self.home, "stub", "linux", self.home / ".venv" / "bin" / "python",
                               "/usr/bin:/bin")
        ops_kit.write(self.home, files, force=False)
        self.up = self.home / "scripts" / "stub-up"
        self.watchdog = self.home / "scripts" / "stub-watchdog"
        self.state = self.home / "logs" / ".watchdog"
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for _ in range(50):
            pids = self.pids()
            if not pids:
                return
            subprocess.run(["kill", "-9", *pids], capture_output=True)
            time.sleep(0.1)

    def run_up(self, *args, env=None):
        full_env = {**os.environ, "DOWN_WAIT_S": "20", **(env or {})}
        full_env.pop("WORLD_OPS_LOCK_HELD", None)
        return subprocess.run([str(self.up), *args], capture_output=True, text=True, env=full_env,
                              timeout=120)

    def run_watchdog(self):
        return subprocess.run([str(self.watchdog)], capture_output=True, text=True, timeout=120)

    def pids(self) -> list[str]:
        match = ops_kit.process_match(self.home)
        out = subprocess.run(["pgrep", "-f", match], capture_output=True, text=True).stdout
        return out.split()

    def wait_gone(self):
        for _ in range(50):
            if not self.pids():
                return
            time.sleep(0.1)

    def test_lifecycle(self):
        # up, with a stale shell export of both kinds the launcher must not hand down
        started = self.run_up("up", env={"ANTHROPIC_API_KEY": "sk-shell",
                                         "STUB_FROM_FILE": "stale-shell-value"})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(len(self.pids()), 1)
        seen = json.loads((self.home / "env-seen.json").read_text(encoding="utf-8"))
        self.assertIsNone(seen["api_key"])
        self.assertIsNone(seen["file_key"], "a harness.env key exported in the shell leaked in")
        self.assertIsNone(seen["lock_env"], "the harness would bypass the ops lock")
        self.assertEqual(seen["path"], "/usr/bin:/bin")
        console = (self.home / "logs" / "console.out").read_text(encoding="utf-8")
        self.assertRegex(console, r"^\d{4}-\d{2}-\d{2}T\S+ stub harness up$")

        # a second up is a no-op, not a second copy
        again = self.run_up("up")
        self.assertIn("already running", again.stdout)
        self.assertEqual(len(self.pids()), 1)
        self.assertEqual(self.run_up("running").returncode, 0)

        # down is deliberate: the watchdog leaves it down
        self.assertEqual(self.run_up("down").returncode, 0)
        self.assertEqual(self.pids(), [])
        self.assertNotEqual(self.run_up("running").returncode, 0)
        self.run_watchdog()
        self.assertEqual(self.pids(), [], "the watchdog undid a deliberate down")

        # a crash (no down marker) is restarted, then backed off
        self.assertEqual(self.run_up("up").returncode, 0)
        subprocess.run(["kill", "-9", *self.pids()], check=True)
        self.wait_gone()
        self.run_watchdog()
        self.assertEqual(len(self.pids()), 1)
        self.assertEqual((self.state / "failures").read_text().strip(), "1")
        self.assertIn("restarted", (self.home / "logs" / "watchdog.log").read_text())
        subprocess.run(["kill", "-9", *self.pids()], check=True)
        self.wait_gone()
        self.run_watchdog()
        self.assertEqual(self.pids(), [], "retried inside the backoff window")

        # the next round past the window restarts it, and a later round that finds it
        # running resets the count
        (self.state / "next_try").write_text("0\n")
        self.run_watchdog()
        self.assertEqual(len(self.pids()), 1)
        self.assertEqual((self.state / "failures").read_text().strip(), "2")
        self.run_watchdog()
        self.assertFalse((self.state / "failures").exists())
        self.assertEqual(self.run_up("down").returncode, 0)

    def test_concurrent_ups_start_one_harness(self):
        procs = [subprocess.Popen([str(self.up), "up"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) for _ in range(6)]
        outputs = [p.communicate(timeout=120)[0] for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), outputs)
        self.assertEqual(len(self.pids()), 1, outputs)
        self.assertEqual(sum("started" in o for o in outputs), 1, outputs)

    def test_a_watchdog_round_racing_a_down_leaves_it_down(self):
        for _ in range(3):
            self.assertEqual(self.run_up("up").returncode, 0)
            down = subprocess.Popen([str(self.up), "down"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            rounds = [subprocess.Popen([str(self.watchdog)]) for _ in range(3)]
            down.wait(timeout=120)
            for r in rounds:
                r.wait(timeout=120)
            self.assertEqual(self.pids(), [], "a watchdog round brought a stopped harness back")
            self.assertTrue((self.state / "down").exists())

    def test_the_watchdog_path_neither_starts_nor_unmarks_a_downed_harness(self):
        self.assertEqual(self.run_up("down").returncode, 0)
        resumed = self.run_up("resume")
        self.assertEqual(resumed.returncode, 3, resumed.stdout + resumed.stderr)
        self.assertTrue((self.state / "down").exists())
        self.assertEqual(self.pids(), [])

    def test_up_after_a_timed_out_down_clears_the_marker(self):
        self.assertEqual(self.run_up("up", env={"STUB_IGNORE_TERM": "1"}).returncode, 0)
        down = self.run_up("down", env={"DOWN_WAIT_S": "1"})
        self.assertEqual(down.returncode, 1, down.stdout + down.stderr)
        self.assertIn("still finishing", down.stderr)
        self.assertTrue((self.state / "down").exists())
        self.assertEqual(len(self.pids()), 1)

        up = self.run_up("up")
        self.assertIn("already running", up.stdout)
        self.assertFalse((self.state / "down").exists(), "the watchdog would never restart it")
        subprocess.run(["kill", "-9", *self.pids()], check=True)
        self.wait_gone()
        self.run_watchdog()
        self.assertEqual(len(self.pids()), 1, "the watchdog did not bring it back")


if __name__ == "__main__":
    unittest.main()
