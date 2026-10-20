# Harness guide

*[한국어](harness-guide.ko.md) · back to the [README](../README.md)*

For people running a harness: what the installer puts where, how to upgrade without losing
work, and the rules the runtime holds you to. To get started, the
[README](../README.md) is enough.

## How the runtime and the commands get to your harness

`npx skills` installs skill directories, not repos — so the runtime cannot simply be
"already there" in your project. It ships inside `start`:

```
skills/start/
  SKILL.md
  scripts/install-kit.py     unpacks the kit into a target directory, idempotently
  assets/kit/
    world/                     the runtime (no deps beyond pyproject.toml)
    templates/               codegen templates the flows fill in
    .claude/the-world/       Claude-only compatibility stubs for agent · tool · verify · plugins
    .claude/hooks/           scope · git · integrity · durable-config guards
    .claude/settings.json    hook registration (merged, never clobbered)
    .codex/hooks/            Codex apply_patch/Bash boundary adapter
    .codex/hooks.json        Codex hook registration (merged, never clobbered)
    env/harness.env.example  pyproject.toml  bin/smoke.py  bin/codex-spike.py
    bin/scaffold-world-skill.py
    bin/world-map-demo.py     six-character learning example
    bin/slack-provision.py    creates the harness's Slack app via the Slack CLI
    bin/slack-pair.py         channel + approver IDs from one @-mention
    docs/world-map.md         portraits, relationships, and tool-map guide
    docs/integrations.md      GitHub · Linear · Jira recipes (poll check + gated write)
skills/audit/
  SKILL.md                   what the audit must hold and how it reports
  scan.py                    read-only evidence scanner (parses the harness, never imports it)
skills/util/
  SKILL.md                   subcommand router
  references/slack-icon.md
skills/help-my-world/
  SKILL.md                   draft, approve, then file an issue on this repository
  collect_env.py             redacted environment report (stdlib only)
```

`/the-world:start` Phase 3 step 1 runs the installer. What it does to the harness directory:

