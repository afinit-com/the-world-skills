#!/usr/bin/env python3
"""EXPERIMENTAL — check a Discord bot token and print its invite URL (docs/discord.md).

    uv run python bin/discord-setup.py            # reads env/harness.env
    uv run python bin/discord-setup.py --channel-env MYBOT_DISCORD_CHANNEL_ID

Discord has no API for creating an application, so four clicks in the Developer Portal
stay manual (create app → Bot → Reset Token → Message Content Intent on). Everything
after that is here:

  1. GET /applications/@me with the token → application id and bot name. A 401 means the
     token is wrong or was reset.
  2. Message Content intent: the application's `flags` carry GATEWAY_MESSAGE_CONTENT
     (1 << 18) or GATEWAY_MESSAGE_CONTENT_LIMITED (1 << 19) when the portal toggle is on.
     Neither bit set → the bot still hears direct @-mentions, but thread follow-ups and
     history arrive empty, so the script says so and exits 1.
  3. The exact OAuth2 invite URL: scopes `bot applications.commands`, permissions =
     View Channel, Send Messages, Send Messages in Threads, Create Public Threads, Read
     Message History, Embed Links. Nothing else — no Administrator, no Manage Messages.
  4. If the channel env var is set: GET /channels/{id} → can the bot see it yet? (Run
     again after opening the invite URL.)

Stdlib only, so it runs before discord.py is installed. Network calls happen only when
you run it; the token is sent only to discord.com and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

API = "https://discord.com/api/v10"
# Discord rejects requests without a "DiscordBot (url, version)" user agent.
USER_AGENT = "DiscordBot (https://github.com/afinit-com/the-world-skills, 0.4)"

GATEWAY_MESSAGE_CONTENT = 1 << 18
GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19

PERMISSIONS = {
    "View Channel": 1 << 10,
    "Send Messages": 1 << 11,
    "Embed Links": 1 << 14,
    "Manage Webhooks": 1 << 29,  # personas other than the bot post through a webhook
    "Read Message History": 1 << 16,
    "Create Public Threads": 1 << 35,
    "Send Messages in Threads": 1 << 38,
}
SCOPES = "bot+applications.commands"

Fetch = Callable[[str], "tuple[int, dict]"]


def permission_integer() -> int:
    value = 0
    for bit in PERMISSIONS.values():
        value |= bit
    return value


def invite_url(app_id: str) -> str:
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        f"&scope={SCOPES}&permissions={permission_integer()}"
    )


def message_content_enabled(flags: int) -> bool:
    return bool(flags & (GATEWAY_MESSAGE_CONTENT | GATEWAY_MESSAGE_CONTENT_LIMITED))


def http_fetch(token: str) -> Fetch:
    def fetch(path: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            API + path,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — fixed https host
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except ValueError:
                body = {}
            return exc.code, body

    return fetch


def run(fetch: Fetch, channel_id: str | None, out=print) -> int:
    """All the checks, against an injected `fetch` (tests pass a fake). Returns the exit
    code: 0 when everything needed is in place, 1 otherwise."""
    status, app = fetch("/applications/@me")
    if status == 401:
        out("✗ token rejected (401). Portal → your app → Bot → Reset Token, then paste the "
            "new one into env/harness.env.")
        return 1
    if status != 200:
        out(f"✗ GET /applications/@me answered {status}: {app.get('message', app)}")
        return 1

    app_id = str(app.get("id", ""))
    bot = app.get("bot") or {}
    out(f"✓ token works — application {app.get('name', '?')} ({app_id}), "
        f"bot user {bot.get('username', '?')} ({bot.get('id', '?')})")

    ok = True
    if message_content_enabled(int(app.get("flags") or 0)):
        out("✓ Message Content intent is enabled")
    else:
        ok = False
        out("✗ Message Content intent looks OFF. Portal → Bot → Privileged Gateway Intents → "
            "turn on MESSAGE CONTENT INTENT → Save. Without it the bot answers direct "
            "@-mentions but cannot read thread follow-ups or thread history.")

    out("\nInvite the bot (open while logged in as a server admin, pick the server):")
    out(f"  {invite_url(app_id)}")
    out("  permissions: " + ", ".join(PERMISSIONS))

    if channel_id:
        status, channel = fetch(f"/channels/{channel_id}")
        if status == 200:
            out(f"\n✓ the bot can see channel #{channel.get('name', '?')} ({channel_id})")
        else:
            ok = False
            out(f"\n✗ the bot cannot see channel {channel_id} ({status}). Invite it with the "
                "URL above, and check the channel's permissions let it View Channel.")
    else:
        out("\n(set the channel env var to also check the bot can see the channel)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--channel-env", default="DISCORD_CHANNEL_ID")
    args = parser.parse_args(argv)
    try:  # fill os.environ from env/harness.env the way the harness itself does
        from world.env import load_harness_env

        load_harness_env()
    except Exception:  # noqa: BLE001 — the shell environment still works
        pass
    token = os.environ.get(args.token_env)
    if not token:
        print(f"✗ {args.token_env} is not set (env/harness.env or the shell).")
        return 1
    return run(http_fetch(token), os.environ.get(args.channel_env))


if __name__ == "__main__":
    sys.exit(main())
