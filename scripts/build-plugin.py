#!/usr/bin/env python3
"""Build a self-contained local marketplace for Codex and Claude Code."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json")
# The three manifests are separate files because each app reads its own and accepts its own
# extra keys (`skills`, Codex's `interface`); no two are
# copies, so they cannot be one file or a symlink. These are the fields they must agree on.
SHARED = ("name", "version", "description", "author", "repository", "license")
# The repository root is itself a marketplace, so a public checkout installs in two lines:
#   claude plugin marketplace add afinit-com/the-world-skills
#   claude plugin install the-world-skills@the-world
# (Codex: `codex plugin marketplace add …` then `codex plugin add …`.) Its catalog files are
# generated from plugin.json by --sync, like the shared manifest fields. The name differs
# from the local build's `the-world-local` so the two can be registered side by side.
PUBLISHED = "the-world"



# Never shipped, wherever under skills/ they turn up: build caches, and everything a
# harness writes while one is tried out inside the kit (the same names as
# templates/gitignore.tmpl). Names, not paths: shutil.ignore_patterns matches at any depth.
PACKAGE_IGNORE = (
    "__pycache__", "*.pyc", ".venv", ".pytest_cache", ".ruff_cache", ".DS_Store", ".git",
    "harness.env", ".harness.env.*", ".env", "WORLD.html",
    "state.json", "ledger.jsonl", "STATUS.md", "HEARTBEAT.md", ".the-world", ".slack",
    "logs", "findings", "tasks", "worktrees", "world-agent-scope.json",
    "client_secret*.json", "*service-account*.json",
)

def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def marketplaces(manifest: dict, market: str, source: str, display: str) -> dict[str, dict]:
    """The two catalog files, keyed by path relative to the marketplace root. Codex wraps
    the source in an object; Claude takes it as a string."""
    name = manifest["name"]
    common = {
        "name": market,
        "owner": manifest["author"],
        "metadata": {"description": manifest["description"], "version": manifest["version"]},
        "plugins": [{"name": name, "source": source, "description": manifest["description"]}],
    }
    return {
        ".claude-plugin/marketplace.json": common,
        ".agents/plugins/marketplace.json": {
            "name": market,
            "interface": {"displayName": display},
            "plugins": [{
                "name": name,
                "source": {"source": "local", "path": source},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }],
        },
    }


def published(manifest: dict) -> dict[str, dict]:
    return marketplaces(manifest, PUBLISHED, "./", "The World")


def sync() -> list[str]:
    """Copy the shared fields from the root manifest into the three app manifests, and
    regenerate the repository's own marketplace catalogs from it.

    App-specific keys are left alone, and a file whose shared fields already match is not
    rewritten — so a version bump is one edit to plugin.json plus this, and the diff shows
    only what moved.
    """
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    missing = [field for field in SHARED if field not in manifest]
    if missing:
        raise ValueError(f"plugin.json is missing {', '.join(missing)}")
    changed = []
    for relative in MANIFESTS[1:]:
        path = ROOT / relative
        native = json.loads(path.read_text(encoding="utf-8"))
        updates = {field: manifest[field] for field in SHARED
                   if native.get(field) != manifest[field]}
        if not updates:
            continue
        native.update(updates)
        write_json(path, native)
        changed.append(f"{relative}: {', '.join(updates)}")
    for relative, expected in published(manifest).items():
        path = ROOT / relative
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        if current == expected:
            continue
        write_json(path, expected)
        changed.append(f"{relative}: {'regenerated' if current is not None else 'created'}")
    return changed


def build(destination: Path, *, force: bool = False) -> Path:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    for relative in MANIFESTS[1:]:
        native = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        for field in SHARED:
            if native.get(field) != manifest.get(field):
                raise ValueError(f"{relative}: {field} differs from plugin.json "
                                 "— run with --sync")
    for relative, expected in published(manifest).items():
        path = ROOT / relative
        if not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != expected:
            raise ValueError(f"{relative} is out of date with plugin.json — run with --sync")
    if destination.resolve().is_relative_to((ROOT / "skills").resolve()):
        raise ValueError("destination must be outside skills/")
    if destination.exists():
        # Refusing an existing directory is the safe default — this writes a whole tree
        # and must never merge into someone's unrelated folder. But refusing with no way
        # to say "yes, that one" meant every rebuild invented a new name, and the five
        # directories that accumulated under dist/ on 2026-09-12 (…-verified, …-final)
        # are what that looks like after an afternoon. --force replaces exactly one
        # directory that build() itself could have produced.
        if not force:
            raise ValueError(
                f"{destination} already exists — pass --force to replace it, "
                "or give a new path"
            )
        if not (destination / "plugins").is_dir():
            raise ValueError(
                f"{destination} exists but does not look like a marketplace build "
                "(no plugins/ inside) — refusing to delete it"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    name = manifest["name"]
    plugin = destination / "plugins" / name
    plugin.mkdir(parents=True)
    for relative in MANIFESTS:
        target = plugin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    # The published catalogs point at "./", which is just as true inside the package. Carrying
    # them keeps the packaged build-plugin.py able to rebuild (its drift check reads them) and
    # makes the package directory a marketplace on its own.
    for relative, catalog in published(manifest).items():
        write_json(plugin / relative, catalog)
    ignore = shutil.ignore_patterns(*PACKAGE_IGNORE)
    for skill_file in sorted((ROOT / "skills").glob("*/SKILL.md")):
        shutil.copytree(skill_file.parent, plugin / "skills" / skill_file.parent.name, ignore=ignore)
    for relative in ("docs/plugins.md", "docs/plugins.ko.md"):
        target = plugin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for filename in ("README.md", "README.ko.md", "LICENSE", "NOTICE", "COPYING"):
        if (ROOT / filename).is_file():
            shutil.copy2(ROOT / filename, plugin / filename)
    (plugin / "scripts").mkdir()
    shutil.copy2(Path(__file__), plugin / "scripts" / Path(__file__).name)
    for relative, catalog in marketplaces(manifest, "the-world-local", f"./plugins/{name}",
                                          "The World (local)").items():
        write_json(destination / relative, catalog)
    return plugin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?",
                        help="Output directory; refused if it exists unless --force")
    parser.add_argument("--force", action="store_true",
                        help="Replace destination if it is a previous build of this script")
    parser.add_argument("--sync", action="store_true",
                        help="Write plugin.json's shared fields into the three app "
                             "manifests and exit, building nothing")
    args = parser.parse_args()
    if args.sync:
        # Deliberately not part of a build: a build must not edit tracked source files
        # behind your back, and the check above already refuses to ship a drifted set.
        if args.destination is not None or args.force:
            parser.error("--sync takes no destination and does not combine with --force")
        try:
            changed = sync()
        except (OSError, ValueError) as error:
            parser.exit(1, f"Sync failed: {error}\n")
        print("\n".join(changed) if changed else "Already in sync.")
        return
    if args.destination is None:
        parser.error("destination is required unless --sync is given")
    try:
        plugin = build(args.destination.expanduser().absolute(), force=args.force)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Build failed: {error}\n")
    print(f"Plugin: {plugin}\nMarketplace: {plugin.parents[1]}\nNot installed or published.")


if __name__ == "__main__":
    main()
