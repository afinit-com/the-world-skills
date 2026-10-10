#!/usr/bin/env python3
"""Collect what a the-world-skills maintainer needs to reproduce a report, as Markdown.

    python3 collect_env.py [HARNESS_DIR]

Stdlib only, so it runs before a harness has a venv, and on the system python3 (or
`python` on Windows). It reads versions, the install profile, git state, and the tail of
the harness logs. It never opens env/ (tokens live there), never prints WORLD.md or a
prompt (the user's own content), and masks token shapes and the home directory in
everything it does print. The user reads the result before anything leaves the machine.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
LOG_LINES = 30

SECRET = re.compile(
    r"xox[abposr]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+|sk-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{20,}"
    r"|(?i:(?:token|secret|password|api_key)\s*[=:]\s*)\S+"
)


def redact(text: str) -> str:
    text = SECRET.sub("[redacted]", text)
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


def run(*argv: str, cwd: Path | None = None) -> str:
    """First line of a command's output, or why there is none."""
    exe = shutil.which(argv[0])
    if exe is None:
        return "not installed"
    try:
        out = subprocess.run([exe, *argv[1:]], cwd=cwd, capture_output=True, encoding="utf-8",
                             errors="replace", timeout=20, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed ({type(exc).__name__})"
    text = (out.stdout or out.stderr).strip()
    return redact(text.splitlines()[0]) if text else f"exit {out.returncode}"


def plugin_version() -> str:
    for parent in HERE.parents:
        for rel in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            path = parent / rel
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("name") == "the-world-skills":
                    return f"{data.get('version', '?')} ({redact(str(parent))})"
    return "unknown"


def gh_auth() -> str:
    if shutil.which("gh") is None:
        return "gh not installed"
    out = subprocess.run([shutil.which("gh"), "auth", "status"], capture_output=True,
                         encoding="utf-8", errors="replace", timeout=20, stdin=subprocess.DEVNULL, check=False)
    return "logged in" if out.returncode == 0 else "not logged in"


def tail(path: Path, n: int = LOG_LINES) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return ""
    return redact("\n".join(lines))


def find_harness(arg: str | None) -> Path | None:
    """The given directory, or the nearest one at or above the current directory that has
    a harness.py or a project-profile.json."""
    if arg:
        path = Path(arg).expanduser().resolve()
        return path if path.is_dir() else None
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "harness.py").is_file() or (candidate / "project-profile.json").is_file():
            return candidate
    return None

def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def world_shape(home: Path) -> list[str]:
    """What this world is made of, read from its code without running it.

    Every harness diverges after /the-world-start, so "it broke" means nothing until the
    maintainer knows which characters, triggers, and tools this one has. The parsing is
    the-world-audit's scanner (a sibling skill in the same plugin), which never imports
    the harness. Names and shapes only: no prompt, no voice, no WORLD.md."""
    scan_path = HERE.parents[1] / "the-world-audit" / "scan.py"
    if not scan_path.is_file() or not (home / "harness.py").is_file():
        return []
    import importlib.util

    spec = importlib.util.spec_from_file_location("the_world_audit_scan", scan_path)
    scan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scan  # its dataclasses resolve types through sys.modules
    try:
        spec.loader.exec_module(scan)
        h = scan.Harness(home, None)
    except Exception as exc:  # noqa: BLE001 — a broken harness is exactly when someone asks
        return ["", "### World shape", "", f"- could not be read ({type(exc).__name__})"]
    lines = ["", "### World shape", ""]
    personas = []
    for _module, node in h.persona_calls:
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        key, display = scan.lit(kw.get("key")), scan.lit(kw.get("display"))
        if isinstance(key, str):
            personas.append(f"{key}" + (f" ({display})" if isinstance(display, str) else ""))
    lines.append("- personas: " + (", ".join(dict.fromkeys(personas)) or "none found"))
    for agent in h.agents:
        kw = agent.kw
        trigger = scan.segment(agent.module, kw["trigger"]) if "trigger" in kw else "?"
        persona = kw.get("persona")
        persona = scan.segment(agent.module, persona) if persona is not None else "main"
        flags = [f for f in ("mutates", "allow_bash", "writes_finding", "task_allowed")
                 if scan.lit(kw.get(f)) is True]
        tools = sorted(scan.idents(kw.get("tools")) - {"make_write_finding", "writer"})
        lines.append(f"- agent `{agent.name}` ({agent.where}): trigger {_short(trigger)}; "
                     f"persona {_short(persona, 40)}; tools {', '.join(tools) or 'none'}"
                     + (f"; {', '.join(flags)}" if flags else ""))
    if h.tools:
        lines.append("- tools defined: " + ", ".join(sorted({t.name for t in h.tools})))
    for problem in h.parse_errors:
        lines.append(f"- parse error: {redact(str(problem))}")
    return lines


