"""The featherweight mention responder — one fresh one-shot query per turn, with
the thread transcript refetched for context (no session resume).

Split out of listener.py. Mixed into SlackListener, so `self` here
is the listener.

Also holds announce_task, which opens the thread a mention conversation later
continues in — the two belong together: announce_task registers the thread in
state["threads"], and _handle_mention is what state entry exists for.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import TYPE_CHECKING, NamedTuple

from ..channels.base import task_card_lines
from .as_human import normalize_message, safe_text
from .rules import RULES_TEMPLATE, pre_approved_clause, slack_mrkdwn, split_approval_request
from .run_footer import footer_context, run_fields
from .ui import EMOJI_DONE, EMOJI_ERROR, EMOJI_QUEUED, EMOJI_WORKING, ToolTrail
from ..context import current_asker, set_current_asker
from ..env import redact
from ..process import task
from ..process.spawn import run_agent_oneshot
from ..process.state import pending_approvals_snippet, record_result_usage
from ..toolkit import make_approval_gated, make_handoff_tool, make_mention_tool
from ..types import Agent, MentionTrigger, RunResult, voice_clause

if TYPE_CHECKING:
    from ..harness import Harness
    from ..types import Persona

TRANSCRIPT_TURNS = 15
TRANSCRIPT_CHARS = 400


FINDING_MAX_BYTES = 60_000  # a finding that big is a bug in the agent, not a message to send
# Slack caps a message at 50 blocks. Header + one section per finding + an overflow
# note leaves plenty of room at 15; the cap exists so a burst of items can't 400.
MAX_BATCH_BUTTONS = 15


def report_button(rel_path: str) -> dict:
    """A '본문 보기' button carrying the finding's harness-relative path.

    The path is the button's `value`, which means it arrives back from Slack as
    untrusted input — Slack will deliver whatever a crafted interaction payload puts
    there, not only what we rendered. `show_report` resolves it under findings_dir
    and refuses anything that lands outside; do not read the value directly."""
    return {
        "type": "button",
        "action_id": "world_show_report",
        "text": {"type": "plain_text", "text": "📄 본문 보기"},
        "value": rel_path[:1900],  # Slack caps button values at 2000 chars
    }


def promote_button(rel_path: str) -> dict:
    """A '📥 지식으로 채택' button on a configured knowledge proposal.

    The click handler is approver-gated and re-validates the untrusted path under the
    configured proposal agent's findings directory."""
    return {
        "type": "button",
        "style": "primary",
        "action_id": "world_promote_knowledge",
        "text": {"type": "plain_text", "text": "📥 지식으로 채택"},
        "value": rel_path[:1900],
    }


def dismiss_button(rel_path: str) -> dict:
    """A '🚫 무시' button on ANY finding announcement. Approver-gated. Dismissing drops the
    finding's thread from state (it stops cluttering the tracked-thread set), and for a
    knowledge proposal additionally deletes the proposal and records its topic so the
    digest won't re-propose it."""
    return {
        "type": "button",
        "action_id": "world_dismiss_report",
        "text": {"type": "plain_text", "text": "🚫 무시"},
        "value": rel_path[:1900],
    }


def _report_actions(rel_path: str, *, knowledge_proposal: bool = False) -> list[dict]:
    """The knowledge-proposal flow's own buttons (본문 보기, 채택, 무시). Only a harness
    that configures `knowledge_proposal_agent` gets them; every other announcement
    carries its agent's declared `buttons` and nothing else (see `task_buttons`)."""
    buttons = [report_button(rel_path)]
    if knowledge_proposal:
        buttons.append(promote_button(rel_path))
    buttons.append(dismiss_button(rel_path))
    return buttons


def task_buttons(agent, task_id: str, primary: str) -> list[dict]:
    """Render `agent.buttons` for one announcement. There is no default set: an agent
    that declares none gets no actions block at all.

    action_ids carry the button's index because Slack refuses an actions block with two
    identical ids. An ask button's value names the agent, the index and the task, never
    the prompt: the value comes back from Slack as untrusted input, so the click handler
    looks the prompt up in code instead of reading it from the payload. show and dismiss
    need a file to act on and are left out when the run produced none."""
    out: list[dict] = []
    for i, button in enumerate(getattr(agent, "buttons", ()) or ()):
        element = {"type": "button", "text": {"type": "plain_text", "text": button.label}}
        if button.style:
            element["style"] = button.style
        if button.kind == "ask":
            element |= {"action_id": f"world_ask_{i}", "value": f"{agent.name}|{i}|{task_id}"}
        elif button.kind == "link":
            element |= {"action_id": f"world_link_{i}", "url": button.url}
        elif not primary:
            continue
        elif button.kind == "show":
            element |= {"action_id": "world_show_report", "value": primary[:1900]}
        else:  # dismiss
            element |= {"action_id": "world_dismiss_report", "value": primary[:1900]}
        out.append(element)
    return out


def _agent_of(rel_path: str) -> str:
    """The producing agent, read off the finding path: findings/<agent>/… → <agent>."""
    parts = rel_path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "findings" else ""


