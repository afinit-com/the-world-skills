#!/usr/bin/env python3
"""slack-pair — learn the channel ID and the approver's user ID from one @-mention, so
nobody has to dig them out of Slack's UI.

    uv run python bin/slack-pair.py --prefix OBS            # OBS_SLACK_* and APPROVER_SLACK_ID
    uv run python bin/slack-pair.py --prefix OBS --yes      # write without asking

Run it after bin/slack-provision.py has written the main app's tokens. It connects with
them over Socket Mode, prints the workspace and the bot's name, and waits. In Slack,
invite the bot to the channel the harness should live in (`/invite @<bot>`; a public
channel can be joined by the bot itself once the channel is set) and say `@<bot> 안녕`
there. The person who says it is proposed as the approver, the channel as the harness's
channel; on yes both go into env/harness.env (mode 600, other lines kept) and the bot
answers in the thread, which doubles as the first live check that it receives and posts.

Nothing is guessed: the IDs come from the event Slack delivers, not from a name lookup.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _provision_module():
    spec = importlib.util.spec_from_file_location("slack_provision", ROOT / "bin" / "slack-provision.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pairing_from_event(event: dict, bot_user_id: str) -> dict | None:
    """(channel, approver) from an app_mention a person sent, or None for anything
    else: another event type, a bot's message, or the bot mentioning itself."""
    if event.get("type") != "app_mention" or event.get("bot_id"):
        return None
    user, channel = event.get("user", ""), event.get("channel", "")
    if not user or not channel or user == bot_user_id:
        return None
    return {"channel": channel, "approver": user, "ts": event.get("ts", "")}


def env_updates(pairing: dict, channel_env: str, approver_env: str) -> dict[str, str]:
    return {channel_env: pairing["channel"], approver_env: pairing["approver"]}


async def wait_for_mention(bot_token: str, app_token: str, timeout: float) -> tuple[dict, dict]:
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient

    web = AsyncWebClient(token=bot_token)
    me = await web.auth_test()
    print(f"workspace: {me.get('team')} ({me.get('url')})", flush=True)
    print(f"bot: @{me.get('user')}", flush=True)
    print("\nIn Slack, in the channel this harness should use:", flush=True)
    print(f"  1. /invite @{me.get('user')}   (skip if the bot is already there)", flush=True)
    print(f"  2. @{me.get('user')} 안녕", flush=True)
    print(f"Waiting up to {int(timeout)}s for that mention...", flush=True)

    found: asyncio.Future = asyncio.get_running_loop().create_future()
    socket = SocketModeClient(app_token=app_token, web_client=web)

    async def on_request(client, req):
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api" or found.done():
            return
        pairing = pairing_from_event(req.payload.get("event", {}), me["user_id"])
        if pairing:
            found.set_result(pairing)

    socket.socket_mode_request_listeners.append(on_request)
    await socket.connect()
    try:
        pairing = await asyncio.wait_for(found, timeout)
    finally:
        await socket.disconnect()
        await socket.close()
    return me, pairing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prefix", help="harness prefix: reads <P>_SLACK_BOT_TOKEN / "
                        "<P>_SLACK_APP_TOKEN and writes <P>_SLACK_CHANNEL")
    parser.add_argument("--bot-env", help="bot token variable (default <P>_SLACK_BOT_TOKEN)")
    parser.add_argument("--app-env", help="app-level token variable (default <P>_SLACK_APP_TOKEN)")
    parser.add_argument("--channel-env", help="channel variable (default <P>_SLACK_CHANNEL)")
    parser.add_argument("--approver-env", default="APPROVER_SLACK_ID")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--yes", action="store_true", help="write without asking")
    args = parser.parse_args(argv)
    p = f"{args.prefix}_" if args.prefix else ""
    bot_env = args.bot_env or f"{p}SLACK_BOT_TOKEN"
    app_env = args.app_env or f"{p}SLACK_APP_TOKEN"
    channel_env = args.channel_env or f"{p}SLACK_CHANNEL"

    provision = _provision_module()
    env_file = provision.default_env_file()
    values = {**provision.read_env(env_file), **os.environ}
    bot, app = values.get(bot_env, ""), values.get(app_env, "")
    if not provision.is_filled(bot) or not provision.is_filled(app):
        print(f"✗ {bot_env} and {app_env} must be set first — run bin/slack-provision.py.", flush=True)
        return 1

    try:
        me, pairing = asyncio.run(wait_for_mention(bot, app, args.timeout))
    except asyncio.TimeoutError:
        print("✗ no mention arrived. Is the bot in that channel, and Socket Mode on?", flush=True)
        return 1

    print(f"\nchannel:  {pairing['channel']}", flush=True)
    print(f"approver: {pairing['approver']}  (the person who mentioned the bot)", flush=True)
    if not args.yes:
        try:
            answer = input(f"Write {channel_env} and {args.approver_env} to {env_file}? [y/N] ")
        except EOFError:  # no terminal to answer from (e.g. run from an agent's shell)
            print("\nNothing written: no terminal to ask. Re-run with --yes to write these.", flush=True)
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing written.", flush=True)
            return 1
    provision.upsert_env(env_file, env_updates(pairing, channel_env, args.approver_env))
    print(f"✓ wrote {channel_env} and {args.approver_env} to {env_file}", flush=True)

    async def confirm() -> None:
        from slack_sdk.web.async_client import AsyncWebClient

        await AsyncWebClient(token=bot).chat_postMessage(
            channel=pairing["channel"], thread_ts=pairing["ts"] or None,
            text=f"연결됐어요. 이 채널에서 일하고, <@{pairing['approver']}>님께 승인을 받을게요.",
        )

    try:
        asyncio.run(confirm())
    except Exception as exc:  # noqa: BLE001 — the IDs are written; the reply is a courtesy
        print(f"(could not reply in the thread: {exc!r})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
