"""Helpers for building agent tools.

Instead of widening a
Bash allowlist, promote each outward capability to a dedicated in-process MCP tool
whose scope is FIXED IN CODE, not in a prompt. A prompt can be talked out of a
rule; these can't:

  - bind cwd / remote / repo / hosts at server construction, not per-call
  - omit dangerous capabilities entirely (for example, git tools have no merge —
    "the tool doesn't exist" beats "please don't use it")
  - truncate output so a chatty subprocess can't blow the agent's context

The `tool.md` skill generates tool modules that import these helpers.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from pathlib import Path
from typing import Any

# Re-exported so existing harness projects keep importing ``tool`` from world.toolkit.
# Provider adapters convert ToolSpec to their own wire representation at execution time.
from .context import current_task
from .tools.spec import ToolSpec, tool  # noqa: F401

MAX_OUTPUT = 4000

# --- the clock an agent does not have ----------------------------------------
#
# compose_prompt() never tells an agent what time it is, and spawned agents have no
# Bash, so `date` is not reachable either. Yet half the bots' prompts end with
# "log the result under logs/<you>/<timestamp>.md". An agent asked for a timestamp it
# cannot observe will supply a plausible one.
#
# Models can invent plausible timestamps. Tool time is observable; model-generated time
# is not, and durable records must not present invented time as fact.
#
# That is the same defect as the fabricated ledger entries (`world.process.ledger`):
# invented data written into a durable record that later readers treat as observation.
# Here the record is the filename that HEARTBEAT.md sorts by and humans read as "when".
#
# So the tool keeps the clock. `{ts}` is the only way to get one into a path, and a
# hand-written timestamp is refused rather than silently trusted.

TS_TOKEN = "{ts}"

# 2026-07-09, 2026-07-09T12-00-00Z, 2026-07-09T120000Z, 20260709T12:00:00Z, 2026_07_09-1200 …
#
# The time-of-day part is OPTIONAL, and it did not used to be. A bare `2026-07-09` was
# allowed on the grounds that "a date alone is the coarser convention the bots already
# use" — but convention was never the objection. The agent cannot observe the date any
# more than it can observe the hour. Nothing guarantees an invented time lands on the
# right side of midnight. A date it did not
# read is a date it made up, and HEARTBEAT.md sorts by these names and humans read them as
# "when". `{ts}` expands anywhere in the path, so `findings/monitor/{ts}-healthcheck.md`
# says everything the hand-written form said, truthfully.
_LOOKS_LIKE_TIMESTAMP = re.compile(
    r"\d{4}[-_]?\d{2}[-_]?\d{2}"           # a date …
    r"(?:[T_ -]\d{2}[-:_]?\d{2})?"         # … with or without a time of day
)


def utc_stamp() -> str:
    """`2026-07-09T05-12-33Z` — filename-safe, sorts lexicographically, real."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def stamp_path(rel: str, ignore_prefixes: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """(resolved_path, error). Expands `{ts}`; refuses a hand-written timestamp.

    `ignore_prefixes` are path prefixes the HARNESS generated, which are exempt from the
    scan. A task id is `YYYYMMDD-xxxx`, so `tasks/20260917-a3f2/work/note.md` reads to
    this check as a fabricated date and every write into a task's work/ was refused. The
    guard exists to stop an AGENT inventing a date it has no clock to know; a directory
    name the harness itself chose is not that, and exempting it keeps the check aimed at
    what it was built for instead of loosening the pattern for everyone.

    The `{ts}` substitution happens FIRST and the check runs on what is left, because
    `{ts}` used to short-circuit it: `logs/agent/{ts}-2026-07-09.md` expanded the token,
    skipped the scan, and wrote an invented date beside a real one. Using the sanctioned
    form for half a filename must not buy amnesty for the other half.
    """
    scanned = rel
    for prefix in sorted(ignore_prefixes, key=len, reverse=True):
        prefix = prefix.rstrip("/") + "/"
        if scanned.startswith(prefix):
            scanned = scanned[len(prefix):]
            break
    # The token is deleted, not replaced with a sentinel, so a date typed AROUND it still
    # reads as one: `2026{ts}0709.md` collapses to `20260709.md` and is refused. A sentinel
    # would let that through. The cost is a path with eight digits split across the token
    # and no date intended, which nobody writes.
    if _LOOKS_LIKE_TIMESTAMP.search(scanned.replace(TS_TOKEN, "")):
        return None, (
            f"'{rel}' contains a date or timestamp you wrote yourself, and you have no "
            f"clock — no Bash, and nothing told you the time. Write '{TS_TOKEN}' where it "
            f"goes (e.g. 'logs/agent/{TS_TOKEN}.md', 'findings/monitor/{TS_TOKEN}-http-5xx.md') "
            "and this tool will substitute the real UTC time."
        )
    return rel.replace(TS_TOKEN, utc_stamp()), None


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n... (truncated) ...\n" + text[-limit // 2 :]


async def run_cmd(
    *args: str, cwd: str, env: dict | None = None, timeout: int = 120
) -> tuple[int, str]:
    """Run a subprocess with combined output, timeout, and truncation."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"(timed out after {timeout}s)"
    return proc.returncode or 0, truncate((out or b"").decode(errors="replace"))