def _defuse_broadcasts(text: str) -> str:
    """Show `<!channel>`, `<!here>`, `<!everyone>` (and any other `<!…>` command) as
    text instead of notifying the channel. A task card carries a run's result and a
    title nobody reviewed before it posted; a poll that summarised outside text (a PR
    body) could otherwise page everyone. Discord has its allowed_mentions for this."""
    return text.replace("<!", "&lt;!")


class MentionTurn(NamedTuple):
    """One mention turn, after its APPROVAL_REQUEST marker has been split off."""

    result: RunResult
    reply: str
    approval_summary: str | None   # None = the model asked for nothing

    @property
    def failed(self) -> bool:
        """Whether this turn produced nothing worth posting.

        A non-"ok" status alone does NOT qualify — usually it just means some tool call
        got denied, and the blocked/denial heuristic behind it is tuned for poll agents'
        writes_report completion contract, a poor fit for this conversational,
        non-writes_report agent. Found live: a single denied Bash `ls`
        classified an otherwise-complete decision-brief + APPROVAL_REQUEST as "blocked"
        and discarded it. So the marker wins; only its absence lets the status decide."""
        return self.approval_summary is None and self.result.status != "ok"


# WHO addressed the turn a tool is running inside, as a Slack user id.
#
# A tool is a plain function: it gets its args and nothing else, so "who asked me
# this" is unreachable from inside one. That matters as soon as a tool acts on the
# asker's behalf somewhere else — dispatching a build that should notify them when
# it finishes, filing something under their name.
#
# The alternative was to put the id in the prompt and have the model pass it as an
# argument. Rejected: the message being answered is excluded from the transcript
# (exclude_ts), so on the FIRST message of a thread the model has no reliable source
# for it, and a silently-omitted argument degrades to "no notification" — the exact
# failure this plumbing exists to prevent.
#
# Set immediately before the turn runs. Tools execute in-process, and asyncio copies
# the context into child tasks at creation, so a tool called during the turn sees it.
class MentionMixin:
    harness: "Harness"
    state: dict

    def _knowledge_proposal_root(self):
        """Configured proposal directory, or None when promotion is disabled."""
        agent = self.harness.knowledge_proposal_agent
        return (self.harness.findings_dir / agent).resolve() if agent else None

    def _is_knowledge_proposal(self, rel_path: str) -> bool:
        root = self._knowledge_proposal_root()
        if root is None:
            return False
        target = (self.harness.home / rel_path).resolve(strict=False)
        return target.suffix == ".md" and target.is_relative_to(root)

    def _is_report(self, target) -> bool:
        tasks = (self.harness.home / "tasks").resolve()
        if target.is_relative_to(tasks):
            parts = target.relative_to(tasks).parts
            return len(parts) >= 3 and parts[1] == "work"
        return target.is_relative_to(self.harness.findings_dir.resolve())

    async def show_report(self, rel_path: str, thread_ts: str) -> None:
        """Post a report's full text into the thread its button was clicked in.

        The whole reason this exists: an announcement names a file, and a path is
        unreadable from a phone. Reading is harmless, so it is not approver-gated — but
        the path still has to be proven to be a report, because `rel_path` comes off a
        button value (see report_button). A report is a .md under a task's work/
        (tasks/<id>/work/…), or under findings/ for the knowledge-proposal flow. Before
        0.2.7 only findings/ was accepted, so "본문 보기" refused every task report."""
        target = (self.harness.home / rel_path).resolve(strict=False)
        if target.suffix != ".md" or not self._is_report(target):
            await self.post(f"`{rel_path}` 는 보고서가 아닙니다.", thread_ts=thread_ts)
            return
        if not target.is_file():
            await self.post(f"`{rel_path}` 가 더는 없습니다 (지워졌거나 이동됨).", thread_ts=thread_ts)
            return
        body = target.read_text(errors="replace", encoding="utf-8")[:FINDING_MAX_BYTES]
        # post() chunks anything over Slack's ~4k limit into follow-ups in this thread.
        await self.post(f"📄 *`{rel_path}`*\n\n{body}", thread_ts=thread_ts)

    async def promote_to_knowledge(self, rel_path: str, thread_ts: str) -> None:
        """Copy a configured proposal finding into knowledge/.

        This is what a '📥 채택' click does. Approver-gating happens at the call site
        (_on_interactive); here we only re-validate the untrusted path.

        The path must land under the configured agent's findings directory so a crafted
        click cannot copy an arbitrary file into the committed knowledge base."""
        root = self._knowledge_proposal_root()
        target = (self.harness.home / rel_path).resolve(strict=False)
        if root is None or target.suffix != ".md" or not target.is_relative_to(root):
            await self.post(f"`{rel_path}` 는 채택 가능한 지식 후보가 아닙니다.", thread_ts=thread_ts)
            return
        if not target.is_file():
            await self.post(f"`{rel_path}` 가 더는 없습니다 (지워졌거나 이동됨).", thread_ts=thread_ts)
            return
        # Proposals are dated (<ts>-<topic>.md) so each run can announce a new file;
        # new file that announces; knowledge/ wants the bare topic slug, so strip the ts —
        # promoting today's and tomorrow's proposal of the same topic overwrites one
        # knowledge file rather than accumulating dated copies.
        name = re.sub(r"^\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ-", "", target.name)
        dest = self.harness.home / "knowledge" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(target.read_text(errors="replace", encoding="utf-8"), encoding="utf-8")
        rel_dest = dest.relative_to(self.harness.home)
        self.harness.log(f"promoted {rel_path} → {rel_dest}")
        # Mark knowledge dirty so an optional indexer can rebuild after promotion.
        self.state.setdefault("knowledge", {})["dirty"] = True
        self.save_state()
        await self.post(f"📥 지식으로 채택했습니다 — `{rel_dest}`.", thread_ts=thread_ts)

    async def dismiss_report(self, rel_path: str, thread_ts: str) -> None:
        """Dismiss a finding on a '🚫 무시' click. Drops its thread from state["threads"]
        (the finding no longer counts toward the tracked-thread set that otherwise only
        grows), keyed by both the clicked message's thread and any thread pointing at this
        finding path. For a configured knowledge proposal it additionally deletes the file
        and records its topic so the digest won't re-propose it.

        Approver-gated at the call site. The finding FILE of a plain (non-proposal)
        finding is left on disk — it is the ability's own record; only the tracked thread
        is cleared."""
        threads = self.state.get("threads", {})
        removed = [
            ts for ts, meta in list(threads.items())
            if ts == thread_ts or rel_path in (meta.get("report_path"), meta.get("finding_path"))
        ]
        for ts in removed:
            threads.pop(ts, None)

        note = " (state에서 스레드 정리)"
        root = self._knowledge_proposal_root()
        target = (self.harness.home / rel_path).resolve(strict=False)
        if root is not None and target.suffix == ".md" and target.is_relative_to(root):
            topic = re.sub(r"^\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ-", "", target.name)[: -len(".md")]
            dismissed = self.state.setdefault("knowledge", {}).setdefault("dismissed", [])
            if topic and topic not in dismissed:
                dismissed.append(topic)
                del dismissed[:-200]
            if target.is_file():
                target.unlink()
            note = f" — `{topic}` 은(는) 다시 제안하지 않습니다"

        self.save_state()
        self.harness.log(f"dismissed {rel_path} (threads dropped: {len(removed)})")
        await self.post(f"🚫 무시했습니다{note}.", thread_ts=thread_ts)

    def _footer(self, last: dict | None) -> dict | None:
        """The run footer, or None when the harness has it switched off. One gate, so
        adding a third footer site cannot forget to honour `Harness(run_footer=)`."""
        return footer_context(last) if self.harness.run_footer else None

    def _report_badge(self, agent_name: str) -> str:
        return next((a.badge for a in self.harness.agents if a.name == agent_name and a.badge), "")

    def _report_header(self, rel_path: str) -> str:
        """The one-line label an announcement leads with, so a human can tell WHO found it
        and WHAT kind it is at a glance: '<badge> *지식 후보* · `curator`' for a
        proposal, '<badge> *보고서* · `monitor`' for a plain report."""
        agent = _agent_of(rel_path)
        badge = self._report_badge(agent)
        kind = "지식 후보" if self._is_knowledge_proposal(rel_path) else "보고서"
        return f"{(badge + ' ') if badge else ''}*{kind}* · `{agent}`"

    async def announce_task(self, record, result, persona: "Persona") -> str:
        """Open the thread a task is reviewed in, and register it for replies.

        Replaces announce_report and its batched sibling. Those existed because
        findings reached Slack by SCANNING findings/** a tick after the fact: the
        announcer held no RunResult (so the run footer had to guess "that agent's last
        run"), could not say which task a file belonged to, and got N unrelated files at
        once whenever it had been offline — which is the only reason batching existed.

        This is called from the scheduler with the task and the result in hand, so none
        of that is needed. One task, one thread: a task that already has one (it was
        asked for in a conversation) is answered there as a reply, and only a task with
        none (a scheduled run) opens a new thread in the channel.
        """
        head = f"*{record.title}*"
        if record.origin.kind == "operator" and record.origin.replay:
            # First line, because it changes how everything under it should be read: a
            # human pushed a past notice back through the pipeline on purpose.
            head = f"♻️ *운영자 재처리 요청*\n{head}"

        body = _defuse_broadcasts("\n".join(
            [head, *task_card_lines(self.harness.home, record, result, render=slack_mrkdwn)]
        ))

        primary = result.artifacts[0] if (result and result.artifacts) else ""
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]
        if primary and self._is_knowledge_proposal(primary):
            elements = _report_actions(primary, knowledge_proposal=True)
        else:
            agent = next((a for a in self.harness.agents if a.name == record.agent), None)
            elements = task_buttons(agent, record.id, primary)
        if elements:
            blocks.append({"type": "actions", "elements": elements})

        ts = await self.post(
            body, identity=persona.key, blocks=blocks, thread_ts=record.thread or None,
            # The real run, not a lookup of the agent's most recent one.
            context=self._footer(run_fields(result)),
        )
        if not ts:
            return ""
        if record.thread:
            # The conversation already owns this thread and its state entry; the reply
            # joins it rather than re-registering it as a task thread.
            return record.thread
        self.state["threads"][ts] = {
            "slug": record.slug or record.id,
            "origin": "task",
            "task_id": record.id,
            "report_path": primary,
            "persona": persona.key,
            "created": time.time(),
            "last_message_ts": time.time(),
        }
        self.save_state()
        # The ts goes BACK to the caller rather than onto the record here. Writing the
        # task from the Slack layer would reopen the process -> slack dependency the old
        # bridge existed to keep shut; the scheduler owns the task's lifecycle and is
        # already the thing that decides a task may be announced at all.
        return ts

    def _roster_lines(self, identity: str) -> str:
        """The main persona sees all the teammates to hand off to; everyone else sees
        ONLY the main persona — a sub-persona should never hand off directly to a
        sibling or to the approver, it routes everything through main (single hub).
        Names only: hand_off picks by name, and a sub-persona that posts through the
        main bot has no Slack id to show."""
        main_key = self.harness.main_persona.key
        if identity != main_key:
            main = self.identities.get(main_key, {})
            return f"- {main.get('display', self.harness.main_persona.display)}"
        lines = [f"- {ident['display']}" for key, ident in self.identities.items()
                 if key != main_key]
        return "\n".join(lines) or "(no teammates configured)"

    async def _thread_transcript(
        self,
        thread_ts: str,
        limit: int = TRANSCRIPT_TURNS,
        exclude_ts: str = "",
        channel: str | None = None,
    ) -> str:
        if not self.web:
            return ""
        try:
            resp = await self.web.conversations_replies(
                channel=channel or self.harness.slack.channel, ts=thread_ts, limit=max(limit, 30)
            )
        except Exception:  # noqa: BLE001 — context seed only, never fatal
            return ""
        lines = []
        for m in resp.get("messages", []):
            normalized = normalize_message(self, m)
            if normalized is None:
                continue
            m, _simulated = normalized
            ts = m.get("ts", "")
            # The harness's OWN turns belong in the transcript. They used to be dropped
            # (`m.get("user") == self.bot_user_id`), which left every thread looking like
            # a monologue by the human: the model saw the questions and never a word of
            # its own answers. Live — asked "확인할 거 뭐 있어?" it listed four
            # pending approvals and numbered them; asked "1번 상세내역 가져와봐" 48 minutes
            # later it replied "지금까지 어떤 메시지도 받지 않아서 '1번'을 알 수 없습니다."
            # That was an accurate report of what it had been shown.
            #
            # `user_to_identity` already maps this bot's user id to the main persona, so
            # its lines are labelled with the persona's name like any other speaker's.
            if ts == exclude_ts:
                continue
            raw_user = m.get("user", "?")
            identity = self.user_to_identity.get(raw_user)
            if identity:
                who = identity
            else:
                # Foreign bot/human — spell out name AND raw ID together, so the
                # model can correctly pick "<name>'s real ID is U0A1B2C3D4E" instead
                # of guessing from whichever ID appears most in the transcript (a
                # live miss: it mentioned itself).
                bot_name = m.get("bot_profile", {}).get("name")
                who = f"{bot_name} ({raw_user})" if bot_name else raw_user
            body = (m.get("text") or "").strip()
            if body:
                lines.append(f"[{who}] {body[:TRANSCRIPT_CHARS]}")
        return "\n".join(lines[-limit:])

    async def _gated_write_tools(self, persona: "Persona", thread_ts: str) -> list:
        """Persona.mention_tools is the write-capable set by convention (Harness.
        mention_tools is read-only). Each one is wrapped so it refuses to fire until
        the approver has clicked approve IN THIS THREAD, and spends that grant on
        success — the approval doctrine, enforced in the call path rather than only
        in RULES_TEMPLATE's prose.

        The closures capture this thread_ts, so a tool handed to one thread's turn
        can never be satisfied by an approval granted in another.

        `mention_tools` may be a callable instead of a list, in which case it is an
        async FACTORY invoked with (listener, thread_ts) and must return the tool list.
        That exists for tools whose scope is per-conversation rather than per-persona:
        a repair persona may commit into a worktree cut for the thread it is answering
        in, so its git tools cannot be bound until the thread is known. The listener is
        passed rather than the harness because a factory usually needs both it and the
        live state (listener.harness, listener.state).

        A factory that raises is treated as "this persona has no write tools here" — a
        repair that cannot be safely prepared must not be attempted."""
        addressee = self.harness.slack.addressee
        label = f"<@{addressee}>" if addressee else "the approver"
        tools = persona.mention_tools
        if callable(tools):
            try:
                tools = await tools(self, thread_ts)
            except Exception as exc:  # noqa: BLE001 — no worktree, no write tools
                self.harness.log(
                    f"{persona.key}: mention_tools factory failed for thread {thread_ts} "
                    f"({exc!r}) — this turn gets no write tools"
                )
                return []
        return [
            make_approval_gated(
                t,
                is_approved=lambda: self.thread_approved(thread_ts),
                consume=lambda: self.consume_thread_approval(thread_ts),
                approver_label=label,
                refusal=lambda: self.last_refusal(thread_ts),
            )
            for t in tools
        ]

    async def _thread_tools(self, persona: "Persona", thread_ts: str) -> list:
        """Persona.thread_tools — the ungated sibling of _gated_write_tools.

        Bound to this thread, like the gated set, and for the same reason: a tool handed
        to one thread's turn must not be able to act on another. NOT wrapped in
        make_approval_gated, because nothing here has an outward side-effect — the
        category is "bound to this conversation, with no side-effect", and a tool
        is its first inhabitant. Demanding an approve click before the harness may ask
        the approver a question is absurd; demanding one before it POSTS somewhere is
        the whole doctrine. Put the second kind in mention_tools.

        A factory that raises is treated as "this persona has no thread tools here",
        same as the gated set: a question this persona cannot prepare is one it must
        not ask.
        """
        tools = persona.thread_tools
        if callable(tools):
            try:
                tools = await tools(self, thread_ts)
            except Exception as exc:  # noqa: BLE001 — a broken factory must not kill the turn
                self.harness.log(
                    f"{persona.key}: thread_tools factory failed for thread {thread_ts} "
                    f"({exc!r}) — this turn gets no thread tools"
                )
                return []
        return list(tools)

    def _mention_tools_for(
        self, identity: str, addressee: str | None, thread_ts: str | None = None
    ) -> list:
        """Per-identity tools, scope fixed in code: the main persona gets
        mention_approver, which can only ever return the approver's id, plus hand_off to
        its roster; every other persona gets hand_off to the main persona only — mirrors
        the roster and post_approval_buttons' requester routing, so a sub-persona has no
        code path to address the approver or a sibling directly.

        hand_off replaced mention_teammate / mention_main: a persona posting through the
        main bot under its own name has no Slack user to mention (see make_handoff_tool).
        It needs `thread_ts`, to bind the queue to this thread; a relay turn has none and
        gets no hand_off."""
        main_key = self.harness.main_persona.key
        tools = []
        if identity == main_key:
            tools.append(make_mention_tool("mention_approver", "the approver", addressee))
            targets = {ident["display"]: key for key, ident in self.identities.items()
                       if key != main_key}
        else:
            targets = {self.identities.get(main_key, {}).get(
                "display", self.harness.main_persona.display): main_key}
        if thread_ts and targets:  # a relay turn has no thread of its own to hand within
            tools.append(make_handoff_tool(
                targets, lambda to, request: self._queue_handoff(thread_ts, identity, to, request),
            ))
        return tools

    # --- handoffs: queued in state, run after the turn, re-run after a restart -----

    def _queue_handoff(self, thread_ts: str, sender: str, to: str, request: str) -> None:
        meta = self.state["threads"].setdefault(thread_ts, {
            "slug": thread_ts, "origin": "mention", "persona": sender, "created": time.time(),
        })
        meta.setdefault("handoffs", []).append({
            "id": uuid.uuid4().hex[:12], "from": sender, "to": to,
            "text": request[:4000], "at": time.time(),
        })
        self.save_state()  # durable before the tool returns: a crash now still hands off

    async def _drain_handoffs(self, thread_ts: str) -> None:
        """Run this thread's queued handoffs in order. Each goes through the same hop
        budget as a bot-to-bot mention (_dispatch_bot_hop: the cap, the approver's
        allow/stop ask, the last-hop note), and is removed only once that returns, so
        a restart mid-turn finds it again. A teammate's own handoffs are drained by its
        own turn, inside this one."""
        # One drain per thread at a time. A person speaking mid-chain starts a second turn
        # in the same thread, and its drain would take the same head-of-queue handoff.
        draining = self.__dict__.setdefault("_draining_threads", set())
        if thread_ts in draining:
            return
        draining.add(thread_ts)
        try:
            await self._drain_queue(thread_ts)
        finally:
            draining.discard(thread_ts)

    async def _drain_queue(self, thread_ts: str) -> None:
        meta = self.state["threads"].get(thread_ts) or {}
        while meta.get("handoffs"):
            handoff = meta["handoffs"][0]
            if not handoff.get("announced"):
                handoff["announced"] = True
                self.save_state()
                to_name = self.identities.get(handoff["to"], {}).get("display", handoff["to"])
                # Mentions stripped: this line is a record, and a <@id> left in it would
                # come back as a bot-authored mention and dispatch the teammate twice.
                shown = re.sub(r"<@[A-Z0-9]+>", "", handoff["text"]).strip()
                await self.post(f"→ {to_name}: {shown[:1500]}",
                                identity=handoff["from"], thread_ts=thread_ts)
            try:
                await self._dispatch_bot_hop(
                    thread_ts, handoff["text"], {"ts": "", "user": ""},
                    handoff["from"], [handoff["to"]],
                )
            finally:
                meta["handoffs"] = [h for h in meta.get("handoffs", [])
                                    if h.get("id") != handoff.get("id")]
                if not meta["handoffs"]:
                    meta.pop("handoffs")
                self.save_state()

    async def resume_after_restart(self) -> None:
        """Called once the socket is up. A queued handoff is re-run, with a line saying
        so. A turn a person asked for that was still running is not re-run, only
        reported: the person may have moved on, and re-running would act on a request
        they can no longer see being answered."""
        ph = self.harness.approval_phrases
        for thread_ts, meta in list(self.state.get("threads", {}).items()):
            running = meta.pop("turn_running", None)
            if meta.get("handoffs"):
                to = meta["handoffs"][0]["to"]
                to_name = self.identities.get(to, {}).get("display", to)
                self.save_state()
                await self.post(ph.handoff_resumed.format(to_name=to_name), thread_ts=thread_ts)
                await self._drain_handoffs(thread_ts)
            elif running:
                self.save_state()
                await self.post(ph.turn_interrupted, identity=running.get("identity"),
                                thread_ts=thread_ts)

    def _rules_for(self, identity: str, persona: "Persona") -> str:
        return RULES_TEMPLATE.format(
            persona_flavor=persona.flavor or persona.display,
            voice=f"\n{clause}" if (clause := voice_clause(persona)) else "",
            approver=(
                f"<@{self.harness.slack.addressee}>"
                if self.harness.slack.addressee else "the approver"
            ),
            roster=self._roster_lines(identity),
            pre_approved=pre_approved_clause(self.harness.pre_approved_tools),
        )

    def _build_prompt(
        self,
        identity: str,
        persona: "Persona",
        text: str,
        transcript: str,
        origin_transcript: str = "",
        origin_channel: str = "",
        note: str = "",
    ) -> str:
        parts = [self._rules_for(identity, persona), self.harness.world_text().strip()]
        # The cross-thread dedup signal. Without it each thread is blind to what the
        # other threads have already put to the approver, and independent sessions
        # re-request the same action. post_approval_buttons registers every live
        # button in state["pending_approvals"] and its comment has always claimed
        # "every thread turn sees the list" — but nothing read it back into a prompt
        # for a while: written, rendered to HEARTBEAT.md, never injected.
        pending = pending_approvals_snippet(self.state)
        if pending:
            parts.append(pending)
        if transcript:
            parts.append(f"이 스레드의 이전 대화:\n{transcript}")
        if origin_transcript:
            # Relay-originated thread (_handle_relay_mention) — the ORIGIN channel's
            # own transcript (with real speaker IDs, e.g. "MainBot (U0A1B2C3D4E)")
            # isn't in this thread's history, only a summary of it is. Without
            # re-fetching it, every follow-up turn has names as bare strings and no
            # real ID to reference (a live miss: asked to mention the
            # main persona, the model had no ID for it in THIS thread's transcript
            # and grabbed its own).
            parts.append(f"원본 채널({origin_channel})의 대화 (실제 화자 ID 포함):\n{origin_transcript}")
        if note:
            # A one-line harness notice about THIS turn (today only rules.LAST_HOP_NOTE),
            # next to the message it qualifies.
            parts.append(note)
        parts.append(f"방금 온 메시지:\n{text}")
        return "\n\n".join(p for p in parts if p)

    async def _run_mention_turn(
        self,
        identity: str,
        persona: "Persona",
        prompt: str,
        *,
        tools: list,
        on_tool=None,
    ) -> MentionTurn:
        """Spawn the ad-hoc mention agent, bank what the call cost, and split its reply.

        Both mention paths — an ordinary thread turn and a relay turn — do exactly this,
        in exactly this order, and differ only in the tools they hand over and where they
        post the answer. That sequence is the duplication; the steps were factored out
        one by one in earlier passes, and this is what was left.

        No MentionTrigger agent is registered by any harness (world has no runtime wiring
        for one beyond this featherweight responder), so the Agent is built here per turn.
        `tools` is the turn's ENTIRE surface: spawned sessions get no built-in tools
        (spawn.py's empty BASE_TOOLS), so anything this turn can do, a caller passed in.

        Usage is recorded before the caller does any of its Slack bookkeeping. The call
        already reached the API and already cost money — a reaction that fails to render
        afterwards must not be able to lose that."""
        mention_agent = Agent(
            name="mention", trigger=MentionTrigger(), persona=persona,
            # False on purpose, and it must stay so: mutates=True now makes spawn.py cut
            # a worktree per run (keyed by task), and a mention turn is no task — every
            # reply in a thread would land in a fresh tree, scattering one conversation's
            # work. A thread that edits code gets ITS worktree from the harness's own
            # mention_tools factory (see approvals.thread_worktree), and with no Bash and
            # no raw Write/Edit this turn has no other way to write to the checkout.
            mutates=False, writes_report=False,
            tools=tools,
            model=self.harness.mention_model,
            allow_bash=False,
            # The "user query comes in → find and run a plugin" path. On by default when
            # the harness has any marketplace configured (Harness.mention_plugins); a query
            # can then reach the harness's installed plugin skills. Poll agents stay opt-in.
            allow_plugins=self.harness.mention_plugins and bool(self.harness.plugins),
        )
        result = await run_agent_oneshot(self.harness, mention_agent, safe_text(prompt), on_tool=on_tool)
        if record_result_usage(self.state, identity, result):
            self.save_state()
        reply, approval_summary = split_approval_request(result.result_text)
        return MentionTurn(result, reply, approval_summary)

    async def _handle_mention(
        self, thread_ts: str, text: str, event: dict, identity: str | None = None,
        *, note: str = "",
    ) -> None:
        """identity is the bot persona this turn answers as — the mentioned
        sub-persona for a bot-to-bot hop, or whatever a human addressed/the
        thread's existing owner. Falls back to main_persona for anything
        unrecognized (e.g. a stale identity key after a WORLD.md edit).
        note goes into the prompt as-is (see _build_prompt)."""
        identity = identity if identity in self.harness.personas else self.harness.main_persona.key
        persona = self.harness.personas[identity]
        msg_ts = event.get("ts", "")
        threads = self.state["threads"]
        meta = threads.setdefault(thread_ts, {
            "slug": thread_ts,
            "origin": "mention",
            "persona": identity,
            "created": time.time(),
        })
        meta["last_message_ts"] = time.time()
        self.save_state()
        phrases = self.harness.status_phrases_for(identity)
        await self._react(msg_ts, add=EMOJI_QUEUED, identity=identity)  # 👀 queued
        await self._set_thread_status(thread_ts, phrases.queued, identity=identity)

        transcript = await self._thread_transcript(thread_ts, exclude_ts=event.get("ts", ""))
        origin_channel = meta.get("relay_channel", "")
        origin_thread_ts = meta.get("relay_thread_ts", "")
        origin_transcript = ""
        if origin_channel and origin_thread_ts:
            origin_transcript = await self._thread_transcript(
                origin_thread_ts, limit=20, channel=origin_channel
            )
        # Before the prompt, so anything this turn calls can attribute back to a person.
        text = safe_text(text)
        set_current_asker(event.get("user", "") or "")
        prompt = self._build_prompt(
            identity, persona, text, transcript, origin_transcript, origin_channel, note=note
        )
        # This persona's OWN mention_tools (a context/scope carve-out, e.g. a write tool
        # only one identity needs) are approval-gated per thread and added to the
        # harness-wide read-only set. Built before the reaction ladder moves, because a
        # failing mention_tools factory logs, and that log should precede "🤔".
        tools = [
            *self.harness.mention_tools,
            *await self._gated_write_tools(persona, thread_ts),
            *await self._thread_tools(persona, thread_ts),
            *self._mention_tools_for(identity, self.harness.slack.addressee, thread_ts),
        ]

        # A restart while this is set means the turn never finished (resume_after_restart).
        meta["turn_running"] = {"identity": identity, "since": time.time()}
        self.save_state()
        try:
            await self._answer(thread_ts, identity, persona, prompt, tools, msg_ts, phrases)
        finally:
            meta.pop("turn_running", None)
            self.save_state()
        await self._drain_handoffs(thread_ts)

    async def _answer(self, thread_ts, identity, persona, prompt, tools, msg_ts, phrases) -> None:
        """The run-and-post half of _handle_mention."""
        await self._react(msg_ts, add=EMOJI_WORKING, remove=EMOJI_QUEUED, identity=identity)  # 🤔
        await self._set_thread_status(thread_ts, phrases.thinking, identity=identity)
        trail = ToolTrail(self, identity, thread_ts, msg_ts, phrases)
        turn = await self._run_mention_turn(
            identity, persona, prompt, tools=tools, on_tool=trail.tool
        )

        ok = turn.result.status == "ok"
        await trail.finish(ok)
        await self._react(  # 🤔 (or 🥱/😨 after a stall) -> 👍/😱
            msg_ts, add=EMOJI_DONE if ok else EMOJI_ERROR, remove=trail.showing, identity=identity,
        )
        await self._set_thread_status(thread_ts, "", identity=identity)  # clear on finish

        if turn.failed:
            await self.post(
                f"⚠️ 응답 중 문제가 발생했습니다 ({turn.result.status}): "
                f"{redact(turn.result.result_text)[:300]}",
                identity=identity, thread_ts=thread_ts,
                context=self._footer(run_fields(turn.result)),
            )
            return

        if turn.reply:
            # This path holds the RunResult itself, so the footer describes the very run
            # that produced this reply — no last-run lookup, no drift window.
            await self.post(
                slack_mrkdwn(turn.reply), identity=identity, thread_ts=thread_ts,
                context=self._footer(run_fields(turn.result)),
            )
        if turn.approval_summary:
            await self.post_approval_buttons(thread_ts, turn.approval_summary, requester=identity)

    async def _escalate_mention(self, thread_ts: str, text: str, event: dict) -> None:
        """This harness has no human-facing conversational interface: a group of harnesses
        speaks through ONE face — the co_approver bot. A human @-mentioning any of
        THIS harness's bots is handed off to that face instead of answered locally: we
        post a mention of it INTO the thread, and its relay watcher (this channel is in
        ITS relay_channels) picks up the app_mention, reads the whole thread, and answers
        in its own channel. This is the exact machinery a sibling→interface approval relay
        already rides — a bot-authored mention of the interface is already load-bearing
        (approval buttons ping it the same way).

        The rule this enforces: a harness behind the face surfaces findings and asks for
        approval; it does not converse with the human itself. Without it, a human
        @-mentioning the main bot got a canned local answer (its mention turn had no read
        tools); now it goes to the one bot meant to answer. Enabled per-harness via
        SlackConfig.escalate_human_mentions.

        Self-echo is safe: this post is authored by our own main bot, so when it comes
        back as a bot event it routes to _dispatch_bot_hop, whose author!=target filter
        drops it (the only identity of ours it could mention is the poster itself)."""
        interface = self.harness.slack.escalate_to
        if not interface:  # flag on but no co_approver configured — degrade to local answer
            await self._handle_mention(thread_ts, text, event)
            return
        main = self.harness.main_persona.key
        msg_ts = event.get("ts", "")
        # Track the thread so a follow-up human reply (no fresh @mention) still escalates
        # instead of falling through as untracked chatter.
        self.state["threads"].setdefault(
            thread_ts,
            {"slug": thread_ts, "origin": "escalated", "persona": main, "created": time.time()},
        )["last_message_ts"] = time.time()
        self.save_state()
        await self._react(msg_ts, add=EMOJI_QUEUED, identity=main)  # 👀
        author = event.get("user", "")
        who = f"<@{author}>님의 요청" if author else "요청"
        line = self.harness.approval_phrases.escalated.format(interface=f"<@{interface}>", who=who)
        await self.post(f"{line}\n\n<@{interface}> {text}", identity=main, thread_ts=thread_ts)
        await self._react(msg_ts, add=EMOJI_DONE, remove=EMOJI_QUEUED, identity=main)
        self.harness.log(f"escalated human mention in thread {thread_ts} -> {interface}")

    async def _handle_relay_mention(
        self, origin_channel: str, origin_thread_ts: str, text: str, event: dict
    ) -> None:
        """A foreign bot (or human) @-mentioned us in a slack.relay_channels channel
        — e.g. a sibling harness's own channel, once that harness starts
        @-mentioning US instead of pinging the human directly. Unlike
        _handle_mention, this NEVER replies in place: the whole point is keeping
        every approval in the one dedicated channel. Fetches context from the origin
        thread, asks the (main-persona) mention agent to summarize + decide (same
        APPROVAL_REQUEST convention), and posts a NEW thread in the dedicated channel
        with a permalink back to the origin. If that reply asks for approval, the
        button relays the decision back into the origin thread on click
        (post_approval_buttons' reply_channel/reply_thread_ts).

        Read-only by construction: the dedicated-channel thread this turn's approval
        would unlock does not exist until AFTER the agent has already run, so there is
        no thread to gate a write tool against — nor, for the same reason, one to bind
        a thread_tool to (a thread-bound tool would have no thread to act in). The agent
        therefore gets only the harness's read-only mention_tools — no
        persona.mention_tools, no _gated_write_tools, no _thread_tools. A relay turn
        proposes; the follow-up turns in the thread it opens (ordinary _handle_mention,
        fully gated) are what act.

        Deliberately skips the reaction ladder / thread-status banner / ToolTrail —
        those all act on a msg_ts within the dedicated channel's own context; wiring
        them to react on the ORIGIN message instead is a real gap, not done here.
        """
        identity = self.harness.main_persona.key
        persona = self.harness.main_persona
        try:
            permalink_resp = await self.web.chat_getPermalink(
                channel=origin_channel, message_ts=origin_thread_ts
            )
            permalink = permalink_resp.get("permalink", "")
        except Exception:  # noqa: BLE001 — nice-to-have context, never fatal
            permalink = ""

        transcript = await self._thread_transcript(
            origin_thread_ts, exclude_ts=event.get("ts", ""), channel=origin_channel,
        )
        parts = [
            self._rules_for(identity, persona),
            self.harness.world_text().strip(),
            "이건 다른 채널(다른 하네스의 채널)에서 온 멘션 릴레이다 — 네 답은 반드시 이 "
            "전용 채널에만 남겨라, 원본 채널에는 아무것도 올리지 마라.",
        ]
        # A relay turn can end in its own APPROVAL_REQUEST, so it needs the same
        # cross-thread dedup signal an ordinary mention turn gets (see _build_prompt).
        pending = pending_approvals_snippet(self.state)
        if pending:
            parts.append(pending)
        if transcript:
            parts.append(f"원본 채널의 이전 대화:\n{transcript}")
        parts.append(f"원본 채널에서 방금 온 멘션:\n{text}")
        if permalink:
            parts.append(f"원본 메시지 링크: {permalink}")
        prompt = "\n\n".join(p for p in parts if p)

        # Read-only tools ONLY — no persona.mention_tools, no _gated_write_tools (see the
        # docstring: there is no thread yet to gate an approval against).
        turn = await self._run_mention_turn(
            identity, persona, prompt,
            tools=[
                *self.harness.mention_tools,
                *self._mention_tools_for(identity, self.harness.slack.addressee),
            ],
        )

        if turn.failed:
            await self.post(
                f"⚠️ 릴레이 응답 중 문제가 발생했습니다 ({turn.result.status}): "
                f"{redact(turn.result.result_text)[:300]}",
                identity=identity,
            )
            return

        header = "🔗 다른 채널에서 멘션됨" + (f" — <{permalink}|원본 보기>" if permalink else "")
        body = f"{header}\n\n{slack_mrkdwn(turn.reply)}" if turn.reply else header
        new_ts = await self.post(body, identity=identity)
        if not new_ts:
            return
        self.state["threads"][new_ts] = {
            "slug": new_ts,
            "origin": "relay",
            "persona": identity,
            "created": time.time(),
            "last_message_ts": time.time(),
            "relay_channel": origin_channel,
            "relay_thread_ts": origin_thread_ts,
        }
        self.save_state()
        if turn.approval_summary:
            await self.post_approval_buttons(
                new_ts, turn.approval_summary, requester=identity,
                reply_channel=origin_channel, reply_thread_ts=origin_thread_ts,
            )
