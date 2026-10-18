---
name: dig-my-sessions
description: Dig through this machine's past coding-agent sessions (Claude Code, Codex, opencode, Cursor, Antigravity CLI `agy`, and the legacy Gemini CLI) over a date range and projects you choose. Two uses. Find a past session — "그거 어느 세션이었지", "전에 ~하던 세션 찾아줘", "which session did X", a ticket id or branch name. And mine the history for chores the user keeps doing by hand, to hand to a World harness, a skill, or a one-line script — "자동화할 거 찾아줘", "위임할 거 찾아줘", "what should I automate", "내가 반복하는 일 뭐야". Read-only and offline unless the user opts in to Jev labelling.
---

# dig-my-sessions

Respond in the user's language. The archive is hundreds of MB across thousands of
transcripts in five formats; no model reads that. `dig.py` does the deterministic
reduction and makes no judgment. **Which chores are worth automating, and in what form,
is the only thing left to you.**

Every command takes the same range flags. Ask for none of them up front; use the defaults
and say what range you read.

```
--days N | --since YYYY-MM-DD [--until YYYY-MM-DD]
--agent claude,codex,opencode,cursor,agy,gemini     (default: every one found)
--project SUBSTR   (repeatable, any)   --exclude-project SUBSTR   (repeatable)
```

## Find a past session

```bash
python3 <this skill>/dig.py find PAY-881
python3 <this skill>/dig.py find checkout retry --since 2026-08-01 --agent codex
```

Every term must appear (AND). It searches whole transcripts — prompts, replies, tool
output — because ticket ids and branch names live in tool results, not in what the user
typed; injected context (skill listings, instructions, environment blocks) is excluded.
Hits rank by relevance and carry a resume command where the agent has one.

Answer with the session, its date, agent and project, and what it was doing, then offer
the resume command. Read the transcript for detail rather than guessing from the snippet.

## Mine for chores

### 1. Read the ledger

`~/.the-world/dig-my-sessions/ledger.md` holds every chore judged on earlier runs,
rejects included. Never re-propose a chore the user declined, nor re-derive a recorded
verdict. It is on the user's machine, never in a repository: it describes their work.

### 2. Scan

```bash
python3 <this skill>/dig.py chores --days 90
python3 <this skill>/dig.py chores --days 90 --json out.json
```

- **session errands**: one row per session (`date · agent · project · turns/span ·
  tools` and the opening prompt). The opener states the errand; later turns are steering.
- **verbatim repeats**: the same request re-typed across sessions. Lexical, so a hit is
  "obviously scriptable" and a miss never means "not recurring".

Sessions a program started are dropped before anything else, because they recur by
construction: SDK and `codex exec` runs, orchestrator-dispatched workers, scheduled "Run
the … skill" prompts, smoke tests, anything run from a temp directory. The header says how
many. `--include-automated` keeps them.

### 3. Jev mode, when the history is too big to read

At one row per session, a few hundred rows read fine in one go and Jev adds nothing. Offer
`--jev` when the scan is across many agents or projects, or runs to thousands of rows.

`--jev` sends each session's opener to TypeSafe's API (`TYPESAFE_API_KEY`). Before the
first run, say so in one line: the opener only, redacted (credentials, cookies, auth
headers, key and token values, emails, URLs, home-directory paths) and cut to 600 characters. Run
`--jev-dry-run` first and show its sample. Ask which projects to leave out (work
repositories usually) and pass them as `--exclude-project`. Send nothing without a yes.

```bash
python3 <this skill>/dig.py chores --days 120 --exclude-project acme --jev-dry-run
python3 <this skill>/dig.py chores --days 120 --exclude-project acme --jev
```

Jev answers only narrow questions. A Choice labels each opener `recurring_chore`,
`one_off`, `exploration` or `other`. A Noul asks "the same chore?" of similar pairs that
code proposes. Code unions the yeses and counts sessions, distinct days, agents, projects
and cadence. Read its **shortlist** as your input to step 4, not as a verdict. A group
marked `ONE SITTING` is one session's worth of retries, not recurrence. Do not ask Jev
how automatable something is. One line cannot show how often a chore recurs, and that is
where the value is.

### 4. Cluster into chores

Group openers by **what work they are**, not by wording. A chore's members share a shape:
the same trigger, the same sources consulted, the same artifact produced. The tool column
tells you what the work touched and separates chores that read alike but aren't.

A real chore recurs on a cadence or per external event, consults the same sources each
time, and has many turns but few decisions. The user is driving a procedure. Discard
one-off debugging, design chats, and anything whose value was the conversation.

### 5. Judge each chore

- `already automated`: a skill, command or harness job covers it. If the user still does
  it by hand, the finding is *why* (unknown, bad trigger, broken).
- `one-liner`: an alias or a short script is enough. Say so. A skill would be ceremony.
- `skill`: deterministic enough to script, with the user in the loop.
- `world job`: it recurs on a schedule or on a change, with no human trigger. It belongs
  to a World harness's characters.
- `not automatable`: the judgment is the work. Say so and drop it.

Rank by (frequency × manual effort per run) ÷ difficulty. Prefer chores whose failure is
boring, and propose no more than about five.

### 6. Propose, then hand off

Report a compact table: chore · evidence (N sessions, distinct days, date range, agents,
projects) · verdict · form · what it saves. Then let the user pick. Never build unasked.

- `world job` → hand it to `/the-world-start` as one sentence with its project, e.g.
  `/the-world-skills:the-world-start <project> 매일 아침 9시에 …`. An existing harness takes it
  through its own commands for adding a bot. Do not build harness code from here.
- `skill` → build it with the user's skill-authoring flow. Deterministic steps go in a
  script, one judgment stays with the model, and the triggers use the user's own phrasing
  from the evidence.
- `one-liner` → write it where the user keeps their shell config, once they agree.

### 7. Record the verdicts

Append every judged chore to the ledger, rejects included, with the reason:

```
| date | chore | evidence | verdict | outcome |
```

A run that proposes nothing new is a good run. Say so and stop.

## Notes

- Only what the human typed counts as a prompt. Slash-command bodies, injected context,
  tool results and task notifications are filtered in `sources.py`. Do not loosen the
  filter. Skill bodies would dominate every cluster.
- Openers are cut to 400 characters. When a cluster hinges on more, read the transcript:
  the `file:` of a `find` hit, or the session's path in `--json` output.
- Cursor's IDE agent chats are read; the `cursor-agent` CLI's store is not. For `agy`,
  prompts come from its `history.jsonl`, and `find` also reads the text fields of each
  conversation's step records. The Antigravity IDE's conversations are not read.
- Everything except `--jev` is offline and read-only. SQLite stores open with `mode=ro`.
