"""Harness — the object a harness project's harness.py builds and runs.

Single asyncio process, two trigger models as concurrent tasks:
poll loop (system-initiated) + Slack listener (human-initiated). Run it from a
foreground terminal / tmux pane so the logs stay visible — this is deliberately
NOT a system daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal as _signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from .env import load_harness_env
from .process.orchestrator import poll_loop
from .process.runlog import RUN_LOG_MAX_BYTES
from .process.scheduler import Scheduler, scheduler_loop
from .process.state import load_state, save_state
from .profile import CHANNEL, DEFAULT_PROVIDER, SUPPORTED_PROVIDERS, reinstall_hint
from .types import (
    Agent,
    ApprovalPhrases,
    DiscordConfig,
    MentionTrigger,
    Persona,
    PluginMarketplace,
    PollTrigger,
    Relationship,
    ScheduleTrigger,
    SlackConfig,
    StatusPhrases,
)
from .world_doc import world_doc_problems

# The guard scripts this kit installs. preflight_guard_hooks only vets commands that
# name one of these, so a harness's own hooks never trip the warning.
_GUARD_SCRIPTS = (
    "world-scope-guard.py",
    "world-git-guard.py",
    "world-integrity-guard.py",
    "durable-config-guard.py",
    "world-guard.py",
)

_INTERVALS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _probe_guard(parts: tuple[str, ...], root: Path) -> str | None:
    interpreter = shutil.which(parts[0])
    script = Path(parts[-1])
    if not script.is_absolute():
        script = root / script
    if interpreter is None:
        return f"{parts[0]}: not on PATH — {script.name} cannot run"
    if not script.is_file():
        return f"{script}: missing — that guard is not enforcing"
    payload = json.dumps({
        "tool_name": "Read", "tool_input": {"file_path": str(root / "WORLD.md")},
        "cwd": str(root),
    })
    try:
        probe = subprocess.run(
            [interpreter, *parts[1:-1], str(script)], input=payload, text=True, encoding="utf-8", errors="replace",
            capture_output=True, cwd=root, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"{script}: probe timed out after 10s — guard cannot load or decide"
    except OSError as exc:
        return f"{script}: probe could not launch ({exc}) — guard cannot load or decide"
    if probe.returncode not in (0, 2):
        diagnostic = next((line.strip() for line in reversed(probe.stderr.splitlines())
                           if line.strip()), "no stderr")
        return f"{script}: probe exit {probe.returncode} — {diagnostic}"
    return None


def _interval_s(spec: str | int) -> int:
    if isinstance(spec, int):
        return spec
    return int(spec[:-1]) * _INTERVALS[spec[-1]]


@dataclass
class TickTask:
    """Direct (non-LLM) periodic bookkeeping riding the poll loop — keepalives,
    health pings. fn(state, listener) is awaited; failures are logged and retried
    next tick, never fatal."""

    name: str
    interval_s: int
    fn: Callable[[dict, object], Awaitable[None]]


class Harness:
    def __init__(
        self,
        name: str,
        *,
        home: str | Path | None = None,
        world_doc: str = "WORLD.md",
        slack: SlackConfig | None = None,
        discord: DiscordConfig | None = None,
        main_persona: Persona | None = None,
        model: str = "sonnet",
        tick_interval: int = 60,
        max_concurrent_runs: int = 2,
        task_types: list | None = None,
        git_root: str | Path | None = None,
        default_branch: str = "main",
        mention_tools: list | None = None,
        pre_approved_tools: list[str] | None = None,
        status_phrases: StatusPhrases | None = None,
        approval_phrases: ApprovalPhrases | None = None,
        mention_model: str = "haiku",
        run_footer: bool = True,
        extra_personas: list[Persona] | None = None,
        report_footer: str | None = None,
        finding_footer: str | None = None,  # the name before 0.2.7
        knowledge_proposal_agent: str | None = None,
        plugins: list["PluginMarketplace"] | None = None,
        plugin_refresh: str | int = "6h",
        mention_plugins: bool = True,
        runner=None,
        provider: str = DEFAULT_PROVIDER,
        codex_model: str | None = None,
        channel=None,
        relationships: list[Relationship] | None = None,
    ) -> None:
        # The env file is already loaded — `import world` did it (see this package's
        # __init__, and why it must happen there rather than here). This call is
        # idempotent (setdefault) and exists only to learn WHICH file was read, so
        # the startup line can say so: a missing harness.env is otherwise
        # indistinguishable from "Slack deliberately off" everywhere downstream.
        self._env_file = load_harness_env()
        self.name = name
        self.home = (Path(home) if home else Path.cwd()).resolve()
        self.world_doc = self.home / world_doc
        self.slack = slack or SlackConfig()
        # EXPERIMENTAL (docs/discord.md). Set → the Discord gateway replaces the Slack
        # listener for this process; None (the default) leaves Slack exactly as it was.
        self.discord = discord
        self.main_persona = main_persona or Persona(key="main", display=name)
        # Posting identities do not always have an agent of their own. Deterministic
        # tick tasks and approval-only responders may still need distinct identities,
        # so deriving the roster purely from agents would silently drop them.
        self.extra_personas = list(extra_personas or [])
        # How many agent runs may be in flight at once. The tick only enqueues, so this
        # is the only thing bounding provider spend and local load — raise it when runs
        # are mostly waiting on I/O, keep it low when they are mostly worktree-heavy.
        self.max_concurrent_runs = max(1, max_concurrent_runs)
        # name -> TaskType. What "done" means for each kind of work, as code. An
        # unregistered type falls back to "finished and left an artifact", which is the
        # contract spawn.enforce_completion already applies.
        self.task_types = {t.name: t for t in (task_types or [])}
        self.relationships = list(relationships or [])
        self.model = model
        self.tick_interval = tick_interval
        # mutates:true agents need a git repo to cut worktrees from; a harness
        # watching external systems only can leave this as home even without git.
        self.git_root = Path(git_root).resolve() if git_root else self.home
        self.default_branch = default_branch
        # Tools available to listener.py's ad-hoc thread-reply responder
        # (_handle_mention). That responder has no MentionTrigger agent of its
        # own to configure, and headless Bash calls are denied with no human
        # around to approve them (no pre-approved command is ever allowlisted
        # for a spawned session) — so without this, a thread reply asking for
        # anything beyond prose always hits "permission denials: Bash:...".
        self.mention_tools = list(mention_tools or [])
        # Tool NAMES the approval doctrine does not apply to. world gates in two
        # independent places — make_approval_gated in the call path (Persona.mention_tools)
        # and RULES_TEMPLATE's prohibition in every prompt — and opening only the first
        # leaves a tool that is callable but that the model refuses to call. Declaring a
        # name here opens the second. It changes no enforcement: a tool must already be
        # in the ungated slot for this to mean anything.
        self.pre_approved_tools = list(pre_approved_tools or [])
        # Ad-hoc mention replies (listener.py's _handle_mention) are usually
        # quick conversational turns, not deep engineering work — default to a
        # cheaper/faster model than the poll agents' (self.model) for cost
        # optimization. Set to self.model (or any tier) to opt out per harness.
        self.mention_model = mention_model
        # The small grey cost/latency line under a posted message (world/slack/run_footer.py).
        # On by default: the numbers are already measured, and a harness that hides what it
        # spends is the kind of invisible cap this kit refuses everywhere else. Set False
        # when the channel is shared with people who should not see per-message spend.
        self.run_footer = run_footer
        # Harness-wide default for ToolTrail's status indicators (listener.py);
        # a Persona with its own status_phrases overrides this — see
        # status_phrases_for().
        self.status_phrases = status_phrases or StatusPhrases()
        # Every user-visible string in the approval flow (and the bot-hop stop
        # notice). Korean by default so existing harnesses are untouched; a harness
        # whose channel is not Korean overrides it, because answering an English
        # question and then offering a `✅ 승인` button is two languages in one thread.
        self.approval_phrases = approval_phrases or ApprovalPhrases()
        # The line that closes a single finding announcement in the bot channel — the
        # reply hint + the approval-doctrine reminder, in the harness's own voice. `{approver}`
        # is substituted with the addressee mention (or ""). Overridden per harness so the
        # announcement sounds like the bots, not like the kit.
        self.report_footer = report_footer or finding_footer or (
            "이 스레드에 답장하면 대화로 이어갈 수 있습니다. 실제 발행/실행은 여전히 "
            "{approver}승인 후에만."
        )
        # Optional promotion flow for one agent whose findings are knowledge proposals.
        # The agent name is configuration, never a kit-level persona or world name.
        if knowledge_proposal_agent and (
            Path(knowledge_proposal_agent).name != knowledge_proposal_agent
            or knowledge_proposal_agent in {".", ".."}
        ):
            raise ValueError("knowledge_proposal_agent must be one agent name, not a path")
        self.knowledge_proposal_agent = knowledge_proposal_agent

        # Git-backed capability registries this harness syncs and hands to plugin-enabled
        # agents (see world.process.plugins). Empty by default — a harness that wants none
        # touches nothing. mention_plugins gates whether the ad-hoc mention responder gets
        # them automatically (the "user query comes in → find and run a plugin" path); a
        # poll agent opts in per-agent with Agent.allow_plugins. plugin_refresh is how
        # often the marketplaces re-sync, as a tick_task.
        self.plugins: list["PluginMarketplace"] = list(plugins or [])
        self.plugin_refresh_s = _interval_s(plugin_refresh)
        self.mention_plugins = mention_plugins
        # Agent execution is a provider adapter. None preserves the historical Claude
        # default and resolves it lazily only when an agent actually runs, so importing
        # the core does not require either provider SDK.
        self.runner = runner
        if provider not in {"claude", "codex"}:
            raise ValueError("provider must be 'claude' or 'codex'")
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"provider={provider!r}: {reinstall_hint(provider='both')}")
        if plugins and "claude" not in SUPPORTED_PROVIDERS:  # plugins.py is Claude-only
            raise ValueError(f"plugins=: {reinstall_hint(provider='both')}")
        if discord is not None and CHANNEL not in ("discord", "both"):
            raise ValueError(f"discord=: {reinstall_hint(channel='discord')}")
        self.provider = provider
        self.codex_model = codex_model
        self.channel = channel

        self.agents: list[Agent] = []
        self.tick_tasks: list[TickTask] = []

        # Layout inside home:
        #   tasks/<id>/work/   what a run produced (world/process/task.py); the
        #                      completion contract for writes_report agents
        #   logs/harness.jsonl one line per run, created on run()
        #   findings/<agent>/  promoted state changes, created only when one is written
        #   state.json         single source of truth
        #   HEARTBEAT.md       regenerated human-readable view (never hand-edit)
        #   .the-world/        generated by the kit: agent scope, worktrees
        self.logs_dir = self.home / "logs"
        self.findings_dir = self.home / "findings"
        self.state_path = self.home / "state.json"
        # HEARTBEAT.md is the committed watchboard — subscriptions, what awaits the
        # approver, what is open, what was found. It changes only when one of those
        # changes. STATUS.md is the volatile view (liveness, spend, live threads),
        # regenerated every tick and gitignored. Splitting them is what makes the first
        # one committable at all: a file carrying a clock is dirty a minute after every
        # commit, forever.
        self.heartbeat_path = self.home / "HEARTBEAT.md"
        self.status_path = self.home / "STATUS.md"
        self.run_log_path = self.logs_dir / "harness.jsonl"
        # Rolled daily to logs/harness.<YYYY-MM-DD>.jsonl.gz and early past this size
        # (world/process/runlog.py). Rolled days are kept forever unless harness.py sets
        # run_log_keep_days to a number of days; None means the disk keeps growing by
        # about one compressed day per day.
        self.run_log_max_bytes: int | None = RUN_LOG_MAX_BYTES
        self.run_log_keep_days: int | None = None
        # Append-only record of every human decision. Unlike state.json (rewritten each
        # tick, holds what is currently true) this holds what happened, forever.
        self.ledger_path = self.home / "ledger.jsonl"
        # Provider-neutral: a Codex-only harness has no `.claude/`, and both runtimes cut
        # worktrees the same way.
        self.worktrees_dir = self.git_root / ".the-world" / "worktrees"
        # Read by both providers' hook adapters. It is regenerated from self.agents on
        # every startup instead of hand-maintained, so it cannot drift from the Agent
        # declarations that are the source of truth. One file, one path.
        self.agent_scope_path = self.home / ".the-world" / "agent-scope.json"
        # Written by kits before 0.2.3; removed on startup so no stale copy is mistaken
        # for the live table.
        self.legacy_agent_scope_path = self.git_root / ".claude" / "world-agent-scope.json"

    # --- registration (the /the-world:agent and /the-world:tool skills append these calls) ---

    def register(self, *agents: Agent) -> None:
        for agent in agents:
            if any(a.name == agent.name for a in self.agents):
                raise ValueError(f"duplicate agent name: {agent.name}")
            self.agents.append(agent)

    def tick_task(self, interval: str | int, name: str | None = None):
        def deco(fn):
            self.tick_tasks.append(TickTask(name or fn.__name__, _interval_s(interval), fn))
            return fn
        return deco

    # --- lookups ------------------------------------------------------------

    @property
    def poll_agents(self) -> list[Agent]:
        return [a for a in self.agents if isinstance(a.trigger, PollTrigger)]

    @property
    def scheduled_agents(self) -> list[Agent]:
        return [a for a in self.agents if isinstance(a.trigger, ScheduleTrigger)]

    def agent_by_name(self, name: str) -> Agent | None:
        """Resolve a task's recorded runner. The task record stores the NAME, not the
        object, so a task that outlives a restart still finds its agent."""
        return next((a for a in self.agents if a.name == name), None)

    @property
    def system_agents(self) -> list[Agent]:
        """Every system-initiated agent the poll loop drives each tick — poll (event)
        and scheduled (wall-clock cron). Mention agents are human-initiated and excluded."""
        return [
            a for a in self.agents if isinstance(a.trigger, (PollTrigger, ScheduleTrigger))
        ]

    @property
    def personas(self) -> dict[str, Persona]:
        personas = {self.main_persona.key: self.main_persona}
        for p in self.extra_personas:
            personas[p.key] = p
        for a in self.agents:
            if a.persona:
                personas[a.persona.key] = a.persona  # an agent's persona wins on key collision
        return personas

    def naming_problems(self) -> list[str]:
        """Names that make one character hard to find. A worker is named three ways: the
        Agent's name (state, task folders, HEARTBEAT, scope), its Persona's key (handoffs,
        relationships), and the display name people see. When the first two differ, a
        person reading STATUS.md sees `watcher` and WORLD.md says `saebyeok`, and nothing
        but the code says they are one character. Two personas sharing a key silently
        replace each other in `personas`."""
        problems = []
        seen: dict[str, str] = {self.main_persona.key: self.main_persona.display}
        for agent in self.agents:
            persona = agent.persona
            if persona is None:
                continue
            if persona.key != agent.name:
                problems.append(f"agent {agent.name!r} speaks as persona {persona.key!r}; name "
                                f"them the same so one word finds the character everywhere")
            if seen.get(persona.key, persona.display) != persona.display:
                problems.append(f"persona key {persona.key!r} is both {seen[persona.key]!r} and "
                                f"{persona.display!r}; the later one replaces the first")
            seen.setdefault(persona.key, persona.display)
        return problems

    def persona_for_agent(self, agent_name: str) -> Persona:
        for a in self.agents:
            if a.name == agent_name and a.persona:
                return a.persona
        return self.main_persona

    def status_phrases_for(self, identity: str) -> StatusPhrases:
        """A Persona's own status_phrases (voice-matched), or this harness's
        default if that persona didn't set one."""
        persona = self.personas.get(identity)
        if persona and persona.status_phrases:
            return persona.status_phrases
        return self.status_phrases

    def world_text(self) -> str:
        return self.world_doc.read_text(encoding="utf-8") if self.world_doc.exists() else ""

    def log(self, msg: str) -> None:
        # ISO 8601 with the UTC offset (2026-10-14T09:00:00+09:00), so a line read on
        # another machine, or wrapped by a launcher, needs no second timestamp.
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"{stamp} [{self.name}] {msg}", flush=True)

    def preflight_mutates(self) -> list[str]:
        """Say at startup, not at the first real run, when a mutates=True worker can never run.

        Such a worker runs only in a git worktree cut from git_root (process/worktrees.py
        run_worktree), so outside a git repository every run of it is refused. That is the
        safe outcome, but it surfaced only when the schedule first fired, possibly the next
        morning. Most workers that hit this did not need mutates at all: a docs, CS-reply or
        research worker writes through `write_scope` and asks before anything outward, and
        needs no git. Returns the warnings it logged, for tests."""
        names = [a.name for a in self.agents if a.mutates]
        if not names:
            return []
        try:
            probe = subprocess.run(
                ["git", "-C", str(self.git_root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            in_repo = probe.returncode == 0
        except OSError:  # no git on PATH: no worktree can be cut either
            in_repo = False
        if in_repo:
            return []
        msg = (
            f"⚠️ {', '.join(names)}: mutates=True, but its work target (git_root = "
            f"{self.git_root}) is not a git repository, so every run of it will be "
            "refused. If it does not edit a git repository's files (docs, CS replies, "
            "research), set mutates=False and bound its writes with write_scope. If it "
            "does edit one, git_root must point at that repository."
        )
        self.log(msg)
        return [msg]

    def preflight_guard_hooks(self) -> list[str]:
        """Say out loud when a registered guard hook cannot actually run.

        The guards are the enforcement — `write_scope` is a comment until
        world-scope-guard.py denies the tool call. But a hook whose command fails to
        execute exits 127, and Claude Code treats any non-zero code that is not 2 as a
        NON-blocking error: the tool call proceeds, the notice goes to the user's
        transcript and never to the model. A missing interpreter therefore leaves an
        agent write-unblocked while the harness looks healthy. That is the one failure
        mode this kit must not have quietly, so it gets checked at startup, where the
        log is being read anyway.

        Only commands naming a kit guard script are inspected; anything else in the
        registry is the harness's own business and a warning about it would be the
        false positive that teaches people to ignore this line.
        """
        problems: list[str] = []
        checked: set[tuple[str, ...]] = set()
        registries = [
            (self.git_root / ".claude" / "settings.json", "PreToolUse"),
            (self.git_root / ".codex" / "hooks.json", "PreToolUse"),
        ]
        for path, event in registries:
            if not path.exists():
                continue
            try:
                groups = json.loads(path.read_text(encoding="utf-8")).get("hooks", {}).get(event, [])
            except (json.JSONDecodeError, OSError) as exc:
                problems.append(f"{path.name}: unreadable ({exc}) — guard hooks unverified")
                continue
            for group in groups:
                for hook in group.get("hooks", []):
                    command = hook.get("command", "")
                    if not any(name in command for name in _GUARD_SCRIPTS):
                        continue
                    # Both root spellings the kit ships resolve to the harness root.
                    resolved = command.replace("${CLAUDE_PROJECT_DIR}", str(self.git_root))
                    resolved = resolved.replace("$CLAUDE_PROJECT_DIR", str(self.git_root))
                    resolved = resolved.replace(
                        "$(git rev-parse --show-toplevel)", str(self.git_root)
                    )
                    try:
                        parts = shlex.split(resolved, posix=os.name != "nt")
                    except ValueError:
                        parts = resolved.split()
                    parts = tuple(part.strip("\"'") for part in parts)
                    if not parts or parts in checked:
                        continue
                    checked.add(parts)
                    problem = _probe_guard(parts, self.git_root)
                    if problem and problem not in problems:
                        problems.append(problem)
        if problems:
            self.log("⚠️  GUARD HOOKS DISABLED — agents are NOT write-blocked:")
            for problem in problems:
                self.log(f"      {problem}")
            self.log("      fix these before trusting any write_scope declaration")
        return problems

    def write_agent_scope(self) -> None:
        """Dump {agent.name: {write_scope, task_allowed}} for world-scope-guard.py to
        read — see agent_scope_path's comment in __init__ for why this is a JSON
        dump rather than a hand-maintained table."""
        self.agent_scope_path.parent.mkdir(parents=True, exist_ok=True)
        scope = {
            a.name: {"write_scope": list(a.write_scope), "task_allowed": a.task_allowed}
            for a in self.agents
        }
        tmp = self.agent_scope_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(scope, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.agent_scope_path)
        if self.legacy_agent_scope_path.is_file():
            self.legacy_agent_scope_path.unlink()
            self.log(f"removed {self.legacy_agent_scope_path.name}: the guard hooks now read "
                     f"{self.agent_scope_path.relative_to(self.home)}")

    def agent_runner(self):
        """Return the configured runner, lazily preserving the Claude default."""

        if self.runner is None:
            if self.provider == "codex":
                from .runners.codex import CodexRunner

                self.runner = CodexRunner(self)
            else:
                from .runners.claude import ClaudeRunner

                self.runner = ClaudeRunner(self)
        return self.runner

    # --- entrypoint -----------------------------------------------------------

    def main(self) -> None:
        parser = argparse.ArgumentParser(description=f"{self.name} harness (built on world)")
        parser.add_argument("--once", action="store_true", help="single tick, then exit")
        parser.add_argument("--dry-run", action="store_true", help="print, spawn nothing")
        parser.add_argument("--no-slack", action="store_true", help="force poll-only")
        parser.add_argument("--tick-interval", type=int, default=self.tick_interval)
        parser.add_argument("--world-map", nargs="?", const="WORLD.html", metavar="PATH",
                            help="export an interactive character/tool map and exit")
        args = parser.parse_args()

        if args.world_map is not None:
            from .world_map import write_world_map

            destination = Path(args.world_map)
            if not destination.is_absolute():
                destination = self.home / destination
            write_world_map(self, destination)
            print(f"캐릭터 관계도: {destination}")
            return

        # Subscription-billing invariant: absent ANTHROPIC_API_KEY, the SDK uses the
        # `claude` CLI login (Max/Pro). A stray key silently flips to metered billing.
        if self.provider == "claude" and os.environ.get("ANTHROPIC_API_KEY"):
            self.log(
                "⚠️  ANTHROPIC_API_KEY is set — the SDK will bill the API key instead of "
                "the subscription. Unset it before running this harness."
            )

        asyncio.run(self._main_async(args))

    def _register_plugin_refresh(self) -> None:
        """Auto-register a tick_task that re-syncs the marketplaces on plugin_refresh_s.
        It rides the same tick machinery as any keepalive: interval-gated, failure logged
        and retried next tick, LLM-free. Registered (not left to the harness author) so a
        configured registry can never silently go stale."""
        from .process import plugins as _plugins

        async def refresh_plugins(state, listener) -> None:
            await _plugins.sync_all(self)

        self.tick_tasks.append(TickTask("plugin_refresh", self.plugin_refresh_s, refresh_plugins))

    def _init_schedules(self, state: dict) -> None:
        """First-sight adoption for ScheduleTrigger agents: one with no recorded
        `last_fired` adopts the current occurrence as already-done, so its first real fire
        is the NEXT scheduled time — not a catch-up run for a time that already passed
        before it was ever registered. Real startup only (the caller guards dry-run), so a
        dry-run stays side-effect-free. After this, orchestrator._due_signal is read-only."""
        from datetime import datetime, timezone

        now = time.time()
        for agent in self.scheduled_agents:
            slot = state.setdefault("agents", {}).setdefault(agent.name, {})
            if "last_fired" not in slot:
                due = agent.trigger.schedule.previous_occurrence(
                    datetime.fromtimestamp(now, tz=timezone.utc)
                )
                slot["last_fired"] = due.timestamp()

    def _preflight(self) -> None:
        """Startup checks that only log: what a person should fix, said before anything runs."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        for problem in world_doc_problems(self.world_text()):
            self.log(f"⚠️  {self.world_doc.name} {problem}")
        for problem in self.naming_problems():
            self.log(f"⚠️  naming: {problem}")
        self.write_agent_scope()
        self.preflight_guard_hooks()
        self.preflight_mutates()

    async def _main_async(self, args) -> None:
        # Slack remains the first channel adapter, but importing world or constructing a
        # core-only harness must not require slack-sdk.
        self._preflight()

        # Sync plugin marketplaces once at startup so the very first tick already has them,
        # then let the refresh tick_task keep them current. Non-fatal: a failed sync logs
        # and leaves any prior clone in place (dry-run skips the network entirely).
        if self.plugins and not args.dry_run:
            from .process import plugins as _plugins

            await _plugins.sync_all(self)
        if self.plugins:
            self._register_plugin_refresh()

        state = load_state(self)
        # First-sight adoption of scheduled agents' current occurrence (real runs only —
        # dry-run must not mutate state). Keeps orchestrator._due_signal read-only.
        if not args.dry_run:
            self._init_schedules(state)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            # Windows has no add_signal_handler: its loop classes never override
            # AbstractEventLoop's `raise NotImplementedError`. Unhandled, this killed
            # every entrypoint — smoke.py and `--once --dry-run` included — because the
            # registration sits ahead of the once/loop branch. Ctrl+C still stops the
            # process there, as KeyboardInterrupt out of asyncio.run.
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                self.log(
                    f"signal {sig.name} not hookable on this platform — "
                    "stop with Ctrl+C (no graceful drain)"
                )

        if self.channel is not None:
            listener = self.channel
        elif args.no_slack or CHANNEL == "none":
            from .channels import NullGateway

            listener = NullGateway()
        elif self.discord is not None:
            from .channels.discord import DiscordGateway

            listener = DiscordGateway(self, state, lambda: save_state(self, state))
        else:
            from .channels.slack import SlackGateway

            listener = SlackGateway(self, state, lambda: save_state(self, state))
        # Only the label differs for Discord; every Slack-path string is unchanged.
        chan = "discord" if self.discord is not None and self.channel is None else "slack"
        use_slack = listener.enabled and not args.no_slack
        if not use_slack:
            why = ("--no-slack" if args.no_slack else
                   "no channel in project-profile.json" if CHANNEL == "none" else "tokens missing")
            what = "slack listener" if chan == "slack" else "discord gateway"
            self.log(f"{what} disabled ({why}) — running poll-only")
            listener_task = None
        else:
            listener_task = asyncio.create_task(listener.start(stop))
        if not args.dry_run:
            # STATUS.md renders this, so "is it running, and is Slack on?" has a written
            # answer that cannot go stale the way a sentence in WORLD.md does.
            state["process"] = {"pid": os.getpid(), "started": time.time(),
                                "channel": f"{chan}: connecting" if use_slack else f"off ({why})"}

        self.log(
            f"started: {len(self.poll_agents)} poll agent(s), "
            f"{len(self.scheduled_agents)} scheduled agent(s), "
            f"{len(self.personas)} persona(s), tick={args.tick_interval}s, "
            f"concurrency={self.max_concurrent_runs}, "
            f"{chan}={'on' if use_slack else 'off'}, "
            # slack=off with env=none is the diagnosable version of the silent
            # degrade: it says "I found no harness.env" instead of implying you meant it.
            f"env={self._env_file.name if self._env_file else 'none'}"
        )
        scheduler = Scheduler(self, state, listener if use_slack else None)
        # --once means one tick, so the queue is drained explicitly after it rather than
        # by a loop that stop.set() would end before it ever ran a pass.
        scheduler_task = (
            None if args.once else asyncio.create_task(scheduler_loop(scheduler, stop))
        )
        try:
            await poll_loop(self, state, listener if use_slack else None, stop, args, scheduler)
            if args.once:
                await scheduler.drain()
                await scheduler.drain_inflight()
        finally:
            stop.set()
            if scheduler_task:
                with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                    await scheduler_task
            if listener_task:
                listener_task.cancel()
                # CancelledError is a BaseException, so it needs naming alongside
                # Exception — awaiting the cancel we just requested is the expected path.
                with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                    await listener_task
            save_state(self, state)
            self.log(f"stopped at {time.strftime('%H:%M:%S')}")
