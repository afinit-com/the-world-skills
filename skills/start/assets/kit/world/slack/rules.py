"""The standing rules injected into every Slack mention turn — the harness's
approval doctrine, in prose, for the model.

Split out of listener.py. This is the one place where the harness
*asks* the model to behave; everything it *enforces* lives in code (spawn.py's
disallowed_tools, the scoped tools' bound roots, _on_interactive's approver gate).
When the two disagree, code wins — so if you loosen a rule here, check whether
the matching enforcement still exists before assuming the loosening took effect.
"""

from __future__ import annotations

import re

from ..types import MESSAGE_FORMAT

APPROVAL_MARKER = "APPROVAL_REQUEST:"

# Rendered into RULES_TEMPLATE when a harness declares Harness.pre_approved_tools.
# It sits inside the "non-negotiable" block on purpose: a carve-out stated anywhere
# weaker loses to the prohibition above it.
PRE_APPROVED_CLAUSE = """
- EXCEPTION — these tools are PRE-APPROVED and need no approval, ever: {tools}.
  Call them directly and report the result. Do not emit APPROVAL_REQUEST for them
  and do not ask whether to proceed; the request that reached you IS the
  authorization. This is a decision the harness already made, not something to
  re-derive per request. Every other side effect still needs approval."""


def pre_approved_clause(tool_names) -> str:
    """The carve-out text for `tool_names`, or "" when a harness declares none."""
    names = [n for n in (tool_names or []) if n]
    return PRE_APPROVED_CLAUSE.format(tools=", ".join(f"`{n}`" for n in names)) if names else ""

# Injected into every bot turn that runs with the thread's bot-to-bot hops spent: the
# one that spends the last hop (listener._dispatch_bot_hop) and any handoff an approver
# releases past the cap (approvals._on_hop_button). The over-cap ask itself is
# deterministic code, not this line — this only keeps the bot's own words honest, so it
# must not discourage the @-mention the ask depends on. A remaining-budget count on
# every turn was considered and rejected: noise on every turn, for one fact that only
# matters once the budget is gone. Keep each one line.
LAST_HOP_NOTE = (
    "참고: 이 스레드는 봇끼리 바로 넘길 수 있는 횟수를 다 썼습니다 — 다른 봇에게 넘길 일이 "
    "있으면 평소처럼 hand_off로 넘기세요. 그 넘김은 승인자에게 버튼으로 확인받은 뒤 전달됩니다."
)
# The same turn when no addressee is configured: nobody to ask, so the handoff only
# gets the stop notice (ApprovalPhrases.hop_limit) and waits for a person.
LAST_HOP_NOTE_UNATTENDED = (
    "참고: 이 스레드는 봇끼리 바로 넘길 수 있는 횟수를 다 썼습니다 — hand_off로 넘겨도 "
    "전달되지 않고, 스레드에서 사람이 답할 때까지 멈춥니다."
)

# Any word character — a letter or digit in ANY script (hangul included), never mere
# punctuation. A summary with none of these carries no request a human could act on.
_SUBSTANTIVE = re.compile(r"\w", re.UNICODE)


def split_approval_request(reply: str) -> tuple[str, str | None]:
    """Cut a turn's raw text at APPROVAL_MARKER into (reply, approval_summary).

    approval_summary is None when the model asked for nothing — which is how a caller
    tells "no buttons" from "buttons with an empty label". When the marker is present
    but the text after it is blank, the summary falls back to a placeholder rather
    than to None, because the request itself was real.

    But a marker followed by punctuation-only garbage is NOT a real request: the model
    emitted a placeholder or a mangled line, and rendering approve/deny buttons for it
    asks a human to decide on nothing (found live: `APPROVAL_REQUEST: ...`)`
    put an empty 🔐 approval button under a question that had already been answered). Those
    drop back to None, and the text before the marker posts as an ordinary reply."""
    if APPROVAL_MARKER not in reply:
        return reply, None
    before, _, after = reply.partition(APPROVAL_MARKER)
    lines = after.strip().splitlines()
    if not lines:
        return before.strip(), "(승인 요청)"  # bare marker: real, just unlabeled
    summary = lines[0].strip()
    if not _SUBSTANTIVE.search(summary):
        return before.strip(), None          # garbled/placeholder marker — not a request
    return before.strip(), summary


