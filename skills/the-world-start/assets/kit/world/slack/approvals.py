"""Approval buttons and Block Kit click handling — the enforcement half of the
harness's approval doctrine (world/slack/rules.py is the asking half).

Split out of listener.py. Mixed into SlackListener, so `self` here
is the listener: `self.web`, `self.post`, `self.state`, `self.harness` all come
from there.

Two invariants worth keeping straight, because they are deliberately asymmetric:
  ASK   — SlackConfig.addressee. One addressee: the co_approver (a relay/proxy
          bot) when configured, else the primary approver. Every ping goes there.
  ACCEPT — SlackConfig.is_approver. EITHER identity's click counts, whichever one
          we happened to address. Broader than the ask on purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .approval_text import decision_from_text, relay_decision
from .rules import LAST_HOP_NOTE
from ..process import approval_scope
from ..process.ledger import LedgerFabricationError, append_ledger

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient

    from ..harness import Harness

MAX_PENDING_APPROVALS = 50
# task.new_task_id's shape. An ask button's task id arrives from Slack untrusted and is
# only quoted into the prompt when it looks like one.
_TASK_ID = re.compile(r"\d{8}-[0-9a-f]{4}")


class ApprovalMixin:
    # --- attributes supplied by SlackListener (declared for readers, not set here)
    harness: "Harness"
    state: dict
    web: "AsyncWebClient | None"

    async def _record_decision(self, thread_ts: str, entry: dict) -> None:
        """Write one human decision to the ledger, and never lose the decision if it can't.

        `append_ledger` refuses when `_decisions_live` is unset — a real guard, and by the
        time either caller here runs, a Slack event has already been delivered over a
        connected socket, so the flag can only be unset because the harness is mis-wired.
        Unhandled, that RuntimeError lands mid-handler: the grant is already saved by the
        line above, and the relay below never posts. The approver's answer is applied and
        never delivered — the requesting harness waits forever on a question that was
        answered.

        The guard's job is to stop the WRITE, and it did. It is not to discard the click.
        So: record what we could, say plainly what we could not, and let the decision land.
        The complaint goes to the thread because a silent audit gap is the failure mode the
        ledger exists to prevent.
        """
        try:
            append_ledger(self.harness, entry)
        except LedgerFabricationError as exc:
            self.harness.log(f"ledger refused a real decision ({entry.get('kind')}): {exc}")
            await self.post(
                "⚠️ 결정은 반영됐지만 감사 기록(ledger.jsonl)에 남기지 못했습니다 — "
                "이 하네스의 Slack 리스너가 연결된 것으로 표시되어 있지 않습니다. "
                "감사 기록에 구멍이 생겼으니 하네스 배선을 확인해 주세요.",
                thread_ts=thread_ts,
            )

    async def post_approval_buttons(
        self,
        thread_ts: str,
        summary: str,
        requester: str = "main",
        *,
        reply_channel: str | None = None,
        reply_thread_ts: str | None = None,
    ) -> None:
        """APPROVAL_REQUEST marker -> approve/deny buttons, approver-gated on click.
        Every string here comes from Harness.approval_phrases (Korean by default).
        Always posted BY the main identity (chat_update can only edit the
        poster's own message); `requester` rides in the button value so the
        decision can be routed back to the right identity later.

        `reply_channel`/`reply_thread_ts` override only WHERE the decision TEXT is
        relayed once clicked — default is this same thread in the dedicated
        channel; a cross-channel relay (_handle_relay_mention) points them at the
        origin channel's thread so the origin sees the decision too. They never
        move the approval GRANT: _on_interactive always keys that off the button
        message's own thread, which is always in the dedicated channel and is the
        thread whose gated write tools the grant unlocks."""
        if not self.web:
            return
        req_name = self.identities.get(requester, {}).get("display", requester)
        slack = self.harness.slack
        value = f"{reply_channel or slack.channel}:{reply_thread_ts or thread_ts}|{requester}"
        main_key = self.harness.main_persona.key
        # SIM108 wants a ternary here. The ternary has nowhere to put these two comments,
        # and the routing rule they record is the whole reason this branch exists.
        if requester == main_key:  # noqa: SIM108
            # The main persona is the harness's sole point of contact with the
            # approver — its own approval asks go straight there.
            addressee = slack.addressee
        else:
            # Everyone else routes through the main persona first, instead of
            # independently pinging the approver — single hub, matches the roster
            # hand-off convention (found live: every persona's approval
            # ask pinged the approver directly, so the hub was never actually
            # looped into sub-persona threads).
            addressee = self.identities.get(main_key, {}).get("user_id")
        mention = f"<@{addressee}> " if addressee else ""
        # Name the files in the ask. The fingerprint is what the code compares; a human
        # cannot review a hash, and "approve this" with nothing but a sentence from the
        # agent is the same unverifiable promise everywhere else in this kit refuses.
        _hash, scope_files = approval_scope.fingerprint(self.thread_worktree(thread_ts))
        scope_line = approval_scope.describe(scope_files)
        if scope_line:
            summary = f"{summary}\n{scope_line}"
        ph = self.harness.approval_phrases
        resp = await self.web.chat_postMessage(
            channel=slack.channel,
            thread_ts=thread_ts,
            text=ph.request_fallback.format(mention=mention, name=req_name, summary=summary),
            blocks=[
                {"type": "section",
                 "text": {"type": "mrkdwn",
                          "text": ph.request_block.format(mention=mention, name=req_name, summary=summary)}},
                {"type": "actions", "elements": [
                    {"type": "button", "style": "primary", "action_id": "world_approve",
                     "text": {"type": "plain_text", "text": ph.approve_button}, "value": value},
                    {"type": "button", "style": "danger", "action_id": "world_deny",
                     "text": {"type": "plain_text", "text": ph.reject_button}, "value": value},
                ]},
            ],
        )
        # Cross-thread dedup registry: every live button lands here, every thread turn
        # reads it back via mentions._build_prompt (state.pending_approvals_snippet),
        # approve/deny removes it.
        self.state.setdefault("pending_approvals", []).append({
            "msg_ts": resp.get("ts", ""),
            "thread_ts": thread_ts,
            "requester": requester,
            "summary": summary[:300],
            "ts": time.time(),
        })
        self.state["pending_approvals"] = self.state["pending_approvals"][-MAX_PENDING_APPROVALS:]
        self.save_state()

    async def on_approver_message(self, thread_ts: str, text: str, event: dict) -> bool:
        """A message authored by an approver. Returns True if it WAS a decision (and
        has been fully handled), False if it's ordinary prose the caller should keep
        routing normally.

        This is the only approval path a bot approver has — see the module header.
        The caller must invoke this BEFORE its foreign-bot drop, or a relay bot's
        approval is discarded before it is ever read (found live:
        listener.py returned early on `author_identity is None`, so the co_approver
        this harness addresses every ask to could never actually answer).

        A grant names the ask it answers. The button path reads that off the pending
        entry it drops (_on_interactive, keyed by the button's own msg_ts). This path
        cannot: a relay signature carries the RELAYING harness's button ts, which is
        meaningless in our own pending list. So it answers the oldest open ask in the
        thread, and refuses to grant when there is none.
        """
        # A bot approver relaying a human's click is believed only for its
        # signature; a human is believed for their words. `bot_id` is set by Slack on
        # every message an app posted — it is not something the message body can claim.
        decision = decision_from_text(
            text,
            author_is_bot=bool(event.get("bot_id")),
            thread_ts=thread_ts,
            seen_nonces=self.state.setdefault("relay_nonces", []),
        )
        if decision is None:
            return False
        meta = self.state["threads"].get(thread_ts)
        if meta is None:
            return False  # a decision in a thread the harness isn't tracking — nothing to resume

        author = event.get("user", "")
        approved = decision == "approve"
        pending = self.state.get("pending_approvals", [])
        here = sorted(
            (p for p in pending if p.get("thread_ts") == thread_ts),
            key=lambda p: p.get("ts", 0.0),
        )

        # An approval with nothing to approve is not an approval. This used to write the
        # literal summary "(message approval)" without ever looking at pending_approvals,
        # so 승인 typed into a thread that had ASKED FOR NOTHING opened a single-use write
        # grant that no record could explain.
        #
        # Deny is deliberately NOT gated this way: revoking a grant that should never have
        # existed is the one repair an approver must always be able to make, and a thread
        # with no pending ask is exactly where a bad grant hides.
        if approved and not here:
            await self.post(
                self.harness.approval_phrases.nothing_pending,
                thread_ts=thread_ts,
            )
            self.harness.log(f"approval message in thread {thread_ts} answered no pending ask — ignored")
            return True

        grants = self.state.setdefault("approved_threads", {})
        if approved:
            # One 승인 answers one ask: the oldest still open. This used to drop EVERY
            # pending entry in the thread while opening a single (single-use) grant, so a
            # thread holding two asks silently lost one — the approver answered once and
            # both questions disappeared.
            answered = here[0]
            grants[thread_ts] = {
                "by": author,
                "at": time.time(),
                "summary": answered.get("summary", "")[:300],
                "ask_msg_ts": answered.get("msg_ts", ""),
            }
            self.state["pending_approvals"] = [p for p in pending if p is not answered]
        else:
            # A denial closes the whole thread's asks, and clears any grant — including
            # one this thread never legitimately earned.
            grants.pop(thread_ts, None)
            self.state["pending_approvals"] = [
                p for p in pending if p.get("thread_ts") != thread_ts
            ]
        # An approver speaking resets the bot-to-bot hop budget, same as any human. The
        # stored handoff goes too: its button is now stale, and releasing it later would
        # replay a turn the conversation has already moved past.
        meta.pop("bot_hops", None)
        meta.pop("pending_hop", None)
        meta.pop("hop_limit_notified", None)
        self.save_state()

        answered_summary = here[0].get("summary", "") if approved else ""
        await self._record_decision(thread_ts, {
            "kind": "message", "decision": "approved" if approved else "denied",
            "by": author,
            # Tag WHO decided — a human, or a co-approver bot
            # relaying a signed decision. A bot message carries `bot_id`; the message body
            # cannot claim it. This supports human-vs-co-approver agreement metrics.
            "by_kind": "co_approver" if event.get("bot_id") else "human",
            "thread_ts": thread_ts,
            "requester": (here[0].get("requester") if approved else None) or meta.get("persona", "?"),
            "summary": (answered_summary or text)[:300],
        })
        if approved:
            remaining = len(here) - 1
            ph = self.harness.approval_phrases
            tail = ph.approved_tail.format(n=remaining) if remaining else ""
            note = f": {answered_summary[:120]}" if answered_summary else ""
            await self.post(ph.approved_msg.format(who=author, note=note, tail=tail), thread_ts=thread_ts)
        else:
            ph = self.harness.approval_phrases
            tail = ph.rejected_tail.format(n=len(here)) if here else ""
            await self.post(ph.rejected_msg.format(who=author, tail=tail), thread_ts=thread_ts)
        # Resume the REQUESTER persona so it can finish the job it was blocked on — the
        # persona that asked for the grant, which may differ from the thread's own author
        # (one persona authors a finding, another executes the write). Resuming the
        # read-only author instead would wake a persona that can never spend the write
        # grant, so the approved commit/PR would never happen. In a single-persona thread
        # requester == meta["persona"], so this is a no-op there. On a denial there is no
        # requester to resume and no grant, so its write tool stays refused — it can only
        # reply, which is exactly the intended behavior.
        resume_persona = (here[0].get("requester") if approved else None) or meta.get("persona")
        await self._handle_mention(thread_ts, text, event, resume_persona)
        return True

    @property
    def _refusals(self) -> dict:
        """thread_ts -> why the last gate check refused. Per-listener, not class-level:
        a dict on the class would be shared by every harness in the process, and a test
        harness leaking a stale-grant message into another one is exactly the kind of
        cross-contamination this cache is too small to be worth."""
        cache = getattr(self, "_refusal_cache", None)
        if cache is None:
            cache = {}
            self._refusal_cache = cache
        return cache

    def thread_worktree(self, thread_ts: str) -> "Path | None":
        """The worktree this thread's agent writes in, from state["worktrees"].

        Looked up rather than passed in: the worktree is cut by the harness project's
        own mention_tools factory, so the kit never receives the path directly. The
        reverse index is already maintained here, so this stays kit-owned.
        """
        for path, meta in self.state.get("worktrees", {}).items():
            if meta.get("thread_ts") == thread_ts:
                return Path(path)
        return None

    def thread_approved(self, thread_ts: str) -> bool:
        """Has the approver granted an approval that still covers what is about to run?

        Two questions now, not one. A grant exists, AND the worktree it was granted
        against still hashes the same. The second question is the whole point: the
        approver said yes to a specific set of changed files, and if those changed
        afterwards then what is about to run is not what was approved.

        A stale grant is DROPPED, not merely reported. Leaving it in place would let the
        next call — after the files settle back or after another edit — find a grant the
        approver never gave for that content. Re-asking costs one click; a silently
        widened approval costs whatever the tool does.
        """
        grant = self.state.get("approved_threads", {}).get(thread_ts)
        if grant is None:
            self._refusals.pop(thread_ts, None)
            return False

        approved_hash = grant.get("scope_hash", "")
        if not approved_hash:
            # Granted before this check existed, or granted with no worktree to pin.
            # Behaves exactly as it did before — an unpinned grant is not a stale one.
            return True

        current, _files = approval_scope.fingerprint(self.thread_worktree(thread_ts))
        if current == approved_hash:
            return True

        self.state.get("approved_threads", {}).pop(thread_ts, None)
        self._refusals[thread_ts] = (
            "the approved files changed after the approval, so that grant no longer "
            "covers what you are about to do"
        )
        self.harness.log(
            f"approval for thread {thread_ts} went stale (worktree changed after the "
            f"grant) — re-asking is required"
        )
        self.save_state()
        return False

    def last_refusal(self, thread_ts: str) -> str:
        """Why the most recent gate check on this thread said no. Consumed on read so a
        stale-grant explanation cannot outlive the refusal it describes."""
        return self._refusals.pop(thread_ts, "")

    def consume_thread_approval(self, thread_ts: str) -> None:
        """Spend the grant. Called only after a gated tool's underlying action
        SUCCEEDS — a failed call leaves the grant intact so the agent can retry
        without bothering the approver again."""
        if self.state.get("approved_threads", {}).pop(thread_ts, None) is not None:
            self.save_state()

    async def ask_worktree_cleanup(self, thread_ts: str, path: str, branch: str) -> None:
        """Ask before destroying an idle worktree that still holds unmerged work.

        Deleting a branch someone may still want is exactly the kind of irreversible,
        unattended act this harness asks about, so it is a button rather than a timer.
        The timer is the fallback, not the decision (see worktrees.sweep)."""
        if not self.web:
            return
        await self.web.chat_postMessage(
            channel=self.harness.slack.channel,
            thread_ts=thread_ts,
            text=f"워크트리 정리 확인: {path}",  # notification fallback
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": (
                    f"🧹 이 스레드의 워크트리 `{path}`(브랜치 `{branch}`)에 머지 안 된 작업이 "
                    "있는데 7일째 조용하네요. 정리할까요? (3일 무응답이면 삭제됩니다)")}},
                {"type": "actions", "elements": [
                    {"type": "button", "action_id": "world_wt_keep",
                     "text": {"type": "plain_text", "text": "🌿 보존"}, "value": path},
                    {"type": "button", "style": "danger", "action_id": "world_wt_remove",
                     "text": {"type": "plain_text", "text": "🧹 지금 삭제"}, "value": path},
                ]},
            ],
        )

    async def _on_worktree_button(self, action_id: str, path: str, resolve_buttons) -> None:
        """Keep or remove the worktree a cleanup ask was posted for.

        `path` arrives from a button value, i.e. from Slack, i.e. untrusted — so it is
        never touched as a filesystem path. It is only ever a KEY into state["worktrees"],
        which this process wrote. A path we did not create resolves to nothing and gets
        told so, rather than being handed to `git worktree remove`."""
        from ..process import worktrees

        wt = self.state.get("worktrees", {}).get(path)
        if not wt:
            await resolve_buttons(f"⚠️ 워크트리 `{path}`는 이미 상태에서 사라졌어요.")
            return

        if action_id == "world_wt_keep":
            wt.pop("confirm_requested_at", None)
            # Refresh activity so the idle clock restarts from now, not from the last
            # message — otherwise "keep" would be re-asked on the very next sweep.
            self.state["threads"].setdefault(wt.get("thread_ts", ""), {})[
                "last_message_ts"] = time.time()
            await resolve_buttons(f"🌿 보존 — `{path}` (idle 타이머 리셋)")
        else:
            err = await asyncio.to_thread(
                worktrees.remove_worktree, self.harness, path, wt["branch"], True)
            if err:
                await resolve_buttons(f"⚠️ 삭제 실패: {err}")
            else:
                self.state["worktrees"].pop(path, None)
                await resolve_buttons(f"🧹 삭제됨 — `{path}` (브랜치 `{wt['branch']}`)")
        self.save_state()

    async def _resolve_single_buttons(self, msg: dict, msg_ts: str, result_line: str) -> None:
        """Strip the buttons off a SINGLE-item announcement after its decision, leaving an
        audit line — so a decided finding can't be clicked again. No-op on a BATCH (>1
        actions block): a finding batch carries one actions block per finding, and one
        dismiss click must not take the other findings' buttons with it. This is why the
        batch-aware guard exists and plain resolve_buttons (which strips every actions
        block) isn't reused here."""
        blocks = msg.get("blocks", [])
        if sum(1 for b in blocks if b.get("type") == "actions") > 1:
            return
        kept = [b for b in blocks if b.get("type") != "actions"]
        kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": result_line}]})
        with contextlib.suppress(Exception):
            await self.web.chat_update(
                channel=self.harness.slack.channel, ts=msg_ts, text=result_line, blocks=kept
            )

    async def post_hop_buttons(
        self, thread_ts: str, from_identity: str, to_identity: str, summary: str, n: int
    ) -> None:
        """A bot handoff hit the per-thread cap: ask the approver whether it may go through.

        Why an ask and not the plain stop notice it replaces: the handing-off bot has
        already finished its turn and its last words were "@Other, this is yours". It
        cannot be told the handoff was dropped, and it does not think it failed. Without
        this, the only party who knows the chain broke is whoever reads the thread.

        Deliberately NOT registered in state["pending_approvals"]. That registry is the
        write-grant ask list, read back into every thread turn's prompt
        (mentions._build_prompt); a handoff release is not a write grant, and listing it
        there would have agents believe they have a pending permission to spend. The cost
        is that nothing reminds anyone this ask is outstanding.
        """
        if not self.web:
            return
        ids = self.identities
        from_name = ids.get(from_identity, {}).get("display", from_identity)
        to_name = ids.get(to_identity, {}).get("display", to_identity)
        addressee = self.harness.slack.addressee
        mention = f"<@{addressee}> " if addressee else ""
        ph = self.harness.approval_phrases
        text = ph.hop_request.format(
            n=n, mention=mention, from_name=from_name, to_name=to_name, summary=summary,
        )
        await self.web.chat_postMessage(
            channel=self.harness.slack.channel,
            thread_ts=thread_ts,
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "actions", "elements": [
                    {"type": "button", "style": "primary", "action_id": "world_hop_allow",
                     "text": {"type": "plain_text", "text": ph.hop_allow_button},
                     "value": thread_ts},
                    {"type": "button", "style": "danger", "action_id": "world_hop_stop",
                     "text": {"type": "plain_text", "text": ph.hop_stop_button},
                     "value": thread_ts},
                ]},
            ],
        )

    async def _on_hop_button(
        self, allow: bool, thread_ts: str, msg: dict, msg_ts: str, clicker: str
    ) -> None:
        """Approver's answer to a capped handoff (post_hop_buttons). Both outcomes reach
        the ledger, which is the only durable record of who tried to hand what to whom —
        state's thread entries are pruned after 14 days of quiet."""
        slack = self.harness.slack
        ph = self.harness.approval_phrases
        meta = self.state.get("threads", {}).get(thread_ts) or {}
        hop = meta.pop("pending_hop", None)
        if hop is None:
            # A human spoke after the ask went out, which reset the budget and cleared the
            # parked handoff. Re-dispatching from a stale button would replay an old turn.
            await self._resolve_single_buttons(msg, msg_ts, ph.hop_stale)
            return
        to_identity = hop.get("to", "")
        to_name = self.identities.get(to_identity, {}).get("display", to_identity)
        await self._resolve_single_buttons(
            msg, msg_ts,
            ph.hop_allowed.format(who=clicker, to_name=to_name) if allow
            else ph.hop_stopped.format(who=clicker),
        )
        if allow:
            # Stay over the cap: every further handoff in this thread asks again. Clearing
            # the notified flag is what re-arms the ask.
            meta.pop("hop_limit_notified", None)
        self.save_state()
        await self._record_decision(thread_ts, {
            "kind": "bot_hop", "decision": "allowed" if allow else "stopped",
            "by": clicker,
            "by_kind": "co_approver" if (slack.co_approver and clicker == slack.co_approver)
            else "human",
            "thread_ts": thread_ts,
            "requester": hop.get("from", "?"),
            "summary": f"{hop.get('from', '?')} → {to_identity}: {hop.get('text', '')[:200]}",
        })
        if allow:
            # Still over the cap, so this turn's own next handoff asks again: say so.
            await self._handle_mention(
                thread_ts, hop.get("text", ""),
                {"ts": hop.get("ts", ""), "user": hop.get("user", "")},
                to_identity, note=LAST_HOP_NOTE,
            )

    async def _on_ask_button(self, value: str, thread_ts: str, msg_ts: str, clicker: str) -> None:
        """Resolve an ask button's untrusted `agent|index|task` value against the agents
        registered in code and hand the declared prompt to that agent's persona, as if
        the clicker had mentioned it in this thread. Anything that does not resolve to
        a declared ask button is dropped without a reply: it was not rendered by us."""
        name, _, rest = value.partition("|")
        index, _, task_id = rest.partition("|")
        agent = next((a for a in self.harness.agents if a.name == name), None)
        if agent is None or not index.isdigit() or int(index) >= len(agent.buttons):
            return
        button = agent.buttons[int(index)]
        if button.kind != "ask":
            return
        task_ref = f" (task `{task_id}`)" if _TASK_ID.fullmatch(task_id) else ""
        text = f"[버튼: {button.label}]{task_ref} {button.prompt}"
        identity = (agent.persona or self.harness.main_persona).key
        await self._handle_mention(thread_ts, text, {"ts": msg_ts, "user": clicker}, identity)

    async def _on_interactive(self, payload: dict) -> None:
        """Block Kit button clicks. Approve/deny is the ONE decision that must stay
        with the approver — everyone else's clicks are politely rejected."""
        if payload.get("type") != "block_actions" or not payload.get("actions"):
            return
        action = payload["actions"][0]
        action_id = action.get("action_id", "")
        value = action.get("value", "")
        clicker = payload.get("user", {}).get("id", "")
        msg = payload.get("message", {})
        msg_ts = msg.get("ts", "")
        thread_ts = msg.get("thread_ts") or msg_ts

        # Reading a finding is not a decision. It must run BEFORE the approver gate, or
        # the one button whose whole purpose is letting a human SEE what they are being
        # asked about would itself require being the approver — and anyone else in the
        # channel would get told off for trying to read.
        if action_id in ("world_show_report", "world_show_finding"):
            await self.show_report(value, thread_ts)
            return
        # A link button opens its URL in the browser; Slack still reports the click.
        # Nothing to do, and no reason to tell a non-approver off for it.
        if action_id.startswith("world_link_"):
            return

        slack = self.harness.slack
        # The ACCEPT gate: either the primary approver or the co_approver. It fails
        # closed: this handler only runs with Slack connected, so an empty approver is
        # a misconfigured harness, not a local dry-run, and skipping the check there let
        # anyone in the channel approve an outward action into the ledger.
        if not slack.is_approver(clicker):
            valid = ", ".join(f"<@{a}>" for a in (slack.approver, slack.co_approver) if a)
            phrases = self.harness.approval_phrases
            text = (phrases.not_approver.format(who=clicker, valid=valid) if valid
                    else phrases.no_approver.format(env=slack.approver_env))
            await self.post(text, thread_ts=thread_ts)
            return

        async def resolve_buttons(result_line: str) -> None:
            # Replace the button message with the decision — buttons disappear,
            # audit line stays.
            try:
                original = msg.get("blocks", [])
                kept = [b for b in original if b.get("type") != "actions"]
                kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": result_line}]})
                await self.web.chat_update(
                    channel=slack.channel, ts=msg_ts, text=result_line, blocks=kept
                )
            except Exception:  # noqa: BLE001 — best-effort edit; fall back to a plain reply
                await self.post(result_line, thread_ts=thread_ts)

        # Destroying a worktree is gated the same way every outward action is: only an
        # approver's click gets past the check above, and this runs after it.
        if action_id in ("world_wt_keep", "world_wt_remove"):
            await self._on_worktree_button(action_id, value, resolve_buttons)
            return

        # Promoting a configured proposal into knowledge/ writes the durable knowledge
        # base, so only an approver may click it. `value` is an untrusted findings path;
        # promote_to_knowledge re-validates it under the configured agent directory.
        if action_id == "world_promote_knowledge":
            await self.promote_to_knowledge(value, thread_ts)
            await self._resolve_single_buttons(msg, msg_ts, f"📥 채택됨 — <@{clicker}>")
            return

        # Dismissing a finding (drop its tracked thread; for a proposal also delete it and
        # block re-proposal) is a decision, so it too sits behind the approver gate.
        if action_id in ("world_dismiss_report", "world_dismiss_finding"):
            await self.dismiss_report(value, thread_ts)
            await self._resolve_single_buttons(msg, msg_ts, f"🚫 무시됨 — <@{clicker}>")
            return

        # A declared ask button: a canned request from the clicker to the agent's persona.
        # It starts a turn that costs money and may lead to an approval, so it sits behind
        # the gate above like any other decision.
        if action_id.startswith("world_ask_"):
            await self._on_ask_button(value, thread_ts, msg_ts, clicker)
            return

        if action_id == "world_select_task":
            # Generic "which candidate should the executor work on next?"
            # primitive (a project's tick_task decides WHEN to ask and WHAT the
            # candidates are — a project-specific task queue may use this path; world
            # core only owns the click -> state write). Value carries whatever
            # the project used to identify the candidate (a finding file path,
            # a JIRA key, ...) — opaque to world itself.
            await resolve_buttons(f"✅ 선택됨 — <@{clicker}>")
            self.state["queue_selected"] = value
            self.state["queue_asked_ts"] = None
            self.save_state()
            return

        # Releasing a capped handoff. Its own action ids on purpose: world_approve writes
        # a write-scope GRANT into approved_threads, and "let the teammate pick this up" must not
        # quietly unlock this thread's gated write tools.
        if action_id in ("world_hop_allow", "world_hop_stop"):
            await self._on_hop_button(
                action_id == "world_hop_allow", thread_ts, msg, msg_ts, clicker
            )
            return

        if action_id not in ("world_approve", "world_deny"):
            return
        approved = action_id == "world_approve"
        ph = self.harness.approval_phrases
        await resolve_buttons(
            (ph.resolved_approved if approved else ph.resolved_rejected).format(who=clicker)
        )

        pending = self.state.get("pending_approvals", [])
        # Grab the summary off the pending entry BEFORE dropping it — it's the
        # record of what was actually approved, and it's gone a line later.
        entry = next((p for p in pending if p.get("msg_ts") == msg_ts), {})
        self.state["pending_approvals"] = [p for p in pending if p.get("msg_ts") != msg_ts]

        # The grant a gated write tool checks. One click authorizes one successful
        # call (make_approval_gated consumes it); a denial leaves nothing behind and
        # clears any stale grant, so an agent cannot bank an old approval.
        #
        # Keyed off the BUTTON MESSAGE's own thread, never off the button value.
        # Those coincide for an ordinary in-channel ask, but a relay ask carries the
        # ORIGIN channel's thread in its value (that's where the decision text has
        # to go back to). Granting on the value would leave the dedicated-channel
        # thread — the one whose gated tools are actually blocked — still refused,
        # while writing a grant onto a foreign thread nothing ever reads.
        grants = self.state.setdefault("approved_threads", {})
        if approved:
            # Pin the grant to the files as they are AT THE MOMENT OF THE CLICK. Anything
            # computed earlier would miss edits made while the approver was reading.
            scope_hash, scope_files = approval_scope.fingerprint(
                self.thread_worktree(thread_ts)
            )
            grants[thread_ts] = {
                "by": clicker,
                "at": time.time(),
                "summary": entry.get("summary", "")[:300],
                "scope_hash": scope_hash,
                "scope_files": scope_files[:50],
            }
        else:
            grants.pop(thread_ts, None)
        self.save_state()

        # value is "channel:thread_ts|requester" (post_approval_buttons); for a
        # cross-channel relay it points back at the origin channel's thread, so whoever
        # asked over there sees the answer without visiting our channel. Parsed here,
        # BEFORE anything reads `requester` — it used to be parsed below the ledger
        # append, which raised UnboundLocalError on every single button click.
        routing, _, requester = (value or "").partition("|")
        reply_channel, _, reply_thread = routing.rpartition(":")

        # Append-only, and written for BOTH outcomes. A ledger that only records
        # approvals cannot answer "what has this harness already been told no to?", which
        # is the question recent_rejections_snippet exists to answer.
        await self._record_decision(thread_ts, {
            "kind": "button", "decision": "approved" if approved else "denied",
            "by": clicker,
            # A button is only clickable by a human in the Slack UI
            # (a bot can't click), so this is a human decision unless the clicker id is the
            # co_approver's — derived generally for correctness.
            "by_kind": "co_approver" if (slack.co_approver and clicker == slack.co_approver) else "human",
            "thread_ts": thread_ts,
            "requester": requester or entry.get("requester", "?"),
            "summary": entry.get("summary", "")[:300],
        })

        # No session to resume yet (see listener.py's docstring) — post the decision as
        # a plain thread reply. relay_decision() and decision_from_text() are the two
        # halves of one wire format: when reply_channel points at ANOTHER harness, this
        # message is the only thing that tells it the answer, and it must be VERIFIABLE
        # there, not merely readable. Signed over the thread it lands in and the ask it
        # answers (`msg_ts`, unique per button message), so a model that can post text
        # into that channel cannot forge one, nor move a captured one to another thread.
        await self.notify_thread(
            reply_thread or thread_ts,
            relay_decision(approved, reply_thread or thread_ts, msg_ts),
            channel=reply_channel or None,
        )

        # Resume the thread's own persona so a tool that was WAITING on this decision
        # actually runs now. The button path used to stop here: it wrote the grant to
        # approved_threads but never re-invoked the agent, so a make_approval_gated tool
        # (git_commit, jira_update, …) stayed un-run — the human clicked 승인 and nothing
        # happened, the grant sitting unconsumed forever. The message path
        # (on_approver_message) has always
        # resumed the thread; the button path must too, or approve-by-click is a no-op for
        # every gated action.
        #
        # Local thread only. A cross-harness relay approval (reply_channel points at
        # ANOTHER harness) has no gated tool waiting in OUR thread — the origin harness
        # runs the work — so resuming here would only generate noise in the dedicated
        # channel. On a denial there is no grant, so the resumed turn can only reply.
        own_channel = not reply_channel or reply_channel == self.harness.slack.channel
        meta = self.state.get("threads", {}).get(thread_ts)
        if own_channel and meta is not None:
            note = (
                self.harness.approval_phrases.resume_approved
                if approved else
                self.harness.approval_phrases.resume_rejected
            )
            # Resume the REQUESTER on approval, not the thread's origin persona — the
            # exact fix on_approver_message already carries (its comment: "resuming the
            # read-only author instead would wake a persona that can never spend the
            # write grant"). This path resumed meta["persona"], so a button-approved
            # executor ask woke the thread's read-only origin persona, who could only
            # narrate that the executor's final step was pending — twice, live, on the
            # same thread. On a denial there is no grant to spend; the
            # origin persona replies, as before.
            resume = (requester if approved else None) or meta.get("persona")
            await self._handle_mention(thread_ts, note, {"ts": "", "user": clicker}, resume)
