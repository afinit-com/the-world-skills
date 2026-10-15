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

and on Windows, in PowerShell 5.1 with nothing to install:

  scripts/<name>-up.ps1, scripts/<name>-watchdog.ps1   the same two scripts
  scripts/windows/<name>-watchdog.xml   a Task Scheduler task: every 120s from logon

and prints the commands that would register the watchdog, for a person to run. The
scripts and their pitfalls are in docs/operations.md. The paths in them are this
machine's, so a profile for another OS is refused rather than rendered here.

`--notify` is refused unless env/harness.env holds the main bot token and the channel,
since a watchdog that silently cannot post is worse than one that never claimed to.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime
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


def dotnet_regex_escape(text: str) -> str:
    """Escape for a .NET regex (PowerShell -match)."""
    return re.sub(r"([\\*+?|{}\[\]()^$.#\s])", r"\\\1", text)


def windows_process_match(home: Path) -> str:
    """The Windows counterpart of process_match, for PowerShell's -match on a process's
    command line: a python interpreter, quoted or not, whose first argument is this
    harness.py. ops-log.py, which starts the harness, carries the path later in its
    command line and so does not match.

    An unquoted program path cannot hold whitespace (it would have been quoted), and the
    pattern says so. Without that, in a home with no space in its path, `[^"]*\\` ran
    across the console writer's whole unquoted command line to the `python.exe
    ...harness.py` at its end, and the writer counted as a second harness."""
    harness = dotnet_regex_escape(str(home / "harness.py"))
    exe = r'pythonw?[0-9.]*(?:\.exe)?'
    return (rf'^(?:"(?:[^"]*\\)?{exe}"|(?:[^"\s]*\\)?{exe})'
            rf'\s+"?{harness}"?(?:\s|$)')


def relative_process_match(*, windows: bool = False) -> str:
    """A README-style launch whose home cannot be established from its command line.
    Refuse to start while one exists, rather than adopt or stop another home's process.
    `uv run python harness.py` has a Python child with exactly this command line too.
    """
    if windows:
        exe = r'pythonw?[0-9.]*(?:\.exe)?'
        return (rf'^(?:"(?:[^"]*\\)?{exe}"|(?:[^"\s]*\\)?{exe})'
                r'\s+"?(?:\.[\\/])?harness\.py"?(?:\s|$)')
    return r'^(.*/)?[Pp]ython[0-9.]* (\./)?harness\.py( |$)'


def ps_quote(text: str) -> str:
    """A PowerShell single-quoted string. PowerShell also takes the curly single quotes
    as quote marks, so they are doubled too."""
    for mark in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        text = text.replace(mark, mark * 2)
    return f"'{text}'"


def systemd_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    left = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if left:
        raise OpsKitError(f"unfilled placeholders: {sorted(set(left))}")
    return out


def host_os() -> str:
    return ("macos" if sys.platform == "darwin" else
            "windows" if sys.platform == "win32" else "linux")


def check_os(os_name: str) -> None:
    if os_name not in ("macos", "linux", "windows"):
        raise OpsKitError(f"this harness's profile is os={os_name!r}; the ops kit renders "
                          "for macos, linux and windows only, so nothing was written.")


def check_host(os_name: str, host: str) -> None:
    """The scripts carry this machine's paths and run its service manager, so they are
    rendered on the machine the harness runs on, for that machine's OS."""
    if os_name != host:
        raise OpsKitError(
            f"this harness's profile is os={os_name!r} but this machine is {host}. The "
            "scripts are rendered with this machine's paths, so nothing was written. Run "
            "this on the machine the harness runs on, or re-install the harness for this "
            f"OS (--os {host}).")


def task_name(name: str) -> str:
    return f"the-world-{slug(name)}-watchdog"


