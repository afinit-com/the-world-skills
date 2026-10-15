"""Public configuration types — what a harness project's harness.py declares.

Pure-Python config: no YAML layer. The /the-world:agent and
/the-world:tool skills edit these declarations by appending to abilities/<name>/agent.py
modules and the harness.py register() call, so the shapes here are the codegen
contract — keep field names stable.
"""

from __future__ import annotations

import calendar
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Union
from zoneinfo import ZoneInfo


@dataclass
class Signal:
    """A poll check's 'yes, spawn now' answer.

    prompt   — trigger payload composed by the check (dynamic: s3 keys, issue #s…).
               spawn.py prepends the agent's standing prompt file and appends the
               recent-findings context snippet.
    on_done  — bookkeeping to run only after a *verified-complete* run. Never mark
               input seen or processed from exit code alone.
    title    — the headline a human sees on the task and its Slack/Discord card. Empty
               falls back to the prompt's first line, so a prompt that opens with an
               instruction to the model shows that instruction as the headline.
    """

    prompt: str
    on_done: Callable[["RunResult"], None] | None = None
    title: str = ""


# check(state, now) -> Signal to spawn, or None to stay idle this tick.
# May be sync or async. Cooldowns live in state (survive restarts for free).
CheckFn = Callable[[dict, float], Union[Signal, None, Awaitable[Union[Signal, None]]]]


@dataclass
class PollTrigger:
    check: CheckFn


@dataclass
class MentionTrigger:
    """Agent activates only when its persona is @-mentioned in the bot channel
    (or handed off by another bot). No poll check."""


