---
name: the-world-start
description: Create or resume a World harness from Claude Code or Codex, which also runs the bots. Build collaborating characters, their tools and relationship map, verify locally, then connect Slack. Use for "the world start", "world init", "하네스 만들어", "감시 봇 만들어", or continuing this harness's setup; not for arbitrary Slack bot development.
disable-model-invocation: true
---

# the-world-start

Turn a plain-language job into a harness: a process that runs agents on changes, on a
schedule, or when mentioned in Slack. Respond in the user's language and keep the term
“하네스”. This is a hands-on learning kit, so the several characters working together are
part of what the user came for. Do not merge characters to simplify setup.

How you get there is yours to decide. What must hold is below.

## Step 0: prerequisites

Before anything else, run `scripts/check-prereqs.py` from this loaded skill's directory
with `python3` (`python` on Windows); add `--runtime claude|codex` once the runtime is
chosen. It checks and reports only. Report each missing item, with the next action it
prints, alongside the first question rather than instead of it. A missing item never holds
up the questions or the design, but installing the kit and verifying wait until the script
exits 0; rerun it before installing.

In the same first reply, state the install profile (see "The profile" below) in one line
of the user's words and let them correct it. They should not have to learn what a profile
is. For a Korean-speaking user: `이 Mac에서 Codex로 실행하고, 봇과는 Slack에서 대화하도록 만들게요.
다르게 하고 싶으면 말해주세요.` Don't turn it into a question unless something is genuinely
unclear, and don't repeat it later unless it changed.

## The first question

Unless you are resuming, open with one question, in the user's language: ask for the path
of the project to hand over, or the job to hand over. For a Korean-speaking user, ask in
exactly these words:

> 맡길 프로젝트의 경로나, 맡기고 싶은 일을 알려주세요!

The project is the repository or directory whose changes the harness watches, whose
state it reads, or where its workers work; it is not the harness's own directory. The
user may answer with either half. Fill in the other from what you can see — the project's
README and agent instructions suggest a job, and a job usually names its project — and
ask only for what is still missing. A job without a project leaves you guessing every
worker's sources and boundaries, so settle both before designing a worker.

When the user gives a project but no job, don't ask "what should it do?" back. Find out
what the user keeps doing there by hand, and propose jobs from that:

- The project itself: README, agent instructions (`CLAUDE.md`, `AGENTS.md`), the
  project's own skills and commands, journals or logs it keeps, and recent `git log`.
- The local session history of coding agents run in that directory, starting with the
  tool you are running in. Claude Code keeps it in
  `~/.claude/projects/<the absolute path with every non-alphanumeric character replaced
  by '-'>/*.jsonl`; Codex in `${CODEX_HOME:-~/.codex}/sessions/**/rollout-*.jsonl`, where
  the first line's `cwd` names the directory. Read what the user typed and which commands
  ran, not whole transcripts.

Propose two or three jobs, each with the evidence behind it (how many sessions asked for
it, which commits or journal entries show it), and let the user pick. If you found no
history, say so and say the proposals come from the project alone.

Once both are settled, ask which world the cast lives in (세계관), before you name a
character, split the job into roles, or write `WORLD.md`. The world is the user's to
choose, not a detail you fill in for them. Offer two or three original worlds that the
project calls to mind: a genre or a setting, such as a pirate crew, a mission-control room,
or a neighborhood post office. Never name a published work, its characters, or a pun on one
(a kit called "The World" does not make JoJo a proposal). The kit must not be the one to
put someone else's work on the table. Then invite theirs, and mean it: a favorite anime,
game, or novel world is just as good and often more fun, and so is one they make up. Once
the user picks a published work, follow it. A portrait still gets its own design rather
than a copy of an official character, and if the harness or its images will be shared
outside the team, say in one line that the work's rights belong to its owner. Don't
propose a cast in the same message. A missing prerequisite such as Python or `uv` can be
reported alongside the question, but it doesn't replace the question.

## What must hold

**Every worker's authority is declared in code, and narrow.** `write_scope` is required
and is never empty; `tasks/{task}/work` is the ordinary lane. A worker that only reads
gets no write path. A new worker never comes out wider than the ones already there.

**A promise in a prompt is not enforcement.** If a boundary matters, it lives in code or
the capability is absent. Prefer a scoped tool over a wider Bash allowlist, and omit a
dangerous operation rather than guarding it with a check. The kit's reasoning for each
boundary, and the incident behind it, is in `docs/philosophy.md` (installed into the
harness). Read it before designing a worker's authority.

**Outward actions wait for a human.** Anything that leaves the machine, or writes outside
a worker's scope, goes through the harness's approval path rather than being done and
reported afterwards. For a worker that watches GitHub, Linear, or Jira, start from the
recipes in `docs/integrations.md` (installed into the harness).

