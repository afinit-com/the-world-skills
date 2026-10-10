"""bin/slack-provision.py against a fake `slack` on PATH — no network, no real CLI.

The fake mirrors what the real CLI's `slack deploy` does for a project with a `deploy`
hook (slackapi/slack-cli cmd/platform/deploy.go → internal/pkg/apps/install.go): record the
app in .slack/apps.json, put SLACK_BOT_TOKEN (and SLACK_APP_TOKEN for a Socket Mode app)
into its own environment unless already set, then run the hook string through a shell.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
SCRIPT = KIT / "bin" / "slack-provision.py"

FAKE_SLACK = textwrap.dedent("""\
    #!{python}
    import json, os, subprocess, sys
    from pathlib import Path
    log = Path(os.environ["FAKE_SLACK_LOG"])
    with log.open("a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                            "stray": os.environ.get("SLACK_BOT_TOKEN")}}) + "\\n")
    if sys.argv[1:3] == ["auth", "list"]:
        print(os.environ.get("FAKE_SLACK_AUTH", "acme (Team ID: T1)"))
        sys.exit(0)
    if sys.argv[1:2] != ["deploy"]:
        sys.exit(64)
    if os.environ.get("FAKE_SLACK_PENDING"):
        print("Your request to install the app is pending")
        sys.exit(0)
    hooks = json.loads(Path(".slack/hooks.json").read_text())
    manifest = json.loads(Path("manifest.json").read_text())
    apps = Path(".slack/apps.json")
    key = Path.cwd().name
    if not apps.exists():
        apps.write_text(json.dumps({{"apps": {{"T1": {{"app_id": "A" + key.upper(), "team_id": "T1"}}}}}}))
    count = len(log.read_text().splitlines())
    os.environ.setdefault("SLACK_BOT_TOKEN", f"xoxb-secret-{{key}}-{{count}}")
    if manifest["settings"].get("socket_mode_enabled") and not os.environ.get("FAKE_SLACK_NO_XAPP"):
        os.environ.setdefault("SLACK_APP_TOKEN", f"xapp-secret-{{key}}-{{count}}")
    sys.exit(subprocess.run(["sh", "-c", hooks["hooks"]["deploy"]]).returncode)
""")

SPEC = {
    "prefix": "Obs",
    "description": "Watches the repo",
    "apps": [
        {"key": "watcher", "display": "Watcher", "bot_token_env": "OBS_PERSONA_WATCHER_TOKEN"},
        {"key": "lumen", "display": "Lumen", "main": True,
         "bot_token_env": "OBS_SLACK_BOT_TOKEN", "app_token_env": "OBS_SLACK_APP_TOKEN"},
    ],
}


class SlackProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "slack"
        fake.write_text(FAKE_SLACK.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "slack.log"
        self.spec = self.tmp / "slack-app" / "slack-apps.json"
        self.spec.parent.mkdir()
        self.spec.write_text(json.dumps(SPEC))
        self.env_file = self.tmp / "env" / "harness.env"
        self.env_file.parent.mkdir()
        self.env_file.write_text("# keep me\nAPPROVER_SLACK_ID=U123\n"
                                 "OBS_SLACK_BOT_TOKEN=xoxb-...\nOBS_SLACK_CHANNEL=C999\n")
        self.env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_SLACK_LOG": str(self.log)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_script(self, *extra: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--spec", str(self.spec),
             "--env-file", str(self.env_file), *extra],
            env={**self.env, **env}, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )

    def calls(self) -> list[dict]:
        """The CLI calls that deploy; `slack auth list` lookups are left out."""
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if c["argv"][:2] != ["auth", "list"]]

    def env_values(self) -> dict[str, str]:
        out = {}
        for line in self.env_file.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k] = v
        return out

    def test_provisions_every_app_and_keeps_the_rest_of_the_env_file(self) -> None:
        result = self.run_script("--team", "T1", SLACK_BOT_TOKEN="xoxb-stray-from-shell")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        values = self.env_values()
        self.assertTrue(values["OBS_SLACK_BOT_TOKEN"].startswith("xoxb-secret-lumen"))
        self.assertTrue(values["OBS_SLACK_APP_TOKEN"].startswith("xapp-secret-lumen"))
        self.assertTrue(values["OBS_PERSONA_WATCHER_TOKEN"].startswith("xoxb-secret-watcher"))
        self.assertEqual(values["APPROVER_SLACK_ID"], "U123")
        self.assertEqual(values["OBS_SLACK_CHANNEL"], "C999")
        text = self.env_file.read_text()
        self.assertTrue(text.startswith("# keep me\n"))
        self.assertEqual(text.count("OBS_SLACK_BOT_TOKEN="), 1)  # replaced in place
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        # never echo a token
        self.assertNotIn("secret", result.stdout + result.stderr)

        calls = self.calls()
        self.assertEqual([Path(c["cwd"]).name for c in calls], ["lumen", "watcher"])  # main first
        for call in calls:
            self.assertEqual(call["argv"][0], "deploy")
            self.assertIn("--team", call["argv"])
            self.assertIsNone(call["stray"])  # the shell's SLACK_BOT_TOKEN never reaches the CLI

    def test_a_localized_app_name_gets_an_ascii_bot_username(self) -> None:
        # A Korean persona name is fine as the app's name and fatal as its bot user's
        # display_name: Slack rejects the manifest with bad_username.
        import importlib.util
        spec = importlib.util.spec_from_file_location("slack_provision", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        template = module.TEMPLATE.read_text(encoding="utf-8")
        config = {"prefix": "Obs", "apps": [
            {"key": "watcher", "display": "관측원", "main": True,
             "bot_token_env": "B", "app_token_env": "A"}]}
        manifest = module.render_manifest(template, config, config["apps"][0])
        self.assertEqual(manifest["display_information"]["name"], "Obs-관측원")
        self.assertEqual(manifest["features"]["bot_user"]["display_name"], "obs")
        sub = dict(config["apps"][0], key="scout", display="정찰병", main=False)
        manifest = module.render_manifest(template, config, sub)
        self.assertEqual(manifest["features"]["bot_user"]["display_name"], "obs-scout")
        chosen = dict(config["apps"][0], bot_username="obs-watch")
        manifest = module.render_manifest(template, config, chosen)
        self.assertEqual(manifest["features"]["bot_user"]["display_name"], "obs-watch")
        with self.assertRaises(module.SpecError):
            module.render_manifest(template, config, dict(chosen, bot_username="관측원"))

    def test_without_a_terminal_the_only_logged_in_workspace_is_used(self) -> None:
        # No team anywhere and no terminal: the CLI would die on "Select an app".
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        deploys = [c["argv"] for c in self.calls()]
        self.assertTrue(deploys)
        for argv in deploys:
            self.assertEqual(argv[argv.index("--team") + 1], "T1")

    def test_without_a_terminal_several_workspaces_is_an_error_naming_them(self) -> None:
        result = self.run_script(FAKE_SLACK_AUTH="a (Team ID: T1)\nb (Team ID: T2)")
        self.assertEqual(result.returncode, 2)
        self.assertIn("T1, T2", result.stderr)
        self.assertIn("--team", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_manifests_main_owns_the_socket_and_subs_only_post(self) -> None:
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        main = json.loads((self.spec.parent / "lumen" / "manifest.json").read_text())
        sub = json.loads((self.spec.parent / "watcher" / "manifest.json").read_text())
        self.assertNotIn("_comment", main)
        self.assertEqual(main["display_information"]["name"], "Obs-Lumen")
        self.assertEqual(main["display_information"]["description"], "Watches the repo")
        self.assertTrue(main["settings"]["socket_mode_enabled"])
        self.assertIn("app_mention", main["settings"]["event_subscriptions"]["bot_events"])
        self.assertEqual(sub["features"]["bot_user"]["display_name"], "Obs-Watcher")
        self.assertEqual(sub["oauth_config"]["scopes"]["bot"], ["chat:write", "reactions:write"])
        self.assertFalse(sub["settings"]["socket_mode_enabled"])
        self.assertNotIn("event_subscriptions", sub["settings"])
        self.assertNotIn("interactivity", sub["settings"])
        hooks = json.loads((self.spec.parent / "lumen" / ".slack" / "hooks.json").read_text())
        self.assertIn("--capture lumen", hooks["hooks"]["deploy"])
        config = json.loads((self.spec.parent / "lumen" / ".slack" / "config.json").read_text())
        self.assertEqual(config["manifest"]["source"], "local")

    def test_rerun_skips_provisioned_apps_and_force_redeploys(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        first = self.env_values()
        again = self.run_script()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("already provisioned", again.stdout)
        self.assertEqual(self.env_values(), first)

        forced = self.run_script("--force", "--only", "watcher")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(Path(self.calls()[-1]["cwd"]).name, "watcher")
        # the app recorded on the first run is reused, not created again
        apps = json.loads((self.spec.parent / "watcher" / ".slack" / "apps.json").read_text())
        self.assertEqual(apps["apps"]["T1"]["app_id"], "AWATCHER")

    def test_pending_admin_approval_is_reported_not_faked(self) -> None:
        before = self.env_file.read_text()
        result = self.run_script(FAKE_SLACK_PENDING="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("admin approval", result.stdout)
        self.assertIn("api.slack.com/apps", result.stdout)  # manual fallback named
        self.assertEqual(self.env_file.read_text(), before)

    def test_missing_app_level_token_names_the_manual_step(self) -> None:
        result = self.run_script(FAKE_SLACK_NO_XAPP="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OBS_SLACK_APP_TOKEN still missing", result.stdout)
        self.assertIn("https://api.slack.com/apps/ALUMEN/general", result.stdout)
        self.assertTrue(self.env_values()["OBS_SLACK_BOT_TOKEN"].startswith("xoxb-"))

    def test_without_the_cli_it_points_at_install_and_the_manual_path(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--spec", str(self.spec), "--env-file", str(self.env_file)],
            env={**os.environ, "PATH": str(self.tmp / "empty")}, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("slack login", result.stderr)
        self.assertIn("From a manifest", result.stderr)

    def test_spec_needs_exactly_one_main(self) -> None:
        spec = json.loads(json.dumps(SPEC))
        spec["apps"][0]["main"] = True
        spec["apps"][0]["app_token_env"] = "OBS_OTHER_APP_TOKEN"
        self.spec.write_text(json.dumps(spec))
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly one app", result.stderr)

    def test_new_env_file_is_created_private(self) -> None:
        self.env_file.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual(set(self.env_values()),
                         {"OBS_SLACK_BOT_TOKEN", "OBS_SLACK_APP_TOKEN", "OBS_PERSONA_WATCHER_TOKEN"})


if __name__ == "__main__":
    unittest.main()
