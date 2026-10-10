# the-world-skills — a harness kit you learn by giving characters tools

*Read this in [한국어](README.ko.md). Issues and pull requests are welcome, but replies can be slow — see [CONTRIBUTING.md](CONTRIBUTING.md).*

Give the characters of a world their own `tool`s, let them work together, and learn the
harness by doing it. Runs Claude Code or Codex agents on a schedule or on a change, and
lets you talk to the characters from Slack. Ships as a plugin for Codex, Claude Code, and
Cursor.

## Quick start

Describe the job you want done in one sentence, and the agent scaffolds a single bot and a
project that runs it. You don't need a world, command names, or Slack tokens up front.

1. Pick your coding tool in the [plugin install guide](docs/plugins.md).
2. From the directory where the project should live, invoke the `the-world-start` skill and
   add what you want done.
3. The agent creates the first bot with default names and settings, then runs local
   verification.
4. To use it from Slack too, ask in the same project to "continue with the Slack
   connection".
   With the [Slack CLI](https://docs.slack.dev/tools/slack-cli) installed and `slack login`
   done once, the agent creates the harness's one Slack app and fills in the tokens for
   you (`bin/slack-provision.py`); every character posts through it under its own name.
   Then say hello to the bot once in your channel (e.g. `@<bot> hi`) and `bin/slack-pair.py` picks up the
   channel and your member ID. Without the CLI, it falls back to pasting a manifest at
   api.slack.com.

Example: "build me a bot that summarizes this repo's changes every day at 9am."
Want a world? Say it in the same breath — "a pirate crew", or your favorite anime's world. In an existing project the
agent picks up the configuration where it left off.

You need **Python 3.11+ and uv**. Fetching dependencies may require internet access. The
first local verification needs no Slack token and no model login. That verification
confirms the bot is registered and ready to run; the actual AI summary and the Slack reply
are checked separately, after you connect them.

It runs on macOS, Linux, and native Windows (no WSL). The agent asks which OS, bot
runtime, and channel the harness is for, and installs only those; the choice is recorded
in `project-profile.json` ([profiles](docs/harness-guide.md#profiles-os-runtime-channel)).


## Install

The repository is its own marketplace, so Claude Code and Codex install it in two lines:

```bash
claude plugin marketplace add afinit-com/the-world-skills
claude plugin install the-world-skills@the-world

codex plugin marketplace add afinit-com/the-world-skills
codex plugin add the-world-skills@the-world
```

Cursor has no command for this: see [the plugin guide](docs/plugins.md) for the team
marketplace import and the local install. You can build a harness from Cursor too; the
bots it creates run on Claude Code or Codex, your choice.

Skills only, without the plugin: `npx skills add afinit-com/the-world-skills`, then run
`/the-world-start` in the directory the harness should live in. (Installed as a plugin, the
same skill is `/the-world-skills:the-world-start` in Claude Code and `$the-world-start` in
Codex.)

## The four skills

| Skill | What it does |
|---|---|
| `the-world-start` | Name a project or a job (given only a project, it reads the project and its local Claude Code/Codex session history and proposes jobs) → scaffold one worker with defaults → local verification → connect Slack and customize when ready. Also resumes incomplete setup. Carries the kit in `assets/kit/`. |
| `the-world-audit` | Read-only risk review of an existing harness. Marks each item closed, open, or not verified across bot authority, tool scope, outward actions and approval, content-borne manipulation, self-reconfiguration, secrets, and silent failure; open items carry a file, a line, and a repro. Never fixes anything. |
| `the-world-util` | Standalone utilities with no harness preconditions, dispatched by subcommand. Today: `slack icon generate`. |
| `help-my-world` | Ask the maintainers for help without being a developer. You say what you wanted and what happened; it fills the repository's issue form with the rest — the steps, a redacted environment report, and your world's shape (characters, triggers, tools) — walks you through GitHub CLI setup if needed, and files nothing until you approve the draft. |

`the-world-start` is `disable-model-invocation: true` — it writes into a git repo, so a
human types it or it doesn't run. `help-my-world` is the same, because it publishes. `the-world-audit` only reads, so it can be invoked by
the model as well.

Everything else a harness needs — adding a bot, wiring a scoped capability, verifying a new
bot, connecting a plugin registry — is **installed into the harness itself** by
`/the-world-start`, so a harness keeps working when you clone it elsewhere.

## The two hard rules

**1. A harness carries its own creation commands.** Quick start uses an exploration crew
with `recruit` and `equip`; you can choose your own universe and words when ready, or ask
for plain names. A pirate crew can recruit an agent and forge a tool; a dog-lover's bots
can adopt and train. Each word becomes a project-local skill command. You can also
describe the job in ordinary language.

**2. Every agent and tool declares an explicit scope, fixed in code.** An agent with no
`write_scope` is write-blocked everywhere by a PreToolUse hook — scope isn't paperwork, it's
the enforcement. A capability is a dedicated **`ToolSpec` exposed through the selected
provider's MCP adapter** whose boundary (cwd, host, repo, "no merge verb") is bound at
construction. The philosophy in one line
([`docs/philosophy.md`](skills/the-world-start/assets/kit/docs/philosophy.md), installed into every harness): *a promise written in a prompt is not
enforcement.*

## See the characters on a map

```bash
uv run python harness.py --world-map
```

Opens as `WORLD.html`: each character's tools, role, and write scope, and how they work
together. See [building the map](skills/the-world-start/assets/kit/docs/world-map.md).

## Running a harness

> **Upgrading re-installs the kit and deletes its trees first.** Commit before you re-run
> the installer, then read `git diff`; anything you added inside `world/` is gone.

What the installer writes where, how to upgrade, provider modes, and the rules the runtime
holds you to are in the [harness guide](docs/harness-guide.md). Integrations with GitHub,
Linear and Jira: [recipes](skills/the-world-start/assets/kit/docs/integrations.md).
Discord (experimental): [docs](skills/the-world-start/assets/kit/docs/discord.md).

## License

[MIT](LICENSE). Copyright 2026 Afinit.

The kit `/the-world-start` unpacks into your harness (`world/`, `.claude/the-world/`, `templates/`, the
guard hooks) carries this license with it; what you then build on top of it — your
`harness.py`, abilities, tools, and world — is yours.
