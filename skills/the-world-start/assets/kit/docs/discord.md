# Discord gateway (EXPERIMENTAL)

> **Status: experimental.** One bot, one channel, one approver. It exists so a harness
> can be tried on Discord, not as a Slack replacement. Slack remains the supported
> channel; nothing here changes how Slack behaves. Expect the surface to change when the
> channel seam is cut properly (Slack-shaped config and listener calls in core code).

## What it does

| | |
|---|---|
| **Mentions** | A human @-mentions the bot in the configured channel → a thread is opened off that message and the **main persona's** turn runs (the thread shows "is typing…" while it does); the reply is posted in the thread, split at Discord's 2000-character limit. A later human message in that thread continues the conversation without another mention. |
| **Task results** | `announce_task` posts a finished/blocked task into the channel and opens a thread off it. Replying in that thread is a conversation with the main persona. A task started from a conversation answers in that conversation's thread. |
| **Approvals** | `Persona.mention_tools` are wrapped by `make_approval_gated`, exactly as on Slack. When the turn ends with `APPROVAL_REQUEST: …`, the bot posts **Approve / Deny** buttons in the thread and pings the approver. A click from `APPROVER_DISCORD_ID` grants **one** successful call of a gated tool **in that thread**, then the turn resumes so the tool actually runs. Anyone else's click gets a reply only they can see, and nothing changes. |

## What is closed, and how

1. **Only the configured approver can approve.** The click handler compares the
   interaction's user id — set by Discord for the person who clicked — with
   `APPROVER_DISCORD_ID`. Nothing in a message body reaches that check.
2. **Typing "approve" grants nothing.** There is no code path from message text to a
   grant. (Slack accepts text decisions from a signed relay bot; Discord does not.)
3. **No approver configured means no one can approve.** The gateway refuses every click
   and says so in its startup log. Slack does the same.
4. **One click, one call, one thread.** The grant is the same `state["approved_threads"]`
   entry Slack uses, keyed by the Discord thread id, spent on the tool's first success,
   and pinned to the thread's worktree contents (a change after the click invalidates it).
   Those functions are shared with Slack, not copied, so the two cannot drift.
5. **A decided ask cannot be decided again.** The pending ask is removed before the first
   await, so a double click finds nothing; the buttons are removed from the message.
6. **The ask is looked up by the clicked message's id in the harness's own list**, never
   by anything carried in the button's `custom_id`.
7. **Bots are never answered** — another bot, a webhook, or this bot itself. Personas pass
   work with the `hand_off` tool instead: queued in `state.json`, run after the turn in the
   same thread, at most 4 per thread until a person speaks, and past that it stops with a
   notice. A queued handoff is re-run after a restart; a person's turn that a restart cut
   short is reported in its thread, not re-run.
8. **Posts never ping `@everyone`, `@here`, or roles**, whatever the model writes.
9. **Every decision is in `ledger.jsonl`** (`"channel": "discord"`), written only after the
   gateway has connected.

## What is open (not supported, or not yet)

- **Mentionable sub-personas.** One Discord application is one bot user, and only the main
  persona answers. Every other persona posts under its own name and avatar
  (`Persona.display`, and `Persona.icon_url`, or an http(s) `Persona.portrait` when
  `icon_url` is empty) through a channel webhook the bot creates, so it needs **Manage Webhooks** (in the invite URL that
  `bin/discord-setup.py` prints). A webhook is not a user: no one can @-mention a
  sub-persona, and without the permission the message goes out as the bot.
- **The over-cap ask.** On Slack the approver can let a handoff past the cap through with
  a button; here it just stops until a person speaks.
- **co_approver, relay channels, escalation.** Not implemented.
- **Text approvals.** Deliberately absent (see 2 above).
- **Reaction ladder / tool trail.** The Slack 👀→🤔→👍 ladder and "thinking…" banner
  have no Discord counterpart. Instead the thread shows Discord's "is typing…" line for
  the whole of each turn; if that call fails the turn still runs and replies.
- **Announcement buttons** (`Agent.buttons`: ask/show/dismiss/link) are not rendered.
- **Worktree cleanup** asks are a text notice ("reply to keep it") instead of buttons.
- **Tick tasks and `mention_tools` factories written against the Slack listener** receive
  the Discord gateway instead. It has `harness`, `state`, `save_state`, `post`,
  `notify_thread`, `thread_approved`, `consume_thread_approval` — not `web`, `identities`
  or any Slack API.
- **Content-borne manipulation** is the same open problem as on Slack: text in a thread can
  talk the model into *asking* for something bad. The approver's click is the check.
- **Restarts.** A pending ask survives a restart (it is in `state.json` and the click is
  handled from the gateway event, not an in-memory view), but this has not been exercised
  against a live server.
