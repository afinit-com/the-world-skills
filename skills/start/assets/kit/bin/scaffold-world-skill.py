#!/usr/bin/env python3
"""Create one world-word skill wrapper for the selected authoring host.

`agent` and `tool` are the creation words (adding a worker, adding a tool); `verify` is the
word for checking the harness end to end (docs/verify.md), and `ops` the word for running
it: start, stop, restart, status, the watchdog (docs/operations.md).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path


def unwritable_dirs(dirs: list[Path]) -> list[Path]:
    """Directories in `dirs` that refuse a write, probed before any wrapper is written.

    Codex's `workspace-write` sandbox protects `.agents/`, so `--host codex|all` used to
    write the other hosts' wrappers and then crash. Directories the probe created are
    removed again on refusal. Same check as install-kit.py's, which this cannot import.
    """
    created: list[Path] = []
    blocked: list[Path] = []
    for d in sorted(set(dirs)):
        if any(b == d or b in d.parents for b in blocked):
            continue
        missing = [p for p in (d, *d.parents) if not p.exists()]
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=d, prefix=".scaffold-probe-"):
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


def profile_provider(root: Path) -> str:
    """The provider install-kit.py recorded, so a Codex-only harness gets no .claude/ wrapper."""
    try:
        return json.loads((root / "project-profile.json").read_text(encoding="utf-8")).get(
            "provider", "both")
    except (OSError, ValueError):
        return "both"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("word")
    parser.add_argument("flow", choices=("agent", "tool", "verify", "ops"))
    parser.add_argument("--provider", choices=("claude", "codex", "both"),
                        default=profile_provider(Path(__file__).resolve().parents[1]))
    parser.add_argument("--host", choices=("claude", "codex", "all"),
                        help="Authoring app; overrides the legacy --provider wrapper destinations")
    parser.add_argument("--universe", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--extra-trigger-phrases", default="")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.word):
        parser.error("word must use lowercase letters, digits, and single hyphens")

    root = Path(__file__).resolve().parents[1]
    # A tool word carries its own flow (split the request, place each piece, confirm a
    # proposal table); a verify word carries the checking flow; an ops word the running
    # one; an agent word uses the shared body.
    template = {"tool": "skill-wrapper-tool.md.tmpl",
                "verify": "verify-wrapper.md.tmpl",
                "ops": "ops-wrapper.md.tmpl"}.get(args.flow, "skill-wrapper.md.tmpl")
    body = (root / "templates" / template).read_text(encoding="utf-8")
    values = {
        "word": args.word,
        "flow_file": args.flow,
        "universe_name": args.universe,
        "flavor_description": args.description,
        "extra_trigger_phrases": args.extra_trigger_phrases,
    }
    for key, value in values.items():
        body = body.replace("{{" + key + "}}", value)

    providers = ("claude", "codex") if args.provider == "both" else (args.provider,)
    if args.host:
        providers = ("claude", "codex") if args.host == "all" else (args.host,)
    roots = {"claude": ".claude/skills", "codex": ".agents/skills"}
    destinations = [root / roots[provider] / args.word / "SKILL.md" for provider in providers]
    blocked = unwritable_dirs([d.parent for d in destinations if not d.exists()])
    if blocked:
        print(
            "scaffold-world-skill: cannot write to "
            + ", ".join(str(d.relative_to(root)) + "/" for d in blocked)
            + "; no wrapper was written.\n"
            "  The host sandbox must allow writes there. Under Codex, `.agents/` is "
            "protected:\n"
            "  approve the escalation (or run with a sandbox that permits it) and re-run.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    for destination in destinations:
        if destination.exists():
            print(f"kept {destination.relative_to(root)} (project-owned)")
            continue
        destination.write_text(body, encoding="utf-8")
        print(f"wrote {destination.relative_to(root)}")


if __name__ == "__main__":
    main()
