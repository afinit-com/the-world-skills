"""Outbound Slack calls — posting as a bot identity, reaction swaps, and the
thread status banner. Every one of these is best-effort by design: an indicator
that fails must never take down the turn it was decorating.

Split out of listener.py. Mixed into SlackListener, so `self` here
is the listener. Also used by the findings bridge, which only needs `post`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from .as_human import safe_payload, safe_text
from ..channels.base import posting_icon

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient

    from ..harness import Harness

SLACK_CHUNK = 3900  # Slack rejects messages over ~4k

# Slack telling us the world is already the way we asked it to be. Not a failure, and
# `_react` provokes both constantly: the ladder removes 👀 that a retry already removed.
BENIGN_SLACK_ERRORS = frozenset({"already_reacted", "no_reaction"})


async def slack_call(web, method: str, **kwargs):
    """One outbound boundary for messages, cards, edits and progress indicators."""
    return await getattr(web, method)(**safe_payload(kwargs, posting=True))


class PostingMixin:
    harness: "Harness"
    web: "AsyncWebClient | None"
    identities: dict[str, dict]
    # Class-level on purpose: "once per process", not once per listener. A broken
    # indicator is a standing condition, and one line about it is the whole point.
    _warned_once: set[str] = set()

    def _web_for(self, identity: str) -> AsyncWebClient | None:
        ident = self.identities.get(identity)
        return (ident["web"] if ident else None) or self.web

    def _face(self, identity: str | None) -> dict:
        """chat.postMessage kwargs that make the main bot's message show `identity`'s
        name and icon, for a persona with no Slack app of its own (no bot_token_env).
        Needs chat:write.customize on the main app. The main persona keeps the app's
        name and only takes Persona.chat_icon as the icon; a persona with its own token
        posts as itself, and an unknown key gets {}."""
        persona = self.harness.personas.get(identity or "")
        if persona is None or persona.token:
            return {}
        icon = posting_icon(persona, self.harness.log)
        if persona.key == self.harness.main_persona.key:
            # The main persona IS the app: its name stays the app's (that is what people
            # @-mention), only its picture follows Persona.chat_icon.
            return {"icon_url": icon} if icon else {}
        face = {"username": persona.display}
        if icon:
            face["icon_url"] = icon
        return face

    async def post(
        self,
        text: str,
        *,
        identity: str = "main",
        thread_ts: str | None = None,
        blocks: list[dict] | None = None,
        channel: str | None = None,
        context: dict | None = None,
    ) -> str | None:
        """Posts to the dedicated channel (optionally into a thread) AS the given
        bot identity; returns the message ts. Chunks long text. Unknown/tokenless
        identity falls back to the main bot. `blocks` (Block Kit, e.g. buttons)
        only applies to the first chunk — callers using blocks are expected to
        keep text short enough for one. `channel` overrides the target — used to
        relay an approve/deny decision back into a relay channel's own thread
        (see world/slack/mentions.py's _handle_relay_mention); every other call site
        leaves it unset and gets the dedicated channel as always.

        `context` is ONE Block Kit context block attached to the LAST chunk — the small
        grey annotation line (see run_footer.py). Kept separate from `blocks` because it
        is positional-by-meaning (always last, always after any buttons) and because a
        caller posting plain text should not have to switch to blocks to annotate it."""
        web = self._web_for(identity)
        if not web:
            return None
        text = safe_text(text, posting=True)
        channel = channel or self.harness.slack.channel
        first_ts = None
        offsets = list(range(0, max(len(text), 1), SLACK_CHUNK))
        for i in offsets:
            body = text[i : i + SLACK_CHUNK] or "(empty)"
            kwargs: dict = {"blocks": blocks} if blocks and i == offsets[0] else {}
            # The footer annotates the whole message, so on a reply long enough to chunk
            # it belongs under the END of the reply, not buried above three more posts.
            # Rendered as its own blocks payload so `text` stays the notification body.
            if context and i == offsets[-1]:
                kwargs.setdefault(
                    "blocks", [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]
                )
                kwargs["blocks"] = [*kwargs["blocks"], context]
            resp = await slack_call(web, "chat_postMessage",
                channel=channel, text=body, thread_ts=thread_ts, **kwargs, **self._face(identity),
            )
            first_ts = first_ts or resp["ts"]
            thread_ts = thread_ts or resp["ts"]  # chunks 2+ go into the same thread
        return first_ts

    async def notify_thread(self, thread_ts: str, text: str, *, channel: str | None = None) -> None:
        await self.post(text, thread_ts=thread_ts, channel=channel)

    async def _best_effort(self, what: str, awaitable) -> None:
        """A cosmetic Slack call: never fail the turn over it, never let it fail silently.

        These used to be `contextlib.suppress(Exception)`. Then someone asked
        whether the `assistant.threads.setStatus` indicator had been removed. It had not —
        the mention handler that calls it was never reaching it (see listener.py's approver
        dedup bug). But nothing in this file could have told them apart: a suppressed call
        that never ran and a suppressed call that failed look identical from outside, which
        is this repo's first rule (docs: "an absent observation must not look like a
        negative result").

        A reaction that is already there, or already gone, is not news — Slack says so by
        name, and those stay quiet. Anything else is logged ONCE per process per failure
        kind, because a broken indicator is a standing condition, not an event, and a line
        per tick would be the noise the second rule warns about.
        """
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001 — cosmetic; the turn it decorates is not
            code = ""
            response = getattr(exc, "response", None)
            if response is not None:
                with contextlib.suppress(Exception):
                    code = response.get("error", "") or ""
            if code in BENIGN_SLACK_ERRORS:
                return
            key = f"{what}:{code or type(exc).__name__}"
            if key not in self._warned_once:
                self._warned_once.add(key)
                self.harness.log(
                    f"slack {what} failed ({code or exc!r}) — cosmetic, the turn continues. "
                    "Logged once per process."
                )

    async def _react(
        self, msg_ts: str, *, add: str | None = None, remove: str | None = None,
        identity: str | None = None,
    ) -> None:
        """Best-effort reaction swap on the triggering message."""
        web = self._web_for(identity or self.harness.main_persona.key)
        if not web or not msg_ts:
            return
        channel = self.harness.slack.channel
        for label, name, call in (
            ("remove", remove, web.reactions_remove),
            ("add", add, web.reactions_add),
        ):
            if not name:
                continue
            await self._best_effort(
                f"reactions.{label}", call(channel=channel, timestamp=msg_ts, name=name)
            )

    async def _set_thread_status(
        self, thread_ts: str, status: str, *, identity: str | None = None
    ) -> None:
        """assistant.threads.setStatus:
        a native "~하는 중..." indicator at the top of the thread, separate from
        both the reaction ladder and ToolTrail's message. Confirmed live
        that this renders in an ordinary channel thread, not just
        the Assistant panel, and that `chat:write` alone is scope
        enough. Empty string clears it. Best-effort, but never silent — see
        _best_effort: a status that stops rendering must be diagnosable."""
        web = self._web_for(identity or self.harness.main_persona.key)
        if not web or not thread_ts:
            return
        await self._best_effort(
            "assistant.threads.setStatus",
            slack_call(web, "assistant_threads_setStatus",
                channel_id=self.harness.slack.channel, thread_ts=thread_ts, status=status,
            ),
        )