# --- Slack-native formatting (code-enforced, not a prompt rule) -----------------
# A cheap mention model reliably emits GitHub-flavored markdown; Slack mrkdwn is
# NOT that. Two things leak as literal syntax and make a relay answer look broken:
# ATX headers `## X` (Slack has no headers — the `##` shows verbatim) and `**bold**`
# (Slack bold is ONE asterisk). Everything Slack already renders — `-`/`1.` lists,
# `>` quotes, `<url|text>` links, `` `code` `` — is left alone. Fenced/inline code is
# protected byte-for-byte so a `**` or `#` INSIDE a code sample is never rewritten.
# Applied to model PROSE only (mention/relay replies), never to world's own structured
# or HMAC-signed posts.
_MD_FENCE = re.compile(r"```(?:.*?```|.*\Z)", re.DOTALL)
_MD_INLINE = re.compile(r"`[^`\n]+`")
# One level of balanced parentheses stays in the URL (a wiki link), so it is not cut at
# its first `)`.
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(<?(https?://(?:[^\s<>()]|\([^\s<>()]*\))+)>?\)")
# Slack escapes only these three. An `&` already starting one of them stays as it is.
_SLACK_AMP = re.compile(r"&(?!(?:amp|lt|gt);)")
_MD_HEADER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_MD_BOLD_STAR = re.compile(r"(?<!\*)\*\*(?!\s)(.+?)(?<!\s)\*\*(?!\*)", re.DOTALL)
_MD_BOLD_US = re.compile(r"(?<![\w_])__(?!\s)(.+?)(?<!\s)__(?![\w_])", re.DOTALL)


def _slack_escape(text: str) -> str:
    """Escape `&`, `<`, `>` as Slack asks. Not html.unescape first: it also reads
    `&not`, `&para`, `&reg` without a semicolon, so `?a=1&notify=1` became `¬ify`."""
    return _SLACK_AMP.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")


def slack_mrkdwn(text: str) -> str:
    """Rewrite a model reply's GitHub markdown into Slack mrkdwn so it renders instead
    of leaking literal `##`/`**`. Idempotent, and code spans pass through untouched."""
    if not text:
        return text
    spans: list[str] = []

    def stash(m: "re.Match") -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    out = _MD_INLINE.sub(stash, _MD_FENCE.sub(stash, text))
    def link(m: "re.Match") -> str:
        # Slack has no escape for a label's pipe: use its visible fullwidth form.
        label = re.sub(r"\x00(\d+)\x00", lambda code: spans[int(code[1])], m[1])
        label = _slack_escape(label).replace("|", "｜")
        url = _slack_escape(m[2]).replace("|", "%7C")
        return f"<{url}|{label}>"

    out = _MD_LINK.sub(link, out)
    out = _MD_HEADER.sub(r"*\1*", out)
    out = _MD_BOLD_STAR.sub(r"*\1*", out)
    out = _MD_BOLD_US.sub(r"*\1*", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], out)


# Thread rules shared by single- and multi-persona harnesses.
RULES_TEMPLATE = """
You are {persona_flavor}{voice}
You are answering inside a single Slack thread that IS this task's full context.
You have NO built-in tools — no Read, no Write, no Bash, no Grep. Everything you
can do is in your tool list, and each tool is already scoped to exactly the paths
it may touch. Pass paths to them relative to the harness root, exactly as you were
given them (e.g. "tasks/20261010-8e0b/work/report.md") — never guess or reconstruct an
absolute path (found live: a cheap-model mention responder
hallucinated two DIFFERENT wrong absolute paths for the same file in one
conversation, concluded a real file "didn't exist" because its own guess was
wrong). If `read_report` reports a path outside your scope, that is the answer —
do not go looking for another route to it. If it reports the file does not exist,
re-check the path you were given before concluding anything is missing; use
`list_reports` on the directory to see what is actually there.
Rules (non-negotiable):
- Investigation, diagnosis, and proposals are free. Any real side-effect
  (posting somewhere visible to others, changing code, executing a repair) is
  FORBIDDEN until {approver} explicitly approves in this thread. Silence is
  never approval.{pre_approved}
- Whenever you need a yes/no decision from {approver} before a side-effect, do
  NOT ask in prose. Before the marker line, give {approver} a short decision
  brief — not a bare "may I do X?":
    1. what you're about to do (2-3 lines)
    2. why — the evidence/trigger behind it
    3. the main risk or trade-off if approved (one line; say "없음" if genuinely none)
    4. your own recommendation, stated plainly (approve / hold off / needs more info)
  {approver} should be able to decide from this alone, without asking a
  follow-up question first. Then end your final message with exactly one line:
  `APPROVAL_REQUEST: <the action, one line>` — the harness renders it as an
  approve/deny button. Then stop; the decision comes back as your next message
  ([승인됨]/[거부됨] prefix).
- If this task genuinely belongs to a teammate (see the roster below), call
  hand_off with their name and what you need from them. They take it after your
  turn ends, in this thread; say in your reply that you handed it over. Do not
  @-mention a teammate to hand off — it does nothing. Handoffs are capped at a
  few hops per thread; past the cap the harness holds the handoff and asks
  {approver} whether it may go through, so it can sit unanswered for a long time.
  Don't chain more than one or two hops deep, and never hand off to yourself. The
  roster below is the ONLY set of teammates you may hand to. mention_approver is
  only for addressing {approver}, never for a handoff. Your reply already reaches
  the thread: don't hand work back just to report a result, only when a teammate
  must decide or do something you can't.
- The harness is not yours to reconfigure: never propose changes to
  .claude/settings*.json, permission allowlists, hooks, or harness code — not
  even via APPROVAL_REQUEST. A denied command is something to report, not an allowlist
  request.
- Your final text is posted to the thread as your own identity, for a human
  skimming Slack. {message_format}
- Reply in the language the humans in this thread write in, whatever language
  this prompt, your persona, the world description, or a teammate's handoff is
  written in. Keep tool names, code, and quoted text as they are.

Teammates you can hand off to (by name, with hand_off):
{roster}
""".strip().replace("{message_format}", MESSAGE_FORMAT)