**Workers hand off to each other by name.** When a job belongs to a teammate, the worker
says so to that teammate rather than widening itself to cover it. A persona passes work
with its `hand_off` tool, not an @-mention; the harness runs the teammate's turn after the
current one, in the same thread, capped per thread, and re-runs a queued handoff after a
restart.

**People talk to the main persona only.** It is the one identity listening on Slack; the
other personas post, and are reached through its handoff. A human @-mention of a
sub-persona is never received. So when you tell the user how to use the harness, every
example addresses the main persona (`@Guide Scout한테 다시 돌려달라고 해줘`), never a worker.

**Buttons are proposed, not defaulted.** A result announcement carries no buttons unless
its worker declares them on `Agent.buttons`. For each worker, propose a set to the user
before writing it: for every button, what the reader does next with this result and why a
click beats typing a mention. Ground the proposal in how Slack buttons behave and in the
reader's moment:

- A click carries no input. If the next step needs a free-form answer, it is a mention.
- A button stays clickable as long as the message lives, and anyone in the channel can
  press it. Every click except `link` and `show` is gated to the approver, so don't put a
  button in front of readers who can't use it.
- An `ask` click costs a model turn; `link` and `show` cost nothing. When the next step is
  looking at something that already exists, link to it.
- Two or three buttons, each label a verb naming the outcome (`원인 더 파봐`, not `확인`).
  At most one `primary`, for the step most readers take. `danger` only for something
  destructive, which still goes through approval. The kit refuses more than five.
- None is a fine answer. A status note nobody acts on needs no buttons.

**Every persona's speaking style is drafted, shown, and confirmed.** For each persona,
draft a style (말투) that fits the chosen world and the character: 존댓말 or 반말, verbal
habits, emoji use, message length. Show it to the user as two or three realistic Slack
messages from that persona: starting a task, reporting a result, and asking for approval.
Write it to `Persona(voice=...)` only after the user edits or approves it; no persona skips
this, and silence is not approval. A style is tone only. Numbers, "unverified" markers,
errors, and approval requests stay exact in every example, and the kit injects that same
rule beside the voice into every prompt.

**Every check says what it covers.** Before a worker depends on a source, confirm the
source exists: the endpoint answers, the property ID is readable, the file is there. A
`/health` that returns 404 is a finding to report, not a URL to poll. When you describe a
worker, name what it checks and what it does not. An HTTP status check is not a health
check of the database, the login, or the pages a browser renders. A person who asks "is it
only a ping?" has been told too little.

**The world's own words are the interface.** Fill `WORLD.md` with the cast, how they
relate, a vocabulary, and what is watched, all in the world's terms. Nothing else goes in
it: it is sent with every mention turn to every persona, so whatever it says is read as a
standing fact. An install step, a command, a path, an env variable, a PID, or "Slack is not
connected yet" stays there after it stops being true. Those go elsewhere:

- How to operate (start, logs, change a target, add a character): `README.md`, for people.
- Values that change (target URL or repo, schedule, time zone): `settings.json`, read by
  `harness.py`. `WORLD.md` may say "every morning" and point at the setting, never the value.
- What is true right now (running, connected, last run): the generated `STATUS.md`.
- One character's procedure: that character's prompt file, not the shared world.

`bin/smoke.py` flags a code block, path, command, PID, env name, or install heading in
`WORLD.md`; fix every `[WARN] WORLD.md` line before you finish.

The two creation words become real commands via
`bin/scaffold-world-skill.py <word> agent|tool --universe ... --description ...`. Default
to `recruit` and `equip` when the user has no preference. Honor explicit naming choices.

**Exit 0 is not completion.** A worker that produces analysis (`writes_finding=True`) is
verified by a file landing in its task's `work/`, never by an exit code.

**Report only what happened.** A dry run is not a Slack connection and not a real agent
result. Name what is still unverified, including any check blocked on missing credentials.
A handoff happened when a `hand_off` call was queued. A persona writing "I'll ask Scout"
is not one, so verify a handoff by the call.

**Resume rather than reinstall.** If `harness.py` or `WORLD.md` is already there, keep its
names, workers, wrappers, and configuration, and add only what is missing. Re-running the
installer replaces kit-owned files and is an upgrade, not a way to continue Slack setup.
Never print token values.

## The profile: OS, runtime, channel

Three choices, stated in the first reply (Step 0) and passed to the installer as `--os`,
`--provider` and `--channel`. To the user they are "where it runs", "which AI runs the
bots" and "where you talk to the bots"; "profile", "provider" and "channel" are the kit's
words, not theirs, and "channel" alone reads as a Slack channel name, which is a later,
separate question.

