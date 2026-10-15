#!/usr/bin/env python3
"""install-kit — unpack the world kit bundled in this skill into a harness directory.

`npx skills add` installs *skill directories*, not repos, so the runtime cannot be
"already here" the way it is in the template repo. It rides along inside this skill
(`assets/kit/`) and this script puts it in place. Everything the LLM cannot get wrong
lives here: which files are kit-owned, which are the harness's own, and how the guard
hooks get registered without clobbering a settings.json that was already there.

    python3 install-kit.py <target-dir> --provider claude|codex|both
                           [--os auto|windows|macos|linux] [--channel slack|discord|both|none]
    (on Windows: `python`, where `python3` may be absent or a Store stub)

The three choices are recorded in `project-profile.json` and only what they need is
copied: a Codex-only harness gets no Claude runner or hooks, a Slack-only one no Discord
gateway. A re-run without a flag keeps what the profile already says, so an upgrade does
not quietly widen or narrow a harness. world/profile.py reads the same file at runtime.

Ownership is the whole point of the split below:

  kit-owned    world/ named world provider hook files, bin scripts, the few remaining
               templates/ files (gitignore, AGENTS.md, codex pyproject, skill and verify
               wrappers, Slack manifest, the two guard templates, the opt-in ops templates)
               docs/philosophy.md docs/codex-compatibility.md docs/world-map.md
               docs/integrations.md docs/operations.md docs/verify.md
               Overwritten on every run — that is what makes a re-run an upgrade.
               Never fork these per-harness (see docs/philosophy.md).
               Newly owned ops/verify files with differing content are copied to .bak
               before replacement (numbered if needed), and named in the log.

  harness-own  pyproject.toml env/harness.env.example AGENTS.md README.md .gitignore
               Created if missing, otherwise left alone. Provider hook registries are
               merged because a project may already register unrelated hooks.

  yours        harness.py WORLD.md abilities/ tools/ slack-app/ env/harness.env logs/
               scripts/ (bin/ops-kit.py renders into it only when asked)
               Never touched. /the-world-start writes these after this runs.

⚠️ A RE-RUN IS DESTRUCTIVE. Each kit-owned TREE is `rmtree`d before it is copied, so
anything you put inside `world/` that the kit does not also ship
is deleted without being named — and any kit file you edited in place is overwritten the
same way. Neither is reported: the log says `replaced world/`, not what went with it.

This is the intended contract (a fork of the runtime is the thing this kit exists to
prevent), but it makes a re-run safe only for a harness that never touched those trees.
Before running against an existing harness: commit or stash first, so `git diff` is the
report this script does not print, and read the harness's own notes on whether it treats
`world/` as its own. On 2026-08-17 a re-run against a harness that did destroyed four local
fixes AND the document listing them, which lived inside `world/`.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

KIT = (Path(__file__).resolve().parent.parent / "assets" / "kit").resolve()

# kit-owned: replaced wholesale on every run
COMMON_TREES = ["world"]
PROVIDER_TREES = {
    "claude": [".claude/the-world"],
    "codex": [],
}
PROVIDER_FILES = {
    "claude": [
        ".claude/hooks/durable-config-guard.py",
        ".claude/hooks/world-git-guard.py",
        ".claude/hooks/world-integrity-guard.py",
        ".claude/hooks/world-scope-guard.py",
    ],
    "codex": [".codex/hooks/world-guard.py"],
}
KIT_FILES = [
    # The templates a harness still reads AFTER install: the wrapper skill bodies that
    # bin/scaffold-world-skill.py fills in (agent words, tool words, verify words), and the
    # Slack app manifest used when the harness is finally provisioned. The rest of
    # templates/ is read from the kit during install and never from the target, so it is
    # not copied.
    "templates/skill-wrapper.md.tmpl",
    "templates/skill-wrapper-tool.md.tmpl",
    "templates/verify-wrapper.md.tmpl",
    "templates/slack-manifest.json.tmpl",
    # Read by bin/ops-kit.py, only when the user opts into running the harness all the
    # time (docs/operations.md).
    "templates/ops/up.sh.tmpl",
    "templates/ops/watchdog.sh.tmpl",
    "templates/ops/ops-common.ps1.tmpl",
    "templates/ops/up.ps1.tmpl",
    "templates/ops/watchdog.ps1.tmpl",
    "templates/ops/task.xml.tmpl",
    "templates/ops/launchd.plist.tmpl",
    "templates/ops/systemd.service.tmpl",
    "templates/ops/systemd.timer.tmpl",
    "bin/smoke.py",
    "bin/codex-spike.py",
    "bin/scaffold-world-skill.py",
    "bin/world-map-demo.py",
    "bin/discord-setup.py",
    "bin/slack-provision.py",
    "bin/slack-pair.py",
    "bin/ops-kit.py",
    "bin/ops-notify.py",
    "bin/ops-log.py",
    "bin/ops-lock.py",
    "docs/philosophy.md",
    "docs/codex-compatibility.md",
    "docs/world-map.md",
    "docs/discord.md",
    "docs/integrations.md",
    "docs/operations.md",
    "docs/verify.md",
]
# These individual paths became kit-owned after harnesses could already have used
# them. Preserve differing content on upgrade; never overwrite an earlier backup.
BACKUP_ON_REPLACE = {
    rel for rel in KIT_FILES if rel.startswith(("templates/ops/", "bin/ops-"))
} | {
    "templates/skill-wrapper-tool.md.tmpl", "templates/verify-wrapper.md.tmpl",
    "docs/operations.md", "docs/verify.md",
}
# harness-owned: only created when absent
SEED_FILES = ["env/harness.env.example"]

# Kit paths that only some profiles carry. Each is left out of the copy (and removed from a
# target that an earlier, wider install left it in) unless the profile selects it.
OPTIONAL = {
    ("os", "windows"): ["world/tools/windows_pipe.py"],
    ("provider", "claude"): ["world/runners/claude.py", "world/process/plugins.py"],
    ("provider", "codex"): ["world/runners/codex.py", "bin/codex-spike.py",
                            "docs/codex-compatibility.md"],
    ("channel", "discord"): ["world/discord", "world/channels/discord.py",
                             "bin/discord-setup.py", "docs/discord.md"],
}
PROFILE_FILE = "project-profile.json"
PROFILE_CHOICES = {
    "os": ("windows", "macos", "linux"),
    "provider": ("claude", "codex", "both"),
    "channel": ("slack", "discord", "both", "none"),
}

# Guard scripts the kit registers. A hook command naming one of these is kit-owned and a
# re-run supersedes it; anything else in a harness's registry belongs to the harness.
# Kept in sync with world/harness.py's _GUARD_SCRIPTS, which vets the same set at startup.
GUARD_SCRIPTS = (
    "world-scope-guard.py",
    "world-git-guard.py",
    "world-integrity-guard.py",
    "durable-config-guard.py",
    "world-guard.py",
)


def fail(msg: str) -> None:
    print(f"install-kit: {msg}", file=sys.stderr)
    raise SystemExit(1)


def unwritable_dirs(dirs: list[Path]) -> list[Path]:
    """Every directory in `dirs` the install could not write into, checked up front.

    Codex's default `workspace-write` sandbox refuses writes under `.codex/` and
    `.agents/` while allowing the rest of the workspace. Copying file by file, the
    installer used to die halfway there: `world/` and `bin/` in place, the guard hooks
    missing, and nothing saying so. Each directory is created and probed with a scratch
    file before any kit file is written. If one is refused, every directory this probe
    created is removed again, so the refusal leaves the target as it found it.
    """
    created: list[Path] = []
    blocked: list[Path] = []
    for d in sorted(set(dirs)):
        if any(b == d or b in d.parents for b in blocked):
            continue
        missing = [p for p in (d, *d.parents) if not p.exists()]
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=d, prefix=".install-kit-probe-"):
                pass
        except OSError:
            blocked.append(d)
        created += [p for p in missing if p.exists()]
    if blocked:
        for p in sorted(created, key=lambda p: len(p.parts), reverse=True):
            try:
                p.rmdir()
            except OSError:
                pass
    return blocked


def current_os() -> str:
    if sys.platform == "win32":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def resolve_profile(target: Path, args: argparse.Namespace) -> dict:
    """Flags win; otherwise what an earlier install recorded; otherwise the defaults."""
    try:
        recorded = json.loads((target / PROFILE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        recorded = {}
    except (OSError, ValueError) as e:
        fail(f"{target / PROFILE_FILE} is unreadable ({e}); fix or delete it and re-run")
    defaults = {"os": current_os(), "provider": "claude", "channel": "slack"}
    profile = {"schema_version": 1}
    for key, choices in PROFILE_CHOICES.items():
        flag = getattr(args, key)
        value = flag if flag not in (None, "auto") else recorded.get(key, defaults[key])
        if value not in choices:
            fail(f"{PROFILE_FILE}: {key}={value!r} is not one of {', '.join(choices)}")
        profile[key] = value
    return profile


def selected(profile: dict, key: str, value: str) -> bool:
    return profile[key] in (value, "both")


def excluded_paths(profile: dict) -> list[str]:
    return [rel for (key, value), rels in OPTIONAL.items()
            if not selected(profile, key, value) for rel in rels]


ONLY_BLOCK = re.compile(r"^# \[only (\w+)=(\w+)\]\n(.*?)^# \[/only\]\n", re.S | re.M)


def render_profile(text: str, profile: dict) -> str:
    """Keep a `# [only channel=discord]` … `# [/only]` block only when the profile selects
    it, so a seeded file never points at a file this install left out."""
    return ONLY_BLOCK.sub(lambda m: m.group(3) if selected(profile, m.group(1), m.group(2))
                          else "", text)


def hook_interpreter(os_name: str | None = None) -> str:
    """The command name the guard hooks are registered under, for THIS platform.

    `python3` is right on macOS/Linux and unreliable on Windows: the classic python.org
    installer ships no `python3.exe`, the new install manager ships one it explicitly
    does not recommend, and an unresolved name lands on the WindowsApps store stub.
    `python` is the one command the Windows docs promise. Getting this wrong is not a
    loud failure — a hook that cannot start exits 127, which Claude Code treats as a
    NON-blocking error, so the tool call proceeds and the guard is off. (Harness.
    preflight_guard_hooks says so at startup; this is the half that prevents it.)
    """
    return "python" if (os_name or current_os()) == "windows" else "python3"


def localize_hook_commands(settings: dict, os_name: str | None = None) -> dict:
    """Rewrite the kit's hook commands for the platform being installed onto."""
    interpreter = hook_interpreter(os_name)
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if command.startswith("python3 "):
                    hook["command"] = interpreter + command[len("python3"):]
    return settings