def result(rc: int, out: str) -> dict[str, Any]:
    """Standard tool return shape. is_error tells the SDK's error routing that
    the underlying subprocess failed — without it, a nonzero rc looks like a
    successful tool call at the MCP layer even though the text says "exit 1"."""
    status = "ok" if rc == 0 else f"exit {rc}"
    return {
        "content": [{"type": "text", "text": f"[{status}]\n{out}"}],
        "is_error": rc != 0,
    }


def make_tool_server(name: str, tools: list[Any]):
    """Bundle @tool functions into an in-process MCP server for ClaudeAgentOptions.

    Returns (mcp_servers_entry, allowed_tool_names) ready to splice into options:
        servers, allowed = make_tool_server("bots", agent.tools)
        ClaudeAgentOptions(mcp_servers={name: servers}, allowed_tools=allowed, ...)
    """
    from .profile import SUPPORTED_PROVIDERS, reinstall_hint

    if "claude" not in SUPPORTED_PROVIDERS:
        raise RuntimeError(f"make_tool_server is Claude-only: {reinstall_hint(provider='both')}")
    from .runners.claude import make_claude_tool_server

    server, allowed = make_claude_tool_server(name, tools)
    return server, allowed


def make_approval_gated(sdk_tool: Any, *, is_approved, consume, approver_label: str = "the approver",
                        refusal=None):
    """Wrap a side-effect @tool so it REFUSES to run until the approver has granted
    an approval in this thread, and spends that grant when it succeeds.

    Why this exists: a ticket-editing tool was once a harness's only outward
    write reachable from a conversational turn, and the only thing stopping
    it from firing before the approve button was clicked was a paragraph in
    RULES_TEMPLATE. Prompt rules are the thing this whole kit exists to not rely on
    (see this module's header): a model under load skips them, and no one finds out
    until the ticket is already edited. Now the refusal is in the call path.

    `is_approved` / `consume` are callables bound to ONE thread by the caller — the
    tool takes no thread argument, so an agent cannot point it at a thread that was
    approved for something else.

    `refusal` is an optional callable returning a sentence explaining THIS refusal.
    "never approved" and "approved, but the files changed underneath it" both come back
    as False here, and telling an agent to go ask again is only useful advice if it also
    learns which of the two happened.

    Consume-on-success, not consume-on-call: a JIRA 500 leaves the grant intact so
    the agent can retry, while a successful write spends it, so a second write needs
    a second click. That asymmetry is the whole point of single-use — don't
    "simplify" it into consuming up front.
    """

    @tool(sdk_tool.name, sdk_tool.description, sdk_tool.input_schema)
    async def _gated(args: dict) -> dict:
        if not is_approved():
            why = (refusal() if refusal else "") or "no approval is on record"
            return result(
                1,
                f"BLOCKED: this action needs {approver_label}'s approval in this thread "
                f"first, and {why}. End your message with an "
                f"`APPROVAL_REQUEST: <the action>` line and stop — the harness will "
                f"render the approve/deny buttons. Do not retry this tool until the "
                f"decision comes back.",
            )
        res = await sdk_tool.handler(args)
        if not res.get("is_error"):
            consume()
        return res

    return _gated


