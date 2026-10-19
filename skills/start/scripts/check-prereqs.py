#!/usr/bin/env python3
"""check-prereqs — report what this machine is missing before /the-world:start installs.

    python3 check-prereqs.py [--runtime claude|codex|both] [--json]
    (on Windows: `python`, where `python3` may be absent or a Store stub)

Checks and reports only: it installs nothing, edits no config, and makes no network call.
Each item is one of three tiers:

  install  needed to install the kit and verify the harness (python3 3.11+, uv, git).
           The exit code is non-zero only when one of these is missing.
  run      needed before the harness's first real run (the runtime CLI and its login).
  later    informational; Slack is connected after local verification.

This file must itself run on an old Python, so it can say "too old" instead of crashing on
the syntax install-kit.py uses. Keep it to 3.6 syntax: no f-string `=`, no walrus, no
`match`, no PEP 604 unions, no `from __future__ import annotations`, no dataclasses.
"""

import argparse
import json
import os
import platform
import shutil
import sys

MIN_PYTHON = (3, 11)

INSTALL = {
    "python": {
        "darwin": "brew install python@3.12",
        "linux": "sudo apt install python3.12  (or your distro's package)",
        "windows": "winget install Python.Python.3.12",
        "any": "or, with uv: uv python install 3.12, then run the kit scripts with "
               "`uv run --no-project --python 3.12 python <script>`",
    },
    "uv": {
        "darwin": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "linux": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "windows": 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"',
    },
    "git": {
        "darwin": "xcode-select --install",
        "linux": "sudo apt install git  (or your distro's package)",
        "windows": "winget install Git.Git",
    },
    "claude": {
        "darwin": "curl -fsSL https://claude.ai/install.sh | bash",
        "linux": "curl -fsSL https://claude.ai/install.sh | bash",
        "windows": 'powershell -c "irm https://claude.ai/install.ps1 | iex"',
    },
    "codex": {
        "darwin": "npm install -g @openai/codex",
        "linux": "npm install -g @openai/codex",
        "windows": "npm install -g @openai/codex",
    },
    "slack": {
        "darwin": "curl -fsSL https://downloads.slack-edge.com/slack-cli/install.sh | bash",
        "linux": "curl -fsSL https://downloads.slack-edge.com/slack-cli/install.sh | bash",
        "windows": 'powershell -c "irm https://downloads.slack-edge.com/slack-cli/install-windows.ps1 | iex"',
    },
}

LOGIN = {
    "claude": "run `claude`, then `/login`",
    "codex": "codex login",
}


def os_key(system):
    system = system.lower()
    if system.startswith("win"):
        return "windows"
    return "darwin" if system == "darwin" else "linux"


def install_hint(tool, system):
    hints = INSTALL[tool]
    hint = hints[os_key(system)]
    if "any" in hints:
        hint = hint + "; " + hints["any"]
    return hint


def item(name, tier, status, detail, next_action=None):
    return {"name": name, "tier": tier, "status": status, "detail": detail,
            "next": next_action}


def check_python(version_info, system):
    version = ".".join(str(part) for part in tuple(version_info)[:3])
    if tuple(version_info)[:2] >= MIN_PYTHON:
        return item("python", "install", "ok", "python3 " + version)
    return item("python", "install", "too_old",
                "python3 %s; the kit needs %d.%d+" % ((version,) + MIN_PYTHON),
                install_hint("python", system))


def check_tool(name, tier, which, system):
    path = which(name)
    if path:
        return item(name, tier, "ok", path)
    return item(name, tier, "missing", name + " not found on PATH", install_hint(name, system))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def claude_login(env, home, exists):
    """Logged in if an API key or OAuth token is in the environment, or Claude Code has
    recorded an account. On macOS the credential itself sits in the Keychain, which this
    does not open; `oauthAccount` in ~/.claude.json is the local trace of a login."""
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    config = env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    if exists(os.path.join(config, ".credentials.json")):
        return True
    for candidate in (os.path.join(config, ".claude.json"), os.path.join(home, ".claude.json")):
        data = read_json(candidate) if exists(candidate) else None
        if isinstance(data, dict) and data.get("oauthAccount"):
            return True
    return None  # no trace either way; may still be logged in another way


def codex_login(env, home, exists):
    if env.get("OPENAI_API_KEY"):
        return True
    codex_home = env.get("CODEX_HOME") or os.path.join(home, ".codex")
    return True if exists(os.path.join(codex_home, "auth.json")) else None


def check_runtime(name, which, system, env, home, exists):
    found = check_tool(name, "run", which, system)
    if found["status"] != "ok":
        found["next"] = found["next"] + "; then " + LOGIN[name]
        return found
    logged_in = (claude_login if name == "claude" else codex_login)(env, home, exists)
    if logged_in:
        found["detail"] += " (logged in)"
        return found
    found["status"] = "login_unknown"
    found["detail"] += " (no login found)"
    found["next"] = LOGIN[name]
    return found


def collect(runtimes, version_info=None, which=None, system=None, env=None, home=None,
            exists=None):
    """Every check, with its inputs injectable so tests need no real interpreter or PATH."""
    version_info = sys.version_info if version_info is None else version_info
    which = which or shutil.which
    system = system or platform.system()
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    exists = exists or os.path.exists
    items = [check_python(version_info, system),
             check_tool("uv", "install", which, system),
             check_tool("git", "install", which, system)]
    for runtime in runtimes:
        items.append(check_runtime(runtime, which, system, env, home, exists))
    slack = check_tool("slack", "later", which, system)
    if slack["status"] != "ok":
        slack["status"] = "info"
        slack["detail"] += "; needed only when connecting Slack"
    items.append(slack)
    return items


def blocking(items):
    return [entry for entry in items if entry["tier"] == "install" and entry["status"] != "ok"]


def render(items):
    lines = []
    for entry in items:
        mark = "ok" if entry["status"] == "ok" else entry["status"].upper()
        lines.append("[%s] %s (%s): %s" % (mark, entry["name"], entry["tier"], entry["detail"]))
        if entry["next"]:
            lines.append("      next: " + entry["next"])
    missing = blocking(items)
    if missing:
        lines.append("Install and verification wait on: "
                     + ", ".join(entry["name"] for entry in missing))
    else:
        lines.append("Ready to install the kit and verify.")
    return "\n".join(lines)


def main(argv=None, out=None, **injected):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Check /the-world:start prerequisites.")
    parser.add_argument("--runtime", choices=("claude", "codex", "both"), default="both",
                        help="bot runtime to check (default: both, until one is chosen)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    runtimes = ("claude", "codex") if args.runtime == "both" else (args.runtime,)
    items = collect(runtimes, **injected)
    ready = not blocking(items)
    if args.json:
        out.write(json.dumps({"ready": ready, "items": items}, indent=2) + "\n")
    else:
        out.write(render(items) + "\n")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
