# Why The World's agents and tools look the way they do

This doc records *why* each boundary exists and the failure mode each principle
prevents, so a future skill author can tell the difference between an
arbitrary convention and a load-bearing one. Each harness's world-word skills
point here instead of encoding steps; this is the reasoning behind the boundaries.

The sentence everything below reduces to: **a promise written in a prompt is
not enforcement.** Read-only, single-approver, fixed-target — every one of
these can become a sentence in a prompt that gets violated when reality does not match
the prompt author's assumptions. The fix
was never "write the sentence more forcefully." It was always: move the
boundary into code, or remove the capability so there's nothing to violate.

Origin incident: a config-diff agent `git add -A`'d and committed a human's
unrelated in-progress edit alongside its own diff, because "read-only" was a
prompt convention, not a code constraint. Nearly every principle here traces
back to that one commit.

## Agent philosophy

**R&R is re-derived from the current SKILL.md/prompt, every time — never
assumed already-correct.** When scoping what an agent may touch, read the
prompt it actually runs and cite the specific lines that need each
capability. "It already has broad access, leave it" is not an allowed answer —
if the prompt changed, the scope table is re-derived, not carried forward.

**Worktree ownership is a trust boundary.** Only agents with `mutates=True`
get their own worktree; everything else starts in the main checkout with no way to
write to it (no write tool bound there, no raw Write/Edit, git writes refused by
the git guard). Mutate-or-not is declared before execution (in the agent's definition, not
decided mid-run) — switching a session from read-only to mutating partway
through risks leaving the shared checkout dirty for whoever runs next.

The kit enforces this at spawn, not in a tool: `world/process/spawn.py` cuts a
worktree per task (`.the-world/worktrees/<agent>-<task id>`, branch
`worktree-<agent>-<task id>`, off `origin/<default_branch>` or the local default
branch when there is no origin) and starts the run there — the Claude runner's `cwd`,
Codex's `-C`. A retried task comes back to the same worktree. If it cannot (no git
repository, `git worktree add` failed, the guard hooks are not committed so the
worktree would have none), the run is refused and recorded as blocked; it never falls
back to the main checkout. Before that, the flag was only a comment and every agent ran at
the git root — the same shape as the origin incident, a promise with no code behind it. The
worktree is swept like any other (merged and clean → pruned; idle and unmerged →
asked in its thread). A cwd is not a sandbox: a tool bound to an absolute path still
writes there, so a tool that edits or commits repo files resolves
`world.context.current_worktree()` rather than a path of its own.

**Exit-0 is not completion.** A headless run can be blocked by a permission
denial and still exit 0 — the subprocess doesn't know the difference between
"finished" and "gave up quietly." Agents that produce analysis
(`writes_report=True`) are verified by checking a file actually landed in
its task's `work/`, not by the exit code.

**No silent caps.** Concurrency limits queue and announce; they don't drop
requests. Retries continue the same session (context intact) rather than
opening a fresh one, and escalate to a human only after a fixed retry budget —
never silently give up.

**Suspect injected context before the agent.** When multiple agents
misbehave in the same way at once, the shared cause is almost always
upstream — a poisoned report, a bad state read, contaminated context fed to
all of them — not simultaneous unrelated bugs in each agent. Origin: a
misclassified "blocked" alert got written back into shared state and then
re-read by every other thread, which each independently escalated to
requesting broad emergency permissions.

**No self-reconfiguration.** An agent thread never proposes changes to its
own settings, allowlist, hooks, or harness code — not even via an approval
request. Denied commands become reports for a human to act on; tuning the
harness is operator work, never something the harness talks itself into.

**Naming is not free — check for collision with meaning and existing
assets first.** A name that clashes with an unrelated existing tool/system
(picking "sentry" when Sentry.io monitoring is already in play) creates
confusion that costs more than the fun of a good name.

**One socket, N identities — but the execution layer is genuinely separate
per persona.** A single Socket Mode connection receives events for every
persona, and by default one Slack app posts for all of them under each one's
name; every persona still gets its own provider-runner turn with its own
system prompt, tool grants, and conversation history. Shared plumbing at the
transport layer must not leak into shared state at the execution layer.