def make_mention_tool(name: str, target_display: str, target_user_id: str | None):
    """A single-purpose @tool that returns the exact `<@user_id>` mention string
    for ONE fixed target, bound at construction — not a generic mention(target)
    tool a caller could point anywhere. A persona whose tool list only contains
    e.g. mention_main literally cannot address anyone else through this tool, no
    matter what it's asked to do (same "the tool doesn't exist" philosophy as
    the rest of this module).

    This does NOT sandbox the model's own prose — text it free-types (including
    an id it recalls from earlier in the thread transcript) still matches
    listener.py's MENTION_RE same as ever. The point is reliability, not a hard
    security boundary: a hand-typed Slack id is exactly the kind of thing that
    can be hallucinated under load — this tool removes the need to recall or
    reconstruct it at all.
    """

    @tool(
        name,
        f"Get the exact Slack mention string for {target_display}. Call this "
        "instead of writing '<@...>' yourself — never guess or reuse an id you "
        "saw earlier in the thread.",
        {},
    )
    async def _mention(_args: dict) -> dict:
        if not target_user_id:
            return result(1, f"{target_display}'s Slack id isn't available right now")
        return result(0, f"<@{target_user_id}>")

    return _mention


def make_teammate_mention_tool(targets: dict[str, str]):
    """The main persona's handoff tool: the exact `<@user_id>` for ONE teammate on its
    roster, picked by name. `targets` maps display name -> user id and is fixed at
    construction, so the name argument only chooses among teammates the hub may already
    address; anything else is refused with the valid names.

    The main persona used to have only mention_approver, while the rules told it to call
    that tool to hand off. It did exactly that, got the approver's id back, and then
    typed the teammate's id from the roster by hand — the thing the mention tools exist
    to prevent."""
    names = ", ".join(f"'{n}'" for n in targets) or "(none)"

    @tool(
        "mention_teammate",
        f"Get the exact Slack mention string for a teammate you are handing work to. "
        f"`name` is one of: {names}. Call this instead of writing '<@...>' yourself.",
        {"name": str},
    )
    async def _mention(args: dict) -> dict:
        wanted = str(args.get("name", "")).strip().lstrip("@").casefold()
        for display, user_id in targets.items():
            if display.casefold() == wanted:
                if not user_id:
                    return result(1, f"{display}'s Slack id isn't available right now")
                return result(0, f"<@{user_id}>")
        return result(1, f"no teammate named '{args.get('name', '')}' — choose one of: {names}")

    return _mention


def make_handoff_tool(targets: dict[str, str], queue):
    """`hand_off(name, request)`: pass this thread's work to a teammate, inside the harness.

    `targets` maps display name -> persona key and is fixed at construction: the main
    persona gets its roster, every other persona gets the main persona only (single hub,
    as before). `queue(to_key, request)` is bound by the caller to ONE thread and ONE
    sender, so no argument can aim the handoff at another thread or forge who sent it.

    This replaces handing off by @-mention. A persona that posts through the main bot
    under its own name has no Slack user to mention, and a mention it typed would come
    back authored by the main bot, so the router would read it as main talking to itself.
    The handoff is queued, not run: the teammate's turn starts after this turn ends, and
    the queue lives in state.json, so a restart in between re-runs it rather than losing it."""
    names = ", ".join(f"'{n}'" for n in targets) or "(none)"

    @tool(
        "hand_off",
        "Hand this thread's work to a teammate. They take it after your turn ends, in this "
        f"same thread. `name` is one of: {names}. `request` is what you need from them, "
        "written for them to act on.",
        {"name": str, "request": str},
    )
    async def _hand_off(args: dict) -> dict:
        wanted = str(args.get("name", "")).strip().lstrip("@").casefold()
        request = str(args.get("request", "")).strip()
        if not request:
            return result(1, "`request` is empty — say what you need from them")
        for display, key in targets.items():
            if display.casefold() == wanted:
                queue(key, request)
                return result(0, f"Queued for {display}. They start after your turn ends; "
                                 "don't also mention them.")
        return result(1, f"no teammate named '{args.get('name', '')}' — choose one of: {names}")

    return _hand_off


