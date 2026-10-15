"""Agent subprocesses must not inherit the harness's secrets (env/harness.env), while
the harness process itself keeps them for its in-process tools."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from world import env as world_env
from world.runners.codex import CodexRunner
from world.types import Agent, PollTrigger

KIT = Path(__file__).resolve().parents[1]
SECRET = "xoxb-123456789-abcdefSECRET"


def agent(**kwargs):
    return Agent("watch", PollTrigger(lambda *_: None), **kwargs)


class LoadedEnvCase(unittest.TestCase):
    """Load a temp harness.env the way `import world` does, and undo it afterwards."""

    def setUp(self):
        self._saved = (dict(world_env._LOADED), set(world_env._FILE_KEYS),
                       set(world_env._WARNED), set(world_env._PERM_WARNED))
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.dir = tempfile.TemporaryDirectory()
        self.env_file = Path(self.dir.name) / "harness.env"
        self.env_file.write_text(
            f"DEMO_SLACK_BOT_TOKEN={SECRET}\n"
            "DEMO_SLACK_CHANNEL=C0123\n"          # not secret-shaped; file origin is enough
            "RELAY_SECRET=relay-hmac\n"
            "APPROVER_SLACK_ID=U0123\n"
        )
        self.env_file.chmod(0o600)
        os.environ["WORLD_ENV_FILE"] = str(self.env_file)
        for key in ("DEMO_SLACK_BOT_TOKEN", "DEMO_SLACK_CHANNEL", "RELAY_SECRET",
                    "APPROVER_SLACK_ID"):
            os.environ.pop(key, None)
        world_env.load_harness_env()

    def tearDown(self):
        self._env.stop()
        for live, saved in zip(
            (world_env._LOADED, world_env._FILE_KEYS, world_env._WARNED,
             world_env._PERM_WARNED),
            self._saved,
        ):
            live.clear()
            live.update(saved)
        self.dir.cleanup()


class ChildEnvTests(LoadedEnvCase):
    def test_harness_process_still_sees_its_tokens(self):
        # mention_tools and integration recipes read os.environ in-process.
        self.assertEqual(os.environ["DEMO_SLACK_BOT_TOKEN"], SECRET)

    def test_file_keys_and_secret_shapes_are_dropped(self):
        os.environ.update({
            "GITHUB_TOKEN": "ghp_x", "GH_TOKEN": "ghp_y", "LINEAR_API_KEY": "lin",
            "JIRA_URL": "https://jira", "SOME_PASSWORD": "pw", "ANTHROPIC_API_KEY": "sk",
            "WORLD_FOO_TOKEN": "t",
        })
        for provider in ("claude", "codex"):
            child = world_env.child_env(provider, {"WORLD_AGENT": "watch"})
            for key in ("DEMO_SLACK_BOT_TOKEN", "DEMO_SLACK_CHANNEL", "RELAY_SECRET",
                        "APPROVER_SLACK_ID", "GITHUB_TOKEN", "GH_TOKEN", "LINEAR_API_KEY",
                        "JIRA_URL", "SOME_PASSWORD", "ANTHROPIC_API_KEY", "WORLD_FOO_TOKEN"):
                self.assertNotIn(key, child, (provider, key))
            self.assertNotIn(SECRET, json.dumps(child))
            self.assertEqual(child["PATH"], os.environ["PATH"])
            self.assertEqual(child.get("HOME"), os.environ.get("HOME"))  # none on Windows
            self.assertEqual(child["WORLD_AGENT"], "watch")
            self.assertEqual(child["WORLD_ENV_FILE"], str(self.env_file))

    def test_provider_login_variables_are_kept_only_for_their_provider(self):
        os.environ.update({
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "CLAUDE_CONFIG_DIR": "/c",
            "ANTHROPIC_BASE_URL": "https://gw", "CODEX_HOME": "/x",
            "OPENAI_API_KEY": "sk-openai", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        })
        claude = world_env.child_env("claude")
        codex = world_env.child_env("codex")
        self.assertEqual(claude["CLAUDE_CODE_OAUTH_TOKEN"], "oauth")
        self.assertEqual(claude["ANTHROPIC_BASE_URL"], "https://gw")
        self.assertNotIn("OPENAI_API_KEY", claude)
        self.assertNotIn("CODEX_HOME", claude)
        self.assertEqual(codex["CODEX_HOME"], "/x")
        self.assertEqual(codex["OPENAI_API_KEY"], "sk-openai")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", codex)
        self.assertEqual(codex["LC_ALL"], "C.UTF-8")

    def test_cloud_credentials_follow_only_a_claude_switched_to_that_cloud(self):
        aws = {"AWS_REGION": "us-east-1", "AWS_SECRET_ACCESS_KEY": "aws-secret"}
        gcp = {"GOOGLE_APPLICATION_CREDENTIALS": "/gcp.json", "CLOUD_ML_REGION": "us-east5"}
        plain = world_env.child_env("claude", environ={**aws, **gcp})
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", plain)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", plain)
        bedrock = world_env.child_env(
            "claude", environ={**aws, **gcp, "CLAUDE_CODE_USE_BEDROCK": "1"})
        self.assertEqual(bedrock["AWS_SECRET_ACCESS_KEY"], "aws-secret")
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", bedrock)
        vertex = world_env.child_env(
            "claude", environ={**aws, **gcp, "CLAUDE_CODE_USE_VERTEX": "1"})
        self.assertEqual(vertex["GOOGLE_APPLICATION_CREDENTIALS"], "/gcp.json")
        self.assertEqual(vertex["CLOUD_ML_REGION"], "us-east5")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", vertex)
        off = world_env.child_env("claude", environ={**aws, "CLAUDE_CODE_USE_BEDROCK": "0"})
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", off)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY",
                         world_env.child_env("codex", environ={**aws, "CLAUDE_CODE_USE_BEDROCK": "1"}))

    def test_windows_process_essentials_are_kept(self):
        win = {"SYSTEMROOT": r"C:\Windows", "APPDATA": r"C:\Users\u\AppData\Roaming",
               "TEMP": r"C:\Temp", "PATHEXT": ".COM;.EXE"}
        for provider in ("claude", "codex"):
            self.assertEqual(win, {k: v for k, v in world_env.child_env(provider, environ=win).items()
                                   if k in win})

    def test_sdk_env_blanks_everything_child_env_drops(self):
        merged = {**os.environ, **world_env.sdk_env("claude", {"WORLD_AGENT": "w"})}
        self.assertEqual(merged["DEMO_SLACK_BOT_TOKEN"], "")
        self.assertEqual(merged["RELAY_SECRET"], "")
        self.assertEqual(merged["PATH"], os.environ["PATH"])
        self.assertEqual(merged["WORLD_AGENT"], "w")


class RunnerChildEnvTests(LoadedEnvCase):
    def test_claude_options_env_hides_tokens_after_sdk_merge(self):
        from tests.test_claude_contract import install_fake_sdk

        install_fake_sdk()
        from world.runners import claude

        harness = SimpleNamespace(
            name="h", git_root=Path("/repo"), model="sonnet", plugins=[], log=lambda _: None
        )
        options = claude._build_options(harness, agent(allow_bash=True))
        # claude_agent_sdk builds the child env as {**os.environ, ..., **options.env}.
        child = {**os.environ, **options.env}
        self.assertEqual(child["DEMO_SLACK_BOT_TOKEN"], "")
        self.assertNotIn(SECRET, json.dumps(child))
        self.assertEqual(child["PATH"], os.environ["PATH"])
        self.assertEqual(child.get("HOME"), os.environ.get("HOME"))  # none on Windows
        self.assertEqual(child["WORLD_AGENT"], "watch")
        self.assertEqual(child["WORLD_HEADLESS"], "1")


class CodexRunnerChildEnvTests(LoadedEnvCase):
    @unittest.skipIf(os.name == "nt", "the fake CLI is a POSIX shell script; Linux and macOS run this")
    def test_codex_child_process_sees_no_harness_token(self):
        out = Path(self.dir.name) / "child-env.json"
        fake = Path(self.dir.name) / "fake-codex"
        fake.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            f"{sys.executable} -c 'import json,os,sys; json.dump(dict(os.environ), "
            f"open(sys.argv[1], \"w\"))' {out}\n"
            "printf '%s\\n' "
            "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"ok\"}}'\n"
        )
        fake.chmod(0o700)
        harness = SimpleNamespace(git_root=Path(self.dir.name), home=Path(self.dir.name),
                                  codex_model=None)
        import asyncio

        result = asyncio.run(
            CodexRunner(harness, executable=str(fake)).run(agent(allow_bash=True), "hi")
        )
        self.assertEqual(result.status, "ok")
        child = json.loads(out.read_text())
        self.assertNotIn("DEMO_SLACK_BOT_TOKEN", child)
        self.assertNotIn("RELAY_SECRET", child)
        self.assertNotIn(SECRET, json.dumps(child))
        self.assertIn("PATH", child)
        self.assertIn("HOME", child)
        self.assertEqual(child["WORLD_AGENT"], "watch")
        self.assertEqual(child["WORLD_HEADLESS"], "1")


REAL_SDK_PROBE = textwrap.dedent(
    """
    import asyncio, json, os, sys
    from pathlib import Path
    from types import SimpleNamespace
    os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
    from world import env as world_env           # import world loads WORLD_ENV_FILE
    from claude_agent_sdk import query
    from world.runners import claude
    from world.types import Agent, PollTrigger
    harness = SimpleNamespace(name="h", git_root=Path.cwd(), model="sonnet", plugins=[],
                              log=lambda _: None)
    options = claude._build_options(harness, Agent("watch", PollTrigger(lambda *_: None)))
    options.cli_path = sys.argv[1]
    async def main():
        try:
            async for _ in query(prompt="hi", options=options):
                pass
        except Exception:
            pass
    asyncio.run(main())
    """
)


class RealClaudeSdkTests(unittest.TestCase):
    """Run the INSTALLED claude_agent_sdk against a fake `claude` binary that dumps its
    environment, so the assertion is about what the SDK really spawns. A separate
    interpreter, because test_claude_contract replaces the SDK module in-process."""

    @unittest.skipIf(os.name == "nt", "the fake CLI is a POSIX shell script; Linux and macOS run this")
    def test_real_sdk_child_sees_no_harness_token(self):
        try:
            import importlib.util

            if importlib.util.find_spec("claude_agent_sdk") is None:
                self.skipTest("claude_agent_sdk not installed")
        except ValueError:
            pass  # replaced in sys.modules by a fake; the subprocess uses the real one
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = root / "harness.env"
            env_file.write_text(f"DEMO_SLACK_BOT_TOKEN={SECRET}\nRELAY_SECRET=hmac\n")
            env_file.chmod(0o600)
            out = root / "child-env.json"
            fake = root / "claude"
            fake.write_text(
                "#!/bin/sh\n"
                f"{sys.executable} -c 'import json,os,sys; json.dump(dict(os.environ), "
                f"open(sys.argv[1], \"w\"))' {out}\n"
                "exit 1\n"
            )
            fake.chmod(0o700)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("DEMO_SLACK_BOT_TOKEN", "RELAY_SECRET")}
            env.update({"WORLD_ENV_FILE": str(env_file), "PYTHONPATH": str(KIT)})
            subprocess.run(
                [sys.executable, "-c", REAL_SDK_PROBE, str(fake)],
                cwd=raw, env=env, timeout=60, capture_output=True,
            )
            self.assertTrue(out.exists(), "fake claude CLI was never spawned")
            child = json.loads(out.read_text())
        self.assertNotIn(SECRET, json.dumps(child))
        self.assertIn(child.get("DEMO_SLACK_BOT_TOKEN", ""), ("",))
        self.assertIn(child.get("RELAY_SECRET", ""), ("",))
        self.assertEqual(child["WORLD_AGENT"], "watch")
        # kept keys ride the SDK merge, so the SDK's own value still wins
        self.assertEqual(child["CLAUDE_CODE_ENTRYPOINT"], "sdk-py")
        self.assertTrue(child.get("PATH"))
        self.assertTrue(child.get("HOME"))


@unittest.skipIf(os.name == "nt", "POSIX modes; Windows ACLs are tests/test_windows_env.py")
class PermissionWarningTests(unittest.TestCase):
    def _load(self, mode: int) -> str:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "harness.env"
            path.write_text(f"PERMTEST_SLACK_BOT_TOKEN={SECRET}\n")
            path.chmod(mode)
            err = io.StringIO()
            with patch.dict(os.environ, {"WORLD_ENV_FILE": str(path)}), \
                    contextlib.redirect_stderr(err):
                os.environ.pop("PERMTEST_SLACK_BOT_TOKEN", None)
                world_env.load_harness_env()
            world_env._LOADED.pop("PERMTEST_SLACK_BOT_TOKEN", None)
            world_env._FILE_KEYS.discard("PERMTEST_SLACK_BOT_TOKEN")
            return err.getvalue()

    def test_group_readable_file_warns_without_values(self):
        text = self._load(0o644)
        self.assertIn("chmod 600 env/harness.env", text)
        self.assertIn("harness.env", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_owner_only_file_is_quiet(self):
        self.assertEqual(self._load(0o600), "")


class RedactTests(unittest.TestCase):
    def test_masks_known_token_shapes(self):
        text = (
            f"auth failed {SECRET} app xapp-1-A0-123-abc user xoxp-9-9-9-zz "
            "gh ghp_abcdefghijklmnopqrstuvwxyz0123 pat github_pat_11ABCDEFG0123456789_abcdefXYZ "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        )
        out = world_env.redact(text)
        for secret in (SECRET, "xapp-1-A0-123-abc", "xoxp-9-9-9-zz",
                       "ghp_abcdefghijklmnopqrstuvwxyz0123", "github_pat_11ABCDEFG0123456789",
                       "eyJhbGciOiJIUzI1NiJ9"):
            self.assertNotIn(secret, out)
        self.assertIn("auth failed", out)
        self.assertIn("[redacted]", out)

    def test_masks_provider_cloud_and_discord_keys_and_credentials_in_urls(self):
        # Shapes a report or a fetch log carries, which a file-mode card can show.
        # Random test data in the Discord bot token's shape: base64 of an 18-digit ID,
        # 6 characters, then 27.
        snowflake = str(secrets.randbelow(9 * 10**17) + 10**17).encode()
        discord = ".".join((base64.urlsafe_b64encode(snowflake).decode().rstrip("="),
                            secrets.token_urlsafe(6)[:6], secrets.token_urlsafe(27)[:27]))
        aws = "AKIA" + secrets.token_hex(8).upper()
        text = (f"anthropic sk-ant-api03-{'a' * 40} openai sk-proj-{'b' * 30} "
                f"aws {aws} discord {discord} "
                "GET https://api.example.com/v1?token=abc123&page=2 login password=hunter2")
        out = world_env.redact_file(text)
        for secret in ("a" * 40, "b" * 30, aws[5:], discord[5:],
                       "abc123", "hunter2"):
            self.assertNotIn(secret, out)
        # The name of what was masked stays, so the log still reads.
        self.assertIn("?token=[redacted]&page=2", out)
        self.assertIn("password=[redacted]", out)

    def test_masks_every_anthropic_key_kind(self):
        # Claude Code's own OAuth token and admin keys, not only API keys.
        for kind in ("api03", "oat01", "admin01"):
            key = "sk-" + f"ant-{kind}-" + secrets.token_urlsafe(40)
            with self.subTest(kind=kind):
                out = world_env.redact_file(f"login {key}.")
                self.assertNotIn(key[5:], out)
                self.assertEqual("login sk-an…[redacted].", out)

    def test_masks_the_values_of_token_and_key_variables(self):
        with patch.dict(os.environ, {"WEATHER_API_KEY": "wk-1234567890", "BOT_TOKEN": "tk-abcdefgh"}):
            out = world_env.redact_file("key wk-1234567890 token tk-abcdefgh")
        self.assertEqual("key [redacted] token [redacted]", out)

    def test_result_redaction_preserves_main_behavior(self):
        texts = ("Open briefing/report.md", "Example token=placeholder is documentation",
                 "sk-" + "this-is-a-normal-document-title", "password=hunter2, retry=3")
        with patch.dict(os.environ, {"DEMO_KEY": "briefing"}, clear=True):
            for text in texts:
                self.assertEqual(text, world_env.redact(text))

    def test_file_redaction_keeps_normal_text_and_separators(self):
        for value in ("briefing", "/srv/app", "https://example.com"):
            with patch.dict(os.environ, {"DEMO_KEY": value}, clear=True):
                for text in ("Open briefing/report.md", "Path /srv/app/reports.md",
                             "URL https://example.com/path", "sk-" + "this-is-a-normal-document-title",
                             "Example token=placeholder is documentation"):
                    self.assertEqual(text, world_env.redact_file(text))
                self.assertEqual("[redacted]", world_env.redact_file(value))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("password=[redacted], retry=3",
                             world_env.redact_file("password=hunter2, retry=3"))
            self.assertEqual("password=[redacted].", world_env.redact_file("password=hunter2."))
            self.assertEqual("?token=[redacted]#section",
                             world_env.redact_file("?token=abc#section"))

    def test_file_redaction_keeps_documentation_references_readable(self):
        with patch.dict(os.environ, {}, clear=True):
            for text in ("api_key=${API_KEY}", "token=$TOKEN", "token=YOUR_TOKEN",
                         "api_key=your-api-key", "token=xxx", "password=XXXXXXXX"):
                with self.subTest(text=text):
                    self.assertEqual(text, world_env.redact_file(text))
            # A real value that merely starts with $ or x is still masked.
            for text in ("password=$ecret123", "token=xA9fQ2"):
                with self.subTest(text=text):
                    self.assertEqual(text.split("=")[0] + "=[redacted]", world_env.redact_file(text))

    def test_file_redaction_masks_everything_result_redaction_masks(self):
        # A file excerpt must never show a secret its result card would hide.
        snowflake = str(secrets.randbelow(9 * 10**17) + 10**17).encode()
        discord = ".".join((base64.urlsafe_b64encode(snowflake).decode().rstrip("="),
                            secrets.token_urlsafe(6)[:6], secrets.token_urlsafe(27)[:27]))
        value = "hunter2hunter2"
        texts = (f"Use {value}.", f"SIGNING_SECRET:{value}", f"https://hooks.x/{value}/run",
                 f"secret {value}-rotated", f"ok {value} ok", f"{SECRET}.", f"({SECRET})")
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": value}, clear=True):
            for text in texts:
                with self.subTest(text=text):
                    result = world_env.redact(text)
                    out = world_env.redact_file(text)
                    self.assertNotIn(value, out)
                    for kept in (value, SECRET[5:]):
                        if kept not in result:
                            self.assertNotIn(kept, out)
        with patch.dict(os.environ, {"GH_TOKEN": value}, clear=True):
            self.assertEqual("Use [redacted].", world_env.redact_file(f"Use {value}."))
            self.assertEqual("GH_TOKEN:[redacted]", world_env.redact_file(f"GH_TOKEN:{value}"))
            self.assertEqual("https://api.x/[redacted]", world_env.redact_file(f"https://api.x/{value}"))
        with patch.dict(os.environ, {}, clear=True):
            out = world_env.redact_file(f"Token was {discord}.")
            self.assertNotIn(discord[5:], out)
            self.assertTrue(out.endswith("[redacted]."))
            # A token redact() already shortened keeps that form under a name.
            self.assertEqual(f"token={SECRET[:5]}…[redacted]", world_env.redact_file(f"token={SECRET}"))

    def test_leaves_ordinary_text_alone(self):
        text = "rate limit (429) while reading C0123 / U0456 — retry later"
        self.assertEqual(world_env.redact(text), text)


if __name__ == "__main__":
    unittest.main()
