#!/usr/bin/env python3
"""world template smoke test — run under the harness venv:

    uv run python bin/smoke.py

Verifies the runtime seams WITHOUT Slack tokens or subscription spend, so it is safe to
run any time (and in CI). It exits non-zero if any check fails.

What it covers:
  - imports resolve + the installed claude-agent-sdk supports plugins
  - cron (ScheduleTrigger): occurrence math, restart-safe dedup, and that a freshly
    started daily schedule does NOT fire on boot (first-sight adoption)
  - plugins: marketplace resolution + spawn wiring (a plugin agent gets the Skill tool
    and plugins=; a non-plugin agent gets neither, invariant intact) — OFFLINE, against a
    fake local marketplace, so no external repo is required
  - a dry-run tick renders

Optional live git-sync: set WORLD_SMOKE_MARKETPLACE_URL to a marketplace git URL to also
exercise the real clone path (needs network), e.g.
    WORLD_SMOKE_MARKETPLACE_URL=https://github.com/your-org/skill-registry.git \
        uv run python bin/smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from world import (
    Agent,
    Harness,
    PluginMarketplace,
    PollTrigger,
    Schedule,
    ScheduleTrigger,
    SlackConfig,
)
from world.process.orchestrator import _due_signal
from world.profile import SUPPORTED_PROVIDERS
from world.world_doc import world_doc_problems

# Codex-only installs ship no plugins module and no claude-agent-sdk. Anywhere else a
# failed import is a real failure and must not be mistaken for "Codex-only".
if "claude" in SUPPORTED_PROVIDERS and "codex" not in SUPPORTED_PROVIDERS:
    from world.process import plugins
    from world.process.spawn import _build_options
else:
    try:
        from world.process import plugins
        from world.process.spawn import _build_options
    except ImportError:
        plugins = None

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _tmp_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="world-smoke-"))
    (home / "logs").mkdir()
    (home / "findings").mkdir()
    return home


def _fake_marketplace(home: Path, name: str = "testmp", plugin: str = "demo") -> None:
    """Write a minimal marketplace into home/.claude/marketplaces/<name>/ so resolve_enabled
    has something real to read without a network clone."""
    root = home / ".claude" / "marketplaces" / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": name, "plugins": [{"name": plugin, "source": f"./plugins/{plugin}"}]})
    , encoding="utf-8")
    pdir = root / "plugins" / plugin
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin, "version": "0.0.1"})
    , encoding="utf-8")
    (pdir / "skills" / plugin).mkdir(parents=True)
    (pdir / "skills" / plugin / "SKILL.md").write_text(
        "---\nname: demo\ndescription: a demo skill\n---\nhello\n"
    , encoding="utf-8")


async def main() -> None:
    section("imports + provider adapters")
    check("core imports", True)
    if plugins is not None:
        check("claude-agent-sdk supports plugins", plugins.sdk_supports_plugins())
    else:
        print("  [PASS] Codex-only install intentionally omits claude-agent-sdk")

    section("cron: occurrence math + restart dedup")
    d = Schedule.daily(18, 0, "UTC")
    due = lambda ndt: d.previous_occurrence(ndt).timestamp()  # noqa: E731
    check(
        "daily previous_occurrence is today 18:00",
        d.previous_occurrence(datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc))
        .isoformat()
        .startswith("2026-07-12T18:00"),
    )
    last = due(datetime(2026, 7, 12, 17, 59, tzinfo=timezone.utc))          # adopt yesterday
    check("fires at 18:00:05", last < due(datetime(2026, 7, 12, 18, 0, 5, tzinfo=timezone.utc)))
    last = datetime(2026, 7, 12, 18, 0, 5, tzinfo=timezone.utc).timestamp()  # on_done records
    check("no refire same day 20:00", not last < due(datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)))
    check("no double-run after restart 22:00",
          not last < due(datetime(2026, 7, 12, 22, 0, tzinfo=timezone.utc)))
    check("fires next day 18:00", last < due(datetime(2026, 7, 13, 18, 0, 5, tzinfo=timezone.utc)))
    # weekly/monthly sanity (Feb-31 clamps to Jan-31)
    check("monthly 31st clamps into February",
          Schedule.monthly(31, 0, 0, "UTC")
          .previous_occurrence(datetime(2026, 2, 15, tzinfo=timezone.utc)).date().isoformat()
          == "2026-01-31")

    section("cron: a freshly started schedule does not fire on boot")
    sched = Agent(name="digest", trigger=ScheduleTrigger(Schedule.daily(18, 0, "Asia/Seoul")),
                  write_scope=("logs/digest",))
    h0 = Harness(name="s0", home=_tmp_home(), slack=SlackConfig())
    h0.register(sched)
    st = {"agents": {}}
    h0._init_schedules(st)
    check("first-sight adoption sets last_fired", "last_fired" in st["agents"]["digest"])
    check("does NOT fire immediately on boot",
          _due_signal(sched, st["agents"]["digest"], time.time()) is None)

    section("provider wiring")
    home = _tmp_home()
    poll = Agent(name="prober", trigger=PollTrigger(check=lambda s, n: None),
                 write_scope=("logs/prober",))
    h = Harness(name="smoke", home=home, slack=SlackConfig())
    h.register(poll, sched)
    if plugins is not None:
        _fake_marketplace(home)
        poll.allow_plugins = True
        h.plugins = [PluginMarketplace(name="testmp", git_url="offline-unused",
                                       enabled=("demo", "missing"))]
        cfgs = plugins.plugin_configs(h)
        check("resolves the one real enabled plugin",
              len(cfgs) == 1 and Path(cfgs[0]["path"]).name == "demo")
        o = _build_options(h, poll)
        check("plugin agent gets the Skill tool", "Skill" in o.tools)
        check("plugin agent gets plugins=", len(o.plugins or []) == 1)
        check("strict_mcp_config stays True", o.strict_mcp_config is True)
        o2 = _build_options(h, sched)
        check("non-plugin agent has no Skill tool", "Skill" not in o2.tools)
        check("non-plugin agent has no plugins", not (o2.plugins or []))

        url = os.environ.get("WORLD_SMOKE_MARKETPLACE_URL")
        if url:
            section(f"plugins: LIVE git sync ({url})")
            hl = Harness(name="live", home=_tmp_home(), slack=SlackConfig(),
                         plugins=[PluginMarketplace(name="live", git_url=url, enabled=())])
            await plugins.sync_all(hl)
            check("clone landed on disk",
                  (hl.home / ".claude" / "marketplaces" / "live" / ".git").exists())
        else:
            print("\n(skipping live git-sync — set WORLD_SMOKE_MARKETPLACE_URL to enable)")
    else:
        from world.runners.codex import build_command

        command = build_command(h, poll, None, executable="codex")
        check("Codex clears inherited MCP", "mcp_servers={}" in command)
        check("Codex disables shell by default", "features.shell_tool=false" in command)

    section("WORLD.md holds only the world")
    world_doc = Path(__file__).resolve().parents[1] / "WORLD.md"
    if world_doc.exists():
        problems = world_doc_problems(world_doc.read_text(encoding="utf-8"))
        for problem in problems:
            print(f"  [WARN] WORLD.md {problem}")
        check("WORLD.md has no install log, command, path, or current state", not problems)
    else:
        print("  (no WORLD.md yet — the kit's own checkout)")

    section("dry-run tick renders")
    await h._main_async(argparse.Namespace(once=True, dry_run=True, no_slack=True, tick_interval=60))
    check("dry-run tick completed without error", True)


asyncio.run(main())
if FAILS:
    print(f"\nSMOKE FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("\nSMOKE OK")
