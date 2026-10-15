# Verifying that a harness does what it says

`bin/smoke.py` and `harness.py --once --dry-run --no-slack` check the runtime's seams. Most
of what a harness actually does happens past them: a handoff queued and run, a sub-persona
posting under its own name, an approval card clicked before a tool runs, a report file
landing, a comment that appears once on GitHub. This is the flow for checking those, in
order, and for reporting what was and was not seen.

Start from this harness's own plan:

```bash
uv run python harness.py --verify-plan
```

It reads `harness.py` and prints the six steps below filled in: the channel and approver,
each persona and how it posts, the tools behind approval, and the workers that must leave
a report. It runs nothing, so every line in it is a box still to check.

Run the flow after local verification passes, again right after Slack is first connected
(the first real thread is where a missing scope or a wrong channel shows), and after any
change to a persona, a tool, or an approval path.

## 1. Local checks

`bin/smoke.py` and the dry run. Fix every `[WARN]` before going on. A dry run is not a
Slack connection and not a real agent result; it proves the tick renders.

## 2. Tool boundaries

For each boundary a tool's code claims (a fixed host, an allowlist, a verdict that blocks
an action, a pinned head SHA, a path), make one call it must refuse and read the refusal.
`world.tool_check` builds a thread-bound tool the way the listener does, from a fake
listener whose `post` and `save_state` reach nothing.

The check stubs the function a tool writes through, so give each write its own function.
A tool built on the kit's `world.toolkit.run_cmd` runs its reads (`gh pr view`) and its
writes (`gh pr comment`) through the same helper; wrap the writes:

```python
# abilities/reviewer/tools.py
from world.toolkit import run_cmd

async def gh_write(*args: str, cwd: str) -> tuple[int, str]:
    """Every gh call that changes something outside goes through here, and only those."""
    return await run_cmd("gh", *args, cwd=cwd)
```

Then the check names that function:

```python
import asyncio

from world.tool_check import FakeListener, bind, refuses, text
from abilities.reviewer.persona import PERSONA   # wherever the persona is declared

async def main():
    listener = FakeListener(state={})
    tools = await bind(PERSONA.mention_tools, listener, "1700000000.000100")
    refused = await refuses(tools, "pr_comment", {"url": "https://example.com/x/y/pull/1"},
                            outward=["abilities.reviewer.tools.gh_write"])  # the name it writes through
    print("other host refused:", text(refused))

asyncio.run(main())
```

Keep a script like this in the harness's own `tests/` so the next change runs it too.

The fake stands in for Slack, not for the tool: the handler runs for real. So a boundary
check never lets a write tool reach the outside. Name the function the tool writes
through in `outward`. `refuses` stubs each one for the call and fails if any was reached,
even when the tool then reported an error or broke on the stub's return value, and it
will not run with none named. An open boundary then shows up as a failed check with
nothing written. If the tool has no single function to stub, that is worth fixing in the
tool before checking it.

Stub a function that only writes. `subprocess.run` does not cover `run_cmd`, which goes
through `asyncio.create_subprocess_exec`, so the write would run. Stubbing `run_cmd`
itself stubs the reads as well: the tool fails on a read it needed, and the check reports
an open boundary where there is none.

Name the function where the tool looks it up: `abilities.reviewer.tools.gh_write`, the name
in the tool's own module, not only the module it came from. A tool module that did
`from tools.github import gh_write` has its own name for it. `refuses` stubs every other
module-level name bound to the same function as well, but a reference kept in a closure, a
default argument, or a dict of handlers is not found, and the real write would run.

A write the handler hands to a task (`asyncio.create_task`) and does not wait for is waited
for, still stubbed, for a couple of seconds; a task still running then is cancelled and
fails the check. A write handed to a thread (`run_in_executor`) is not seen; keep the write
in the handler's own `await`.

`bind` returns the tools without the approval gate the listener wraps around
`mention_tools`. That is on purpose: this step checks the tool's own code. The gate is the
kit's, and the kit tests it.

## 3. Process and connection

The process is running (the README says how it is started), `STATUS.md` is fresh and names
the channel state, and the newest log in `logs/` shows no error since the last start.
Connected means `STATUS.md` says so now, not that it said so once.

## 4. A real thread

Open the home channel in a browser and read the thread a person started. Scroll it and
take screenshots; a claim about a thread cites what was on the screen.

- The main persona answered.
- A scheduled worker's card has a readable title and the body its `Agent.announce`
  selects: the final summary for `result` (default), the completion check for `evidence`,
  or the primary report's opening for `file`. For `file`, compare with the first
  current-run artifact inside that task's `work/` (completion orders paths lexically),
  stopping before the first `## `, or the next if the report starts with one.
  Missing, unreadable, empty, or binary reports fall back to the final summary; a path
  or symlink outside that task's `work/` never supplies the body. On Slack and Discord,
  check that long excerpts are marked, code fences close, credentials stay masked,
  and channel-wide mentions do not notify anyone. Use local fakes for these edge cases;
  do not send credentials or broadcast mentions into a real channel to test them.
- Each handoff is a `→ <name>: …` line, and the teammate answered in the same thread.
- Each sub-persona posted under its own name, icon, and voice. One that posts through the
  main app needs `chat:write.customize`; without it the post quietly shows the main app's
  name, and the dry run cannot see that.
