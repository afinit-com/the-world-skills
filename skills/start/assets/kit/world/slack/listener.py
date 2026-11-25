"""Slack bot listener — connection lifecycle, outbound posting, event dispatch.

Split into focused modules; SlackListener is still
the whole public surface (world.harness imports only it), composed from its siblings:

  ui.py         ToolTrail + the reaction/status indicators (👀→🤔→👍/😱,
                assistant.threads.setStatus, stall escalation)
  rules.py      RULES_TEMPLATE — the approval doctrine, as prose, for the model
  post.py       PostingMixin — post/_react/_set_thread_status, all best-effort
  approvals.py  ApprovalMixin — approve/deny buttons and their click gate
  mentions.py   MentionMixin — the one-shot mention responder + announce_task

This package may import world.process (a mention turn spawns an agent). The reverse
never happens: findings reach Slack through world.bridge, which the orchestrator
calls, and which sits above both.

What stays here: the socket lifecycle and _on_request's dispatch — including
bot-to-bot mention routing (MENTION_RE + user_to_identity + hop cap): a
message authored by one of THIS harness's own bot identities that @-mentions a
*different* bot identity dispatches to it, up to MAX_BOT_HOPS times per thread;
any human message in the thread resets the counter. The turn that spends the last
hop, and any handoff released past the cap, gets one line in its prompt saying so
(rules.LAST_HOP_NOTE). Past the cap the
handoff is parked in the thread's `pending_hop` and the approver is asked whether it may go
through (approvals.post_hop_buttons); with no addressee configured there is no one
to ask, so the thread just gets the stop notice. Foreign bots (not one of ours)
are ignored entirely, with two configured exceptions, each exactly one bot ID: the
co_approver, and the driver (SlackConfig.driver_env, see _dispatch_driver). Neither
opens the door to bots in general. SlackConfig.guest_bots_env names further bots that
get the driver's rules and nothing more.

Guest channels (SlackConfig.guest_channels_env, see _dispatch_guest) are the one place
besides the home channel where a mention is answered in its own thread. The thread's
channel is recorded on its state record and read back by PostingMixin.channel_of.

🔒 If you ever revisit that last rule: Slack does NOT deliver `app_mention` for a
bot-authored message. Only `message` arrives, with bot_id set and no subtype
(measured). So the human path's `etype == "app_mention"` test cannot be
reused for foreign bots — the proof of address has to be MENTION_RE finding one of
our user_ids in the text. An attempt to accept foreign bots was built and reverted
(the consumer stopped needing it); this is the part of it worth
keeping, because it is a Slack fact, not a design choice. The driver path is built on
it: a driver's message counts only when its text mentions one of our identities, and
the event type is never consulted.

Deliberately not implemented:
  - Session-per-thread (ClaudeSDKClient + session_id resume) — every mention
    reply here is a fresh one-shot query with the thread transcript refetched
    for context. Correct, just not context-cache-efficient.
  - Concurrency cap and queueing, idle-disconnect,
    retry-on-failure, worktree cleanup buttons.

Every one of the above is a known, intentional cut, not an oversight. Add it when a
harness actually needs it (for example, heavy mention traffic needs a concurrency cap).

The former Write/Edit guard is no longer needed. spawn.py does not hand agents
Write/Edit at all
(an empty BASE_TOOLS), so there is nothing left to guard — the scoped tools bind their
own roots instead.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from .approvals import ApprovalMixin
from .as_human import normalize_message
from .mentions import MentionMixin
from .post import PostingMixin, remember_channel
from .rules import (  # noqa: F401 — APPROVAL_MARKER re-exported
    APPROVAL_MARKER,
    LAST_HOP_NOTE,
    LAST_HOP_NOTE_UNATTENDED,
)

if TYPE_CHECKING:
    from ..harness import Harness

SEEN_TS_TTL_S = 600  # de-dup window for at-least-once event retries / dual-delivery
MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)>")
MAX_BOT_HOPS = 4  # caps bot-to-bot handoffs per thread until a human speaks again


class SlackListener(PostingMixin, ApprovalMixin, MentionMixin):
    def __init__(self, harness: "Harness", state: dict, save_state_cb) -> None:
        self.harness = harness
        self.state = state
        self.save_state = save_state_cb
        self.ready = asyncio.Event()
        self.socket: SocketModeClient | None = None
        self.bot_user_id: str | None = None

        slack = harness.slack
        self.web = AsyncWebClient(token=slack.bot_token) if slack.bot_token else None
        # One identity per persona in the harness (main + any agent personas) —
        # matches harness.personas. A persona without its own bot_token_env
        # posts through the main bot token.
        self.identities: dict[str, dict] = {}
        for key, persona in harness.personas.items():
            self.identities[key] = {
                "display": persona.display,
                "web": AsyncWebClient(token=persona.token) if persona.token else self.web,
                "user_id": None,
            }
        self.user_to_identity: dict[str, str] = {}
        self._seen_msg_ts: dict[str, float] = {}
        # msg ts -> guest channel, for a mention that arrived there (channel_of).
        self._guest_ts: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        slack = self.harness.slack
        return bool(slack and slack.bot_token and slack.app_token and slack.channel)

    # --- lifecycle -----------------------------------------------------------

    async def start(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            await stop.wait()
            return

        auth = await self.web.auth_test()
        # Slack answered, so a human can now reach us — and only now may a human's
        # decision be written to the ledger. See ledger.append_ledger.
        from ..process.ledger import mark_decisions_live

        mark_decisions_live(self.harness)
        self.bot_user_id = auth["user_id"]
        main_key = self.harness.main_persona.key
        if main_key in self.identities:
            self.identities[main_key]["user_id"] = self.bot_user_id
        self.user_to_identity[self.bot_user_id] = main_key
        await self._join_home_channel()
        for guest in self.harness.slack.guest_channels:
            await self._join_channel(guest)
        for key, ident in self.identities.items():
            if key == main_key:
                continue
            if not self.harness.personas[key].token:
                # Posts through the main bot under its own name (PostingMixin._face). It
                # has no Slack user of its own: auth_test on the shared client would
                # return MAIN's id and map it to this persona, so every message the main
                # bot posts would read as this persona's. Handoffs to it go through the
                # hand_off tool, not a mention.
                continue
            try:
                a = await ident["web"].auth_test()
                ident["user_id"] = a["user_id"]
                self.user_to_identity[a["user_id"]] = key
            except Exception as exc:  # noqa: BLE001 — a bad sub-persona token must not block startup
                self.harness.log(f"persona {key} auth failed (token missing/bad?): {str(exc)[:120]}")

        self.socket = SocketModeClient(app_token=self.harness.slack.app_token, web_client=self.web)
        self.socket.socket_mode_request_listeners.append(self._on_request)
        await self.socket.connect()
        self.ready.set()
        slack = self.harness.slack
        approver = ("set" if slack.approvers or slack.co_approver
                    else f"NOT SET ({slack.approver_env}) — every approval click is refused")
        self.harness.log(f"slack listener connected on {slack.channel}, approver {approver}")
        driver = getattr(slack, "driver", None)
        if driver and driver in (*slack.approvers, slack.co_approver):
            self.harness.log(
                f"⚠️  {slack.driver_env} names an ID that is also an approver. The driver "
                "bot never approves, so that ID's decisions are refused; use two IDs.")
        if set(getattr(slack, "guest_bots", ())) & {*slack.approvers, slack.co_approver}:
            self.harness.log(
                f"⚠️  {slack.guest_bots_env} names an ID that is also an approver. A guest "
                "bot never approves, so that ID's decisions are refused; use two IDs.")
        if "process" in self.state:
            self.state["process"]["channel"] = f"slack: connected on {slack.channel}"
            self.save_state()
        resume = asyncio.create_task(self.resume_after_restart())

        await stop.wait()
        resume.cancel()
        await self.socket.disconnect()
        # disconnect() only closes the active websocket session; the
        # ClientSession the SocketModeClient constructor opened for itself
        # (self.socket.aiohttp_client_session) needs its own close() call, or
        # asyncio warns "Unclosed client session" on process exit every time.
        await self.socket.close()
        self.harness.log("slack listener stopped")

    async def _join_home_channel(self) -> None:
        """Enter the dedicated channel without an invite (channels:join). Already a
        member is fine; a private channel cannot be joined this way and needs
        `/invite @<bot>` once, which is what the log line says."""
        await self._join_channel(self.harness.slack.channel)

    async def _join_channel(self, channel: str) -> None:
        try:
            await self.web.conversations_join(channel=channel)
        except Exception as exc:  # noqa: BLE001 — a failed join must not stop the listener
            code = ""
            response = getattr(exc, "response", None)
            if response is not None:
                code = response.get("error", "") or ""
            hint = ("invite it once with /invite in that channel"
                    if code in ("method_not_supported_for_channel_type", "missing_scope")
                    else "check the channel id")
            self.harness.log(f"slack: could not join {channel} "
                             f"({code or exc!r}) — {hint}")

    # --- socket-mode dispatch --------------------------------------------------

    def _already_handled(self, msg_ts: str) -> bool:
        """Has some earlier delivery of this message already been ACTED ON?

        One human message can arrive as BOTH app_mention and message.channels in a
        nondeterministic order, and Slack retries
        events at-least-once. Dedup is on ts.

        Checking and recording are separate on purpose. They used to be one call, made
        before the dispatch decision — so when message.channels arrived first for a
        mention in an untracked thread, it consumed the ts, did nothing (it is not an
        app_mention, and there is no thread to continue), and the real app_mention that
        followed was dropped as a duplicate. The mention was answered or ignored
        depending on which of two events Slack happened to deliver first. Found live:
        a direct mention in the bot channel got no reaction, no reply,
        and no log line.

        Record only when a delivery actually dispatches. A delivery this listener has
        no use for must leave the ts free for its sibling."""
        return msg_ts in self._seen_msg_ts

    def _mark_handled(self, msg_ts: str) -> None:
        now_mono = time.monotonic()
        self._seen_msg_ts[msg_ts] = now_mono
        self._seen_msg_ts = {
            k: v for k, v in self._seen_msg_ts.items() if now_mono - v < SEEN_TS_TTL_S
        }

    def _unmark_handled(self, msg_ts: str) -> None:
        """Give the ts back, for a delivery that marked it and then did not dispatch.

        Only the approver branch needs this: it must mark BEFORE awaiting
        on_approver_message, and that call turns out to be a no-op for ordinary prose.
        Safe because on_approver_message reaches every `return False` before its first
        await, so no sibling delivery can have run inside the mark."""
        self._seen_msg_ts.pop(msg_ts, None)

    def _is_co_approver_ask(self, author: str, mentioned: list[str]) -> bool:
        """Whether an external bot's message is the ONE we listen to: the configured
        co_approver, explicitly @-mentioning one of this harness's own identities."""
        co = self.harness.slack.co_approver
        return bool(co and author == co and mentioned)

    async def _dispatch_driver(
        self, thread_ts: str, text: str, event: dict, msg_ts: str, mentioned: list[str]
    ) -> None:
        """A message from the trusted driver bot (SlackConfig.driver_env).

        Answered like a person's mention: the ordinary mention turn, with the same
        tools and the same approval gates. What differs from a person is everything
        that would let a bot keep itself going:

          - it must @-mention one of our identities every time, in a tracked thread
            too. A bot's message arrives as `message`, never `app_mention` (see the
            module docstring), so finding our user id in the text is the only proof
            it was talking to us.
          - it spends the thread's bot-hop budget instead of resetting it. Past
            MAX_BOT_HOPS the thread gets the stop notice once and waits for a person.
          - it is never an approver (SlackConfig.is_approver), so it cannot answer the
            approval its own request raised.
          - it is answered here even when escalate_human_mentions hands people to the
            face: that rule is about who a person talks to, and a probe is asking
            whether THIS harness answers.

        A guest bot (SlackConfig.guest_bots_env) gets exactly this path. Reached from
        the home channel and from a guest channel (_dispatch_guest); _on_request returns
        for a relay channel before this runs.
        """
        if not mentioned:
            return
        meta = self.state["threads"].get(thread_ts) or {}
        if meta.get("bot_hops", 0) >= MAX_BOT_HOPS:
            if not meta.get("hop_limit_notified"):
                meta["hop_limit_notified"] = True
                self._mark_handled(msg_ts)
                self.save_state()
                ph = self.harness.approval_phrases
                addressee = self.harness.slack.addressee
                await self.post(
                    ph.hop_limit.format(
                        n=MAX_BOT_HOPS,
                        who=f"<@{addressee}>" if addressee else ph.hop_limit_human),
                    thread_ts=thread_ts,
                )
            return
        self._mark_handled(msg_ts)
        await self._handle_mention(thread_ts, text, event, mentioned[0], bot_hop=True)

    async def _dispatch_human(
        self, thread_ts: str, text: str, event: dict, etype: str, msg_ts: str,
        author: str, mentioned: list[str],
    ) -> None:
        """Human-authored message in the dedicated channel: a fresh @mention or a
        follow-up in a thread we track. A person speaking resets the bot-to-bot hop
        counter — the harness's full attention, every time."""
        threads = self.state["threads"]
        if thread_ts in threads:
            threads[thread_ts].pop("bot_hops", None)
            threads[thread_ts].pop("hop_limit_notified", None)
            # Any parked handoff dies with the budget: its button is stale from here, and
            # releasing it later would replay a turn the person has already overtaken.
            threads[thread_ts].pop("pending_hop", None)
        # A plain message counts only when it addresses nobody else: "<@colleague> 봐줘"
        # is a person talking to a person, and the bot stays out of it.
        plain_channel = (self.harness.slack.respond_to_channel_messages
                         and etype == "message" and bool(author) and bool(text)
                         and (bool(mentioned) or not MENTION_RE.search(text)))
        if etype == "app_mention" or thread_ts in threads or plain_channel:
            target = (mentioned[0] if mentioned else
                      threads.get(thread_ts, {}).get("persona") or self.harness.main_persona.key)
            self._mark_handled(msg_ts)
            # A harness with no human-facing interface hands every human
            # conversation to the single face (the co_approver bot) rather than
            # answering as one of its own personas. Approvals never reach here —
            # the approver's clicks/decisions are consumed by _on_request
            # (is_approver) before dispatch — so this only ever escalates a genuine
            # question/instruction.
            if self.harness.slack.escalate_to:
                await self._escalate_mention(thread_ts, text, event)
            else:
                await self._handle_mention(thread_ts, text, event, target)
        # else: this delivery is not actionable (a plain channel message in a thread
        # we don't follow). Leave msg_ts unmarked — its app_mention sibling may still
        # be in flight, and it is the one that matters.

    def _channel_kind(self, channel: str | None) -> str | None:
        """"home", "guest", "relay", or None for a channel that is none of ours.

        Home wins over both listings of the same channel, so a dedicated channel that
        is also listed as a relay does not bounce every mention through the relay path.
        A guest listing wins over a relay listing, because it is the wider grant."""
        slack = self.harness.slack
        if channel == slack.channel:
            return "home"
        if channel in getattr(slack, "guest_channels", ()):
            return "guest"
        if channel in slack.relay_channels:
            return "relay"
        return None

    def _is_trusted_bot(self, author: str, *, simulated: bool = False) -> bool:
        """The driver or a guest bot (SlackConfig.trusted_bots), posting as itself.
        One of our own identities is never one, and neither is a simulated person."""
        slack = self.harness.slack
        trusted = getattr(slack, "trusted_bots", None)
        if trusted is None:  # a stand-in config that only knows the driver
            trusted = (getattr(slack, "driver", None),)
        return (bool(author) and not simulated and author in trusted
                and author not in self.user_to_identity)

    async def _dispatch_guest(
        self, channel: str, thread_ts: str, text: str, event: dict, msg_ts: str,
        author: str, author_identity: str | None, mentioned: list[str],
        *, is_bot: bool, simulated: bool,
    ) -> None:
        """A message in a guest channel (SlackConfig.guest_channels_env).

        The channel belongs to other people and usually to another bot, so the harness
        is a guest in it: it answers when it is @-mentioned, in that thread, and is
        otherwise silent. What differs from the home channel:

          - a mention is required EVERY time, from a person too, in a thread we already
            answered in as well. The other party in such a thread is often another bot
            talking to the same person, and answering every follow-up would have two
            bots replying to one message.
          - a person's message in a thread of ours, without a mention, is not answered;
            it only resets the thread's bot-hop budget, as a person speaking does at home.
          - the only foreign bots heard are the driver and the guest bots, under the
            driver's rules (_dispatch_driver). Our own identities are not routed here:
            there are no bot-to-bot handoffs in someone else's channel.
          - an approver's typed decision counts only in a thread we answered in here.
            Their other messages in the channel are not ours to read.

        The mention turn, its tools and its approval gate are the home channel's. The
        thread record gets the channel (_handle_mention), and every post, reaction and
        approval card for the thread follows it (PostingMixin.channel_of).
        """
        slack = self.harness.slack
        meta = self.state["threads"].get(thread_ts) or {}
        ours = meta.get("channel") == channel
        if ours and not simulated and slack.is_approver(author):
            self._mark_handled(msg_ts)  # before the await, as in _on_request
            if await self.on_approver_message(thread_ts, text, event):
                return
            self._unmark_handled(msg_ts)
        if author_identity is not None:
            return
        trusted_bot = self._is_trusted_bot(author, simulated=simulated)
        if is_bot and not trusted_bot:
            return
        if not mentioned:
            if ours and not trusted_bot and any(
                    k in meta for k in ("bot_hops", "hop_limit_notified", "pending_hop")):
                for key in ("bot_hops", "hop_limit_notified", "pending_hop"):
                    meta.pop(key, None)
                self.save_state()
            return
        remember_channel(self, msg_ts, channel)
        if trusted_bot:
            await self._dispatch_driver(thread_ts, text, event, msg_ts, mentioned)
        else:
            # Either delivery of a person's mention may arrive first (app_mention or
            # message); our id in the text is the proof, so both are treated as one.
            await self._dispatch_human(
                thread_ts, text, event, "app_mention", msg_ts, author, mentioned)

    async def _dispatch_bot_hop(
        self, thread_ts: str, text: str, event: dict, author_identity: str, mentioned: list[str]
    ) -> None:
        """Bot-authored message: only THIS harness's own identities talking to a
        DIFFERENT bot counts. Each thread gets at most MAX_BOT_HOPS
        dispatches until a human speaks again."""
        threads = self.state["threads"]
        targets = [m for m in mentioned if m != author_identity]
        meta = threads.get(thread_ts)
        if not targets or meta is None:
            return
        hops = meta.get("bot_hops", 0)
        if hops >= MAX_BOT_HOPS:
            if not meta.get("hop_limit_notified"):
                meta["hop_limit_notified"] = True
                ph = self.harness.approval_phrases
                if self.harness.slack.addressee:
                    # Park the handoff and ask. The bot that tried to pass the work has
                    # already finished its turn believing it did — see post_hop_buttons.
                    meta["pending_hop"] = {
                        "from": author_identity,
                        "to": targets[0],
                        "text": text,
                        "ts": event.get("ts", ""),
                        "user": event.get("user", ""),
                    }
                    self.save_state()
                    # Strip the raw <@id> tokens out of the quoted line. The ask is posted
                    # by the main bot, so a mention left in it comes straight back as one
                    # more bot-authored mention of a different identity — this same path.
                    # The hop_limit_notified flag above already swallows that echo, and
                    # not emitting it at all is one less thing depending on that order.
                    # Who was handing to whom is in the ask's own line, not in the quote.
                    quoted = MENTION_RE.sub("", text).strip()
                    await self.post_hop_buttons(
                        thread_ts, author_identity, targets[0], quoted[:300], MAX_BOT_HOPS
                    )
                else:
                    # Nobody configured to ask. Say it stopped and leave it there.
                    self.save_state()
                    await self.post(
                        ph.hop_limit.format(n=MAX_BOT_HOPS, who=ph.hop_limit_human),
                        thread_ts=thread_ts,
                    )
            return
        meta["bot_hops"] = hops + 1
        self.save_state()
        # The hop that spends the budget is told so; no earlier one is (rules.LAST_HOP_NOTE).
        note = ""
        if hops + 1 == MAX_BOT_HOPS:
            note = LAST_HOP_NOTE if self.harness.slack.addressee else LAST_HOP_NOTE_UNATTENDED
        await self._handle_mention(thread_ts, text, event, targets[0], note=note)

    async def _on_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type == "interactive":
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            await self._on_interactive(req.payload)
            return
        if req.type != "events_api":
            return
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        event = req.payload.get("event", {})
        etype = event.get("type")
        channel = event.get("channel")
        # The dedicated-channel guard: None is a channel that is not ours, not a guest
        # and not a relay channel.
        kind = self._channel_kind(channel)
        if kind is None or etype not in ("app_mention", "message") or event.get("subtype"):
            return
        is_guest_channel, is_relay_channel = kind == "guest", kind == "relay"
        normalized = normalize_message(self, event)
        if normalized is None:
            return
        event, simulated = normalized
        msg_ts = event.get("ts", "")
        if self._already_handled(msg_ts):
            return

        if is_relay_channel:
            # A relay channel (e.g. a sibling harness's own channel) never gets
            # tailed for plain chatter — only a direct mention wakes us up, whether
            # that mention came from a human or one of ITS bots. Always relays into
            # the dedicated channel; see _handle_relay_mention's docstring for why
            # it never replies here. Our own messages there (the relayed approve/deny
            # text) can never re-enter: they're `message`, not `app_mention`.
            if simulated or etype != "app_mention" or event.get("user") == self.bot_user_id:
                return
            self._mark_handled(msg_ts)
            await self._handle_relay_mention(
                channel,
                event.get("thread_ts") or event.get("ts"),
                (event.get("text") or "").strip(),
                event,
            )
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        text = (event.get("text") or "").strip()
        author = event.get("user", "")
        author_identity = self.user_to_identity.get(author)
        is_bot = not simulated and (bool(event.get("bot_id")) or author_identity is not None)

        # Which bot identities does this message @-mention? (order preserved)
        mentioned = [
            self.user_to_identity[u] for u in MENTION_RE.findall(text) if u in self.user_to_identity
        ]

        if is_guest_channel:
            await self._dispatch_guest(
                channel, thread_ts, text, event, msg_ts, author, author_identity, mentioned,
                is_bot=is_bot, simulated=simulated)
            return

        # An approver's DECISION, delivered as a message rather than a button click.
        # This must run before the human/bot split below: the co_approver this harness
        # addresses every ask to is a relay BOT, which (a) cannot click a Block Kit
        # button — Slack only generates those from real UI interactions — and (b) is
        # not one of our own personas, so the `author_identity is None` drop further
        # down would discard its answer unread. Approval was unreachable end-to-end
        # until this hook existed. Ordinary prose from the approver
        # returns False and falls through to normal routing.
        if not simulated and self.harness.slack.is_approver(author):
            # Mark BEFORE the await: on_approver_message resumes the thread's persona,
            # which takes seconds, and the sibling delivery of this same message lands
            # meanwhile. Marking after would let it be handled twice.
            self._mark_handled(msg_ts)
            if await self.on_approver_message(thread_ts, text, event):
                return
            # Ordinary prose. It used to STAY marked and "fall through to the routing
            # below on THIS delivery" — but this delivery may be the useless one. When
            # message.channels arrives first for a mention that opens a NEW thread, the
            # routing below matches neither `app_mention` nor a tracked thread, returns
            # having dispatched nothing, and the real app_mention behind it is dropped as
            # a duplicate. That is _already_handled's own documented bug, reintroduced
            # here for the one author it matters most for: the approver.
            #
            # So give the ts back. Nothing awaited inside the mark (on_approver_message
            # reaches every `return False` before its first await), and whichever delivery
            # actually dispatches will mark it again.
            self._unmark_handled(msg_ts)

        # The driver is recognised by its ID, before the human/bot split: whether or not
        # Slack marked the message as a bot's, it gets the driver's rules and never a
        # person's (which would reset the hop budget it is meant to spend). One of our
        # own identities is never the driver.
        # A guest bot (SlackConfig.guest_bots_env) is recognised the same way.
        if self._is_trusted_bot(author, simulated=simulated):
            await self._dispatch_driver(thread_ts, text, event, msg_ts, mentioned)
            return

        if not is_bot:
            # Bot-authored mentions arrive as message events, never app_mention.
            human_type = "app_mention" if simulated and mentioned else etype
            await self._dispatch_human(thread_ts, text, event, human_type, msg_ts, author, mentioned)
            return

        if author_identity is None:
            # An external bot, whose say-so we do not act on — with ONE exception: the
            # co_approver. We already honour its word for APPROVALS, which is a strictly
            # larger grant than answering a question, so refusing to hear it ask one is
            # inconsistent rather than safe.
            #
            # It lets the co_approver check this harness is alive by CONVERSATION instead
            # of by inspecting a filesystem path. A path-based check can name a harness
            # directory that has since been retired and report a live harness dead
            # while it is answering in that very channel; whoever replies to a mention
            # IS the live one.
            #
            # Still narrow: exactly one bot id, resolved from co_approver, and it lands in
            # the ordinary mention path — same tools, same gates, same hop accounting.
            if not self._is_co_approver_ask(author, mentioned):
                return
            self._mark_handled(msg_ts)
            await self._handle_mention(thread_ts, text, event, mentioned[0])
            return
        self._mark_handled(msg_ts)
        await self._dispatch_bot_hop(thread_ts, text, event, author_identity, mentioned)