| | Files | On re-run |
|---|---|---|
| kit-owned | `world/` `.claude/the-world/` (Claude) `templates/` wrapper + manifest selected provider hooks `bin/smoke.py` `bin/codex-spike.py` `bin/slack-provision.py` `bin/slack-pair.py` `docs/philosophy.md` | **replaced** — a re-run is an upgrade, and the kit-owned trees are deleted first ([read this](#upgrading)) |
| harness-owned | `pyproject.toml` `env/harness.env.example` `.gitignore` | created if absent, else kept |
| profile | `project-profile.json` (OS · runtime · channel) | rewritten from the flags; a flag left out keeps the recorded value |
| merged | `.claude/settings.json` and/or `.codex/hooks.json` | guard hooks added, existing hooks kept |
| yours | `harness.py` `WORLD.md` `AGENTS.md` if present `abilities/` `tools/` `slack-app/` `env/harness.env` provider skill wrappers | never touched |

**The harness ends up self-contained.** Phase 3 step 7 scaffolds each world-word as
`.claude/skills/<word>/SKILL.md` and/or `.agents/skills/<word>/SKILL.md`, and that wrapper
points at `WORLD.md` and `docs/philosophy.md` inside the harness. Clone it onto another
machine and `/recruit` still works with nothing installed globally. That is also why the
guidance lives in the kit payload rather than as sibling skills here: a command whose logic sits
outside the harness is a command that breaks the moment the harness moves.

So don't fix `world/` or `.claude/the-world/` inside a harness — fix `assets/kit/` here and
re-run the installer. That instruction is what makes the next section matter: a harness
that followed it is safe to upgrade, and one that did not will lose those edits silently.

If the kit does not run on your machine at all (that is how native Windows support
started), fix the smallest thing in `world/`, list each change in a note **outside**
`world/`, and open an issue here. Don't build a second pipeline inside the harness, such as
a `kit-source/` checkout, a `patches/` directory, or a patch exporter. That makes three
copies of the runtime to keep in step, and nobody who opens the harness can tell which
one is real.

<a id="upgrading"></a>
To upgrade an existing harness after `npx skills update`:

```bash
cd <harness> && git status --short          # MUST be clean — see below
python3 <installed start skill>/scripts/install-kit.py .   # keeps project-profile.json; Windows: `python`
uv pip install -e .
uv run python bin/smoke.py
git diff                                    # the report the installer does not print
```

> **A re-run is destructive, and it does not say what it destroyed.** Each kit-owned tree
> is deleted before it is copied, so a file you added inside `world/` or
> `.claude/the-world/` is removed without being named, and a kit file you edited in place is
> overwritten the same way. The log says `replaced world/` either way.
>
> That is the intended contract — one runtime, never forked, is the point. But it makes a
> re-run safe only for a harness that left those trees alone, and a long-lived harness
> often has not: once, a re-run destroyed four local fixes in one harness's `world/`
> **and the document that listed them**, which lived in `world/` too. A clean tree beforehand
> is what made `git checkout` able to undo it.
>
> So: commit or stash first, then read `git diff` — that diff is the only report of what
> the upgrade took. And if your harness deliberately owns its `world/`, keep that note
> **outside** the trees above (`docs/`, or the top of `harness.py`), or the upgrade will
> delete the map along with the territory.

### Profiles: OS, runtime, channel

The installer takes three choices and records them in `project-profile.json`:

| Flag | Values | What it changes |
|---|---|---|
| `--os` | `windows` `macos` `linux` (default: this machine) | whether the Windows tool bridge is copied; the guard hooks' interpreter (`python` on Windows, `python3` elsewhere); the verification commands it prints |
| `--provider` | `claude` `codex` `both` | which runner, hooks, and docs are copied; `Harness()`'s default provider |
| `--channel` | `slack` `discord` `both` `none` | whether the Discord gateway is copied; `none` runs poll and schedule only |

Only what the profile selects is copied, so a macOS harness has no Windows pipe code and a
Windows · Codex · Slack harness has no Claude runner, `.claude/` hooks, or Discord code to read past. `world/profile.py` reads the
same file at runtime: asking such a harness for `provider="claude"` or a `DiscordConfig`
fails at construction, with the reinstall flag in the message, rather than as an
ImportError later. Re-running without flags keeps the recorded profile. A flag changes it,
and files the new profile drops are removed and named in the log. A harness installed
before profiles existed has no file and keeps everything.

### Native Windows

Windows runs natively, with no WSL needed. Codex's per-run tool bridge, a 0600 unix socket
elsewhere, is a named pipe on Windows. Its DACL admits only the current user, it rejects
remote clients, and it takes a random name per run that it refuses to share.
`env/harness.env` gets the same user-only ACL, because `chmod 600` does nothing there.
`pywin32` and `tzdata` (Windows ships no time zone database for `Schedule`) come from
`pyproject.toml` with a `sys_platform` marker. Every file, pipe, and hook payload is read
and written as UTF-8 whatever the console code page, and a test fails the build on any text
I/O that names no encoding. Run `uv` from PowerShell. Without `uv run`, the interpreter is
`.venv\Scripts\python.exe`.

Provider modes are `claude`, `codex`, and `both`; the default remains `claude` so an
existing harness upgrade does not acquire new platform files unexpectedly. A Codex harness
sets `Harness(provider="codex")`. `bin/codex-spike.py` performs a live read-only + scoped
MCP verification using the saved Codex CLI login.
Codex-only installs omit `claude-agent-sdk`; changing such an install to `both` preserves
its project-owned `pyproject.toml` and prints an explicit action to add that dependency.
The implementation decision, matrix, and residual risks are recorded in
[`docs/codex-compatibility.md`](../skills/start/assets/kit/docs/codex-compatibility.md).

## One way to give an agent a capability

> The World deliberately has **one** way to give an agent a capability: a provider-neutral
> `ToolSpec`. Claude converts it to an in-process SDK MCP tool; Codex exposes it through a
> private per-run stdio MCP bridge while the scoped handler stays in the parent process.
> The providers offer broader surfaces — external MCP
> servers over stdio/SSE/HTTP, plugins, `skills`, per-subagent tool sets — and every one of
> them hands over a surface the harness didn't bound. Plugin loading
> (`Agent.allow_plugins` with a `PluginMarketplace`, see `world/process/plugins.py`; the Claude
> runner only — the Codex runner refuses it) is the single, explicitly full-trust exception. Don't add
> a second path.

## Inherited invariants (don't relax without deciding to)

- **Single human approver** for anything outward. A bot co-approver may only *relay* a
  human decision (HMAC-signed), never originate one.
- **Claude subscription billing only.** `ClaudeRunner` refuses `ANTHROPIC_API_KEY`; Codex
  authentication is independent and uses the saved Codex CLI login.
- **exit-0 ≠ completion.** A `writes_report` agent's real completion is a file under its
  task's `work/`; exit-0 with no file is recorded blocked and refires. What "finished"
  means per kind of work is a `TaskType` completion predicate, not a status anyone assigns.
- **Boundaries in code, not prompts.** Spawned agents get an empty base tool set (no Bash,
  no Write) and only the scoped tools you hand them.
- **No silent caps, no invisible daemons.** Degrade loudly (`env=none`, `slack=off`) and run
  where the logs are visible. Every posted message carries what the run cost — latency,
  billed model, tokens, cache hit rate, tool count, USD (`Harness(run_footer=False)` to
  hide it). A missing field is omitted, never zeroed: a Codex footer says `USD 미제공`
  instead of a dollar figure, because an absent cost and a genuinely free call must not look alike.

Each traces to a real incident; the reasoning is in
[`docs/philosophy.md`](../skills/start/assets/kit/docs/philosophy.md).

> **Env var naming.** A harness's config vars carry no kit-wide prefix: `<PROJECT>_THING`
> for what one harness owns, a bare `THING` for what a set of harnesses genuinely shares
> (the approver is one human — one identity, one variable). The bare ones can collide with
> something already exported in your shell, and the shell wins; the loader says so on stderr
> rather than letting a harness read someone else's token in silence.
>
> `WORLD_*` is a different namespace and stays prefixed: `WORLD_AGENT` and `WORLD_HEADLESS` are
> injected by the runtime at spawn time and read by the guard hooks — `WORLD_AGENT` is the channel the
> scope guard trusts, so a bare name there would be both collision-prone and spoofable.
>
> The `world` package name and the `env/harness.env` filename are mechanism, not branding — the
> flows and hooks assume them, so renaming those is a coordinated edit, not a find-replace.

## Provenance

Here the kit is packaged inside the `start` skill: it scaffolds into a target
directory and copies and merges its guard hooks and `settings.json` instead of assuming
them present.
