#!/usr/bin/env python3
"""slack-provision — create the harness's Slack app (and any persona that wants its own)
with the Slack CLI and put the tokens into env/harness.env.

One app is the default: the main persona's. Every other persona posts through it under its
own name and icon (chat:write.customize, see world/slack/post.py), so the spec usually
lists only the main app. List another persona only to give it its own Slack app, with its
own profile; it counts against a free workspace's 10 apps.

    uv run python bin/slack-provision.py                 # every app in the spec
    uv run python bin/slack-provision.py --team T0123ABC # pick the workspace up front
    uv run python bin/slack-provision.py --only watcher  # one app (e.g. after /recruit)
    uv run python bin/slack-provision.py --dry-run       # write the projects, run nothing

Needs the Slack CLI (`slack`, https://docs.slack.dev/tools/slack-cli) and one
`slack login` to the workspace. Nothing else: no browser, no copy-pasting tokens.

WHY `slack deploy` AND NOT `slack run`

The harness is one process holding N identities (one Socket Mode main persona, and
any posting-only sub-personas that have an app of their own). `slack run` serves ONE app, appends
"(local)" to its name and bot name, and only lives as long as the CLI does. So the CLI is
used to provision, never to run: each app gets a tiny project under `slack-app/<key>/`
whose `deploy` hook is this script in `--capture` mode. `slack deploy` creates the app
from `manifest.json` (or updates it), installs it to the workspace with the CLI's own
login (no OAuth click), and runs the deploy hook with the fresh SLACK_BOT_TOKEN and
SLACK_APP_TOKEN in its environment — the hooks reference documents this
(docs.slack.dev/tools/slack-cli/reference/hooks/#deploy). The hook writes them into
env/harness.env and exits; the CLI exits with it. harness.py then runs as before.

THE SPEC — slack-app/slack-apps.json (written by /the-world:start, yours to edit)

    {
      "prefix": "Obs",                       # app name = "<prefix>-<display>"
      "description": "One line in the world's voice",
      "team": "T0123ABC",                    # optional; else the only logged-in one, else the CLI asks
      "apps": [
        {"key": "lumen", "display": "Lumen", "main": true,
         "bot_token_env": "OBS_SLACK_BOT_TOKEN", "app_token_env": "OBS_SLACK_APP_TOKEN",
         "icon": "portraits/lumen.png",       # optional, relative to the harness
         "bot_username": "obs-lumen"},        # optional; needed only when the app name
                                              # is not ASCII (see bot_username below)
        {"key": "watcher", "display": "Watcher",
         "bot_token_env": "OBS_PERSONA_WATCHER_TOKEN"}
      ]
    }

Exactly one app is `main`: it gets the full manifest from
templates/slack-manifest.json.tmpl (events, Socket Mode, interactivity). Every other app
is posting-only: chat:write + reactions:write, no events, no socket.

RE-RUNS

An app whose project already records a deployed app (`slack-app/<key>/.slack/apps.json`)
AND whose token variables are already filled in env/harness.env is skipped. Anything
else is deployed again, which reuses the recorded app rather than creating a second one
— but only while that apps.json is there. It is the CLI's only record of which app this
project is; delete `slack-app/<key>/.slack/` (or the harness folder) and the next deploy
creates a new app. Keep it, or put it back, to keep the app.
`--force` re-deploys regardless (e.g. after a manifest change). A redeploy passes
`--manifest-source local`: the manifest rendered from the spec overwrites the app's
settings, including any changed by hand at api.slack.com. env/harness.env is
updated in place: other lines are kept, the file stays mode 600, and no token value is
ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "slack-manifest.json.tmpl"
DEFAULT_SPEC = ROOT / "slack-app" / "slack-apps.json"

# Variables the CLI would otherwise inherit from the launching shell. setAppEnvironmentTokens
# in the CLI keeps an already-exported SLACK_BOT_TOKEN rather than the new app's, so a stray
# one would be captured as this app's token. They never reach the CLI.
SCRUBBED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CLI_XOXB", "SLACK_CLI_XAPP",
                "SLACK_USER_TOKEN")
POSTING_ONLY_SCOPES = ["chat:write", "reactions:write"]
KEY_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
ENV_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
APP_NAME_MAX = 35  # Slack's limit for display_information.name


class SpecError(ValueError):
    pass


# ── spec ─────────────────────────────────────────────────────────────────────────────

def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"no spec at {path} — write it first (see this script's docstring)")
    except json.JSONDecodeError as e:
        raise SpecError(f"{path} is not valid JSON: {e}")
    apps = spec.get("apps")
    if not isinstance(apps, list) or not apps:
        raise SpecError("spec needs a non-empty \"apps\" list")
    if sum(bool(a.get("main")) for a in apps) != 1:
        raise SpecError("exactly one app must be \"main\": true (the Socket Mode owner)")
    seen_keys: set[str] = set()
    seen_env: set[str] = set()
    for app in apps:
        key = app.get("key", "")
        if not KEY_RE.fullmatch(key):
            raise SpecError(f"app key {key!r} must be lowercase letters/digits, - or _")
        if key in seen_keys:
            raise SpecError(f"duplicate app key {key!r}")
        seen_keys.add(key)
        if not app.get("display"):
            raise SpecError(f"{key}: \"display\" is required")
        names = [app.get("bot_token_env", "")]
        if app.get("main"):
            names.append(app.get("app_token_env", ""))
        for name in names:
            if not ENV_RE.fullmatch(name or ""):
                raise SpecError(f"{key}: token variable {name!r} must look like OBS_SLACK_BOT_TOKEN"
                                + (" (main needs bot_token_env and app_token_env)" if app.get("main") else ""))
            if name in seen_env:
                raise SpecError(f"{key}: token variable {name} is used twice")
            seen_env.add(name)
        if len(app_name(spec, app)) > APP_NAME_MAX:
            raise SpecError(f"{key}: app name {app_name(spec, app)!r} is longer than "
                            f"{APP_NAME_MAX} characters, Slack's limit")
    apps.sort(key=lambda a: not a.get("main"))  # main first: it is the one people talk to
    return spec


def app_name(spec: dict, app: dict) -> str:
    prefix = spec.get("prefix", "")
    return f"{prefix}-{app['display']}" if prefix else app["display"]


def token_envs(app: dict) -> list[str]:
    return [app["bot_token_env"]] + ([app["app_token_env"]] if app.get("main") else [])


# ── manifest ─────────────────────────────────────────────────────────────────────────

# The app's name may be in any script ("Obs-관측원"); the bot user's display_name
# may not. Slack refuses the whole manifest with `invalid_manifest` / `bad_username` at
# /features/bot_user/display_name, after the user has already logged in and picked a
# workspace. So a name outside this set is replaced before the CLI ever sees it.
BOT_USERNAME = re.compile(r"[A-Za-z0-9._-]{1,80}")


def bot_username(spec: dict, app: dict, name: str) -> str:
    """`bot_username` from the spec, else the app name if Slack accepts it, else the
    prefix lowercased (`@lighthouse`): the main app is the harness, so the harness's name
    is the handle people type. A persona with an app of its own gets `<prefix>-<key>`."""
    explicit = app.get("bot_username")
    if explicit is None and BOT_USERNAME.fullmatch(name):
        return name
    if explicit is None:
        prefix = re.sub(r"[^a-z0-9._-]", "", spec.get("prefix", "").lower())
        parts = (prefix,) if app.get("main") and prefix else (prefix, app["key"].lower())
        explicit = "-".join(p for p in parts if p)[:21]
    if not BOT_USERNAME.fullmatch(explicit) or explicit.lower() == "slackbot":
        raise SpecError(
            f"{app['key']}: bot_username {explicit!r} must be ASCII letters, digits, '.', '_' "
            "or '-' (Slack rejects any other bot display_name)")
    return explicit


def render_manifest(template: str, spec: dict, app: dict) -> dict:
    def j(value: str) -> str:  # a value spliced into a JSON string literal
        return json.dumps(value, ensure_ascii=False)[1:-1]

    name = app_name(spec, app)
    description = spec.get("description") or f"{name} — a World harness bot"
    text = template.replace("{{Prefix}}-{{MainPersona}}", j(name))
    text = text.replace("{{Prefix}}", j(spec.get("prefix", ""))).replace("{{MainPersona}}", j(app["display"]))
    text = re.sub(r"\{\{[^}]*\}\}", j(description), text)
    manifest = json.loads(text)
    manifest.pop("_comment", None)
    manifest["features"]["bot_user"]["display_name"] = bot_username(spec, app, name)
    if not app.get("main"):
        manifest["oauth_config"]["scopes"]["bot"] = list(POSTING_ONLY_SCOPES)
        settings = manifest.setdefault("settings", {})
        settings.pop("event_subscriptions", None)
        settings.pop("interactivity", None)
        settings["socket_mode_enabled"] = False
    return manifest


def hook_command(spec_path: Path, env_file: Path, key: str) -> str:
    # The CLI runs a hook string through $SHELL -c (powershell on Windows).
    parts = [sys.executable, str(Path(__file__).resolve()), "--capture", key,
             "--spec", str(spec_path), "--env-file", str(env_file)]
    if sys.platform == "win32":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(p) for p in parts)


def write_project(project: Path, manifest: dict, hook: str, icon: Path | None) -> None:
    (project / ".slack").mkdir(parents=True, exist_ok=True)
    (project / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hooks = {"runtime": "python", "hooks": {"deploy": hook}}
    (project / ".slack" / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    config = project / ".slack" / "config.json"
    if not config.exists():  # the CLI adds its project_id here; keep it on re-runs
        config.write_text(json.dumps({"manifest": {"source": "local"}}, indent=2) + "\n", encoding="utf-8")
    if icon is not None:
        (project / "assets").mkdir(exist_ok=True)
        for old in (project / "assets").glob("icon.*"):
            old.unlink()
        shutil.copyfile(icon, project / "assets" / f"icon{icon.suffix.lower()}")


def deployed_app_ids(project: Path) -> list[str]:
    try:
        data = json.loads((project / ".slack" / "apps.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [a["app_id"] for a in (data.get("apps") or {}).values()
            if isinstance(a, dict) and a.get("app_id")]


# ── env file ─────────────────────────────────────────────────────────────────────────

def default_env_file() -> Path:
    override = os.environ.get("WORLD_ENV_FILE")
    return Path(override) if override else ROOT / "env" / "harness.env"


def _value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return re.sub(r"\s+#.*$", "", value).strip()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, raw = line.partition("=")
            values[key.strip()] = _value(raw)
    return values


def is_filled(value: str | None) -> bool:
    return bool(value) and "..." not in value


def restrict_env_file(path: Path) -> None:
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        sid_text = win32security.ConvertSidToStringSid(sid)
    finally:
        token.Close()
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;FA;;;{sid_text})", win32security.SDDL_REVISION_1)
    win32security.SetFileSecurity(str(path),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor)


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value for each update, in place; every other line is kept as it was."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# written by bin/slack-provision.py")
        lines.extend(f"{k}={v}" for k, v in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".harness.env.")
    os.close(fd)
    try:
        restrict_env_file(Path(tmp))
        with Path(tmp).open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    restrict_env_file(path)


# ── capture (run BY the Slack CLI as the deploy hook) ───────────────────────────────

def capture(key: str, spec_path: Path, env_file: Path) -> int:
    spec = load_spec(spec_path)
    app = next((a for a in spec["apps"] if a["key"] == key), None)
    if app is None:
        print(f"slack-provision: {key} is not in {spec_path}", file=sys.stderr)
        return 1
    bot = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot.startswith("xoxb-"):
        print("slack-provision: the CLI handed the deploy hook no bot token "
              "(is chat:write in the manifest?)", file=sys.stderr)
        return 1
    updates = {app["bot_token_env"]: bot}
    if app.get("main"):
        xapp = os.environ.get("SLACK_APP_TOKEN", "")
        if xapp.startswith("xapp-"):
            updates[app["app_token_env"]] = xapp
        else:
            print(f"slack-provision: no app-level token came back for {key}; "
                  f"{app['app_token_env']} still needs one (see the summary)", file=sys.stderr)
    upsert_env(env_file, updates)
    print(f"slack-provision: wrote {', '.join(updates)} to {env_file}")
    return 0


# ── provision ────────────────────────────────────────────────────────────────────────

TEAM_ID = re.compile(r"Team ID:\s*(T[A-Z0-9]+)")


def logged_in_teams(slack: str) -> list[str]:
    """Workspace IDs `slack auth list` knows, in its order; [] when it cannot say."""
    try:
        out = subprocess.run([slack, "auth", "list"], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    return list(dict.fromkeys(TEAM_ID.findall(out.stdout + out.stderr)))


def has_terminal() -> bool:
    """Whether a person can answer a prompt on stdin. On Windows `isatty()` is also true
    for NUL, which is what an agent's shell or Claude Code's `!` hands a child, so a real
    console is told apart by having a size."""
    if not sys.stdin.isatty():
        return False
    if os.name == "nt":
        try:
            os.get_terminal_size(sys.stdin.fileno())
        except OSError:
            return False
    return True


def resolve_team(team: str | None, slack: str | None) -> str | None:
    """The workspace to deploy to; SpecError when none can be chosen.

    Without a team the CLI asks "Select an app" on the terminal, and a run
    with no terminal (an agent's shell, Claude Code's `!`) dies on that prompt with
    prompt_error. So off a terminal one logged-in workspace is taken as the answer, and
    several are an error that names them."""
    if team or slack is None or has_terminal():
        return team
    teams = logged_in_teams(slack)
    if len(teams) == 1:
        return teams[0]
    found = ", ".join(teams) if teams else "none found by `slack auth list`"
    raise SpecError("no workspace chosen and no terminal for the CLI to ask on "
                    f"(logged in: {found}). Add \"team\": \"T…\" to the spec or pass --team T…")


def deploy_flags(slack: str) -> set[str]:
    """The long flags `slack deploy --help` lists; empty when it cannot say. Asked rather
    than assumed because the kit does not pin a CLI version, and an older CLI refuses
    an unknown flag outright (the whole deploy fails, not just the flag)."""
    try:
        out = subprocess.run([slack, "deploy", "--help"], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"--[a-z][a-z-]+", out.stdout + out.stderr))


def selected_apps(spec: dict, only: list[str] | None) -> list[dict]:
    if not only:
        return spec["apps"]
    unknown = set(only) - {a["key"] for a in spec["apps"]}
    if unknown:
        raise SpecError(f"not in the spec: {', '.join(sorted(unknown))}")
    return [a for a in spec["apps"] if a["key"] in only]


def manual_fallback() -> str:
    return ("Manual path: paste templates/slack-manifest.json.tmpl (filled in) at "
            "https://api.slack.com/apps → Create New App → From a manifest, install it, "
            "and copy the xoxb/xapp tokens into env/harness.env.")


def provision(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec).resolve()
    env_file = Path(args.env_file).resolve() if args.env_file else default_env_file().resolve()
    slack = shutil.which("slack")
    try:
        spec = load_spec(spec_path)
        apps = selected_apps(spec, args.only)
        team = resolve_team(args.team or spec.get("team"), None if args.dry_run else slack)
    except SpecError as e:
        print(f"slack-provision: {e}", file=sys.stderr)
        return 2
    if slack is None and not args.dry_run:
        print("slack-provision: the Slack CLI (`slack`) is not on PATH.\n"
              "  Install: https://docs.slack.dev/tools/slack-cli/guides/installing-the-slack-cli-for-mac-and-linux\n"
              "  Then: slack login\n  " + manual_fallback(), file=sys.stderr)
        return 2

    template = TEMPLATE.read_text(encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    results: list[tuple[str, str]] = []
    failed = False
    for app in apps:
        key = app["key"]
        project = spec_path.parent / key
        icon = None
        if app.get("icon"):
            icon = (ROOT / app["icon"]).resolve()
            if not icon.is_file():
                print(f"slack-provision: {key}: icon {icon} not found", file=sys.stderr)
                return 2
        write_project(project, render_manifest(template, spec, app),
                      hook_command(spec_path, env_file, key), icon)
        have = read_env(env_file)
        filled = all(is_filled(have.get(name)) for name in token_envs(app))
        if deployed_app_ids(project) and filled and not args.force:
            results.append((key, "already provisioned — skipped (--force to redeploy)"))
            continue
        command = [slack or "slack", "deploy", "--hide-triggers", "--skip-update"]
        if team:
            command += ["--team", team]
        if deployed_app_ids(project):
            # A redeploy. Slack adds settings of its own to a live app
            # (app_level_token_rotation_enabled, is_mcp_enabled — found live), so the
            # remote manifest drifts from ours and the CLI asks which one wins. Off a
            # terminal it cannot ask and fails with app_manifest_update_error. The
            # project manifest is the source of truth here: it is what this script
            # renders from the spec on every run.
            if args.dry_run or "--manifest-source" in deploy_flags(slack or "slack"):
                command += ["--manifest-source", "local"]
            else:
                print(f"slack-provision: {key}: this Slack CLI has no --manifest-source; if "
                      "the app's settings drifted, the redeploy asks on the terminal "
                      "(or fails without one) — `slack upgrade` avoids that.", file=sys.stderr)
        if args.dry_run:
            results.append((key, f"dry run — would run `{' '.join(command)}` in {project}"))
            continue
        print(f"\n── {app_name(spec, app)} ({key}) ──", flush=True)
        # Interactive on purpose: the CLI may ask which workspace, or whether to send an
        # admin-approval request. Its stdin/stdout are the user's terminal.
        proc = subprocess.run(command, cwd=project, env=child_env)
        have = read_env(env_file)
        missing = [name for name in token_envs(app) if not is_filled(have.get(name))]
        if proc.returncode != 0:
            failed = True
            results.append((key, f"`slack deploy` failed (exit {proc.returncode})"))
        elif app["bot_token_env"] in missing:
            failed = True
            results.append((key, "no token captured — if the CLI said an admin approval request "
                                 "is pending, re-run this after it is approved"))
        elif missing:
            ids = deployed_app_ids(project)
            where = f"https://api.slack.com/apps/{ids[0]}/general" if ids else "the app's Basic Information page"
            results.append((key, f"bot token written; {missing[0]} still missing — generate an "
                                 f"App-Level Token with connections:write at {where} and put it "
                                 f"in env/harness.env"))
        else:
            results.append((key, f"wrote {', '.join(token_envs(app))}"))

    print("\nslack-provision summary")
    for key, status in results:
        print(f"  {key}: {status}")
    print(f"  env file: {env_file}")
    print("  Next: uv run python bin/slack-pair.py --prefix <PREFIX>, then say @<bot> 안녕 in "
          "the harness channel; it writes the channel ID and APPROVER_SLACK_ID for you.")
    if failed:
        print("  " + manual_fallback())
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the harness's Slack apps with the Slack CLI.")
    parser.add_argument("--spec", default=str(DEFAULT_SPEC), help="default: slack-app/slack-apps.json")
    parser.add_argument("--env-file", help="default: env/harness.env (or $WORLD_ENV_FILE)")
    parser.add_argument("--team", help="workspace ID or domain; otherwise the spec's, else the only logged-in one")
    parser.add_argument("--only", action="append", metavar="KEY", help="provision just this app (repeatable)")
    parser.add_argument("--force", action="store_true", help="redeploy apps that already have tokens")
    parser.add_argument("--dry-run", action="store_true", help="write the app projects, run nothing")
    parser.add_argument("--capture", metavar="KEY", help=argparse.SUPPRESS)  # the deploy hook
    args = parser.parse_args(argv)
    if args.capture:
        env_file = Path(args.env_file) if args.env_file else default_env_file()
        try:
            return capture(args.capture, Path(args.spec), env_file)
        except SpecError as e:
            print(f"slack-provision: {e}", file=sys.stderr)
            return 1
    return provision(args)


if __name__ == "__main__":
    raise SystemExit(main())
