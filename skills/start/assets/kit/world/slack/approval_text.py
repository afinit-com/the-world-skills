"""Recognizing an approval decision written as a Slack MESSAGE.

A decision reaches the harness three ways, and they are not interchangeable:

  BUTTON  — _on_interactive. Only a HUMAN can produce one. Slack emits block_actions
            payloads from real UI interactions; there is no Web API that clicks on an
            app's behalf.
  TOKEN   — a human types the whole message as one word: "승인", "approve", "✅".
  RELAY   — a bot forwards a decision a human already made elsewhere through a signed
            result that the receiving harness can verify. A bot cannot click, so this
            path is required for cross-harness approval.

WHO MAY DO WHICH. Humans decide; bots relay. A human's message is trusted for its
content: a bare token is a decision. A BOT's message is trusted only for its SIGNATURE
— `_on_interactive` signs `thread|decision|msg_ts` with the harness secret, and nothing
else in the kit can produce that HMAC.

That asymmetry is the whole file. Without it, any tool that can post text into another
harness's channel can approve on that harness's behalf, because the receiving harness
recognizes the sender as an approver and reads the words. Without relay evidence, an
authorized bot could manufacture an approval and unlock gated tools with no human in
the loop. A co-approver carries a human decision; the signature prevents it from
manufacturing one.

REPLAY. The signature covers the button message's `ts`, which is unique per ask, and
the receiver records the ones it has honored. A captured decision cannot be re-posted
to grant a second time, nor moved to another thread.

FAIL CLOSED. No `RELAY_SECRET` in the environment means no bot can relay
anything. Humans keep working. A missing secret must never silently widen the gate.

MATCHING IS EXACT. The whole message, minus mentions and edge punctuation, must BE one
of the tokens; the marker is anchored at the start. "승인 불가" and "do not approve" are
None — a keyword search would call them approvals. None means "ordinary prose", which
the caller routes as a normal message. Widening this to fuzzy intent matching trades a
bounded, auditable gate for a model's opinion about what the approver meant, on the one
boundary where being wrong means an unauthorized side-effect.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

SECRET_ENV = "RELAY_SECRET"

# The wire format. Both halves of a cross-harness relay agree on these bytes; treat them
# as a protocol constant, not as copy. `[승인됨:v1:<msg_ts>:<sig>]` — the trailing prose
# after the marker is for the agent reading the thread; only the marker is parsed.
RELAY_APPROVED = "승인됨"
RELAY_DENIED = "거부됨"
_MARKER_RE = re.compile(r"^\[(승인됨|거부됨):v1:([0-9.]+):([0-9a-f]{32})\]")

APPROVE_TOKENS = frozenset({"승인", "승인함", "승인합니다", "approve", "approved", "lgtm", "✅"})
DENY_TOKENS = frozenset({"거부", "반려", "deny", "denied", "reject", "rejected", "❌"})

_MENTION_RE = re.compile(r"<@[^>]+>")
_EDGE_PUNCT = r"\s`*_~\[\]\(\)<>:;.,!?\"'"
_EDGE_RE = re.compile(rf"^[{_EDGE_PUNCT}]+|[{_EDGE_PUNCT}]+$")

MAX_RELAY_NONCES = 200


def _secret() -> bytes | None:
    raw = os.environ.get(SECRET_ENV, "").strip()
    return raw.encode() if raw else None


def sign_decision(thread_ts: str, decision: str, msg_ts: str) -> str | None:
    """HMAC over the three things that make a decision unique: which thread it applies
    to, what it decided, and which ask it answers. None when no secret is configured —
    the caller then posts an unsigned message, which no bot-side receiver will honor."""
    key = _secret()
    if not key:
        return None
    payload = f"{thread_ts}|{decision}|{msg_ts}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]


def relay_decision(approved: bool, thread_ts: str = "", msg_ts: str = "") -> str:
    """The message a harness posts into the thread a decision belongs to.

    `thread_ts` is where the decision lands (the ORIGIN thread for a cross-harness
    relay); `msg_ts` is the button message being answered. Both are bound into the
    signature, so a receiving harness can tell a relayed human decision from a
    model-authored sentence that merely looks like one."""
    kind = RELAY_APPROVED if approved else RELAY_DENIED
    body = ("요청한 작업을 진행해도 됩니다." if approved else
            "요청한 작업은 진행하지 마세요. 대안이 있으면 제안만 하세요.")
    sig = sign_decision(thread_ts, "approve" if approved else "deny", msg_ts)
    marker = f"[{kind}:v1:{msg_ts}:{sig}]" if sig else f"[{kind}]"
    return f"{marker} {body}"


def decision_from_text(
    text: str,
    *,
    author_is_bot: bool = False,
    thread_ts: str = "",
    seen_nonces: list | None = None,
) -> str | None:
    """"approve" | "deny" | None. None means the message is not a decision.

    `author_is_bot` decides which evidence counts. A human is believed for what they
    wrote. A bot must present a signature this harness could only have produced by a human
    clicking a button — see the module header for why that asymmetry is the point.

    `seen_nonces` (a list the caller persists) blocks replay: a signed decision answers
    exactly one ask, once."""
    raw = _MENTION_RE.sub(" ", text or "").strip()
    if not raw:
        return None

    m = _MARKER_RE.match(raw)
    if m:
        kind, msg_ts, sig = m.groups()
        decision = "approve" if kind == RELAY_APPROVED else "deny"
        expected = sign_decision(thread_ts, decision, msg_ts)
        if not expected or not hmac.compare_digest(expected, sig):
            return None  # forged, replayed onto another thread, or no secret configured
        if seen_nonces is not None:
            if msg_ts in seen_nonces:
                return None  # this ask was already answered
            seen_nonces.append(msg_ts)
            del seen_nonces[:-MAX_RELAY_NONCES]
        return decision

    if author_is_bot:
        # A bot's prose is never a decision. Only a signature is.
        return None

    stripped = _EDGE_RE.sub("", raw).lower()
    if stripped in APPROVE_TOKENS:
        return "approve"
    if stripped in DENY_TOKENS:
        return "deny"
    return None


def looks_like_decision(text: str) -> bool:
    """Would ANY receiver read this message as a decision? Used by model-callable
    posting tools to refuse to write one. Deliberately broad: it ignores the signature
    and the author, because the tool's job is to never emit decision-shaped text at all,
    not to reason about who would believe it."""
    raw = _MENTION_RE.sub(" ", text or "").strip()
    if not raw:
        return False
    if raw.startswith(f"[{RELAY_APPROVED}") or raw.startswith(f"[{RELAY_DENIED}"):
        return True
    stripped = _EDGE_RE.sub("", raw).lower()
    return stripped in APPROVE_TOKENS or stripped in DENY_TOKENS