def render(home: Path, name: str, os_name: str, python: Path, run_path: str, *,
           notify: tuple[str, str] | None = None, keep_days: int | None = None,
           templates: Path = KIT_TEMPLATES, user: str | None = None,
           now: datetime | None = None) -> dict[str, tuple[str | bytes, int]]:
    """Every file to write, relative to `home`, as (content, mode). Pure: reads only the
    templates, so a test can render without a venv, a CLI or a service manager.

    `user` (DOMAIN\\name) and `now` are for the Windows task only, and default to this
    session's user and the current time."""
    check_os(os_name)
    name = slug(name)
    home = home.resolve()
    if os_name == "windows":
        return _render_windows(home, name, python, run_path, notify=notify,
                               keep_days=keep_days, templates=templates, user=user, now=now)
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
        "RELATIVE_MATCH": q(relative_process_match()),
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


def _render_windows(home: Path, name: str, python: Path, run_path: str, *,
                    notify: tuple[str, str] | None, keep_days: int | None, templates: Path,
                    user: str | None, now: datetime | None) -> dict[str, tuple[str | bytes, int]]:
    up = home / "scripts" / f"{name}-up.ps1"
    watchdog = home / "scripts" / f"{name}-watchdog.ps1"
    notify_args = ("@(" + ", ".join(ps_quote(a) for a in ("--token-env", notify[0],
                                                          "--channel-env", notify[1])) + ")"
                   if notify else "@()")
    values = {
        "NAME": name,
        "HOME_DIR": ps_quote(str(home)),
        "PYTHON": ps_quote(str(python)),
        "UP": ps_quote(str(up)),
        "MATCH": ps_quote(windows_process_match(home)),
        "RELATIVE_MATCH": ps_quote(relative_process_match(windows=True)),
        "RUN_PATH": ps_quote(run_path),
        "INTERVAL_S": str(INTERVAL_S),
        "BACKOFF_MIN_S": str(BACKOFF_MIN_S),
        "BACKOFF_MAX_S": str(BACKOFF_MAX_S),
        "KEEP_DAYS": ps_quote("" if keep_days is None else str(int(keep_days))),
        "NOTIFY": notify_args,
        "NOTIFY_NOTE": " and to Slack" if notify else "",
    }
    read = lambda n: (templates / n).read_text(encoding="utf-8")  # noqa: E731
    if user is None:
        user = "\\".join(filter(None, (os.environ.get("USERDOMAIN"),
                                        os.environ.get("USERNAME") or os.environ.get("USER"))))
    if not user:
        raise OpsKitError("cannot tell which Windows user the watchdog task runs as "
                          "(USERDOMAIN / USERNAME are not set)")
    # pythonw.exe has no console, and starts PowerShell with a hidden one, so a round does
    # not flash a window every two minutes as `powershell -WindowStyle Hidden` would.
    hidden = "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:], creationflags=0x08000000))"
    arguments = subprocess.list2cmdline([
        "-c", hidden, "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(watchdog)])
    task = fill(read("task.xml.tmpl"), {
        "NAME": name, "TASK_NAME": task_name(name), "INTERVAL_S": str(INTERVAL_S),
        # Task Scheduler repeats in whole minutes, one at the least.
        "INTERVAL_M": str(max(1, INTERVAL_S // 60)),
        "USER": xml_escape(user),
        "START": (now or datetime.now()).replace(microsecond=0).isoformat(),
        "LAUNCHER": xml_escape(str(python.with_name("pythonw.exe"))),
        "ARGUMENTS": xml_escape(arguments),
        "HOME_DIR": xml_escape(str(home)),
    })
    # PowerShell 5.1 reads a script without a byte order mark in the ANSI code page, which
    # garbles a non-ASCII path (a Korean user name); schtasks /XML wants UTF-16.
    bom = "\ufeff"
    return {
        f"scripts/{name}-up.ps1": (bom + fill(read("up.ps1.tmpl"), values), 0o755),
        f"scripts/{name}-watchdog.ps1": (bom + fill(read("watchdog.ps1.tmpl"), values), 0o755),
        f"scripts/windows/{name}-watchdog.xml": ((bom + task).encode("utf-16-le"), 0o644),
    }


def windows_command(script: str, *args: str) -> str:
    return " ".join(["powershell -NoProfile -ExecutionPolicy Bypass -File", script, *args])


def next_steps(home: Path, name: str, os_name: str) -> list[str]:
    name = slug(name)
    up = f"scripts/{name}-up"
    if os_name == "windows":
        up_ps1 = f"scripts\\{name}-up.ps1"
        task = task_name(name)
        return [
            f"Nothing is running or registered yet. From {home}, in PowerShell:",
            f"  {windows_command(up_ps1)}            # start it in the background now",
            f"  {windows_command(up_ps1, 'status')}",
            "To have the watchdog restart it after a crash, and start it at logon:",
            f"  schtasks /Create /TN {task} /XML scripts\\windows\\{name}-watchdog.xml",
            "To stop it for a while (the watchdog leaves it down until `up`):",
            f"  {windows_command(up_ps1, 'down')}",
            "To take it down for good, remove the watchdog FIRST, then stop it:",
            f"  schtasks /Delete /TN {task} /F",
            f"  {windows_command(up_ps1, 'down')}",
        ]
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


def resolve_windows_run_path(provider: str, path: str | None = None) -> str:
    """PATH with each agent CLI's directory first. Windows has no login shell to ask, and
    a scheduled task starts with the user's own environment, so this session's PATH is
    the one to search; pinning the CLI's directory first keeps the harness on the CLI
    that was found now (claude.exe or an npm claude.cmd) whatever comes earlier later."""
    path = os.environ.get("PATH", "") if path is None else path
    clis = PROVIDER_CLIS[provider]
    found = {c: shutil.which(c, path=path) for c in clis}
    missing = [c for c, where in found.items() if not where]
    if missing:
        raise OpsKitError(
            f"could not find {', '.join(missing)} on PATH; the watchdog would start a "
            "harness whose every run fails. Install it, or add it to your user PATH, open "
            "a new terminal, and re-run.")
    dirs: list[str] = []
    for where in found.values():
        d = str(Path(where).parent)
        if d.lower() not in (x.lower() for x in dirs):
            dirs.append(d)
    rest = [p for p in path.split(os.pathsep) if p and p.lower() not in (d.lower() for d in dirs)]
    return os.pathsep.join(dirs + rest)


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


def write(home: Path, files: dict[str, tuple[str | bytes, int]], force: bool) -> list[str]:
    exists = [rel for rel in files if (home / rel).exists()]
    if exists and not force:
        raise OpsKitError(f"already there: {', '.join(exists)}; re-run with --force to "
                          "replace them (they are rendered, not hand-edited)")
    log = []
    for rel, (content, mode) in files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
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
        os_name = profile.get("os") or host_os()
        notify = None
        if args.notify:
            if not args.prefix:
                raise OpsKitError("--notify needs --prefix (the harness's env prefix, as in "
                                  "bin/slack-pair.py --prefix)")
            notify = (f"{args.prefix}_SLACK_BOT_TOKEN", f"{args.prefix}_SLACK_CHANNEL")
        check_os(os_name)
        check_host(os_name, host_os())
        if os_name == "windows":
            python = home / ".venv" / "Scripts" / "python.exe"
            needed = [python, python.with_name("pythonw.exe")]
        else:
            python = home / ".venv" / "bin" / "python"
            needed = [python]
        for exe in needed:
            if not exe.exists():
                raise OpsKitError(f"{exe} not found; create the venv first "
                                  "(uv venv .venv, then uv pip install -e .)")
        if notify:
            check_notify_env(home / "env" / "harness.env", notify)
        provider = profile.get("provider", "claude")
        run_path = (resolve_windows_run_path(provider) if os_name == "windows"
                    else resolve_run_path(provider))
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
