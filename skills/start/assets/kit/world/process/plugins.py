"""Plugin/marketplace sync — the git-backed capability registry, LLM-free.

The SDK's `ClaudeAgentOptions.plugins=` takes LOCAL paths only; resolving a git
marketplace (clone from GitHub/GitLab) is a CLI feature the SDK does not carry. So world
does the git itself here — clone on first sight, fetch+hard-reset to the tracked ref on
every refresh — into `home/.claude/marketplaces/<name>/`, then reads that repo's
`.claude-plugin/marketplace.json` to turn each enabled plugin name into the local dir the
spawn hands over.

Everything in this module is deterministic subprocess work. It rides a tick_task (see
Harness._register_plugin_refresh), so a network blip logs and retries next tick, never
crashes the loop, and never blocks a Claude call.

FULL-TRUST boundary (stated, not hidden): a resolved plugin dir is passed to the agent
whole — skills, commands, AND its hooks / `.mcp.json` MCP servers. strict_mcp_config does
not block plugin-provided MCP servers, and nothing here strips a plugin's hooks. That is
the trust decision PluginMarketplace documents: add only a registry you trust to run code
as you. What this module DOES guarantee is no *silent* loading — `enabled` is an explicit
allowlist, and any name missing from the synced marketplace.json is logged, not skipped
into the void.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions

from ..toolkit import run_cmd

if TYPE_CHECKING:
    from ..harness import Harness
    from ..types import PluginMarketplace


def marketplaces_dir(harness: "Harness") -> Path:
    return harness.home / ".claude" / "marketplaces"


def cache_dir(harness: "Harness", mp: "PluginMarketplace") -> Path:
    return marketplaces_dir(harness) / mp.name


def sdk_supports_plugins() -> bool:
    """True if the installed claude-agent-sdk's ClaudeAgentOptions carries a `plugins`
    field. Older SDKs don't; without this check we'd pass a kwarg that TypeErrors every
    spawn, or (worse) set an attribute the SDK ignores — a silent no-op. Callers log when
    plugins are wanted but unsupported, so the gap is visible, not mysterious."""
    return "plugins" in {f.name for f in dataclasses.fields(ClaudeAgentOptions)}


async def sync_marketplace(harness: "Harness", mp: "PluginMarketplace") -> str | None:
    """git clone (first time) or fetch + hard-reset to the tracked ref. Returns the short
    HEAD sha, or None on failure — leaving the last good clone in place. Non-fatal."""
    dest = cache_dir(harness, mp)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not (dest / ".git").is_dir():
        rc, out = await run_cmd(
            "git", "clone", "--depth", "1", "--branch", mp.ref, mp.git_url, str(dest),
            cwd=str(dest.parent), timeout=300,
        )
        if rc != 0:
            # --branch rejects a raw commit sha; retry as a full clone then checkout.
            rc, out = await run_cmd("git", "clone", mp.git_url, str(dest),
                                    cwd=str(dest.parent), timeout=300)
            if rc == 0:
                await run_cmd("git", "checkout", mp.ref, cwd=str(dest))
        if rc != 0:
            harness.log(f"plugin sync: clone failed for {mp.name}: {out[:200]}")
            return None
    else:
        rc, out = await run_cmd("git", "fetch", "--depth", "1", "origin", mp.ref, cwd=str(dest))
        if rc == 0:
            rc, out = await run_cmd("git", "reset", "--hard", "FETCH_HEAD", cwd=str(dest))
        if rc != 0:
            harness.log(f"plugin sync: fetch/reset failed for {mp.name}: {out[:200]}")
            return None

    rc, sha = await run_cmd("git", "rev-parse", "--short", "HEAD", cwd=str(dest))
    return sha.strip() if rc == 0 else None


def _marketplace_manifest(dest: Path) -> dict | None:
    try:
        return json.loads((dest / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def resolve_enabled(harness: "Harness", mp: "PluginMarketplace") -> list[dict]:
    """The `[{"type": "local", "path": ...}]` configs for mp's enabled plugins, read from
    the synced marketplace.json. Every enabled name that doesn't resolve is logged — a
    typo'd plugin name must not vanish silently into "0 plugins loaded"."""
    dest = cache_dir(harness, mp)
    manifest = _marketplace_manifest(dest)
    if manifest is None:
        harness.log(f"plugin: {mp.name} not synced or marketplace.json unreadable — 0 plugins")
        return []
    by_name = {p.get("name"): p for p in manifest.get("plugins", [])}
    configs: list[dict] = []
    for name in mp.enabled:
        entry = by_name.get(name)
        if entry is None:
            harness.log(f"plugin: '{name}' not in {mp.name}/marketplace.json — skipped (check the name)")
            continue
        source = entry.get("source", "")
        path = (dest / source).resolve()
        if not path.is_dir():
            harness.log(f"plugin: '{name}' source '{source}' missing on disk — skipped")
            continue
        configs.append({"type": "local", "path": str(path)})
    return configs


def plugin_configs(harness: "Harness") -> list[dict]:
    """Every enabled plugin's local config across all configured marketplaces — the list
    the spawn passes to `ClaudeAgentOptions.plugins=`."""
    configs: list[dict] = []
    for mp in harness.plugins:
        configs.extend(resolve_enabled(harness, mp))
    return configs


# `https://user:token@host/…` in a clone URL comes back in git's own error text.
_URL_USERINFO = re.compile(r"://[^/@\s]+@")


@dataclasses.dataclass(frozen=True)
class SyncSummary:
    """What one marketplace sync came to. `sha` is None when the clone or fetch failed
    (the last good clone, if any, is still in place). `errors` holds every line the sync
    logged about this marketplace, with URL credentials removed, because a summary may
    be posted to a channel (Harness._post_sync_summary)."""

    marketplace: str
    sha: str | None
    resolved: int
    enabled: int
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.sha is not None and self.resolved == self.enabled and not self.errors

    def line(self) -> str:
        return (f"{self.marketplace} @ {self.sha or '??'} — "
                f"{self.resolved}/{self.enabled} enabled plugin(s) resolved")


class _LogTap:
    """The harness, with the lines it logs also kept. sync_marketplace and
    resolve_enabled say what went wrong only through harness.log, and a summary needs
    those words too."""

    def __init__(self, harness: "Harness") -> None:
        self._harness = harness
        self.lines: list[str] = []

    def log(self, text: str) -> None:
        self.lines.append(text)
        self._harness.log(text)

    def __getattr__(self, name: str):
        return getattr(self._harness, name)


async def sync_all(harness: "Harness") -> list[SyncSummary]:
    """Sync every configured marketplace and log a one-line summary each. Called once at
    startup and again on the refresh tick_task. Returns one SyncSummary per marketplace,
    in order, so the caller can report them somewhere other than the log."""
    if not harness.plugins:
        return []
    if not sdk_supports_plugins():
        harness.log(
            "plugin: this claude-agent-sdk has no ClaudeAgentOptions.plugins field — "
            "marketplaces will sync but agents cannot load them; upgrade the SDK"
        )
    summaries: list[SyncSummary] = []
    for mp in harness.plugins:
        tap = _LogTap(harness)
        sha = await sync_marketplace(tap, mp)
        resolved = len(resolve_enabled(tap, mp))
        summary = SyncSummary(
            marketplace=mp.name, sha=sha, resolved=resolved, enabled=len(mp.enabled),
            errors=tuple(_URL_USERINFO.sub("://[redacted]@", line) for line in tap.lines),
        )
        harness.log(f"plugin: {summary.line()}")
        summaries.append(summary)
    return summaries
