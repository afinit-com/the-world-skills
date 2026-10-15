"""tasks/<id>/ — one directory per task, and the only place `done` can be written.

The durable record this kit needed and did not have. Five stores used to hold the pieces of a
single piece of work under five different keys (state.json's `threads` by thread_ts,
ledger.jsonl by nothing, run.jsonl by agent name, backlog/ by slug, findings/ by agent
name), so nothing could join "what was asked" to "what actually happened". A task_id
joins them.

Layout, and why it is four things rather than one file:

    tasks/<id>-<slug>/
        task.md        what is CURRENTLY true. Rewritten in place.
        events.jsonl   what HAPPENED. Append-only, never rewritten.
        source/        the original, byte-for-byte as the collector received it.
        work/          run artifacts (what logs/ and findings/ used to hold).

The task.md / events.jsonl split is the one ledger.py already established for state vs
decisions, applied one level down: losing task.md costs a tick, losing events.jsonl
costs the only record of what was approved, retried, or done by hand.

The load-bearing rule here: **`done` is not a status anyone may assign.** `transition()`
refuses it outright. The only path is `verify()`, which runs the task type's completion
predicate and writes `done` only when the predicate returns one. That is what makes
"PR opened" a milestone instead of a finish line, and it is an executable boundary
rather than a sentence in a prompt (see docs/philosophy.md).

Frontmatter is flat dotted keys, not nested YAML: types.py records the no-YAML-layer
decision for this kit, and a nested structure hand-parsed without a parser is a trap.
Lists are JSON arrays so a resource URI containing a comma cannot split a field.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import shutil
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Sequence

# queued    registered, waiting for a slot / its dependencies / a free lock
# running   a slot is held and the lease is live
# waiting   parked on something outside the harness: approval, a human answer
# verifying the work is done being attempted; the completion predicate decides
# done      the completion predicate said so. Nothing else can write this.
# blocked   retry budget spent, or structurally stuck. A human is needed.
# dropped   decided against. The file stays — "what were we already told no to?"
STATUSES = ("queued", "running", "waiting", "verifying", "done", "blocked", "dropped")

TERMINAL = ("done", "dropped")

ORIGIN_KINDS = ("collector", "schedule", "conversation", "operator")
APPROVAL_STATES = ("none", "requested", "granted", "stale", "denied")
VERDICT_STATES = ("done", "pending", "failed")

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?$")
ID_RE = re.compile(r"^\d{8}-[0-9a-f]{4}$")

MAX_BODY_BYTES = 64_000
# The task type every plain agent run gets when its Agent declares none.
AGENT_RUN_TYPE = "agent-run"
# Something a human asked for that the harness could not do. No agent, never queued —
# it sits in `waiting` until a person resolves or drops it. This is what backlog/ was.
ASK_TYPE = "ask"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class TaskError(ValueError):
    """A value the caller may not write. Carries the message a human or agent reads."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(TS_FMT)


def to_epoch(stamp: str) -> float:
    """`2026-09-16T04:12:33Z` → epoch seconds. 0.0 for an empty or unparseable stamp,
    which reads as "long expired" everywhere a lease is compared."""
    if not stamp:
        return 0.0
    try:
        parsed = datetime.datetime.strptime(stamp, TS_FMT)
    except ValueError:
        return 0.0
    return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()