# --- scoped filesystem tools -------------------------------------------------
#
# The scoped replacements for the built-ins Write/Read/Glob inside a harness's own
# tree. They exist because spawn.py hands agents an EMPTY base tool set: there is no
# Write to fence off with a PreToolUse hook, so the capability has to be handed back
# deliberately, with its roots bound at construction.
#
# This is the difference between a veto and an absence. A hook denial is something
# the model sees, reasons about, and routes around; a root it was never given is
# nowhere it can go. `write_roots` comes from the agent's own `Agent.write_scope`,
# and no argument moves it.
#
# Without these tools, every `writes_finding=True` agent can exit successfully while
# writing nothing and remain classified BLOCKED. Keep one implementation in the kit.
#
# Deliberately NOT implemented: no delete, no move, no append, no mkdir outside a
# write root, no glob over the whole tree. An agent needing one of those should be
# given a tool that does exactly that, and nothing else.

MAX_FINDING_BYTES = 64_000  # a finding is a note, not a data dump
MAX_LIST = 200

# A write root containing this token is resolved against the task the run belongs to.
# `tasks/{task}/work` is where a run's artifacts go, and the id comes from the parent
# process (context.current_task), never from the agent — a task id the agent could pass
# would let it write into another task's directory, which is the "a tool's own parameter
# reopens its boundary" failure in docs/philosophy.md.
TASK_TOKEN = "{task}"