@dataclass(frozen=True)
class Schedule:
    """A wall-clock recurrence — daily / weekly / monthly at a fixed local time.

    This is NOT an interval ('every 6h' is a tick_task or a poll cooldown). It is
    'every day at 18:00 Asia/Seoul', 'every Monday 09:00', 'the 1st of each month 08:00'.

    The safety property the whole thing exists for: dedup is by OCCURRENCE, not interval.
    `previous_occurrence(now)` returns the most recent scheduled datetime <= now; the
    orchestrator fires only when the agent's persisted `last_fired` predates that
    occurrence, and records `last_fired` only after a verified-complete run. So a daily,
    weekly, OR monthly job runs an occurrence exactly once — a restart at any hour cannot
    double-fire it (state.json holds last_fired), and downtime across an occurrence catches
    up with a single run for the latest missed one, never a storm. An interval scheme
    (`now - last >= 86400`) can't promise that: it drifts on late runs and re-fires on a
    restart that lands mid-window.
    """

    kind: str                       # "daily" | "weekly" | "monthly"
    hour: int
    minute: int = 0
    weekday: int | None = None      # weekly: 0=Mon … 6=Sun (matches datetime.weekday())
    day: int | None = None          # monthly: 1..31, clamped to the month's last day
    tz: str = "UTC"                 # IANA name, e.g. "Asia/Seoul" (no dep — stdlib zoneinfo)

    @classmethod
    def daily(cls, hour: int, minute: int = 0, tz: str = "UTC") -> "Schedule":
        return cls("daily", hour, minute, tz=tz)

    @classmethod
    def weekly(cls, weekday: int, hour: int, minute: int = 0, tz: str = "UTC") -> "Schedule":
        return cls("weekly", hour, minute, weekday=weekday, tz=tz)

    @classmethod
    def monthly(cls, day: int, hour: int, minute: int = 0, tz: str = "UTC") -> "Schedule":
        return cls("monthly", hour, minute, day=day, tz=tz)

    def previous_occurrence(self, now: datetime) -> datetime:
        """Most recent scheduled datetime <= `now`, tz-aware in this schedule's zone."""
        zone = ZoneInfo(self.tz)
        local = now.astimezone(zone)
        if self.kind == "daily":
            cand = local.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if cand > local:
                cand -= timedelta(days=1)
            return cand
        if self.kind == "weekly":
            cand = local.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            cand -= timedelta(days=(local.weekday() - (self.weekday or 0)) % 7)
            if cand > local:
                cand -= timedelta(days=7)
            return cand
        if self.kind == "monthly":
            day = min(self.day or 1, calendar.monthrange(local.year, local.month)[1])
            cand = local.replace(day=day, hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if cand > local:
                y, m = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
                day = min(self.day or 1, calendar.monthrange(y, m)[1])
                cand = datetime(y, m, day, self.hour, self.minute, tzinfo=zone)
            return cand
        raise ValueError(f"unknown schedule kind: {self.kind!r}")

    def next_occurrence(self, now: datetime) -> datetime:
        """First scheduled datetime strictly after `now`, tz-aware in this schedule's zone.
        Display only (STATUS.md, dry-run) — firing is decided by previous_occurrence."""
        prev = self.previous_occurrence(now)
        if self.kind == "daily":
            return prev + timedelta(days=1)
        if self.kind == "weekly":
            return prev + timedelta(days=7)
        y, m = (prev.year, prev.month + 1) if prev.month < 12 else (prev.year + 1, 1)
        day = min(self.day or 1, calendar.monthrange(y, m)[1])
        return datetime(y, m, day, self.hour, self.minute, tzinfo=ZoneInfo(self.tz))

    def describe(self) -> str:
        """Human cadence, e.g. "daily 09:00 Asia/Seoul" — what STATUS.md shows."""
        at = f"{self.hour:02d}:{self.minute:02d} {self.tz}"
        if self.kind == "weekly":
            return f"weekly {calendar.day_abbr[self.weekday or 0]} {at}"
        if self.kind == "monthly":
            return f"monthly day {self.day or 1} {at}"
        return f"{self.kind} {at}"


@dataclass
class ScheduleTrigger:
    """Wall-clock cron trigger: spawn this agent on a Schedule. A third trigger model
    beside PollTrigger (system, event-driven) and MentionTrigger (human). Restart-safe
    dedup lives in the orchestrator (state["agents"][name]["last_fired"]); see Schedule.

    `prompt` is the trigger payload the run gets (the standing instructions still come
    from the agent's prompt_file). Override it to say what the scheduled run should do.
    `title` is the card headline, as on Signal; empty means the prompt's first line."""

    schedule: Schedule
    prompt: str = "정기 실행 시각입니다. 표준 지침(prompt.md)에 따라 이번 회차를 수행하세요."
    title: str = ""


@dataclass
class StatusPhrases:
    """Customizable text for status banners and stall-escalation reactions.

    Defaults are neutral Korean. Override per Persona to match the world selected for
    a harness; otherwise Harness.status_phrases is used.
    """

    queued: str = "요청을 확인하는 중..."
    thinking: str = "생각하는 중..."
    tool: str = "{tool} 실행하는 중..."       # {tool} = the redacted tool name
    stall_soft: str = "아직 작업 중이에요..."           # auto-upgrades at 10s
    stall_hard: str = "생각보다 오래 걸리고 있어요..."  # auto-upgrades at 30s

    def tool_text(self, tool_name: str) -> str:
        return self.tool.format(tool=tool_name)


@dataclass
class ApprovalPhrases:
    """Every user-visible string in the approval flow, plus the bot-hop stop notice.

    These were hardcoded Korean, which is fine for a harness whose channel is Korean
    and wrong for one whose audience is not — a bot that answers an English question
    in English and then hands back a `✅ 승인` button is speaking two languages in one
    thread. Defaults are the original Korean, so every existing harness is unchanged;
    override per harness via Harness(approval_phrases=...).

    `resume_approved` / `resume_rejected` are NOT display copy — they are fed back into
    the model as the message that resumes a blocked turn, so they carry the same
    language as the rest of the conversation for the same reason.

    What is deliberately NOT here: approval_text.py's RELAY_APPROVED / _MARKER_RE /
    APPROVE_TOKENS. Those are protocol, not copy — a signed marker whose literal is
    part of the signature, and a token set that already accepts approve/approved/lgtm/✅
    alongside 승인. Localizing them would break relays mid-thread.
    """

    request_fallback: str = "🔐 {mention}승인 요청 ({name}): {summary}"
    request_block: str = "🔐 *승인 요청* — {name}\n{mention}{summary}"
    approve_button: str = "✅ 승인"
    reject_button: str = "❌ 거부"
    nothing_pending: str = (
        "⚠️ 이 스레드에는 승인 대기 중인 요청이 없습니다 — 승인할 대상이 없어 "
        "아무 권한도 열지 않았습니다. 승인 버튼이 달린 스레드에서 결정해 주세요."
    )
    approved_msg: str = "✅ 승인됨 — <@{who}> (메시지){note}{tail}"
    rejected_msg: str = "❌ 거부됨 — <@{who}> (메시지){tail}"
    approved_tail: str = " · 이 스레드 남은 대기 {n}건"
    rejected_tail: str = " · 대기 {n}건 취소"
    resolved_approved: str = "✅ 승인됨 — <@{who}>"
    resolved_rejected: str = "❌ 거부됨 — <@{who}>"
    not_approver: str = "<@{who}> 이 버튼은 승인권자({valid})만 누를 수 있어요."
    # Slack is connected but no approver is configured: nobody's click counts, so the
    # operator is told what to set instead of a stranger being told off.
    no_approver: str = (
        "⚠️ 승인권자가 설정되지 않아 이 버튼을 처리하지 않았어요 — env/harness.env에 "
        "`{env}`를 넣고 하네스를 다시 시작하세요."
    )
    resume_approved: str = "승인되었습니다 — 방금 승인된 작업을 지금 이어서 진행하세요."
    resume_rejected: str = "거부되었습니다 — 그 작업은 진행하지 마세요. 대안이 있으면 제안만 하세요."
    hop_limit: str = "🛑 봇끼리 {n}턴 주고받아서 여기서 멈춥니다 — {who} 답장이 있어야 이어갑니다."
    hop_limit_human: str = "사람"  # substituted for {who} when no addressee is configured
    # The over-cap handoff ask. `hop_limit` above is the fallback for a harness with no
    # addressee configured: nobody to ask, so all it can do is say it stopped.
    hop_request: str = (
        "🛑 봇끼리 {n}턴 주고받아서 멈췄습니다. {mention}{from_name} → {to_name} 위임을 "
        "이어가도 될까요?\n> {summary}"
    )
    hop_allow_button: str = "▶️ 넘기기 허용"
    hop_stop_button: str = "🛑 여기서 멈춤"
    hop_allowed: str = "▶️ 허용됨 — <@{who}> · {to_name}이(가) 이어받습니다"
    hop_stopped: str = "🛑 멈춤 — <@{who}>"
    # A human @-mention handed to the co_approver (SlackConfig.escalate_human_mentions).
    escalated: str = (
        "이 하네스는 {interface}을 통해 답합니다 — {who}을 그대로 넘깁니다. "
        "답변은 {interface}이 이어서 드립니다."
    )
    hop_stale: str = "⚠️ 이 위임 요청은 이미 지났습니다 — 그 사이 사람이 말해서 스레드가 풀렸습니다."
    # Posted on startup into a thread whose work a restart cut short (see
    # MentionMixin.resume_after_restart). A queued handoff is re-run; a turn a person
    # asked for is not, because the person may have moved on.
    handoff_resumed: str = "🔁 재시작 후 이어서 진행합니다 — {to_name}에게 넘긴 일을 다시 시작합니다."
    turn_interrupted: str = "⚠️ 작업 중에 하네스가 재시작돼서 중단됐어요. 다시 할까요? 원하시면 다시 말씀해 주세요."


# Rides along with every non-empty Persona.voice. A voice is tone only: a cheap model
# told to "be casual" will round a number or soften an approval ask to stay in character,
# and those are exactly the things a reader acts on.
VOICE_GUARD = (
    "This style is tone only. Never let it blur facts that must be exact: numbers, "
    "paths and ids, \"unverified\" markers, errors, and approval requests "
    "(APPROVAL_REQUEST lines) stay exact and complete, in plain words."
)


def voice_clause(persona: "Persona | None") -> str:
    """The speaking-style text to inject for `persona`, or "" when it declares none."""
    if persona is None or not persona.voice.strip():
        return ""
    return f"Your speaking style: {persona.voice.strip()}\n{VOICE_GUARD}"


def is_http_url(value: str) -> bool:
    """Whether a picture field holds a web URL rather than a file in the harness. Spaces
    around it and the scheme's case do not count: " HTTPS://…" pasted from a settings
    file is still a URL, and read as a path it is a file that does not exist."""
    return value.strip().lower().startswith(("https://", "http://"))


@dataclass
class Persona:
    """A Slack posting identity — one themed bot from the harness's WORLD.md.

    Architecture: ONE Socket Mode connection (the main persona) receives
    every channel event; sub-personas are posting-only identities (xoxb, no xapp)
    driven by the same process.
    """

    key: str                        # "oracle" — routing key, slug-safe
    display: str                    # "Oracle" — Slack display name (sans harness prefix)
    flavor: str = ""                # world-voice one-liner injected into this persona's prompts
    bot_token_env: str | None = None  # env var holding xoxb-…; None → posts as main persona
    status_phrases: StatusPhrases | None = None  # None → Harness.status_phrases default
    # Extra @tool objects ONLY this identity gets when answering a Slack mention
    # (listener.py's _handle_mention), layered on top of Harness.mention_tools.
    # Two reasons to reach for this instead of just widening mention_tools for
    # everyone: (1) context — every tool's name+description+schema rides along
    # on every mention turn regardless of whether that persona ever needs it,
    # so a write-capable tool only one persona actually uses shouldn't be
    # loaded for the other three; (2) scope fixed in code, not prompt — same
    # toolkit.py philosophy as the mention_main/mention_approver tool.
    #
    # May instead be an ASYNC FACTORY `(harness, thread_ts) -> list`, for tools whose
    # scope is per-conversation rather than per-persona: a repair persona may commit into
    # a worktree cut for the thread it is answering in, so its git tools cannot be bound
    # until the thread exists. Every tool returned either way is approval-gated before
    # the persona sees it (mentions.py's _gated_write_tools).
    mention_tools: list[Any] | Callable[..., Awaitable[list]] = field(default_factory=list)
    # Thread-bound, and NOT approval-gated. The third slot, for
    # a tool that must know which thread it is answering in (so a persona cannot act in
    # a thread it is not answering in), but for which requiring
    # an approve click before the harness may ask the approver a *question* would be
    # absurd.
    #
    # Neither existing slot fits. Harness.mention_tools is built once at startup and
    # knows no thread; Persona.mention_tools is thread-bound but gated UNCONDITIONALLY.
    # Passing thread_ts as a tool PARAMETER instead is what philosophy.md names
    # directly — "a tool's own parameters can reopen the boundary it claims to have" —
    # so the binding stays in a closure and this category gets its own slot.
    #
    # The category: "bound to this conversation, with no outward side-effect." Anything
    # with an outward side-effect belongs in mention_tools, where the gate is. Same two
    # shapes as mention_tools: a list, or an async FACTORY `(listener, thread_ts) ->
    # list` — which is what a thread-bound tool actually needs.
    thread_tools: list[Any] | Callable[..., Awaitable[list]] = field(default_factory=list)
    # Two pictures, two readers. `portrait` is a local image file inside the harness
    # (PNG/JPEG/WebP, 2 MB or less) that the world map embeds; `icon_url` is a public
    # http(s) image URL that Slack and Discord show beside this persona's messages.
    portrait: str = ""
    # How this persona talks on Slack (register, verbal habits, emoji, length), as the
    # user approved it. Injected beside flavor with VOICE_GUARD; "" adds nothing.
    voice: str = ""
    # After voice, so positional Persona(...) calls keep their meaning. See `portrait`.
    icon_url: str = ""

    @property
    def token(self) -> str | None:
        return os.environ.get(self.bot_token_env) if self.bot_token_env else None

    def __post_init__(self) -> None:
        self.icon_url = (self.icon_url or "").strip()
        self._check_icon_url()

    def _check_icon_url(self) -> None:
        # A new field, so no harness relies on a non-URL here: refuse it rather than
        # post under some other picture without a word.
        if self.icon_url.strip() and not is_http_url(self.icon_url):
            raise ValueError(
                f"persona {self.key!r}: icon_url must be a public http(s) image URL, "
                f"got {self.icon_url!r}; a local image file for the world map goes in portrait")

    @property
    def chat_icon(self) -> str:
        """The public URL chat shows as this persona's picture, or "" for none.
        icon_url when set; a harness written before icon_url existed put that URL in
        portrait, so an http(s) portrait still counts, but only when icon_url is empty."""
        self._check_icon_url()  # also catches an icon_url assigned after construction
        if self.icon_url.strip():
            return self.icon_url.strip()
        return self.portrait.strip() if is_http_url(self.portrait) else ""


@dataclass(frozen=True)
class Relationship:
    """A directed story relationship between persona keys, not a routing permission."""

    source: str
    target: str
    label: str
    description: str = ""


# Slack's own limits for a button: label (plain_text) 75 chars, value 2000, url 3000.
BUTTON_LABEL_MAX = 75
# Not a Slack limit (an actions block takes 25): past a handful, a reader stops reading
# and starts hunting. An announcement that needs more is two announcements.
MAX_BUTTONS = 5


@dataclass(frozen=True)
class Button:
    """One button under a worker's result announcement, declared on `Agent.buttons`.

    There are no default buttons. Each worker gets the ones its reader will actually
    press, proposed per worker when it is built. Four kinds, all handled by the kit:

      Button.ask(label, prompt)   a canned request from the clicker to this worker's
                                  persona, in the task's thread — e.g. "원인 더 파봐".
                                  The persona answers like any mention, with the same
                                  tools and the same approval path.
      Button.link(label, url)     opens a fixed URL (a dashboard, the Jenkins job).
      Button.show(label)          posts the run's primary file into the thread.
      Button.dismiss(label)       stops tracking the thread for this result.

    Clicks other than link and show are decisions, so they sit behind the approver gate
    like every other button the kit renders.
    """

    kind: str          # "ask" | "link" | "show" | "dismiss"
    label: str
    prompt: str = ""   # ask: the request text, fixed in code — never read back from Slack
    url: str = ""      # link
    style: str = ""    # "" | "primary" | "danger" (Slack's only two styles)

    def __post_init__(self) -> None:
        if self.kind not in ("ask", "link", "show", "dismiss"):
            raise ValueError(f"unknown button kind {self.kind!r}")
        if not self.label or len(self.label) > BUTTON_LABEL_MAX:
            raise ValueError(f"button label must be 1-{BUTTON_LABEL_MAX} chars: {self.label!r}")
        if self.style not in ("", "primary", "danger"):
            raise ValueError(f"button style must be '', 'primary' or 'danger': {self.style!r}")
        if self.kind == "ask" and not self.prompt.strip():
            raise ValueError(f"ask button {self.label!r} needs a prompt")
        if self.kind == "link" and not self.url.startswith(("https://", "http://")):
            raise ValueError(f"link button {self.label!r} needs an http(s) url")

    @classmethod
    def ask(cls, label: str, prompt: str, *, style: str = "") -> "Button":
        return cls("ask", label, prompt=prompt, style=style)

    @classmethod
    def link(cls, label: str, url: str) -> "Button":
        return cls("link", label, url=url)

    @classmethod
    def show(cls, label: str = "본문 보기") -> "Button":
        return cls("show", label)

    @classmethod
    def dismiss(cls, label: str = "무시") -> "Button":
        return cls("dismiss", label)


@dataclass
class Agent:
    """One unit of delegated work: a trigger, a standing prompt, a persona voice,
    and the tools it may use."""

    name: str                       # "config-drift" — state key + logs/findings dir name
    trigger: PollTrigger | MentionTrigger
    prompt_file: str | Path | None = None  # standing instructions (markdown), relative to home
    persona: Persona | None = None  # who announces this agent's findings; None → main persona
    badge: str = ""                 # optional emoji shown next to this agent's findings in the
                                    # bot channel, so a human can tell WHO found it at a glance
                                    # (all abilities may share one persona, so the persona name
                                    # doesn't distinguish them — the badge does). "" → no badge.
    # What this agent SUBSCRIBES to, in a human's words: a JQL query, a GitHub search,
    # a set of Slack channels. Rendered into HEARTBEAT.md's watchboard, which is the
    # committed answer to "what is the harness actually watching right now?" — a question
    # nothing else in the repo answers, because the queries live inside check.py bodies.
    # Purely declarative: world never executes these, the check does. Keeping them next to
    # the check that uses them is the only thing stopping the two from drifting.
    watches: tuple[str, ...] = ()
    model: str | None = None        # None → harness default
    mutates: bool = False           # True → each task runs in its own git worktree, never the
                                    # main checkout. Enforced at spawn (spawn.py →
                                    # worktrees.run_worktree): the runner's cwd / codex -C is
                                    # the worktree, and a run that cannot get one is refused
                                    # (blocked), never moved back to git_root. It was once
                                    # only a comment — every agent ran at git_root.
                                    # cwd is not a sandbox: a tool bound to an absolute path
                                    # still writes there, so repo-editing tools should
                                    # resolve world.context.current_worktree().
    writes_report: bool = False     # completion contract: a real run MUST leave a file under
                                    # its task's work/ — exit-0 with no file is
                                    # recorded as "blocked".
    writes_finding: bool = False    # the name before 0.2.7; read as writes_report
    # Provider-neutral ToolSpec values from world.toolkit. ClaudeRunner still accepts
    # historical Claude SDK tool objects as a compatibility shim; Codex fails closed.
    tools: list[Any] = field(default_factory=list)
    allow_bash: bool = False        # True → this agent gets raw Bash. Default False:
                                    # every other built-in is removed unconditionally
                                    # (spawn.py's empty BASE_TOOLS), so defaulting Bash to
                                    # True would make it the one built-in an agent gets
                                    # by forgetting a keyword — exactly backwards. Flip
                                    # it per-agent, with a comment saying why the scoped
                                    # tools don't cover the job.
    heartbeat_fields: list[str] = field(default_factory=list)  # state keys shown in HEARTBEAT.md
    write_scope: tuple[str, ...] = ()  # repo-relative dirs this agent may write to.
                                    # PRIMARY enforcement is the agent's own scoped write
                                    # tool, whose roots are bound at construction — pass
                                    # the same tuple to make_write_report() that you set
                                    # here. The world-scope-guard hook still reads this
                                    # as a backstop, but spawn.py removes Write/Edit
                                    # outright, so
                                    # in normal operation the hook never fires. Declaring
                                    # a scope here WITHOUT giving the agent a write tool
                                    # bound to it grants nothing (a
                                    # config-diff agent once self-committed outside its
                                    # lane).
    task_type: str = ""             # name of the TaskType whose completion predicate
                                    # decides when this agent's work is DONE. Empty →
                                    # "agent-run": finished and left an artifact, which
                                    # is the contract spawn.enforce_completion already
                                    # applies. Set it when finishing means something the
                                    # harness must go and check (a DDL task is not done
                                    # because a PR opened).
    task_allowed: bool = False      # may this agent spawn sub-agents? default False —
                                    # most shouldn't. Enforced by spawn.py's base tool
                                    # set: the CLI's sub-agent tool is named "Agent"
                                    # (not "Task"), and it is simply absent unless this
                                    # is True.
    allow_plugins: bool = False     # may this agent DISCOVER and INVOKE the harness's
                                    # installed plugin skills/commands? default False.
                                    # When True, spawn.py adds the "Skill" tool to the
                                    # surface and hands the agent the harness's synced
                                    # plugins (see Harness.plugins / world.process.plugins).
                                    # A THIRD authority axis alongside allow_bash and
                                    # task_allowed — audit it the same way (the-world-audit
                                    # skill reports all three).
                                    # Full-trust caveat: a loaded plugin brings its OWN
                                    # hooks and MCP servers, which strict_mcp_config does
                                    # NOT block — so within a plugin's scope the "empty
                                    # base tools / authority fixed in code" invariant is
                                    # relaxed. Only enable for a registry you trust to run
                                    # code as you. The mention responder sets this itself
                                    # (Harness.mention_plugins); for a poll agent it is an
                                    # explicit, defensible opt-in.
    buttons: tuple[Button, ...] = ()  # under this agent's result announcement. Empty →
                                    # no buttons at all; the kit adds none of its own.

    def __post_init__(self) -> None:
        self.writes_report = self.writes_report or self.writes_finding
        self.buttons = tuple(self.buttons)
        if len(self.buttons) > MAX_BUTTONS:
            raise ValueError(f"{self.name}: at most {MAX_BUTTONS} buttons, got {len(self.buttons)}")
        if sum(b.style == "primary" for b in self.buttons) > 1:
            raise ValueError(f"{self.name}: at most one primary button")


@dataclass
class SlackConfig:
    """All values resolved from env at startup — tokens never live in code/repo.
    Missing tokens degrade gracefully to poll-only mode."""

    bot_token_env: str = "SLACK_BOT_TOKEN"        # main persona xoxb (the socket owner)
    app_token_env: str = "SLACK_APP_TOKEN"        # xapp for Socket Mode
    channel_env: str = "SLACK_CHANNEL"            # the ONE dedicated channel ID
    approver_env: str = "APPROVER_SLACK_ID"       # sole approver + escalation target
    # Deliberate, explicit widening of the "single approver, no exceptions" invariant —
    # a second Slack ID (human or bot) whose
    # approve/deny decision is ALSO honored, alongside approver_env.
    # Motivating case: a relay/proxy bot (e.g. another harness's persona) that
    # forwards the request to the real human through its own channel and answers
    # on their behalf — world has no way to verify that relay actually happened,
    # so setting this is a trust decision the harness operator makes explicitly,
    # not something world defaults to or recommends.
    #
    # A BOT co_approver cannot click the approve/deny buttons: Slack only emits
    # block_actions from real UI interactions, and no Web API clicks on an app's
    # behalf. It answers by POSTING a decision message instead, which
    # slack_approvals.decision_from_text recognizes. Both paths grant the same
    # thing; do not "fix" the button gate by assuming every approver can click.
    co_approver_env: str | None = None
    # Comma-separated channel IDs (env var name) beyond the dedicated channel
    # where the harness should still react to an app_mention — e.g. a sibling
    # sibling harness's own channel, once that harness starts @-mentioning US
    # instead of pinging the human directly. Only app_mention fires here
    # (never plain "message" events) — the harness never tails a whole
    # foreign channel, it only wakes up when actually addressed. See
    # world/slack/mentions.py's _handle_relay_mention: the reply (context +
    # approve/deny) always lands in the dedicated channel, never in the relay
    # channel itself — "all approvals happen in one place" stays true.
    relay_channels_env: str | None = None
    # Hand every HUMAN @-mention of any of this harness's bots to the co_approver instead
    # of answering locally.
    #
    # This is the "one face for a group of harnesses" rule: a harness behind the face
    # surfaces findings and asks for approval, but the human converses only with the face.
    # Requires co_approver to be set (that IS the escalation target); a no-op otherwise.
    # The face's own harness leaves this False — it IS the face.
    #
    # Without it such a harness answers humans out of a mention turn with no read tools,
    # which gives a canned answer to a real question.
    escalate_human_mentions: bool = False
    # Opt in to human plaintext in the dedicated channel only. Relay channels
    # still require a mention, and bot messages retain the existing hop rules.
    respond_to_channel_messages: bool = False

    @property
    def bot_token(self) -> str | None:
        return os.environ.get(self.bot_token_env)

    @property
    def app_token(self) -> str | None:
        return os.environ.get(self.app_token_env)

    @property
    def channel(self) -> str | None:
        return os.environ.get(self.channel_env)

    @property
    def approver(self) -> str | None:
        return os.environ.get(self.approver_env)

    @property
    def co_approver(self) -> str | None:
        return os.environ.get(self.co_approver_env) if self.co_approver_env else None

    @property
    def relay_channels(self) -> list[str]:
        if not self.relay_channels_env:
            return []
        raw = os.environ.get(self.relay_channels_env, "")
        return [c.strip() for c in raw.split(",") if c.strip()]

    @property
    def escalate_to(self) -> str | None:
        """The bot a human @-mention is handed off to when escalate_human_mentions
        is on — the co_approver (the single face). None disables escalation,
        so a harness that flips the flag without a co_approver just answers locally."""
        return self.co_approver if self.escalate_human_mentions else None

    @property
    def addressee(self) -> str | None:
        """WHO gets @-mentioned when the harness needs a decision. The co_approver
        (a relay/proxy bot) when configured, else the primary approver.

        Every outward "please decide this" ping must go through here — approval
        buttons, finding announcements, PR-ready pings. Reading `.approver`
        directly at a ping site is the bug this property exists to prevent: it
        splits the harness across two addressees, so the relay never sees half the
        asks (found live: announce_report and refresh_pending_prs
        pinged the human directly while the buttons pinged the relay).

        This is the ASK side only. The ACCEPT side is `is_approver`, which stays
        broader on purpose — a decision from either identity counts, whichever
        one we happened to address.
        """
        return self.co_approver or self.approver

    def is_approver(self, slack_id: str) -> bool:
        """True for the primary approver OR the co-approver (if configured).
        The one check every approve/deny click must pass — see
        listener.py's _on_interactive."""
        return bool(slack_id) and slack_id in (self.approver, self.co_approver)


@dataclass
class DiscordConfig:
    """EXPERIMENTAL — the Discord gateway's whole configuration (docs/discord.md).

    Deliberately NOT a SlackConfig look-alike: one bot, one channel, one approver. There is
    no co_approver, no relay channel, no escalation and no per-persona token, because the
    Discord gateway implements none of them. A field for a thing that does not exist would
    be a promise in the shape of a setting. Values are env var NAMES, resolved at startup,
    so a token never lives in code.

    Passing this to `Harness(discord=...)` selects the Discord gateway instead of Slack.
    """

    bot_token_env: str = "DISCORD_BOT_TOKEN"      # Developer Portal → Bot → token
    channel_env: str = "DISCORD_CHANNEL_ID"       # the ONE text channel the bot listens in
    approver_env: str = "APPROVER_DISCORD_ID"     # the ONE Discord user id whose click counts

    @property
    def bot_token(self) -> str | None:
        return os.environ.get(self.bot_token_env)

    @property
    def channel_id(self) -> str | None:
        return os.environ.get(self.channel_env)

    @property
    def approver(self) -> str | None:
        return os.environ.get(self.approver_env)

    def is_approver(self, user_id: str) -> bool:
        """The click gate. `user_id` must come from the interaction's own user (set by
        Discord), never from anything a message says. No approver configured means NO
        ONE may approve — unlike Slack's unconfigured dry-run, which does not gate."""
        approver = self.approver
        return bool(user_id) and bool(approver) and str(user_id) == approver


@dataclass
class PluginMarketplace:
    """A git-backed Claude Code marketplace (a repo with `.claude-plugin/marketplace.json`
    and a `plugins/` tree, e.g. github.com/your-org/skill-registry) the harness
    syncs and hands to plugin-enabled agents.

    The SDK's `ClaudeAgentOptions.plugins=` takes LOCAL paths only — cloning a git
    marketplace is a CLI feature, not an SDK one. So world clones/pulls the repo itself
    (world.process.plugins.sync_marketplace, deterministic and LLM-free, riding a
    tick_task) into `home/.claude/marketplaces/<name>/`, reads its marketplace.json to
    resolve each enabled plugin's source dir, and passes those local dirs to the spawn.

    FULL TRUST: a loaded plugin is handed to the agent whole — its skills, commands,
    AND its hooks / `.mcp.json` MCP servers (which strict_mcp_config does not block).
    Only add a marketplace you trust to run code as you; world cannot fence a plugin's
    own hooks the way it fences a poll agent. `enabled` is an explicit allowlist by
    plugin name — no marketplace is loaded wholesale; a name not found in the synced
    marketplace.json is logged, never silently skipped.
    """

    name: str                       # marketplace name; also the cache subdir under .claude/marketplaces/
    git_url: str                    # clonable git URL (…/skill-registry.git)
    enabled: tuple[str, ...] = ()    # plugin names (marketplace.json plugins[].name) to load; () = none
    ref: str = "main"               # branch / tag / commit to track on each sync


@dataclass
class RunResult:
    status: str  # "ok" | "error" | "rate_limited" | "blocked" | "skipped"
    result_text: str = ""
    duration_s: float = 0.0
    # Usage/cost — straight from ResultMessage (the SDK already computes these
    # per call). 0 for skipped/dry-run or any path that never got a ResultMessage
    # back (track cost on error/blocked/rate_limited
    # paths too — a call that reached the API cost money even if it failed).
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    # What the run footer needs beyond the counters. `model` is the model that was
    # BILLED, read back from the provider's result — not the one the agent asked for,
    # so a fallback or an override shows up instead of being papered over. Empty when
    # the provider does not report it (Codex). `tools_available` is the size of the
    # spawn's whole tool surface, stamped by spawn.py.
    model: str = ""
    tools_available: int = 0
    # Provider-normalized permission refusals. Completion enforcement lives above the
    # provider: a writes_report agent that produced its file still completed even if an
    # optional call was denied on the way, while a denial plus no output is blocked.
    permission_denials: tuple[str, ...] = ()
    # Files this run left behind, relative to the harness home. Computed by
    # spawn.enforce_completion, which already diffs before/after to decide whether the
    # run completed — it used to throw the paths away, so the announcer had to go and
    # rediscover them by scanning the filesystem a tick later.
    artifacts: tuple[str, ...] = ()
