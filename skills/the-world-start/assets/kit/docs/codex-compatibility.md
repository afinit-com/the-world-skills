# Codex compatibility and operating decisions

## Decision

The first Codex adapter uses `codex exec --json`, not the Python/TypeScript SDK. The CLI
path meets the required boundaries:

- `--ignore-user-config` plus a root `mcp_servers={}` override prevents inherited user and
  project MCP servers from joining the run.
- `--sandbox read-only` prevents provider-native repository writes.
- `features.shell_tool=false`, `agents.enabled=false`, and disabled apps/browser/computer
  surfaces remove capabilities unless the `Agent` explicitly opts in.
- a required per-run stdio MCP adapter exposes only `Agent.tools`; handlers and approval
  state remain in the parent process behind a mode-0600 Unix socket, or a Windows named
  pipe with an explicit current-user DACL and remote clients rejected.
- JSONL provides final messages, MCP calls, errors, and token usage in a stable stream.

The live probe is `bin/codex-spike.py`. It calls a scope-bound finding tool and verifies
the expected file rather than accepting the model's final text as completion.

Official interfaces used here:

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex hooks](https://learn.chatgpt.com/docs/hooks)

## Security invariants

1. `ToolSpec` is the only portable capability declaration.
2. Codex starts read-only and receives a freshly constructed MCP table for each run.
3. MCP calls are pre-approved only because every exposed handler was selected in code;
   outward effects still require the kit's one-shot approval wrapper.
4. `apply_patch` and Bash hooks are backstops. The primary boundary does not depend on a
   project hook being trusted or firing.
5. malformed Codex hook input and unparseable patch targets deny in headless sessions.
6. an agent that promises a finding is successful only when the file exists.

## Compatibility matrix

| Provider | Trigger | Slack path | Automated evidence |
|---|---|---|---|
| Claude | poll/schedule | finding announcement | core completion, schedule, state, installer tests; existing smoke |
| Claude | mention | thread + one-shot approval | fake gateway approval test; Claude adapter compatibility contract |
| Codex | poll/schedule | finding announcement | fake CLI contract, JSONL parser, live scoped-MCP spike |
| Codex | mention | thread + one-shot approval | same provider-neutral mention path and fake gateway approval test |

Across the matrix, tests cover path escape, missing-output blocking, blocked and rate-limit
backoff, ledger fabrication, single-use grants, installer re-entry, Codex hook fixtures,
and provider tool-surface construction.

Run offline checks:

```bash
PYTHONPATH=. python3 -B -m unittest discover -s tests -v
python3 -m compileall -q world tests
```

Run the live Codex boundary probe (uses the saved CLI login and one small model turn):

```bash
uv run python bin/codex-spike.py
```

## Residual risks and explicit limits

- The private bridge uses Unix domain sockets on macOS/Linux/WSL and Windows named pipes
  on native Windows. Windows requires pywin32 (installed by the platform dependency).
  Both transports trust other processes running as the same user, just as a 0600 socket
  does. Administrator or SYSTEM processes can bypass Windows user ACLs. No TCP port or
  serialized Python object is used. Windows transport tests cover separate-process stdio,
  concurrent calls, unknown-tool denial, explicit DACL, local address validation, cleanup.
- Codex's default `workspace-write` sandbox protects `.codex/` and `.agents/`, so an
  installer or `bin/scaffold-world-skill.py` run inside it is refused there. Both probe
  every destination first and exit non-zero with nothing written. Approve the escalation
  (or use a sandbox that permits those paths) and re-run; never copy the rest by hand.
  Unapproved, the guard hooks and Codex word commands are not installed.
- Project Codex hooks require the operator to review/trust them for interactive use.
  Scheduled runs remain safe if hooks are skipped because shell/apply-patch authority is
  removed or read-only before the model starts.
- Codex plugin loading is deliberately rejected. A plugin can add hooks and MCP servers
  outside `Agent.tools`; support waits for a way to scope those surfaces per run.
- Codex JSONL reports tokens but not a computed USD total, so `cost_usd` remains zero and
  the run footer and STATUS.md say `USD 미제공` instead of showing `$0`. Codex's
  `input_tokens` already includes `cached_input_tokens`; the runner subtracts it so the
  shared counters (fresh input + cache read) do not count cached input twice.
- Codex defers MCP tools behind `functions.exec`, so a model that only scans its top-level
  tool list can report a granted tool "unavailable" or describe a `hand_off` it never
  made. The runner names the turn's WORLD tools at the top of the prompt and says how to
  reach them; it grants nothing. A handoff is verified by the queued call, not the reply.
- `web_search` is disabled per run like the other non-`ToolSpec` surfaces.
- The MCP adapter implements the small protocol surface The World uses (`initialize`, `ping`,
  `tools/list`, `tools/call`). Add protocol conformance tests before adding resources,
  prompts, elicitation, or sampling.
- Slack remains the only channel implementation. `ConversationGateway` is intentionally a
  narrow boundary, not a promise that another channel already exists.
