---
name: audit
description: The World — Read-only risk review of an existing World harness. Reports, item by item, what is closed, what is open, and what could not be verified across bot authority, tool scope, outward actions and approval, content-borne manipulation, self-reconfiguration, secrets, and silent failure. It never fixes anything. Use for "the world audit", "하네스 점검", "하네스 보안 검토", "권한 감사", "is this harness safe", or before connecting a harness to Slack or giving a bot more authority.
---

# audit

Review a harness that `start` built and say what is closed and what isn't.
Respond in the user's language. The sentence the kit reduces to is "a promise written in a
prompt is not enforcement", so every item here is judged by what the code does, never by
what a prompt or a comment promises.

## What must hold

**Read-only.** Do not edit, create, delete, commit, install, or run the harness. No
`harness.py`, no `bin/smoke.py`, no `uv sync`, no `import world`; importing the harness
loads its credentials and constructs its tools. Reading files, `git log`/`git diff`, and
feeding a crafted JSON payload to a guard hook on stdin (hooks only print a verdict) are
fine. If the user asks you to fix something, finish the report first and say the fix is a
separate step.

**Three words, no fourth.** Each item is `closed` (the boundary is in code and nothing
reopens it), `open` (a concrete way through), or `not verified` (you could not decide
from what you read). Never report "no issues" or "secure". Something you didn't check is
`not verified`, and an empty `open` list still comes with its `not verified` list.

**Every open item is reproducible.** It names a file and a line and gives a way to see
it happen: a call with specific arguments, a message to a specific bot, or a hook payload.
An open item without both is `not verified`.

**Never print a secret.** Name the variable or the file and line. The scanner shows only
the first four characters of a token. Do the same, and say to rotate it.

## How

1. **Find the harness.** It is the directory holding `harness.py` (and usually
   `WORLD.md`). If the user gave none, look at the current directory, then ask.

2. **Run the scanner** from this skill's directory:

   ```bash
   python3 <this skill>/scan.py <harness>          # grouped report
   python3 <this skill>/scan.py <harness> --json   # the same, as data
   ```

   It parses the harness with `ast` and never imports it. It compares `world/` and the
   guard hooks with the kit bundled in `start`, and it checks the kit lines
   that carry each boundary. Its `closed` means "no pattern matched", which is evidence
   but not proof.

3. **Confirm, then widen.** For each `open` item, open the file at the line and confirm
   it is real. Drop false positives and say why. Then read what a pattern scan can't
   judge:
   - every tool module: what it is bound to (one repo, account, or host as a constant),
     which operations exist and which were left out, and whether any parameter lets the
     caller pick the target;
   - every worker's prompt file against its authority: does each capability trace to a
     line that needs it?
   - every poll check: where its prompt text comes from, and whether anything outside
     the harness writes there;
   - anything the scanner marked `not verified`.

   For a guard hook you suspect, prove it by piping a PreToolUse payload to it, for
   example `echo '{"tool_name":"Write","tool_input":{"file_path":"<abs path>"}}' |
   WORLD_AGENT=<agent> python3 .claude/hooks/world-scope-guard.py`. Exit 2, or a JSON
   `deny`, means refused.

4. **Report.** Group items by the seven categories below, open items first. For each one,
   give its status, one sentence on what is wrong or what holds, the `file:line`, and for
   open items the repro. Close with counts per status and the one open item to fix first,
   and say what you could not check (for example, approval was never exercised because
   Slack isn't connected).

## The seven categories

The scanner's checks map onto these. Your reading fills the gaps it names.

1. **Bot authority that leaves the defaults.** `allow_bash`, `task_allowed`,
   `allow_plugins` (a plugin brings its own hooks and MCP servers, which
   `strict_mcp_config` does not block), the mention responder loading plugins, and a
   `write_scope` that is empty, too wide, or not matched by a write tool bound to the same
   roots. `mutates=True` is checked against where its tools actually write: the kit spawns
   such a run in its own worktree (`cwd` / Codex `-C`) and refuses it without one, but a
   cwd is not a sandbox, so Bash and any tool bound to an absolute path can still reach
   the main checkout. A harness whose `world/` predates spawn-time worktrees (no
   `worktrees.run_worktree(` in `world/process/spawn.py`) spawns every agent at the git
   root, and its `mutates=True` agents are reported open.
2. **Tools whose scope is not really closed.** `shell=True`, string-built commands,
   denylists, free parameters such as repo, host, url, or account, and paths not resolved
   and checked for containment (`..`, absolute paths, symlinks).
3. **Outward actions and approval.** Tools that leave the machine without
   `make_approval_gated` (or a persona's `mention_tools`, which the kit wraps),
   `pre_approved_tools`, an unset approver (the kit's gate then lets anyone approve), a
   co-approver whose decisions arrive as posted text, and `danger` buttons.
4. **Content-borne manipulation.** Slack or fetched text reaching a bot that holds more
   than its task lane, findings carrying that text into other bots' prompts, and ledger
   lines (read back into every spawn) that the approver did not write.
5. **Self-reconfiguration.** Any scope or tool that reaches hooks, settings, the scope
   table, harness code, credentials, or the ledger; all four Claude guard hooks (or the
   Codex guard) registered for the tools they guard; `disableAllHooks` anywhere a spawn
   loads settings from; kit files changed from what the kit ships.
6. **Secrets and environment.** Token patterns in code, prompts, logs, tasks, or the
   ledger; `env/harness.env` committed or not ignored; credentials a Bash-holding bot can
   read from its inherited environment; credentials each tool holds and whether they are
   the narrowest that work.
7. **Silent failure.** Unattended workers with no artifact contract (`writes_report`
   or a `task_type`), poll checks that mark input seen before `on_done`, swallowed
   exceptions, and whether concurrency still queues rather than drops (true while
   `world/` matches the kit).

Why each boundary exists, and the incident behind it, is in the harness's own
`docs/philosophy.md`. Cite it when a finding needs its reason.