- **Not exercised live.** Everything above is tested with fakes only. Expect rough edges
  the first time it meets a real server.

## Setup

Discord has no API for creating an application, so four clicks happen in the Developer
Portal. Everything else is checked by a script.

### In the Developer Portal — <https://discord.com/developers/applications>

1. **New Application** → name it → Create.
2. **Bot** tab → **Reset Token** → copy it into `env/harness.env` as `DISCORD_BOT_TOKEN`.
3. Same tab, **Privileged Gateway Intents** → turn on **MESSAGE CONTENT INTENT** → Save.
   (Unverified apps in fewer than 100 servers need no review for this.)
4. Leave **General Information → Interactions Endpoint URL** empty. Interactions come over
   the gateway *or* that URL, never both; the gateway is what lets this run without a
   public URL.

### Then run the script

```
uv pip install -e '.[discord]'        # discord.py, only needed by a Discord harness
uv run python bin/discord-setup.py
```

It reads the token from `env/harness.env`, calls `GET /applications/@me`, and prints:

- the application and bot name (or "token rejected" for a wrong/reset token);
- whether the Message Content intent appears on — the application's `flags` carry
  `GATEWAY_MESSAGE_CONTENT` (`1 << 18`) or `GATEWAY_MESSAGE_CONTENT_LIMITED` (`1 << 19`);
- **the invite URL**: scopes `bot applications.commands`, permissions View Channel,
  Send Messages, Send Messages in Threads, Create Public Threads, Read Message History,
  Embed Links, Manage Webhooks (`309774601216`). Open it as a server admin and pick the
  server;
- if `DISCORD_CHANNEL_ID` is set, whether the bot can see that channel yet.

It exits non-zero until all of that is in place. `applications.commands` is not used by
the gateway today (buttons do not need it); it is included so slash commands can be added
later without re-inviting.

### Configure the harness

In `env/harness.env` (Discord: User Settings → Advanced → Developer Mode on; then
right-click → **Copy ID**):

```
DISCORD_BOT_TOKEN=...
DISCORD_CHANNEL_ID=123456789012345678    # right-click the channel → Copy Channel ID
APPROVER_DISCORD_ID=234567890123456789   # right-click yourself → Copy User ID
```

In `harness.py`:

```python
from world import DiscordConfig, Harness

h = Harness(name="my-bots", discord=DiscordConfig(), ...)
```

`DiscordConfig()` reads the three names above; pass `bot_token_env=` / `channel_env=` /
`approver_env=` to use project-prefixed names. With `discord=` set, the Discord gateway
replaces the Slack listener for that process. `--no-slack` still forces poll-only.

```
uv run python harness.py
```

The startup line says `discord=on`, then `discord gateway connected (EXPERIMENTAL) …,
approver set`. @-mention the bot in the channel to try it.

## Why webhooks, not one application per persona

Discord gives each application exactly one bot user, so a multi-persona harness has two
shapes. The kit takes the second, the same choice as Slack's one app with
`chat:write.customize`:

- **One application per persona.** Each persona is a real user: it can be @-mentioned, so
  a human can address it and a bot-to-bot handoff has something to route on. Cost: N
  portal setups, N tokens, N gateway connections, N invites.
- **One bot + webhooks that post as personas** (custom name and avatar per message).
  One setup, one connection. Webhook messages **can** carry buttons: Discord's docs say
  *"Application-owned webhooks can always send components"* (a webhook the bot creates
  through the API), and only non-application-owned webhooks are limited to non-interactive
  components ([Execute Webhook]). So buttons are not a blocker —
  verified against the docs, not live. The real limits: a webhook is not a user, so no
  one can @-mention a persona (every mention goes to the one bot, which then has to route
  by text), the bot needs **Manage Webhooks**, and every click still reaches the one
  application, so the approval gate stays single and central.

The approval gate is indifferent to the choice — it keys on the clicker, not the poster —
so this is a routing/identity decision, not a security one.

## Sources

- Application flags and `GET /applications/@me`: <https://docs.discord.com/developers/resources/application>
- Permission bit values: <https://docs.discord.com/developers/topics/permissions>
- Bot invite URL, `bot` / `applications.commands` scopes: <https://docs.discord.com/developers/topics/oauth2>
- Message Content intent (what still arrives without it): <https://docs.discord.com/developers/events/gateway>
- Interactions over the gateway vs an endpoint URL, the 3-second response window, ephemeral replies: <https://docs.discord.com/developers/interactions/receiving-and-responding>
- [Execute Webhook]: <https://docs.discord.com/developers/resources/webhook>
- Message Content as a privileged intent: <https://github.com/discord/discord-api-docs/discussions/5412>, <https://support-dev.discord.com/hc/en-us/articles/5324827539479-Message-Content-Intent-Review-Policy>