What one app costs, said plainly. Closed: which persona a turn belongs to is
decided by the harness (a person's message is the main persona's turn; a
teammate's turn starts only from a `hand_off` call made during another
persona's turn), so typing "@Scout" or "Scout:" opens nobody's tools, and each
persona's tools are still chosen in code per turn. Not closed: when every
persona had its own Slack app, a sub-persona's token carried only `chat:write`
and `reactions:write`, so Slack itself was a second wall behind the code. One
app means one token with the main persona's scopes for all of them. The model
never sees a token, so the wall that remains is the code; a persona that needs
Slack's wall back can still be given its own app (`bot_token_env`).

**When asked to prove zero flaws, say what's closed and what isn't — don't
claim zero.** A design review that's asked to guarantee "no security flaws"
should answer with a numbered list of what's actually closed and what remains
open (documented residual risk), not a blanket assurance. Adversarially
verifying a design (having an independent pass try to break it, then fixing
what breaks) produces a design you can make honest claims about; iterating
the same reviewer on the same assumptions doesn't.

## Tool philosophy

The tool pattern in the kit's `toolkit.py`: instead of widening a Bash allowlist so an agent can run
one more command, promote that capability to a dedicated `ToolSpec` whose handler scope
stays fixed in the parent process while provider adapters change only its wire form. Its
scope is fixed in code. **A prompt can be talked out of a rule; code can't.**

**Bash gets wrapped, not allowlisted wider.** Every capability an agent needs
— reads included, not just writes — becomes a typed tool. The end state for
any agent is `allow_bash=False`: Bash removed entirely once its whole surface
is covered by scoped tools, not merely restricted by allowlist entries that
can always gain one more line.

**Denylists are structurally incomplete — scope by allowlist, not
blocklist.** A tool that blocks known-dangerous flags (`--output`, `-o`) will
always have an unconsidered flag left over (`git diff --no-index /etc/hosts`,
`git blame --contents=/etc/hosts` read arbitrary local files past a
flag-denylist). The fix is to enumerate the allowed subcommands and flags per
subcommand explicitly, and reject everything else by default.

**Remove the shell itself; don't try to escape it.** Every subprocess call
goes through `exec` with an argv list, never `shell=True` and never string
interpolation of user/agent-controlled input. Shell metacharacter injection is
then not mitigated — it's structurally impossible, because there's no shell
parsing the string. SQL and similar inputs go over stdin, not into a
command-line string, for the same reason.

**A tool's own parameters can reopen the boundary it claims to have.** A
`target` parameter documented in every prompt as fixed but accepted
as a free-form string from the caller is a boundary that exists in the prompt
and nowhere else — indistinguishable from the read-only violation this whole
doc starts with, just one layer down. If a value should always be one
constant, don't expose it as a parameter at all.

**One enforcement layer per boundary, not defense in depth by duplication.**
Enforcing the same rule in two places (a PreToolUse hook *and* a
permission-mode callback) doesn't add safety — it adds a second place that can
silently drift out of sync with the first (worse: some permission modes never
invoke the callback at all, making the second layer a silent no-op). Pick the
one mechanism that's actually guaranteed to run, and put the rule there only.

**Omit the dangerous sibling; don't forbid it.** If an agent needs to open a
PR but must never merge one, the tool module simply has no merge function —
not a merge function guarded by a check. A capability that was never
implemented cannot be invoked no matter how the agent is convinced to try.

**Don't build a tool for what the built-in tools already cover.** File and
repo exploration needs no custom tool — Read/Grep/Glob already do it without
touching Bash. Build a tool for a genuinely new capability, not as a wrapper
around something already available.

**Content-borne manipulation is a different problem than permission scope,
and tool design can't close it.** Text planted in an issue body or a query
result can talk an agent into taking a legitimate, in-scope action (writing
to a path it's genuinely allowed to write) for an illegitimate reason. That's
a content-trust problem for the human approval step to catch, not something
a tool boundary can be tightened to prevent — don't blur the two together or
claim a tool design "closes" an injection risk it structurally can't.

**Verify the boundary adversarially, not just the happy path.** After writing
a tool, try to make it do the forbidden thing yourself — wrong repo, a path
escape, one extra flag — before calling it done. Assuming a tool is
well-scoped because it looks well-scoped is exactly the assumption that let
the `git_read` arbitrary-file-read slip through undetected until someone
actually tried it.