def harness_section(home: Path) -> list[str]:
    lines = ["", "### Harness", "", f"- path: `{redact(str(home))}`"]
    profile = home / "project-profile.json"
    if profile.is_file():
        try:
            data = json.loads(profile.read_text(encoding="utf-8"))
            lines.append("- profile: " + ", ".join(f"{k}={data[k]}" for k in ("os", "provider", "channel")
                                                    if k in data))
        except (OSError, ValueError):
            lines.append("- profile: unreadable project-profile.json")
    else:
        lines.append("- profile: none (installed before profiles, or not a harness)")
    if (home / ".git").exists():
        branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=home)
        head = run("git", "log", "-1", "--format=%h %s", cwd=home)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=home, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=20, check=False).stdout.splitlines()
        kit = [d for d in dirty if d[3:].startswith(("world/", "bin/", ".claude/hooks/", ".codex/hooks/"))]
        lines.append(f"- git: {branch} @ {head}; {len(dirty)} changed paths, {len(kit)} of them kit-owned")
    else:
        lines.append("- git: not a repository")
    status = home / "STATUS.md"
    if status.is_file():
        text = status.read_text(encoding="utf-8", errors="replace")
        process = re.search(r"## Process\n\n(.*?)\n\n", text, re.DOTALL)
        pulse = re.search(r"## Agent pulse\n\n(.*?)(?:\n\n|$)", text, re.DOTALL)
        for block in (process, pulse):
            if block:
                lines += [redact(line) for line in block.group(1).splitlines()]
    try:
        lines += world_shape(home)
    except Exception as exc:  # noqa: BLE001 — the report must come out regardless
        lines += ["", "### World shape", "", f"- could not be read ({type(exc).__name__})"]
    for rel in ("runtime/harness.log", "logs/harness.jsonl"):
        text = tail(home / rel)
        if text:
            lines += ["", f"<details><summary>last {LOG_LINES} lines of {rel}</summary>", "",
                      "```", text, "```", "", "</details>"]
    return lines


def main() -> None:
    lines = [
        "### Environment",
        "",
        f"- the-world-skills: {plugin_version()}",
        (f"- OS: {platform.system()} {platform.release()} ({platform.machine()}); "
         f"{platform.platform(terse=True)}"),
        f"- python (this run): {platform.python_version()}",
        f"- uv: {run('uv', '--version')}",
        f"- git: {run('git', '--version')}",
        f"- claude: {run('claude', '--version')}",
        f"- codex: {run('codex', '--version')}",
        f"- slack CLI: {run('slack', '--version')}",
        f"- gh: {run('gh', '--version')}; {gh_auth()}",
        (f"- locale encoding: {sys.getfilesystemencoding()} / "
         f"{os.environ.get('PYTHONIOENCODING') or 'default'}"),
    ]
    home = find_harness(sys.argv[1] if len(sys.argv) > 1 else None)
    lines += harness_section(home) if home else ["", "### Harness", "", "- none found from here"]
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
