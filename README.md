# the-world-skills — a harness kit you learn by giving characters tools

**[the-world.gon.quest](https://the-world.gon.quest)** · *Read this in [한국어](README.ko.md). Issues and pull requests are welcome, but replies can be slow — see [CONTRIBUTING.md](CONTRIBUTING.md).*

Give the characters of a world their own `tool`s, let them work together, and learn the
harness by doing it. Runs Claude Code or Codex agents on a schedule or on a change, and
lets you talk to the characters from Slack. Ships as a plugin for Claude Code and Codex.

## Quick start

**1. Install.** Pick your app.

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude plugin marketplace add afinit-com/the-world-skills
claude plugin install the-world@the-world
```

</details>

<details>
<summary><b>Codex</b></summary>

```bash
codex plugin marketplace add afinit-com/the-world-skills
codex plugin add the-world@the-world
```

</details>

> **Installed before 0.4.0?** The plugin was renamed from `the-world-skills` to
> `the-world`, so `/the-world-skills:the-world-start` became `/the-world:the-world-start`.
> Switch once:
>
> ```bash
> claude plugin marketplace update the-world && claude plugin uninstall the-world-skills@the-world && claude plugin install the-world@the-world
> codex plugin marketplace upgrade && codex plugin remove the-world-skills@the-world && codex plugin add the-world@the-world
> ```

<details>
<summary><b>Other agents (skills only)</b></summary>

```bash
npx skills add afinit-com/the-world-skills
```

Then the skill is plain `/the-world-start`. More in the [plugin guide](docs/plugins.md).

</details>

**2. Say what you want done**, from the project you want it to work on:

```text
/the-world:the-world-start summarize this repo's changes every day at 9am
```

In Codex it is `$the-world-start …`. The bot gets its own folder next to the project
(`~/code/my-app` → `~/code/my-app-world`); started from an empty folder, it lives there
instead and you name the project in the sentence. The agent asks which world the characters
live in, unless you said it already ("as a pirate crew", or your favorite anime's world),
and shows each character's way of talking for your OK. Then it builds the bot, checks it
locally, and tells you what it made.

**3. Talk to it in Slack** (optional): say "connect Slack". The agent asks which channel
and who approves, creates the Slack app for you, and you say hello to the bot once in the
channel to connect it.

You need **Python 3.11+ and [uv](https://docs.astral.sh/uv/)**, and for step 3 the
[Slack CLI](https://docs.slack.dev/tools/slack-cli) with `slack login` done once. Runs on
macOS, Linux, and native Windows. Stuck? `/the-world:help-my-world`.

<details>
<summary>What happens in each step</summary>

- The agent asks which OS, bot runtime, and channel the harness is for, installs only
  those, and records the choice in `project-profile.json`
  ([profiles](docs/harness-guide.md#profiles-os-runtime-channel)).
- The first check needs no Slack token and no model login. It confirms the bot is
  registered and ready; the real AI run and the Slack reply are checked after you connect
  them.
- Slack: `bin/slack-provision.py` creates the harness's one Slack app through the Slack CLI
  and fills in the tokens; every character posts through it under its own name.
  `bin/slack-pair.py` picks up the channel and your member ID from the hello. Without the
  CLI, you paste a manifest at api.slack.com instead.
- In an existing project, the agent picks up the setup where it left off.

</details>

## The five skills

| Skill | What it does |
|---|---|
| `the-world-start` | Name a project or a job (given only a project, it reads the project and its local Claude Code/Codex session history and proposes jobs) → scaffold one worker with defaults → local verification → connect Slack and customize when ready. Also resumes incomplete setup. Carries the kit in `assets/kit/`. |
| `the-world-audit` | Read-only risk review of an existing harness. Marks each item closed, open, or not verified across bot authority, tool scope, outward actions and approval, content-borne manipulation, self-reconfiguration, secrets, and silent failure; open items carry a file, a line, and a repro. Never fixes anything. |
| `dig-my-sessions` | Dig through this machine's past sessions of Claude Code, Codex, opencode, Cursor, Antigravity CLI (`agy`) and the legacy Gemini CLI, over a date range and projects you choose. Finds the session that touched a ticket, branch or topic, with its resume command. Or it mines the history for chores you keep doing by hand, drops the sessions a program started, and proposes what to hand to a world, a skill or a one-line script. Offline and read-only. The optional `--jev` mode sends redacted openers to TypeSafe's Jev for labelling, and only after you agree. |
| `the-world-util` | Standalone utilities with no harness preconditions, dispatched by subcommand. Today: `slack icon generate`. |
| `help-my-world` | Ask the maintainers for help without being a developer. You say what you wanted and what happened; it fills the repository's issue form with the rest — the steps, a redacted environment report, and your world's shape (characters, triggers, tools) — walks you through GitHub CLI setup if needed, and files nothing until you approve the draft. |

`the-world-start` is `disable-model-invocation: true` — it writes into a git repo, so a
human types it or it doesn't run. `help-my-world` is the same, because it publishes. `the-world-audit` and `dig-my-sessions` only read, so the
model can invoke them as well.

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