- Tool calls came in the order the job needs.
- An approval card says what the proposal in the thread says, and after a person approved,
  the tool ran once.
- A repeated ask got a "대기 중인 승인 요청: 기존 승인 카드" link with no preview under it,
  not a second card. A button with no open ask behind it (an answered card, a forged value)
  changes nothing: the clicker alone sees "이미 처리됐거나 더는 유효하지 않습니다", the
  card text stays as it was, and the tool does not run again. Buttons inside a link
  preview send Slack no click at all, so pressing one shows nothing.

**Bot-token conversation checks (Slack only).** When Slack MCP and a user token are
unavailable, an authorized verifier can use the harness's own bot token. Initialize the
credential once, then restart the harness so the listener loads it:

```bash
uv run python bin/say-as-human.py init
uv run python bin/say-as-human.py send 'What can you check without writing anything?'
uv run python bin/say-as-human.py send --thread 1700000000.000100 'What did you find?'
```

For a harness using `OBS_SLACK_BOT_TOKEN`, pass `--prefix OBS` before `init` or `send`.
The matching key is `OBS_SLACK_AS_HUMAN_SECRET`; without a prefix it is
`SLACK_AS_HUMAN_SECRET`. `init` keeps an existing value and the env file's permissions,
prints no value, and creates a new env file with mode 600. For an existing short value,
clear its key in the env file and unset the shell override, then run the matching
`init` command and restart; `init` preserves existing nonempty credentials. An empty or missing secret, or
one shorter than 16 characters (the harness warns once), turns this feature off: messages, history and model input are then handled as if it did
not exist, and harness posts only drop a complete marker. Clear the value in
`env/harness.env` and restart to disable it.
`send` prints the posted message ts and accepts `--channel` to override the configured
channel; the receiver still accepts messages only in its own home channel.

The CLI prefixes the message with `[as-human:<secret>] `. The listener accepts this only
from the main bot itself, treats the body as a message from the configured primary
approver (the person `slack-pair` paired), and removes the prefix before passing input or
history to the model. The secret is a credential: keep `env/harness.env` out of prompts
and screenshots. The original Slack message still contains it and is visible to channel
members and apps that can read history; use only a trusted channel. Harness replies,
cards and progress messages strip the reserved pattern, so the model cannot generate a
new simulation. Other bots cannot use it. A person's marker is stripped as text and does
not change their identity.

For browser verification, Slack web can redraw a thread late and restore the original
as-human input after a DOM-only mask. Before capturing, let the thread finish loading,
mask every visible input marker and credential, then wait for a redraw and check the
visible thread again. Repeat the check after opening or scrolling the thread; a previous
mask is not evidence that it stayed masked. Inspect the captured image locally before
saving or sharing it; if any credential appears, discard the capture immediately and
repeat the masking and visual check. Prefer a view containing only harness replies.
After testing, ask the channel owner to delete the as-human input messages (including
thread replies); this is recommended cleanup, not permission for a verifier to delete them.

This exercises mentions and thread replies, including handoffs. A new thread automatically
mentions the main bot unless the text already mentions a bot: the main bot, or a persona
app listed in `slack-app/slack-apps.json`, using its configured `bot_token_env` key.
If that roster is missing or invalid, the CLI keeps the main mention. Mentioning only a person still adds the
main bot, so the harness answers. A thread reply is sent as written and keeps its existing
owner unless explicitly addressed elsewhere. It cannot simulate approval button
clicks, and a simulated "approve" message never grants `mention_tools` access. Hand an
approval card to the human approver and report its click path as unverified until they
click. Discord is outside this feature's scope. The same authorization and outward-action
limits below apply to CLI messages.

**Browser rules.** These hold for every browser check, by any tool:

- Read before you write. Send a new message only when the person you are working with
  allowed it in this conversation, and only a request that leaves nothing outside Slack
  (a question, a dry-run, a read). A test message that makes a worker comment, push, or
  merge is an outward action, not a check.
- **Never press an approve or deny button**, and never any button that grants, even when it
  is in front of you and the person seems to expect it. An approval is a person's
  decision; a click from the verifier is a forged one. Hand the card to the approver and
  check what happens after they click.
- Don't dismiss, edit, or delete messages, and don't change channel or app settings.

## 5. What was left behind

- Each worker with `writes_report=True` left a file under its task's `work/` for every run
  you checked. An exit code is not a report.
- `state.json`: a finished thread has no `handoffs` left, and `pending_approvals` and
  `approved_threads` hold only what is still open. A grant still sitting there after its
  tool ran means the tool did not succeed, or ran twice.
- Outside: read back what the tool did (`gh api …`, the Linear or Jira issue). It matches
  the approved proposal, came from the expected account, and appears once. The same
  action approved in two threads leaves two results; look for that.

## 6. Report

Two lists, kept apart.

- **Confirmed:** each check that was made, with what was seen: thread ts, file paths,
  comment ids, SHAs.
- **Not confirmed:** every check not made, and why: no credentials yet, not allowed to
  send a message, or the path never ran. A path that never ran is unverified, not passed:
  if every verdict so far was "hold", the approve and merge path has not been exercised.

Don't round the second list into the first. "Works in the dry run" and "the button was
there" are not "the tool ran".