def from_epoch(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(
        seconds, tz=datetime.timezone.utc
    ).strftime(TS_FMT)


def slugify(text: str) -> str:
    """Best-effort kebab slug for a directory name. The id stays the key, so an
    unusable input costs readability in `ls`, never a lookup."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:62].strip("-")
    return cleaned if SLUG_RE.match(cleaned) else ""


def new_task_id(now: datetime.datetime | None = None) -> str:
    """`20260916-a3f2`. Sortable, safe in a filename, short enough to say out loud in
    Slack. Not a counter: a counter needs cross-process coordination the moment
    execution is split, and date+random needs none."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{now.strftime('%Y%m%d')}-{secrets.token_hex(2)}"


def _claim_task_dir(home: Path, now: datetime.datetime | None = None) -> str:
    """A fresh id whose directory this call created. The suffix is 16 random bits, so two
    tasks of one day share an id often enough to matter (about 7% of days with 100
    tasks), and creating the directory with exist_ok wrote the second task over the
    first: one of them was gone without a word. mkdir without exist_ok is atomic, so
    another process filing a task at the same moment cannot take the same one either.

    On Windows a directory removed a moment ago (a create that failed and cleaned up)
    stays "delete pending" for a while, and mkdir on it is a PermissionError, so that
    is taken too. Only once in a row, though: a second id refused the same way says
    the tasks directory itself is not writable, and that is the error to raise."""
    root = tasks_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    denied = False
    for _ in range(1000):
        task_id = new_task_id(now)
        try:
            (root / task_id).mkdir()
        except FileExistsError:
            denied = False
            continue
        except PermissionError:
            if denied:
                raise
            denied = True
            continue
        return task_id
    raise TaskError(f"no free task id left for {task_id[:8]} after 1000 tries")


# --- the record ---------------------------------------------------------------


@dataclass
class Origin:
    """Where this task came from. Stamped by the entry path in code, never by a model.

    `kind="operator"` with `replay=True` is a human pushing a past notice back through
    the pipeline. Slack renders that as "운영자 재처리 요청" on the first line — the
    label is data, not a rendering rule someone has to remember to apply.
    """

    kind: str = "conversation"
    at: str = ""
    by: str = ""
    replay: bool = False
    source_hash: str = ""   # dedup key: sha256 of the preserved source/ bytes

    def validate(self) -> None:
        if self.kind not in ORIGIN_KINDS:
            raise TaskError(f"origin.kind must be one of {', '.join(ORIGIN_KINDS)} — got '{self.kind}'")


@dataclass
class Approval:
    """A grant bound to a specific change, not to a conversation.

    The defect this closes: toolkit.make_approval_gated binds a grant to a THREAD and
    spends it on one successful call, so a grant survives the files underneath it
    changing. `scope_hash` is recomputed at spend time; a mismatch means the approver
    said yes to something that no longer exists, and the grant goes `stale`.
    """

    state: str = "none"
    by: str = ""
    at: str = ""
    scope_hash: str = ""

    def validate(self) -> None:
        if self.state not in APPROVAL_STATES:
            raise TaskError(
                f"approval.state must be one of {', '.join(APPROVAL_STATES)} — got '{self.state}'"
            )


@dataclass
class Task:
    id: str
    type: str
    title: str
    status: str = "queued"
    policy: str = ""
    # Which registered agent runs this. The scheduler resolves it by name rather than
    # holding the Agent object, so a task that outlives a restart still knows its runner.
    agent: str = ""
    origin: Origin = field(default_factory=Origin)
    approval: Approval = field(default_factory=Approval)
    # Lock units, and the whole basis for parallelism: `writes` is exclusive, `reads` is
    # shared. Two tasks whose writes intersect serialize; two that touch nothing in
    # common run at the same time. Resource URIs, so intersection is a string compare —
    # they have to be spelled consistently.
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    attempts: int = 0
    retry_budget: int = 3
    # Set once when a slot is claimed (claim time + the type's timeout), not renewed on
    # a heartbeat: rewriting a committed file every few seconds to say "still alive" is
    # churn. It is both the run timeout and the crash-recovery marker — on restart, a
    # `running` task whose lease has passed is requeued.
    lease_until: str = ""
    # The Slack thread this task is reviewed in. One task, one thread: replies land on
    # the work they are about, and the record points back at the conversation.
    thread: str = ""
    slug: str = ""
    created: str = ""
    updated: str = ""
    body: str = ""

    def validate(self) -> None:
        if not ID_RE.match(self.id or ""):
            raise TaskError(f"'{self.id}' is not a task id — expected YYYYMMDD-xxxx (e.g. 20260916-a3f2)")
        if self.slug and not SLUG_RE.match(self.slug):
            raise TaskError(f"'{self.slug}' is not a usable slug — lowercase letters, digits, hyphens")
        if self.status not in STATUSES:
            raise TaskError(f"status must be one of {', '.join(STATUSES)} — got '{self.status}'")
        if not (self.title or "").strip():
            raise TaskError("a task with no title cannot be reviewed by anyone — give it one")
        if len(self.body.encode()) > MAX_BODY_BYTES:
            raise TaskError(f"body is over {MAX_BODY_BYTES} bytes — summarize instead of dumping")
        overlap = set(self.reads) & set(self.writes)
        if overlap:
            raise TaskError(
                f"{', '.join(sorted(overlap))} is in both reads and writes — a writer already "
                "holds it exclusively, so listing it as a read grants nothing and misreports intent"
            )
        self.origin.validate()
        self.approval.validate()

    @property
    def dirname(self) -> str:
        """The id alone. The slug lives in frontmatter and in the Slack title, not in
        the path: a directory named `<id>-<slug>` meant every lookup had to glob for it,
        and it made `tasks/{task}/work` — the per-run write root, which both the write
        tool and the guard hook expand from a bare id — unresolvable without that glob."""
        return self.id

    def lease_expired(self, now: float) -> bool:
        return to_epoch(self.lease_until) <= now

    def retries_left(self) -> int:
        return max(0, self.retry_budget - self.attempts)


@dataclass(frozen=True)
class Verdict:
    """The completion predicate's answer, and the only thing that can produce `done`.

    `evidence` is written verbatim into events.jsonl, because the single human-facing
    channel explains a task by quoting its own record rather than by narrating from a model.
    A verdict with no evidence is a verdict no one can check, so one is required.
    """

    state: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in VERDICT_STATES:
            raise TaskError(f"verdict state must be one of {', '.join(VERDICT_STATES)}")
        if not self.evidence:
            raise TaskError("a verdict with no evidence cannot be checked by a human — say why")

    @classmethod
    def done(cls, *evidence: str) -> "Verdict":
        return cls("done", tuple(evidence))

    @classmethod
    def pending(cls, *evidence: str) -> "Verdict":
        return cls("pending", tuple(evidence))

    @classmethod
    def failed(cls, *evidence: str) -> "Verdict":
        return cls("failed", tuple(evidence))


@dataclass(frozen=True)
class TaskType:
    """What "finished" means for one kind of work, as code.

    `milestones` are recorded and rendered but cannot move `status`. That is the whole
    point: for a schema migration, "PR opened" is a milestone and the finish line is a
    predicate that actually checks the migrated schema and that the verification
    resources were cleaned up.

    `completion=None` falls back to the contract the kit already enforces in
    spawn.enforce_completion — an artifact landed under the task's work/. Exit-0 was
    never completion; this generalizes that check rather than replacing it.
    """

    name: str
    completion: Callable[[Task], Verdict] | None = None
    milestones: tuple[str, ...] = ()
    retry_budget: int = 3
    timeout_s: int = 1800


# --- frontmatter --------------------------------------------------------------
#
# Flat dotted keys. Scalars bare, everything else JSON, so parsing is per-field and
# typed rather than a guess at what a value looks like.

_ORDER = (
    "id", "type", "title", "status", "policy", "agent",
    "origin.kind", "origin.at", "origin.by", "origin.replay", "origin.source_hash",
    "approval.state", "approval.by", "approval.at", "approval.scope_hash",
    "reads", "writes", "depends_on",
    "attempts", "retry_budget", "lease_until",
    "thread", "slug", "created", "updated",
)
_LISTS = ("reads", "writes", "depends_on")
_INTS = ("attempts", "retry_budget")
_BOOLS = ("origin.replay",)


def _split(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    _, _, rest = text.partition("---\n")
    raw, sep, body = rest.partition("\n---\n")
    if not sep:
        return {}, text
    meta = {}
    for line in raw.splitlines():
        key, colon, value = line.partition(":")
        if colon:
            meta[key.strip()] = value.strip()
    return meta, body.lstrip("\n")


def _header_value(value: str) -> str:
    """A header value on one line. The header is one key per line and _split keeps a
    repeated key's last value, so a value with a line break in it wrote keys of its own:
    a title of "Daily\nid: …" came back with another task's id. Every boundary
    str.splitlines() honours, since that is what _split reads the header with."""
    return " ".join(value.splitlines())


def _render(task: Task) -> str:
    flat = {
        "id": task.id, "type": task.type, "title": task.title, "status": task.status,
        "policy": task.policy, "agent": task.agent,
        "origin.kind": task.origin.kind, "origin.at": task.origin.at,
        "origin.by": task.origin.by,
        "origin.replay": "true" if task.origin.replay else "false",
        "origin.source_hash": task.origin.source_hash,
        "approval.state": task.approval.state, "approval.by": task.approval.by,
        "approval.at": task.approval.at, "approval.scope_hash": task.approval.scope_hash,
        "reads": json.dumps(list(task.reads), ensure_ascii=False),
        "writes": json.dumps(list(task.writes), ensure_ascii=False),
        "depends_on": json.dumps(list(task.depends_on), ensure_ascii=False),
        "attempts": str(task.attempts), "retry_budget": str(task.retry_budget),
        "lease_until": task.lease_until, "thread": task.thread, "slug": task.slug,
        "created": task.created, "updated": task.updated,
    }
    lines = [f"{key}: {_header_value(flat[key])}" for key in _ORDER]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + task.body.strip() + "\n"


def _parse(meta: dict, body: str) -> Task:
    def lst(key: str) -> tuple[str, ...]:
        try:
            loaded = json.loads(meta.get(key) or "[]")
        except json.JSONDecodeError:
            return ()
        return tuple(str(item) for item in loaded) if isinstance(loaded, list) else ()

    def num(key: str, default: int) -> int:
        try:
            return int(meta.get(key, default))
        except (TypeError, ValueError):
            return default

    return Task(
        id=meta.get("id", ""),
        type=meta.get("type", ""),
        title=meta.get("title", ""),
        status=meta.get("status", "queued"),
        policy=meta.get("policy", ""),
        agent=meta.get("agent", ""),
        origin=Origin(
            kind=meta.get("origin.kind", "conversation"),
            at=meta.get("origin.at", ""),
            by=meta.get("origin.by", ""),
            replay=meta.get("origin.replay") == "true",
            source_hash=meta.get("origin.source_hash", ""),
        ),
        approval=Approval(
            state=meta.get("approval.state", "none"),
            by=meta.get("approval.by", ""),
            at=meta.get("approval.at", ""),
            scope_hash=meta.get("approval.scope_hash", ""),
        ),
        reads=lst("reads"), writes=lst("writes"), depends_on=lst("depends_on"),
        attempts=num("attempts", 0), retry_budget=num("retry_budget", 3),
        lease_until=meta.get("lease_until", ""),
        thread=meta.get("thread", ""),
        slug=meta.get("slug", ""),
        created=meta.get("created", ""), updated=meta.get("updated", ""),
        body=body,
    )


# --- directory IO -------------------------------------------------------------


def tasks_dir(home: Path) -> Path:
    return home / "tasks"


def task_dir(home: Path, task_id: str) -> Path | None:
    """`tasks/<id>/`, or None when it does not exist. Every lookup goes through here so
    the layout stays one decision in one place."""
    if not ID_RE.match(task_id or ""):
        return None
    directory = tasks_dir(home) / task_id
    return directory if directory.is_dir() else None


def task_work_dir(home: Path, task_id: str) -> Path | None:
    """`tasks/<id>-<slug>/work` — where a run's artifacts land."""
    directory = task_dir(home, task_id)
    return directory / "work" if directory else None


def events_path(home: Path, task_id: str) -> Path | None:
    directory = task_dir(home, task_id)
    return directory / "events.jsonl" if directory else None


def append_event(home: Path, task_id: str, kind: str, **fields) -> None:
    """One line per thing that happened. Append-only; nothing in this module rewrites it.

    Failures, retries and manual interventions are events like any other — a task whose
    history only records successes is a task no one can debug.
    """
    directory = task_dir(home, task_id)
    if directory is None:
        raise TaskError(f"no task directory for '{task_id}' — create the task first")
    entry = {"at": utc_now(), "kind": kind, **fields}
    with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_events(home: Path, task_id: str) -> list[dict]:
    path = events_path(home, task_id)
    if path is None or not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace", encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def create(
    home: Path,
    *,
    type: str,  # noqa: A002 — matches the frontmatter key; renaming it here desyncs the two
    title: str,
    slug: str = "",
    origin: Origin | None = None,
    task_type: TaskType | None = None,
    source: bytes | str | None = None,
    source_name: str = "source.txt",
    body: str = "",
    now: datetime.datetime | None = None,
    **fields,
) -> Task:
    """Create `tasks/<id>-<slug>/`, preserve the source, and open the event log.

    `source` is written byte-for-byte and hashed into `origin.source_hash`. That hash is
    the collector's dedup key — `find_by_source_hash` answers "have we already taken
    this in?" without re-reading every task.
    """
    stamp = utc_now()
    origin = origin or Origin(kind="conversation", at=stamp)
    if not origin.at:
        origin = replace(origin, at=stamp)

    raw = source.encode() if isinstance(source, str) else source
    if raw is not None and not origin.source_hash:
        origin = replace(origin, source_hash=f"sha256:{sha256(raw).hexdigest()}")

    task_id = _claim_task_dir(home, now)
    try:
        return _fill_task_dir(home, task_id, type=type, title=title, slug=slug, origin=origin,
                              task_type=task_type, stamp=stamp, body=body, raw=raw,
                              source_name=source_name, fields=fields)
    except BaseException:
        # The directory is this call's own (it claimed it), so a task that could not be
        # written in full leaves nothing behind: no half-written record to list.
        shutil.rmtree(tasks_dir(home) / task_id, ignore_errors=True)
        raise


def _fill_task_dir(home: Path, task_id: str, *, type: str, title: str, slug: str,  # noqa: A002
                   origin: "Origin", task_type, stamp: str, body: str, raw: bytes | None,
                   source_name: str, fields: dict) -> "Task":
    """Write a claimed task directory: the record, its source, its first event."""
    task = Task(
        id=task_id,
        type=type,
        # As task.md will hold it, so the record handed back reads the same as one re-read.
        title=_header_value(title),
        slug=slug,
        origin=origin,
        # An explicit retry_budget wins over the type's default: passing both used to
        # be a TypeError rather than an override, which is a poor way to learn that
        # the caller and the type disagree.
        retry_budget=fields.pop("retry_budget", task_type.retry_budget if task_type else 3),
        created=stamp,
        updated=stamp,
        body=body,
        **fields,
    )
    task.validate()

    directory = tasks_dir(home) / task.dirname
    (directory / "work").mkdir(exist_ok=True)
    if raw is not None:
        (directory / "source").mkdir(exist_ok=True)
        (directory / "source" / source_name).write_bytes(raw)
    (directory / "task.md").write_text(_render(task), encoding="utf-8")
    append_event(
        home, task.id, "created",
        origin=origin.kind, replay=origin.replay, by=origin.by, title=title,
    )
    return task


def read(home: Path, task_id: str) -> Task | None:
    directory = task_dir(home, task_id)
    if directory is None or not (directory / "task.md").is_file():
        return None
    meta, body = _split((directory / "task.md").read_text(errors="replace", encoding="utf-8"))
    return _parse(meta, body)


def save(home: Path, task: Task) -> Path:
    """Rewrite task.md. `created` is never touched; `updated` always is."""
    task.validate()
    directory = task_dir(home, task.id)
    if directory is None:
        raise TaskError(f"no task directory for '{task.id}' — use create()")
    task.updated = utc_now()
    path = directory / "task.md"
    path.write_text(_render(task), encoding="utf-8")
    return path


def list_tasks(home: Path, *, status: str | Iterable[str] | None = None) -> list[Task]:
    """Oldest first. Sorted by `created`, NOT by directory name: an id carries a random
    suffix so the day's tasks would otherwise come back in an arbitrary order, and the
    scheduler reads this as its queue. The id breaks ties so the order is at least
    stable — two tasks filed in the same second are ordered arbitrarily but repeatably,
    which is as much fairness as a second-resolution stamp can offer."""
    root = tasks_dir(home)
    if not root.is_dir():
        return []
    wanted = {status} if isinstance(status, str) else (set(status) if status else None)
    out = []
    for directory in sorted(root.iterdir()):
        if not (directory / "task.md").is_file():
            continue
        meta, body = _split((directory / "task.md").read_text(errors="replace", encoding="utf-8"))
        task = _parse(meta, body)
        if wanted is None or task.status in wanted:
            out.append(task)
    out.sort(key=lambda t: (t.created, t.id))
    return out


def find_by_source_hash(home: Path, source_hash: str) -> Task | None:
    """The collector's dedup check. Empty hash matches nothing — an un-hashed task is
    not evidence that some other source was already taken in."""
    if not source_hash:
        return None
    return next(
        (t for t in list_tasks(home) if t.origin.source_hash == source_hash), None
    )


def find_by_slug(home: Path, slug: str, *, type: str | None = None) -> Task | None:  # noqa: A002
    """The newest unsettled task carrying this slug.

    An ask is re-invoked by slug, because the agent naming it has no way to know the id
    the harness generated. Settled tasks are skipped so re-using a slug after something
    was dropped opens a new ask instead of reanimating the old record.
    """
    if not slug:
        return None
    for task in reversed(list_tasks(home)):
        matches = task.slug == slug and task.status not in TERMINAL
        if matches and (type is None or task.type == type):
            return task
    return None


def record_resolution(home: Path, task: Task, *, by: str, note: str) -> None:
    """A human says this ask is handled — built, answered elsewhere, or moot.

    This is the evidence `ask_completion` reads. `done` stays something earned rather
    than assigned: for an agent run the predicate probes the artifact, and for an ask
    there is nothing to probe, so the earned thing is a recorded human decision. An
    event, not a field, because a field can be set and unset with nothing left behind.
    """
    append_event(home, task.id, "resolved", by=by, note=note[:500])


def ask_completion(home: Path):
    """Completion contract for ASK_TYPE: a human recorded a resolution."""

    def predicate(task: Task) -> Verdict:
        event = next(
            (e for e in reversed(read_events(home, task.id)) if e.get("kind") == "resolved"),
            None,
        )
        if event is None:
            return Verdict.pending("아직 사람이 해결로 표시하지 않음")
        note = str(event.get("note", "")).strip()
        return Verdict.done(
            f"{event.get('by', '?')}가 해결로 표시" + (f": {note[:200]}" if note else "")
        )

    return predicate


def ask_type(home: Path) -> TaskType:
    return TaskType(ASK_TYPE, completion=ask_completion(home))


OPEN_STATUSES = ("queued", "running", "waiting", "verifying")


def has_open_task(home: Path, agent: str) -> Task | None:
    """Is this agent's previous ask still unfinished?

    A poll check fires on a condition, and the condition stays true until the work lands.
    Inline execution used to hide that: the run finished before the next check looked.
    With a queue, the same check re-fires every tick while its task waits, so without
    this the queue fills with duplicates of one ask. `blocked` and the terminal statuses
    deliberately do NOT suppress — a task parked for a human must not silence the trigger
    that would notice the problem again.
    """
    if not agent:
        return None
    return next(
        (t for t in list_tasks(home, status=OPEN_STATUSES) if t.agent == agent), None
    )


# --- transitions --------------------------------------------------------------


def transition(home: Path, task: Task, to: str, *, reason: str = "", **fields) -> Task:
    """Move a task's status and record it. Refuses `done`.

    `done` is not a status a caller may assign — it is the output of a completion
    predicate, and `verify()` is the only writer. Without this refusal the whole
    type-specific completion contract is advisory: any caller that felt finished could
    say so, which is exactly the "a promise in a prompt is not enforcement" failure the
    kit exists to avoid. Callers that believe the work is finished call verify().
    """
    if to == "done":
        raise TaskError(
            "'done' cannot be assigned — call verify() with the task's TaskType so the "
            "completion predicate decides, and it writes done only on a done verdict"
        )
    if to not in STATUSES:
        raise TaskError(f"status must be one of {', '.join(STATUSES)} — got '{to}'")
    if task.status in TERMINAL:
        raise TaskError(f"{task.id} is already {task.status} — a settled task does not move again")

    was, task.status = task.status, to
    save(home, task)
    append_event(home, task.id, "transition", **{"from": was, "to": to, "reason": reason, **fields})
    return task


def record_milestone(home: Path, task: Task, name: str, task_type: TaskType, **fields) -> None:
    """A checkpoint worth showing a human that is explicitly NOT completion.

    Declared on the TaskType so a typo cannot invent one: an unknown milestone is a
    caller that thinks it reached a checkpoint the type does not have.
    """
    if name not in task_type.milestones:
        raise TaskError(
            f"'{name}' is not a milestone of task type '{task_type.name}' "
            f"({', '.join(task_type.milestones) or 'none declared'})"
        )
    append_event(home, task.id, "milestone", name=name, **fields)


def record_attempt(home: Path, task: Task, *, outcome: str, summary: str = "", ref: str = "",
                   artifacts: list[str] | None = None) -> Task:
    """Count one execution attempt and spend one unit of the retry budget.

    Exhausting the budget is what `blocked` means here — philosophy.md's "escalate to a
    human only after a fixed retry budget, never silently give up", with the budget in
    code instead of in the sentence.
    """
    task.attempts += 1
    save(home, task)
    append_event(
        home, task.id, "attempt",
        n=task.attempts, outcome=outcome, summary=summary[:500], ref=ref,
        artifacts=artifacts or [],
    )
    if outcome != "ok" and task.retries_left() == 0 and task.status not in TERMINAL:
        transition(home, task, "blocked", reason=f"retry budget spent ({task.retry_budget})")
    return task


def record_manual(home: Path, task: Task, *, by: str, summary: str) -> None:
    """A human did something outside the harness. That is an input to the next
    judgement, so it belongs in the record rather than in someone's memory."""
    append_event(home, task.id, "manual", by=by, summary=summary[:500])


def verify(home: Path, task: Task, task_type: TaskType) -> Verdict:
    """Run the completion contract and write `done` only if it says so.

    The fallback when a type declares no predicate is the check the kit already makes —
    an artifact landed under the task's work/ — so an unconfigured type is still held to
    "exit-0 is not completion" rather than to nothing.
    """
    if task_type.completion is not None:
        verdict = task_type.completion(task)
    else:
        directory = task_dir(home, task.id)
        produced = bool(directory and any((directory / "work").glob("*")))
        verdict = (
            Verdict.done(f"artifact present under tasks/{task.id}/work/")
            if produced
            else Verdict.pending(f"nothing written under tasks/{task.id}/work/")
        )

    append_event(
        home, task.id, "verify",
        verdict=verdict.state, evidence=list(verdict.evidence), type=task_type.name,
    )
    if verdict.state == "done" and task.status not in TERMINAL:
        was, task.status = task.status, "done"
        save(home, task)
        append_event(home, task.id, "transition", **{"from": was, "to": "done", "reason": "verified"})
    return verdict


# --- approval scope -----------------------------------------------------------


def compute_scope_hash(home: Path, files: Sequence[str], targets: Sequence[str]) -> str:
    """Fingerprint of exactly what an approval covers: the changed files' contents plus
    the resources that will be written.

    A missing file hashes as `<missing>` rather than being skipped, so deleting an
    approved file changes the fingerprint instead of leaving it untouched.
    """
    digest = sha256()
    for rel in sorted(set(files)):
        path = home / rel
        try:
            body = path.read_bytes()
        except OSError:
            body = b"<missing>"
        digest.update(rel.encode())
        digest.update(sha256(body).digest())
    for target in sorted(set(targets)):
        digest.update(target.encode())
    return f"sha256:{digest.hexdigest()}"


def grant_approval(home: Path, task: Task, *, by: str, scope_hash: str) -> Task:
    task.approval = Approval(state="granted", by=by, at=utc_now(), scope_hash=scope_hash)
    save(home, task)
    append_event(home, task.id, "approval", decision="granted", by=by, scope_hash=scope_hash)
    return task


def deny_approval(home: Path, task: Task, *, by: str, reason: str = "") -> Task:
    task.approval = Approval(state="denied", by=by, at=utc_now())
    save(home, task)
    append_event(home, task.id, "approval", decision="denied", by=by, reason=reason)
    return task


def check_approval(home: Path, task: Task, files: Sequence[str], targets: Sequence[str]) -> bool:
    """Is the grant still good for what is about to run? Called at spend time.

    Recomputes the fingerprint and, on a mismatch, marks the grant `stale` and returns
    False. A grant that survives its own subject changing is the gap this closes: the
    approver said yes to a specific diff against specific targets, and neither is what
    is in front of us any more. Whether stale becomes a re-ask or an automatic renewal
    is the task policy's call — this function only refuses to pretend.
    """
    if task.approval.state != "granted":
        return False
    current = compute_scope_hash(home, files, targets)
    if current == task.approval.scope_hash:
        return True
    task.approval = replace(task.approval, state="stale")
    save(home, task)
    append_event(
        home, task.id, "approval", decision="stale",
        was=task.approval.scope_hash, now=current,
        reason="the approved files or targets changed after the grant",
    )
    return False


# --- scheduling reads ---------------------------------------------------------


def conflicts(task: Task, running: Iterable[Task]) -> list[str]:
    """Which running tasks this one must wait behind. Writers are exclusive, readers
    are shared, so two tasks that touch nothing in common never appear here — that is
    where the parallelism comes from."""
    mine_w, mine_r = set(task.writes), set(task.reads)
    blocked = []
    for other in running:
        if other.id == task.id:
            continue
        theirs_w, theirs_r = set(other.writes), set(other.reads)
        if (mine_w & theirs_w) or (mine_w & theirs_r) or (mine_r & theirs_w):
            blocked.append(other.id)
    return blocked


def ready(home: Path, task: Task, running: Iterable[Task]) -> tuple[bool, str]:
    """(may this task start now, why not). Pure reads of the record — no judgement."""
    if task.status != "queued":
        return False, f"status is {task.status}"
    for dep in task.depends_on:
        upstream = read(home, dep)
        if upstream is None:
            return False, f"depends on {dep}, which does not exist"
        if upstream.status != "done":
            return False, f"depends on {dep} ({upstream.status})"
    clash = conflicts(task, running)
    if clash:
        return False, f"resource held by {', '.join(clash)}"
    return True, ""


def claim(home: Path, task: Task, task_type: TaskType, *, now: float) -> Task:
    """Take a slot: set the lease and move to running. The lease is the run timeout and
    the crash marker at once — see `reclaim_expired`."""
    task.lease_until = from_epoch(now + task_type.timeout_s)
    save(home, task)
    return transition(home, task, "running", reason="claimed", lease_until=task.lease_until)


def reclaim_expired(home: Path, now: float) -> list[Task]:
    """Requeue every `running` task whose lease has passed.

    Run at startup and periodically. In a single-process harness this is mostly the
    restart path: the process died holding tasks, and without this they stay `running`
    forever with nothing running them.
    """
    recovered = []
    for task in list_tasks(home, status="running"):
        if not task.lease_expired(now):
            continue
        append_event(home, task.id, "lease_expired", lease_until=task.lease_until)
        recovered.append(transition(home, task, "queued", reason="lease expired"))
    return recovered
