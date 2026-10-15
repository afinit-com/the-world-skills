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
listener whose `post` and `save_state` reach nothing:

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
through (`gh`, an HTTP post, `subprocess.run`) in `outward`. `refuses` stubs each one for
the call and fails if any was reached, even when the tool then reported an error, and it
will not run with none named. An open boundary then shows up as a failed check with
nothing written. If the tool has no single function to stub, that is worth fixing in the
tool before checking it.

Name the function where the tool looks it up: `abilities.reviewer.tools.gh_write`, the name
in the tool's own module, not only the module it came from. A tool module that did
`from tools.github import gh_write` has its own name for it. `refuses` stubs every other
module-level name bound to the same function as well, but a reference kept in a closure, a
default argument, or a dict of handlers is not found, and the real write would run.

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
- Each handoff is a `→ <name>: …` line, and the teammate answered in the same thread.
- Each sub-persona posted under its own name, icon, and voice. One that posts through the
  main app needs `chat:write.customize`; without it the post quietly shows the main app's
  name, and the dry run cannot see that.
- Tool calls came in the order the job needs.
- An approval card says what the proposal in the thread says, and after a person approved,
  the tool ran once.

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
