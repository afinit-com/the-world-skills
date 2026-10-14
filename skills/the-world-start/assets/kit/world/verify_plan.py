"""This harness's verification plan: docs/verify.md's six steps, filled from its declarations.

`harness.py --verify-plan` prints it. Like world_map.py it reads what harness.py declares
and runs nothing: no agent, no check, no tool factory, no Slack call. Every line is an
unchecked box; the plan says what to look at, never that something passed.

A value that comes from env (the channel, the approver) is shown when set and named when
not, so a plan made before Slack is connected says which steps cannot run yet instead of
leaving them out.
"""

from __future__ import annotations

from .profile import CHANNEL


def _names(tools) -> str:
    if callable(tools):
        return "built per thread by a factory (bind it with world.tool_check)"
    return ", ".join(f"`{getattr(t, 'name', type(t).__name__)}`" for t in tools) or "none"


def _channel(harness) -> str:
    if getattr(harness, "discord", None) is not None:
        return "discord"
    return "none" if CHANNEL == "none" else "slack"


def _env(name: str | None, value: str | None) -> str:
    if not name:
        return "not configured"
    return f"`{value}` (`{name}`)" if value else f"`{name}` is not set"


def verify_plan(harness) -> str:
    personas = harness.personas
    main = harness.main_persona
    subs = [p for k, p in personas.items() if k != main.key]
    gated = [p for p in personas.values() if callable(p.mention_tools) or p.mention_tools]
    reporters = [a for a in harness.agents if a.writes_report or a.writes_finding]
    channel = _channel(harness)

    out = [
        f"# Verification plan: {harness.name}",
        "",
        "Generated from harness.py by `--verify-plan`. Nothing below has been checked; "
        "docs/verify.md says how to check each line and what to report.",
        "",
        "## 1. Local checks",
        "- [ ] `uv run python bin/smoke.py` exits 0 with no `[WARN] WORLD.md` line",
        "- [ ] `uv run python harness.py --once --dry-run --no-slack` renders a tick",
        "",
        "## 2. Tool boundaries (world.tool_check, read-only toward the outside)",
    ]
    tool_lines = []
    if harness.mention_tools:
        tool_lines.append(f"- shared, read-only on every mention turn: {_names(harness.mention_tools)}")
    for p in personas.values():
        if callable(p.mention_tools) or p.mention_tools:
            tool_lines.append(f"- {p.display} (`{p.key}`), approval-gated: {_names(p.mention_tools)}")
        if callable(p.thread_tools) or p.thread_tools:
            tool_lines.append(f"- {p.display} (`{p.key}`), thread-bound: {_names(p.thread_tools)}")
    for a in harness.agents:
        if a.tools:
            tool_lines.append(f"- worker `{a.name}`: {_names(a.tools)}; "
                              f"write_scope {', '.join(a.write_scope) or 'none'}")
    if tool_lines:
        out += tool_lines
        out.append("- [ ] for each boundary a tool's code claims (host, allowlist, verdict, "
                   "pinned SHA, path), one call it must refuse, and the refusal it gave")
    else:
        out.append("- no scoped tools are declared; nothing to test here")

    out += ["", "## 3. Process and connection",
            "- [ ] the harness process is running (how it is started: README.md)",
            "- [ ] STATUS.md is fresh (its update time is recent) and names the channel state",
            "- [ ] the end of the newest file in `logs/` has no error since the last start"]

    out += ["", "## 4. A real thread"]
    if channel == "none":
        out.append("- this harness has no conversation channel (`--channel none`); "
                   "step 4 does not apply, and the report says so")
    elif channel == "discord":
        out.append("- Discord is experimental (docs/discord.md); check the same lines "
                   "below in its thread and say which could not be checked")
    if channel != "none":
        if channel == "slack":
            slack = harness.slack
            out += [f"- home channel: {_env(slack.channel_env, slack.channel)}",
                    f"- approver: {_env(slack.approver_env, slack.approver)}"]
            if not slack.channel:
                out.append("- the channel is not set, so this step cannot run yet")
        out.append(f"- [ ] {main.display} (`{main.key}`) answers a mention in the thread")
        for p in subs:
            how = (f"its own app (`{p.bot_token_env}`)" if p.bot_token_env else
                   "the main app with `chat:write.customize` (without it, the post "
                   "shows the main app's name)")
            icon = "its portrait" if p.portrait else "no portrait set (the main app's icon)"
            out.append(f"- [ ] {p.display} (`{p.key}`) posts under its own name, "
                       f"{icon} and voice, through {how}")
        if subs:
            out.append("- [ ] each handoff shows as a `→ <name>: …` line and the teammate "
                       "answers in the same thread")
        for p in gated:
            out.append(f"- [ ] {p.display}: the proposal it wrote matches the approval card, "
                       "and after a person approves, the tool ran exactly once")
        out.append("- never click approve or deny yourself; hand the card to the approver")

    out += ["", "## 5. What was left behind"]
    for a in reporters:
        out.append(f"- [ ] `{a.name}`: a report file under `tasks/<task>/work/` for each "
                   "run checked, not just an exit code")
    out += ["- [ ] `state.json`: no `handoffs` left on a finished thread; "
            "`pending_approvals` and `approved_threads` hold only what is still open"]
    for p in gated:
        out.append(f"- [ ] {p.display}: the outward result (read it back, e.g. `gh api`) "
                   "matches the approved proposal and account, and appears once")

    out += ["", "## 6. Report",
            "- confirmed: each line above that was checked, with the ids, paths and SHAs seen",
            "- not confirmed: every line left unchecked, and why (no credentials, the path "
            "never ran, not allowed to send)",
            ""]
    return "\n".join(out)