- **OS**: where the harness will run. Detect it, say it, and ask only if the harness will
  run somewhere other than this machine. Native Windows is supported; don't steer a
  Windows user to WSL. Only a `windows` profile carries the Windows tool bridge, so a
  harness moved to Windows later is re-installed with `--os windows`.
- **Runtime**: `claude` or `codex`, which runs the bots. Default to the tool the user is
  talking to you in. Use `both` only when they ask for it.
- **Channel**, where people talk to the bots: `slack` (the default), `discord`
  (experimental), or `none` for a harness that only polls and schedules. Not the Slack
  channel name; that is picked up during the Slack connection.

The installer copies only what the profile selects and records it in
`project-profile.json`. The harness then carries one runtime and one channel, with nothing
else to read past, and `world/` refuses a configuration outside the profile. Never install
`both` to keep options open. A later re-run with a flag adds the other runtime cleanly.

On Windows:

- Run the kit scripts with `python` and the harness with `uv run` from PowerShell.
  Without uv, the interpreter is `.venv\Scripts\python.exe`.
- Pass any non-ASCII text you hand to another CLI (a `gh issue`/`gh pr` body, a commit
  message, a Slack message) as a UTF-8 file, such as `--body-file`, `-F`, or `git commit -F`,
  never through a PowerShell pipe or here-string. PowerShell re-encodes piped text to the
  console code page, and Korean arrives as `?` with no error. For Python you start
  yourself, set `$env:PYTHONUTF8 = "1"`.
- If something in the kit fails on this machine, fix the minimum in `world/`, list each
  change in a note outside `world/`, and tell the user to report it upstream. Don't build a
  `kit-source/` checkout, a `patches/` directory, or a patch exporter in the harness.

## The runtime you are building against

The kit is bundled at `assets/kit/`; install it with `scripts/install-kit.py <target>
--os <os> --provider <runtime> --channel <channel>` from this loaded skill's directory, resolving paths from where this skill is loaded rather than the
target directory. Do not modify the installed plugin; scaffold into the target.