def _guard_script(command: str) -> str | None:
    """The kit guard script a command runs, if any — the identity a re-run supersedes on."""
    for name in GUARD_SCRIPTS:
        if name in command:
            return name
    return None


def merge_hook_file(source: Path, dest: Path, log: list[str], label: str,
                    os_name: str | None = None) -> None:
    """Merge hook groups by matcher and command without replacing user settings.

    Dedupe is by command STRING, so a kit whose command spelling changed (a different
    interpreter, `${VAR}` instead of `$VAR`) would otherwise register a second entry
    beside the stale one and run every guard twice — the first copy failing. Entries
    naming a kit guard script are therefore superseded rather than accumulated. A hook
    the harness wrote itself names no kit script and is never touched.
    """

    kit_settings = localize_hook_commands(json.loads(source.read_text(encoding="utf-8")), os_name)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(kit_settings, indent=2) + "\n", encoding="utf-8")
        log.append(f"wrote    {label} (guard hooks registered)")
        return
    try:
        existing = json.loads(dest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{dest} is not valid JSON ({e}); merge the hooks by hand")

    added = 0
    removed = 0
    hooks = existing.setdefault("hooks", {})
    for event, groups in kit_settings.get("hooks", {}).items():
        dest_groups = hooks.setdefault(event, [])
        for group in groups:
            match = next((g for g in dest_groups if g.get("matcher") == group.get("matcher")), None)
            if match is None:
                dest_groups.append(json.loads(json.dumps(group)))
                added += len(group.get("hooks", []))
                continue
            wanted = group.get("hooks", [])
            superseded = {_guard_script(h.get("command", "")) for h in wanted} - {None}
            kept = []
            for existing_hook in match.get("hooks", []):
                script = _guard_script(existing_hook.get("command", ""))
                if script in superseded and existing_hook.get("command") not in {
                    h.get("command") for h in wanted
                }:
                    log.append(f"replaced {label}: stale {script} entry")
                    removed += 1
                    continue
                kept.append(existing_hook)
            match["hooks"] = kept
            have = {h.get("command") for h in kept}
            for hook in wanted:
                if hook.get("command") not in have:
                    match.setdefault("hooks", []).append(hook)
                    added += 1
    # `removed` alone still means the file changed: a stale spelling dropped while the
    # current one was already registered adds nothing but must not be left on disk.
    if added or removed:
        dest.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        log.append(f"merged   {label} (+{added} guard hook entries, -{removed} stale)")
    else:
        log.append(f"kept     {label} (guard hooks already registered)")


def merge_settings(target: Path, log: list[str], os_name: str | None = None) -> None:
    """Register the guard hooks in target/.claude/settings.json, idempotently.

    Dedupes on the hook command string, so a re-run (or a harness whose settings.json
    already carries the hooks) adds nothing. An existing file's other keys survive.
    """
    merge_hook_file(
        KIT / ".claude" / "settings.json",
        target / ".claude" / "settings.json",
        log,
        ".claude/settings.json",
        os_name,
    )


def merge_codex_hooks(target: Path, log: list[str], os_name: str | None = None) -> None:
    merge_hook_file(
        KIT / ".codex" / "hooks.json",
        target / ".codex" / "hooks.json",
        log,
        ".codex/hooks.json",
        os_name,
    )


def seed_gitignore(target: Path, log: list[str]) -> None:
    """Ensure the harness ignores state/secrets. Appends only what is missing."""
    wanted = [
        line.strip()
        for line in (KIT / "templates" / "gitignore.tmpl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dest = target / ".gitignore"
    if not dest.exists():
        dest.write_text("\n".join(wanted) + "\n", encoding="utf-8")
        log.append("wrote    .gitignore")
        return
    have = {line.strip() for line in dest.read_text(encoding="utf-8").splitlines()}
    missing = [w for w in wanted if w not in have]
    if missing:
        body = dest.read_text(encoding="utf-8")
        sep = "" if body.endswith("\n") else "\n"
        dest.write_text(f"{body}{sep}\n# world harness\n" + "\n".join(missing) + "\n", encoding="utf-8")
        log.append(f"appended .gitignore (+{len(missing)} patterns)")
    else:
        log.append("kept     .gitignore")


def seed_codex_agents(target: Path, log: list[str]) -> None:
    dest = target / "AGENTS.md"
    if dest.exists():
        log.append("kept     AGENTS.md (project-owned)")
        return
    shutil.copy2(KIT / "templates" / "AGENTS.md.tmpl", dest)
    log.append("wrote    AGENTS.md (minimal WORLD guidance)")


def seed_readme(target: Path, log: list[str]) -> None:
    """The page for people: how to run it and which file to open for each change. Without
    it, operating notes ended up in WORLD.md, which every persona reads every turn."""
    dest = target / "README.md"
    if dest.exists():
        log.append("kept     README.md (yours)")
        return
    body = (KIT / "templates" / "README.md.tmpl").read_text(encoding="utf-8")
    dest.write_text(body.replace("{{HARNESS_TITLE}}", target.name), encoding="utf-8")
    log.append("wrote    README.md (fill the … with this harness's paths)")


def seed_pyproject(target: Path, provider: str, log: list[str]) -> None:
    dest = target / "pyproject.toml"
    if dest.exists():
        log.append("kept     pyproject.toml (harness-owned)")
        if provider in {"claude", "both"} and "claude-agent-sdk" not in dest.read_text(encoding="utf-8"):
            log.append("ACTION   add claude-agent-sdk>=0.1.0 before running ClaudeRunner")
        return
    source = KIT / ("templates/pyproject.codex.toml" if provider == "codex" else "pyproject.toml")
    shutil.copy2(source, dest)
    log.append(f"wrote    pyproject.toml ({provider} dependencies)")


def copy_kit(target: Path, trees: list[str], files: list[str], excluded: list[str],
             log: list[str]) -> None:
    """Replace the kit-owned trees and files, leaving out what the profile excludes."""
    for rel in trees:
        src, dest = KIT / rel, target / rel
        verb = "replaced" if dest.exists() else "wrote   "
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".ruff_cache"),
        )
        log.append(f"{verb} {rel}/")
        for rel_out in excluded:
            if rel_out.startswith(rel + "/"):
                path = target / rel_out
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)

    # Files a wider, earlier install left behind (trees above were already recopied).
    for rel in excluded:
        path = target / rel
        if not any(rel.startswith(t + "/") for t in trees) and path.is_file():
            path.unlink()
            log.append(f"removed  {rel} (not in this profile)")

    for rel in files:
        src, dest = KIT / rel, target / rel
        verb = "replaced" if dest.exists() else "wrote   "
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel in BACKUP_ON_REPLACE and dest.is_file() and dest.read_bytes() != src.read_bytes():
            backup = dest.with_name(dest.name + ".bak")
            n = 0
            while backup.exists() or backup.is_symlink():
                n += 1
                backup = dest.with_name(f"{dest.name}.bak.{n}")
            shutil.copy2(dest, backup)
            log.append(f"backed up {rel} -> {backup.relative_to(target).as_posix()}")
        shutil.copy2(src, dest)
        log.append(f"{verb} {rel}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Unpack the world kit into a harness directory.",
        epilog=(
            "A RE-RUN IS DESTRUCTIVE: world/ is deleted before it "
            "are copied, so a file you added inside them is removed and a kit file you "
            "edited is overwritten — neither is named in the log. Commit or stash the "
            "target first and read `git diff` afterwards; that diff is the only report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("target", nargs="?", default=".", help="harness directory (default: cwd)")
    ap.add_argument(
        "--provider",
        choices=PROFILE_CHOICES["provider"],
        help="bot runtime(s) to install (default: the recorded profile, else claude)",
    )
    ap.add_argument(
        "--os",
        choices=("auto", *PROFILE_CHOICES["os"]),
        help="OS the harness runs on (default: the recorded profile, else this machine)",
    )
    ap.add_argument(
        "--channel",
        choices=PROFILE_CHOICES["channel"],
        help="conversation channel; none = poll/schedule only "
             "(default: the recorded profile, else slack)",
    )
    args = ap.parse_args()

    if not (KIT / "world" / "__init__.py").is_file():
        fail(
            f"the bundled kit is missing at {KIT}.\n"
            "  Only SKILL.md was installed — reinstall the whole skill directory:\n"
            "    npx skills add afinit-com/the-world-skills -s the-world-start -a claude-code -g"
        )

    target = Path(args.target).expanduser().resolve()
    if KIT == target or KIT in target.parents:
        fail("refusing to install into the skill's own bundle")
    target.mkdir(parents=True, exist_ok=True)

    log: list[str] = []
    profile = resolve_profile(target, args)
    args.provider = profile["provider"]
    excluded = excluded_paths(profile)
    providers = ("claude", "codex") if args.provider == "both" else (args.provider,)
    trees = COMMON_TREES + [rel for provider in providers for rel in PROVIDER_TREES[provider]]
    files = KIT_FILES + [rel for provider in providers for rel in PROVIDER_FILES[provider]]
    files = [rel for rel in files if rel not in excluded]
    registries = [".claude/settings.json"] if "claude" in providers else []
    registries += [".codex/hooks.json"] if "codex" in providers else []
    blocked = unwritable_dirs(
        [(target / rel).parent for rel in trees + files + SEED_FILES + registries] + [target]
    )
    if blocked:
        fail(
            "cannot write to "
            + ", ".join(str(d.relative_to(target)) + "/" for d in blocked)
            + f" under {target}; nothing was installed.\n"
            "  The host sandbox must allow writes there. Under Codex, `.codex/` and "
            "`.agents/` are protected:\n"
            "  approve the escalation (or run with a sandbox that permits them) and re-run. "
            "Do not copy the rest of the kit by hand."
        )

    copy_kit(target, trees, files, excluded, log)

    seed_pyproject(target, args.provider, log)
    seed_readme(target, log)

    for rel in SEED_FILES:
        src, dest = KIT / rel, target / rel
        if dest.exists():
            log.append(f"kept     {rel} (harness-owned)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_profile(src.read_text(encoding="utf-8"), profile), encoding="utf-8")
        log.append(f"wrote    {rel}")

    if "claude" in providers:
        merge_settings(target, log, profile["os"])
    if "codex" in providers:
        merge_codex_hooks(target, log, profile["os"])
        seed_codex_agents(target, log)
    seed_gitignore(target, log)
    left = [rel for provider, rels in PROVIDER_FILES.items() if provider not in providers
            for rel in rels if (target / rel).exists()]
    if left:  # registries may hold the user's own hooks too, so they are not edited here
        log.append("kept     " + ", ".join(left) + " (not in this profile; remove them and "
                   "their hook entries by hand if no longer wanted)")
    (target / PROFILE_FILE).write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    log.append(f"wrote    {PROFILE_FILE} " + json.dumps(
        {k: v for k, v in profile.items() if k != "schema_version"}))

    print(f"world kit -> {target}")
    for line in log:
        print(f"  {line}")
    print(
        "\nNext (still /the-world-start's job):\n"
        "  1. write harness.py and WORLD.md (see the skill: what must hold, and world/'s surface)\n"
        "  2. uv venv .venv && uv pip install -e .\n"
        "  3. uv run python bin/smoke.py"
    )
    if profile["os"] == "windows":
        print(
            "\nWindows: run these from PowerShell with uv (not WSL). Without uv, the venv's\n"
            "interpreter is .venv\\Scripts\\python.exe. pywin32 and tzdata come with step 2."
        )


if __name__ == "__main__":
    main()
