#!/usr/bin/env python3
"""ops-kit — render an opt-in launcher and watchdog for running this harness all the time.

    uv run python bin/ops-kit.py                       # scripts/<name>-up, -watchdog, unit
    uv run python bin/ops-kit.py --notify --prefix OBS # also post restarts to Slack
    uv run python bin/ops-kit.py --keep-days 30        # delete rolled console logs after 30 days

The kit sets up no service: a harness runs while `harness.py` runs and a reboot stops it.
This is for a harness that has to outlive that. It WRITES files only; it never loads,
enables or starts anything. What it writes, under the harness's own `scripts/`:

  scripts/<name>-up               up | down | restart | status | running | healthy | attach
  scripts/<name>-watchdog         one round: restart the harness if it died or lost its
                                  console writer
  scripts/launchd/<label>.plist   macOS: run the watchdog every 120s and at login
  scripts/systemd/<name>-watchdog.service, .timer   Linux: the same as a user timer

and prints the commands that would register the watchdog, for a person to run. Windows
is not supported yet: a `windows` profile is refused, loudly, rather than given scripts
that would not run there. The scripts and their pitfalls are in docs/operations.md.

`--notify` is refused unless env/harness.env holds the main bot token and the channel,
since a watchdog that silently cannot post is worse than one that never claimed to.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parents[1]
KIT_TEMPLATES = ROOT / "templates" / "ops"
INTERVAL_S = 120
BACKOFF_MIN_S = 30
BACKOFF_MAX_S = 1800
PROVIDER_CLIS = {"claude": ("claude",), "codex": ("codex",), "both": ("claude", "codex")}


class OpsKitError(Exception):
    pass


def slug(name: str) -> str:
    """A name safe in a file name, a launchd label and a systemd unit name."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    s = re.sub(r"-{2,}", "-", s)  # `--` may not appear inside the plist's XML comment
    if not s:
        raise OpsKitError(f"--name {name!r} has no usable characters")
    return s


def ere_escape(text: str) -> str:
    """Escape for a POSIX extended regex (pgrep -f)."""
    return re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", text)


def process_match(home: Path) -> str:
    """pgrep -f pattern for this harness's process: a python interpreter whose first
    argument is exactly this harness.py. Not anchored on the venv's python: a macOS
    framework build re-executes `.venv/bin/python` as `…/Python.app/Contents/MacOS/Python`,
    so that path never appears in the process list. An editor or a pager open on the file
    does not match, and neither does another harness's harness.py."""
    return f"^(.*/)?[Pp]ython[0-9.]* {ere_escape(str(home / 'harness.py'))}( |$)"


def systemd_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    left = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if left:
        raise OpsKitError(f"unfilled placeholders: {sorted(set(left))}")
    return out


def check_os(os_name: str) -> None:
    if os_name not in ("macos", "linux"):
        raise OpsKitError(
            f"this harness's profile is os={os_name!r}; the ops kit renders launchd (macOS) "
            "and systemd (Linux) only. Windows Task Scheduler is not supported yet, so "
            "nothing was written. Run `uv run python harness.py` in a terminal that stays "
            "open, or register a scheduled task by hand.")