If the installer or `bin/scaffold-world-skill.py` exits with `cannot write to ...`, the
host sandbox refused a path (Codex's `workspace-write` protects `.codex/` and `.agents/`)
and nothing was written. Ask the user to approve writing those paths (escalation) and
re-run. Never hand-copy the rest of the kit around it. If not approved, report the guard
hooks and word commands as not installed.

`world/` is kit-owned and stays as shipped. Its surface:

```python
from world import (Agent, Button, Harness, MentionTrigger, Persona, PollTrigger, RunResult,
                   Schedule, ScheduleTrigger, Signal, SlackConfig)

h = Harness(name=..., provider="claude"|"codex", home=Path(__file__).resolve().parent,
            world_doc="WORLD.md", main_persona=Persona(key=..., display=..., flavor=...),
            slack=SlackConfig(..., respond_to_channel_messages=False))   # see below
h.register(AGENT)                    # once per worker
h.main()                             # entry point

AGENT = Agent(name=..., trigger=PollTrigger(check=check),   # or MentionTrigger()
              prompt_file=...,       # standing instructions, relative to home
              persona=Persona(...), writes_finding=True, mutates=False,
              write_scope=("tasks/{task}/work",), tools=[...], allow_bash=False,
              buttons=(Button.ask("원인 더 파봐", "<request to this worker's persona>"),
                       Button.link("Jenkins 열기", "https://..."),
                       Button.show(), Button.dismiss()))   # none unless proposed
```

One character, one word. Name each worker's `Agent` the same as its `Persona.key`, its
folder (`abilities/<key>/`), and its prompt file, so `STATUS.md`, `HEARTBEAT.md`, a task
folder, and `WORLD.md` all show the same word for it; startup logs `naming:` when they
differ. Put the main persona in its own folder too, and the values a person will change
(target URL or repo, schedule, time zone) in `settings.json` that `harness.py` reads.

A poll `check(state, now)` is non-LLM and returns `Signal(prompt=..., on_done=...)` to
spawn or `None` to stay idle; per-worker bookkeeping lives in `state["agents"][name]` so it
survives restarts, and `on_done` runs only after a verified-complete run.
`ScheduleTrigger(Schedule.daily(9, 0, "Asia/Seoul"))` is the wall-clock alternative. On a
harness's first start it adopts the current occurrence as already done and waits for the
next one, so a dry run is not a first real run and the report should say when that is.
`Harness(provider=...)` takes `claude` or `codex`.

`mutates=True` makes each task run in its own git worktree (the kit cuts it at spawn
and refuses the run if it cannot), so leave it `False` unless the worker edits the repo.
A docs, CS-reply or research worker needs no git: `mutates=False`, its writes bounded by
`write_scope`, and anything outward (sending a reply, publishing) in the persona's
`mention_tools` so it waits for an approve click. In a project that is not a git
repository, `mutates=True` is refused on every run, and the dry run says so up front.

Verify from the generated harness directory:

```bash
uv run python bin/smoke.py
uv run python harness.py --once --dry-run --no-slack
```

`HEARTBEAT.md` and `.the-world/agent-scope.json` are where you confirm the worker is
registered with the scope you declared.

Defer Slack credentials and app provisioning until local verification passes, unless the
user has them ready or asks for Slack now. When connecting, ask for the channel, the
approver, and the Slack plan; on a free plan messages vanish past 90 days, so anything
that must outlive that belongs in a file rather than a thread. One Slack app serves every
persona: it is the main persona, and the others post through it under their own name and
`Persona.portrait` icon (an http(s) URL) with `chat:write.customize`, so a free plan's
10-app limit never counts personas. Give a persona its own app (`bot_token_env`) only when
the user wants it to have its own Slack profile. Real tokens go in `env/harness.env`, never
a committed file.

People in a dedicated channel often just type, without an @-mention, and read the
silence as a broken bot. Ask whether the harness's channel is its own. If it is, offer
`SlackConfig(respond_to_channel_messages=True)`: a plain message there goes to the main
persona, which answers in a thread. Relay channels, bots, and edits still need a mention.

Create the app with the Slack CLI rather than by hand. Write `slack-app/slack-apps.json`
(prefix, description, `team` — the workspace ID from `slack auth list` — and one entry for
the main persona with its key, display name, and the bot- and app-token variable names
`harness.py` reads, marked `main`; add an entry for another persona only if it gets its own
app). Fill `team`: without it the CLI asks which workspace, and a shell with no terminal
(yours, or the user's `!`) cannot answer. Slack accepts any script in the app name but only
ASCII letters, digits, `.`, `_` and `-` in the bot user's name. When the display name is not
ASCII, the main app's bot is named after the prefix (`@lighthouse`), and a persona with its
own app `<prefix>-<key>`; set `bot_username` on the entry to choose another. A `Persona.portrait` must be a public https image URL. A file uploaded
to a Slack channel is not one, because it needs a workspace login. Then have the user run
`uv run python bin/slack-provision.py` in their own terminal, since the CLI may ask which
workspace or whether to request admin approval. It needs `slack` installed and one
`slack login`, creates and installs the app from the manifest template with no browser
click, and writes the tokens into `env/harness.env` without printing them. Re-running it
skips apps that already have tokens. Read its summary rather than assuming: a pending
admin approval or a missing app-level token is reported there, with the next step. Without
the CLI, or if the user prefers, fall back to pasting `templates/slack-manifest.json.tmpl`
at api.slack.com.

Never ask the user to paste a token into the chat, and never `cat`, print, or read back
`env/harness.env`; a chat transcript is not a secret store. Tokens reach that file only
through `bin/slack-provision.py` or the user editing it directly in their own editor. To
check whether a value is filled in, rely on the provisioner's summary or the harness's
startup line, not on the file's contents.

Don't ask the user to look up the channel ID or their member ID. Have them run
`uv run python bin/slack-pair.py --prefix <PREFIX>`, invite the bot to the channel, and
say `@<bot> 안녕` there: the script reads the channel and the speaker from that one
event, proposes the speaker as the approver, writes both on a yes, and answers in the
thread. Only if that cannot run, tell them where to find each: the channel ID is at the
bottom of the channel's details (`C…`), a member ID under the profile's ⋮ → Copy member ID
(`U…`). A public channel is joined by the bot itself at startup; a private one needs the
invite.

Fill the installer's `README.md` before you finish: replace every `…` with this
harness's real file for each change (voice, schedule, target, procedure, Slack, adding a
character) and where reports land, in the user's language. It is the page a person opens
without you; `WORLD.md` is not.

Give the harness a history. Upgrading re-installs the kit and deletes its trees first, and
`git diff` is then the only record of what changed, so a harness outside any git repository
gets `git init` and one local commit after verification passes (never a push; `.gitignore`
already keeps tokens, state, and task output out). A harness inside an existing repository
is the user's to commit: leave it and say so.

Finish by telling the user the harness path, the project it works on, and the profile it was
installed for. Give each worker's job, when it runs next in their time zone, and what it
does not check. Say which checks actually passed, what remains unverified, and one next
action. Also say that nothing runs unless `harness.py` is running: the kit sets up no
service, so a reboot stops it until the user starts it again or asks you to register one.
The harness needs neither this plugin nor the kit to run. Re-running the installer is only
for upgrades.
