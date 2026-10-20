---
name: help-my-world
description: The World — Ask the-world-skills maintainers for help by filing a GitHub issue they can act on without asking back. Built for people who are not developers — they say what they wanted and what happened, in their own words; the agent fills in the rest from the session and the harness (plugin version, OS, runtime, install profile, the world's characters, triggers and tools, git state, recent logs), redacted by a bundled script, in the repository's own issue form. Walks the user through installing and logging in to the GitHub CLI if needed, shows the full draft, and files nothing until the user says to. Use for "help my world", "help me", "도와줘", "이거 이슈로 올려줘", "더월드 버그 제보", "봇이 이상해", or when start or a harness failed and the user wants the maintainers to know.
disable-model-invocation: true
---

# /help-my-world — ask the maintainers for help

After the `start` skill builds it, every world is different: its own characters, jobs, tools, and
schedule. The person asking for help is often not a developer and should not have to
become one to be helped. So the user says what happened in their words, and you do the
rest: the maintainer who reads the issue was not here, and gets everything they would
otherwise have to ask for.

Talk to the user in their language and in plain words. Never ask them for "steps to
reproduce", "expected behavior", a version, or a log: you find those. Write the answers
in the issue in their language; the headings come from the repository's form, so every
issue scans the same way.

## 1. What happened

Ask one question, unless the session already answers it: **what were you trying to do,
and what happened instead?** A screenshot, a copied bot reply, or "it just stopped" are
all fine answers.

Then work out the rest yourself, from this session and the harness: the command or
message that started it, the step it stopped at, the exact error text or bot reply,
whether it happens again, what was already tried. Say in one line what you took from the
session. Ask a second question only if the issue would be useless without it.

Pick the form from what they described:

- `1-help.yml` (label `question`): stuck, confused, or a bot doing something odd. The
  default; when unsure, this one.
- `2-bug.yml` (label `bug`): something clearly broke, with an error or a crash.
- `3-feature.yml` (label `enhancement`): it works, and they want it to do more.

## 2. The environment

Run the bundled collector from this skill's directory, pointing it at the harness when
there is one (default: the current directory and its parents):

```bash
python3 <this skill's dir>/collect_env.py [HARNESS_DIR]   # `python` on Windows
```

It prints Markdown: plugin version, OS, Python, uv, git, the claude/codex/slack/gh CLIs,
the install profile, the world's shape (characters, each agent's trigger and tools, the
tools the harness defines, read from code without running it), git state with how many
changed paths are kit-owned, the Process and Agent pulse lines of `STATUS.md`, and the
last 30 lines of the harness logs. It never opens `env/`, never prints `WORLD.md`, a
prompt, or a voice, and masks token shapes and the home directory.

Read its output before using it. Remove anything that still looks private: a customer
name, an internal URL, a person's name, message text from a channel. Logs are folded in
`<details>` blocks; keep only the lines that bear on the problem when they are long.

## 3. GitHub, set up once

The issue is filed with the GitHub CLI (`gh`). Check it before drafting, so the user is
not asked to approve a draft that cannot be sent:

```bash
gh --version && gh auth status
```

If either fails, walk the user through it one step at a time, saying in one sentence
what each step is for (`gh` is the program that lets you post to GitHub from here):

1. A GitHub account. If they have none: sign up at https://github.com/signup.
2. Install `gh`. macOS: `brew install gh` (without Homebrew, the installer at
   https://cli.github.com). Windows: `winget install --id GitHub.cli`, then open a new
   terminal. Linux: the package for their distribution, listed at https://cli.github.com.
   Run the install yourself when the user agrees.
3. Log in. `gh auth login` asks questions and opens a browser, so the user runs it in
   their own terminal (Claude Code: type `! gh auth login`; Codex: a terminal window).
   Tell them the answers ahead: GitHub.com, HTTPS, "Login with a web browser", then copy
   the one-time code into the page that opens.
4. Run `gh auth status` again and say it worked.

If they would rather not, or it fails twice, stop setting up and use the fallback in 6.

## 4. The form

Read the chosen form from the repository:

```bash
gh api repos/afinit-com/the-world-skills/contents/.github/ISSUE_TEMPLATE/1-help.yml \
  -H "Accept: application/vnd.github.raw"
```

Use its `labels:` as the issue's labels, and nothing else. Use each `body` field's
`label`, in order, as a `### <label>` heading, which is how GitHub renders a filed form.
Under each heading put that field's answer: the user's words for what they wanted and
what happened, what you worked out for the rest, one of the listed `options` for a
dropdown (from the collector), and the trimmed collector output under `Environment
report`. Leave an optional field with nothing to say as `_No response_`, as the web form
does. Never invent an answer for a required one; ask.

## 5. The draft

Title: one line naming the symptom and where, under 80 characters, in the user's
language (`등대 관측소 봇이 오늘 일지가 없다고 답함`).

End the body with a line `Filed with /help-my-world`. Show the user the title, the labels,
and the whole body, in plain words about what will be public ("this will be visible on
GitHub to anyone who can see the repository"), and ask whether to file it. Any change they
ask for, make and show again. Filing is publishing: nothing is sent before a yes.

## 6. Filing

Write the issue as one UTF-8 JSON file and send that file. Passing Korean (or any
non-ASCII) text as a command-line argument is what turned two earlier issues into `?????`
on Windows, so neither the title nor the body goes on the command line:

```bash
# issue.json: {"title": "...", "body": "...", "labels": ["question"]}
gh api repos/afinit-com/the-world-skills/issues --method POST --input issue.json --jq .html_url
```

Give the user the URL it prints and say a maintainer will answer there. Delete
`issue.json` afterwards.

If GitHub cannot be reached this way (no `gh` after step 3, or the repository answers 404
because it is private and this account cannot see it), say so plainly and do not work
around it. Save the draft as `the-world-issue.md` in the current directory, tell the user
where it is, and give them the web form to paste it into,
`https://github.com/afinit-com/the-world-skills/issues/new?template=1-help.yml` (or the
form you chose). Without `gh` the form cannot be read either, so draft under the
headings What I wanted to do, What happened instead, What I tried, the-world-skills
version, Runtime, OS, Environment report.