def render(home: Path, name: str, os_name: str, python: Path, run_path: str, *,
           notify: tuple[str, str] | None = None, keep_days: int | None = None,
           templates: Path = KIT_TEMPLATES) -> dict[str, tuple[str, int]]:
    """Every file to write, relative to `home`, as (content, mode). Pure: reads only the
    templates, so a test can render without a venv, a CLI or a service manager."""
    check_os(os_name)
    name = slug(name)
    home = home.resolve()
    up = home / "scripts" / f"{name}-up"
    watchdog = home / "scripts" / f"{name}-watchdog"
    label = f"local.the-world.{name}.watchdog"
    q = shlex.quote
    notify_flags = (f"--token-env {notify[0]} --channel-env {notify[1]}" if notify else "")
    shell = {
        "NAME": name,
        "HOME_DIR": q(str(home)),
        "PYTHON": q(str(python)),
        "UP": q(str(up)),
        "WATCHDOG": q(str(watchdog)),
        "MATCH": q(process_match(home)),
        "RUN_PATH": q(run_path),
        "CAFFEINATE": "1" if os_name == "macos" else "0",
        "INTERVAL_S": str(INTERVAL_S),
        "BACKOFF_MIN_S": str(BACKOFF_MIN_S),
        "BACKOFF_MAX_S": str(BACKOFF_MAX_S),
        "KEEP_DAYS": "" if keep_days is None else str(int(keep_days)),
        "NOTIFY": q(notify_flags) if notify else "",
        "NOTIFY_NOTE": " and to Slack" if notify else "",
    }
    read = lambda n: (templates / n).read_text(encoding="utf-8")  # noqa: E731
    files = {
        f"scripts/{name}-up": (fill(read("up.sh.tmpl"), shell), 0o755),
        f"scripts/{name}-watchdog": (fill(read("watchdog.sh.tmpl"), shell), 0o755),
    }
    if os_name == "macos":
        plist = f"{label}.plist"
        files[f"scripts/launchd/{plist}"] = (fill(read("launchd.plist.tmpl"), {
            "NAME": name, "LABEL": label, "PLIST_NAME": plist,
            "WATCHDOG": xml_escape(str(watchdog)), "INTERVAL_S": str(INTERVAL_S),
            "LAUNCHD_OUT": xml_escape(str(home / "logs" / "watchdog.launchd.out")),
        }), 0o644)
    else:
        unit = {"NAME": name, "WATCHDOG": systemd_quote(str(watchdog)),
                "INTERVAL_S": str(INTERVAL_S)}
        files[f"scripts/systemd/{name}-watchdog.service"] = (
            fill(read("systemd.service.tmpl"), unit), 0o644)
        files[f"scripts/systemd/{name}-watchdog.timer"] = (
            fill(read("systemd.timer.tmpl"), unit), 0o644)
    return files


def next_steps(home: Path, name: str, os_name: str) -> list[str]:
    name = slug(name)
    up = f"scripts/{name}-up"
    if os_name == "macos":
        label = f"local.the-world.{name}.watchdog"
        register = [
            f"cp scripts/launchd/{label}.plist ~/Library/LaunchAgents/",
            f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist",
        ]
        unregister = f"launchctl bootout gui/$(id -u)/{label}"
    else:
        register = [
            "mkdir -p ~/.config/systemd/user",
            f"cp scripts/systemd/{name}-watchdog.* ~/.config/systemd/user/",
            "systemctl --user daemon-reload",
            f"systemctl --user enable --now {name}-watchdog.timer",
            'loginctl enable-linger "$USER"   # keeps it running while logged out',
        ]
        unregister = f"systemctl --user disable --now {name}-watchdog.timer"
    return [
        f"Nothing is running or registered yet. From {home}:",
        f"  {up}            # start it in the background now",
        f"  {up} status",
        "To have the watchdog restart it after a crash or a reboot:",
        *[f"  {line}" for line in register],
        "To stop it for a while (the watchdog leaves it down until `up`):",
        f"  {up} down",
        "To take it down for good, remove the watchdog FIRST, then stop it:",
        f"  {unregister}",
        f"  {up} down",
    ]