def _expand(roots: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve TASK_TOKEN. Outside a run there is no current task, so a task-scoped root
    resolves to nothing at all — the safe direction: no place to write, rather than the
    whole tasks/ tree."""
    task = current_task()
    out = []
    for root in roots:
        if TASK_TOKEN not in root:
            out.append(root)
        elif task:
            out.append(root.replace(TASK_TOKEN, task))
    return tuple(out)


def _roots_msg(roots: tuple[str, ...]) -> str:
    return ", ".join(f"`{r}/`" for r in _expand(roots)) or "(none right now)"


def _roots_desc(roots: tuple[str, ...]) -> str:
    """The roots as a tool DESCRIPTION states them. A description is built once, when
    the tool is made, which is outside any run — so `_roots_msg` there resolved every
    task-scoped root to nothing and froze "(none right now)" into the prompt. The agent
    was then told it could write nowhere and had to guess its own task directory. Name
    the token instead and let the call fill it in (`_fill_task`)."""
    listed = ", ".join(f"`{r}/`" for r in roots) or "(none)"
    if any(TASK_TOKEN in r for r in roots):
        listed += (
            f". Write `{TASK_TOKEN}` literally where your task id goes "
            f"(e.g. `tasks/{TASK_TOKEN}/work/{TS_TOKEN}-note.md`); it is replaced with the "
            "task this run belongs to"
        )
    return listed


def _fill_task(rel: str) -> str:
    """Substitute TASK_TOKEN in a path the agent passed. The id comes from the run's
    context, never from the agent, so this names the agent's own task and nothing else.
    Outside a run the token stays put and the path matches no root."""
    task = current_task()
    return rel.replace(TASK_TOKEN, task) if task else rel


def _resolve_in(home: Path, rel: str, roots: tuple[str, ...]) -> Path | None:
    """Resolve `rel` (harness-relative, or absolute-but-inside) and return it only if
    it lands inside one of `roots`. None means 'no such place' — callers turn that
    into an error naming the roots, so the agent learns its lane from the tool's own
    reply rather than from a hook denial. resolve() before the containment check, so
    a symlink cannot be written through."""
    p = Path(rel)
    p = (home / p) if not p.is_absolute() else p
    p = p.resolve(strict=False)
    for root in _expand(roots):
        r = (home / root).resolve(strict=False)
        if p == r or p.is_relative_to(r):
            return p
    return None


def make_write_finding(home: Path, write_roots: tuple[str, ...]):
    """A write tool that can only ever create files under `write_roots`.

    Parent directories are created, but only inside a root — so
    `tasks/<id>/work/notes/x.md` works without a separate mkdir tool, while
    `../../etc/x` has no reachable parent to create."""

    @tool(
        "write_finding",
        "Write a markdown note to a file. This is the ONLY way you can write "
        f"anything — you have no Write/Edit tool. Allowed paths: {_roots_desc(write_roots)}. "
        f"`path` is relative to the harness root. You have no clock, so never type a date "
        f"or a timestamp: write '{TS_TOKEN}' where one belongs (e.g. 'logs/agent/{TS_TOKEN}.md', "
        f"'findings/monitor/{TS_TOKEN}-http-5xx.md') and the real UTC time is substituted. "
        "Overwrites the file if it already exists.",
        {"path": str, "content": str},
    )
    async def write_finding(args: dict) -> dict:
        raw = _fill_task(str(args["path"]))
        # Containment BEFORE the timestamp scan. `{ts}` only ever names a file, never
        # which root the path lands in, so the order is safe — and it decides which
        # refusal the agent is told. Scanning first meant a path aimed at another task's
        # directory came back as "you invented a date" (a task id is YYYYMMDD-xxxx),
        # which is true of the id the HARNESS generated and says nothing about the real
        # problem: that directory is not yours.
        if _resolve_in(home, raw, write_roots) is None:
            return result(1, f"'{raw}' is outside your write scope ({_roots_msg(write_roots)})")
        rel, err = stamp_path(raw, _expand(write_roots))
        if err:
            return result(1, err)
        target = _resolve_in(home, rel, write_roots)
        if target is None:
            return result(1, f"'{rel}' is outside your write scope ({_roots_msg(write_roots)})")
        if target.is_dir():
            return result(1, f"'{rel}' is a directory, not a file")
        content = str(args["content"])
        if len(content.encode()) > MAX_FINDING_BYTES:
            return result(1, f"content is over {MAX_FINDING_BYTES} bytes — summarize instead of dumping")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return result(0, f"wrote {len(content)} chars to {target.relative_to(home.resolve())}")

    return write_finding


def make_read_finding(home: Path, read_roots: tuple[str, ...]):
    """A read tool bound to `read_roots` — typically another agent's findings dir.
    Output goes through `truncate`, so a runaway file can't blow the agent's context."""

    @tool(
        "read_finding",
        "Read a markdown finding file written by another agent. This is the ONLY "
        f"way you can read files — you have no Read tool. Allowed paths: {_roots_desc(read_roots)}. "
        "`path` is relative to the harness root (e.g. 'tasks/20261010-8e0b/work/report.md').",
        {"path": str},
    )
    async def read_finding(args: dict) -> dict:
        rel = _fill_task(str(args["path"]))
        target = _resolve_in(home, rel, read_roots)
        if target is None:
            return result(1, f"'{rel}' is outside your read scope ({_roots_msg(read_roots)})")
        if not target.is_file():
            return result(1, f"'{rel}' does not exist — re-check the path you were given")
        return result(0, truncate(target.read_text(errors="replace", encoding="utf-8")))

    return read_finding


def make_list_findings(home: Path, read_roots: tuple[str, ...]):
    """Directory listing bound to `read_roots` — the scoped stand-in for Glob.

    Recursive and newest first: reports live in `tasks/<id>/work/`, so a listing of only
    the directory's own files showed `tasks/` as empty and a persona told the user there
    was no report when one had landed that morning."""

    @tool(
        "list_findings",
        "List the markdown files under one of your readable directories, subdirectories "
        "included, newest first, as paths relative to it. You have no Glob tool. Allowed "
        f"directories: {_roots_desc(read_roots)}.",
        {"directory": str},
    )
    async def list_findings(args: dict) -> dict:
        rel = _fill_task(str(args["directory"]))
        target = _resolve_in(home, rel, read_roots)
        if target is None:
            return result(1, f"'{rel}' is outside your read scope ({_roots_msg(read_roots)})")
        if not target.is_dir():
            return result(1, f"'{rel}' is not a directory")
        files = sorted(target.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        names = [p.relative_to(target).as_posix() for p in files]
        if not names:
            return result(0, f"(no .md files under {rel})")
        listing = "\n".join(names[:MAX_LIST])
        if len(names) > MAX_LIST:
            listing += f"\n... ({len(names) - MAX_LIST} more)"
        return result(0, listing)

    return list_findings


def make_write_ask(home: Path, *, thread_ts: str = ""):
    """Record something the harness was asked for and cannot do, as a task.

    This was `backlog/<slug>.md`, and the move is mostly a rename: that directory was
    already a task table — a slug for an id, a status for a lifecycle,
    a source and a thread link. What it never had was an executor, which is exactly
    right, and is why these tasks are created `waiting` with no agent. The scheduler
    only ever claims `queued`, so nothing will try to run one.

    The agent keeps the vocabulary it had (open/done/dropped) because that is what the
    words mean to whoever is asking; the mapping to task statuses is in code here rather
    than in the tool's description.

    `created`/`updated` are stamped by the record layer, for the same reason as before:
    an agent has no clock, so a date it writes is a date it invented.
    """

    @tool(
        "write_ask",
        "Record a request you cannot carry out. Call this whenever you are asked for "
        "something that is not in your tool list — you have no Write, no Edit, no Bash, "
        "so almost every '이거 구현해줘' is outside your hands, and you know that before "
        "you try. Say so plainly, ask whatever you still need to ask in the thread, then "
        "write the answer down here. Re-invoke with the same `slug` to update it. You have no "
        "clock — never write a date anywhere, not in the body either. "
        "slug: lowercase-kebab, how you and other items refer to it "
        "(e.g. 'cron-via-slack'). "
        "status: open (asked for, not built) · done (built, or answered elsewhere) · "
        "dropped (decided against). "
        "body: markdown. Lead with a `# title`, quote the ask verbatim, then what the "
        "conversation established. Link related items with [[their-slug]].",
        {"slug": str, "status": str, "body": str},
    )
    async def write_ask(args: dict) -> dict:
        # Local import: world.process depends on world.toolkit (see its package
        # docstring), so the reverse cannot happen at module scope.
        from .process import task as tasks

        slug, status, body = str(args["slug"]), str(args["status"]), str(args["body"]).strip()
        if not tasks.SLUG_RE.match(slug):
            return result(1, f"'{slug}' is not a usable slug — lowercase letters, digits "
                             "and hyphens only (e.g. 'cron-via-slack'), 2-62 chars")
        if status not in ("open", "done", "dropped"):
            return result(1, f"status must be one of open, done, dropped — got '{status}'")
        if not body:
            return result(1, "an ask with no body records nothing — write it down")

        record = tasks.find_by_slug(home, slug, type=tasks.ASK_TYPE)
        try:
            if record is None:
                record = tasks.create(
                    home, type=tasks.ASK_TYPE,
                    title=body.splitlines()[0].lstrip("# ").strip()[:120] or slug,
                    slug=slug, body=body,
                    origin=tasks.Origin(kind="conversation", by=thread_ts or ""),
                )
            else:
                record.body = body
                tasks.save(home, record)
            if thread_ts and not record.thread:
                record.thread = thread_ts
                tasks.save(home, record)
            if status == "dropped":
                tasks.transition(home, record, "dropped", reason="decided against")
            elif status == "done":
                # Not assignable — recorded as a human resolution, then verified. Same
                # rule as every other task: the predicate writes done, nobody else.
                tasks.record_resolution(home, record, by=thread_ts or "conversation",
                                        note="agent marked the ask handled")
                tasks.verify(home, record, tasks.ask_type(home))
            elif record.status != "waiting":
                tasks.transition(home, record, "waiting", reason=f"ask is {status}")
        except tasks.TaskError as exc:
            return result(1, str(exc))
        return result(0, f"recorded ask {record.id} ({slug}) as {status}")

    return write_ask


# --- scoped repository readers ------------------------------------------------
#
# spawn.py hands agents an empty base tool set: no Read, no Grep, no Glob. Any agent
# that must look at a tree needs those capabilities handed back with a root bound at
# construction. Once spawn.py stops handing out built-ins, repository readers must be
# granted explicitly or agents cannot inspect the repository they audit.
#
# The root is the whole boundary. A path outside it does not resolve, so there is no
# rule to talk the model out of.

GREP_MAX_MATCHES = 100
GLOB_MAX_HITS = 300
MAX_READ_BYTES = 512_000


def _within(root: Path, path_str: str) -> Path | None:
    """None if `path_str` does not land inside `root`. resolve() before the check, so
    a `..` segment or a symlink cannot walk out of it."""
    p = Path(path_str)
    if not p.is_absolute():
        p = root / p
    p = p.resolve(strict=False)
    return p if (p == root or p.is_relative_to(root)) else None


def make_repo_readers(root: Path, what: str = "the repository") -> list:
    """read / grep / glob, all bound to `root`. Returns the three tools as a list.

    `what` names the tree in each description, because the description is the only
    thing the model reads before choosing a tool: "the billing-api repository" tells
    it far more than "the repository"."""
    root = root.resolve()

    def _reject(path_str: str) -> dict:
        return result(1, f"'{path_str}' is outside {what} ({root})")

    @tool(
        "repo_read",
        f"Read a file in {what}. This is the ONLY way you can read a file — you have "
        "no Read tool. `path` is relative to the repository root. Long files are "
        "truncated; grep first if you are looking for something specific.",
        {"path": str},
    )
    async def repo_read(args: dict) -> dict:
        target = _within(root, str(args["path"]))
        if target is None:
            return _reject(str(args["path"]))
        if not target.is_file():
            return result(1, f"'{args['path']}' does not exist in {what}")
        if target.stat().st_size > MAX_READ_BYTES:
            return result(1, f"'{args['path']}' is over {MAX_READ_BYTES} bytes — grep it instead")
        return result(0, truncate(target.read_text(errors="replace", encoding="utf-8")))

    @tool(
        "repo_grep",
        f"Search {what} for a regex (ripgrep). This is the ONLY way you can search — "
        "you have no Grep tool. `path` is a directory relative to the repository "
        "root ('.' for all of it). Returns file:line:text.",
        {"pattern": str, "path": str},
    )
    async def repo_grep(args: dict) -> dict:
        base = _within(root, str(args["path"]))
        if base is None:
            return _reject(str(args["path"]))
        if not base.is_dir():
            return result(1, f"'{args['path']}' is not a directory")
        # `--` ends option parsing: a pattern starting with '-' is a pattern, not a flag.
        rc, out = await run_cmd(
            "rg", "--line-number", "--no-heading", "--color", "never",
            "--max-count", str(GREP_MAX_MATCHES), "--", str(args["pattern"]), ".",
            cwd=str(base),
        )
        if rc == 1 and not out:
            return result(0, "(no matches)")  # rg exits 1 on no-match; not an error here
        return result(rc, out)

    @tool(
        "repo_glob",
        f"List files in {what} matching a glob. This is the ONLY way you can list "
        "files — you have no Glob tool. `pattern` is relative to the repository root, "
        "e.g. 'docs/**/*.md'.",
        {"pattern": str},
    )
    async def repo_glob(args: dict) -> dict:
        hits = sorted(
            str(p.relative_to(root))
            for p in root.glob(str(args["pattern"]))
            if p.is_file() and _within(root, str(p)) is not None
        )
        if not hits:
            return result(0, "(no matches)")
        listing = "\n".join(hits[:GLOB_MAX_HITS])
        if len(hits) > GLOB_MAX_HITS:
            listing += f"\n... ({len(hits) - GLOB_MAX_HITS} more)"
        return result(0, listing)

    return [repo_read, repo_grep, repo_glob]