def resolve_run_path(provider: str, shell: str | None = None) -> str:
    """The login shell's PATH with each agent CLI's directory first.

    A login shell, because that is where a person's PATH is set up, and launchd/systemd
    start the watchdog without it. Falls back to an interactive login shell, where zsh
    users usually add ~/.local/bin. A CLI that cannot be found is an error here, at
    render time, rather than every run failing later under the watchdog.
    """
    shell = shell or os.environ.get("SHELL") or "/bin/sh"
    clis = PROVIDER_CLIS[provider]
    probe = "".join(f'printf "__CLI__%s\\n" "$(command -v {c})"; ' for c in clis)
    probe += 'printf "__PATH__%s\\n" "$PATH"'
    last_found: dict[str, str] = {}
    for flags in ("-lc", "-ilc"):
        try:
            out = subprocess.run([shell, flags, probe], capture_output=True, text=True, encoding="utf-8",
                                 timeout=20, stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        found = [line[len("__CLI__"):] for line in out.splitlines() if line.startswith("__CLI__")]
        paths = [line[len("__PATH__"):] for line in out.splitlines() if line.startswith("__PATH__")]
        last_found = dict(zip(clis, found))
        if paths and len(found) == len(clis) and all(found):
            dirs = []
            for cli_path in found:
                d = str(Path(cli_path).parent)
                if d not in dirs:
                    dirs.append(d)
            rest = [p for p in paths[-1].split(":") if p and p not in dirs]
            return ":".join(dirs + rest)
    missing = [c for c in clis if not last_found.get(c)]
    raise OpsKitError(
        f"could not find {', '.join(missing)} in a login shell ({shell}); the watchdog "
        "would start a harness whose every run fails. Install it, or put it on PATH in "
        "your shell profile, and re-run.")


def check_notify_env(env_file: Path, keys: tuple[str, str]) -> None:
    """Refuse --notify unless both values are filled in. Never prints a value."""
    sys.path.insert(0, str(ROOT))
    from world.env import parse_value

    values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m:
                values[m.group(1)] = parse_value(m.group(2))
    # The example file's placeholders (xoxb-..., C0XXXXXXX) count as missing.
    missing = [k for k in keys
               if not values.get(k) or values[k].endswith("...") or values[k] == "C0XXXXXXX"]
    if missing:
        raise OpsKitError(
            f"--notify needs {' and '.join(missing)} filled in {env_file}; nothing was "
            "written. Finish the Slack connection (bin/slack-provision.py, bin/slack-pair.py) "
            "or render without --notify.")


def write(home: Path, files: dict[str, tuple[str, int]], force: bool) -> list[str]:
    exists = [rel for rel in files if (home / rel).exists()]
    if exists and not force:
        raise OpsKitError(f"already there: {', '.join(exists)}; re-run with --force to "
                          "replace them (they are rendered, not hand-edited)")
    log = []
    for rel, (content, mode) in files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(mode | stat.S_IRUSR)
        log.append(f"{'replaced' if rel in exists else 'wrote   '} {rel}")
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="script and unit name (default: the harness folder's name)")
    ap.add_argument("--notify", action="store_true",
                    help="post restarts and failed restarts to the harness's Slack channel")
    ap.add_argument("--prefix", help="with --notify: the <PREFIX>_SLACK_BOT_TOKEN / "
                    "<PREFIX>_SLACK_CHANNEL names in env/harness.env")
    ap.add_argument("--keep-days", type=int, help="delete rolled console logs older than this "
                    "(default: keep them all)")
    ap.add_argument("--force", action="store_true", help="replace scripts rendered earlier")
    args = ap.parse_args(argv)

    home = ROOT
    try:
        profile_path = home / "project-profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
        os_name = profile.get("os") or ("macos" if sys.platform == "darwin" else
                                        "windows" if sys.platform == "win32" else "linux")
        notify = None
        if args.notify:
            if not args.prefix:
                raise OpsKitError("--notify needs --prefix (the harness's env prefix, as in "
                                  "bin/slack-pair.py --prefix)")
            notify = (f"{args.prefix}_SLACK_BOT_TOKEN", f"{args.prefix}_SLACK_CHANNEL")
        check_os(os_name)
        python = home / ".venv" / "bin" / "python"
        if not python.exists():
            raise OpsKitError(f"{python} not found; create the venv first "
                              "(uv venv .venv && uv pip install -e .)")
        if notify:
            check_notify_env(home / "env" / "harness.env", notify)
        run_path = resolve_run_path(profile.get("provider", "claude"))
        name = args.name or home.name
        files = render(home, name, os_name, python, run_path, notify=notify,
                       keep_days=args.keep_days)
        log = write(home, files, args.force)
    except OpsKitError as exc:
        print(f"ops-kit: {exc}", file=sys.stderr)
        return 1
    for line in log:
        print(f"  {line}")
    print()
    print("\n".join(next_steps(home, name, os_name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
