#!/usr/bin/env python3
"""Send Slack conversation checks using this harness's bot token (Slack only)."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slack_sdk.web.async_client import AsyncWebClient

from world.env import env_path, load_harness_env, parse_env_file
from world.slack.listener import MENTION_RE


def init_secret(key: str) -> None:
    """Keep an existing value and file mode; never print the credential."""
    path = env_path()
    if parse_env_file(path).get(key) or os.environ.get(key):
        print('as-human secret already configured')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Append a final assignment: an earlier empty declaration must not win.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(f'\n{key}={secrets.token_urlsafe(32)}\n')
    print('as-human secret initialized; restart the harness to load it')


async def send(prefix: str, text: str, *, thread: str | None = None,
               channel: str | None = None) -> str:
    p = f'{prefix}_' if prefix else ''
    secret = os.environ.get(p + 'SLACK_AS_HUMAN_SECRET', '')
    token = os.environ.get(p + 'SLACK_BOT_TOKEN', '')
    channel = channel or os.environ.get(p + 'SLACK_CHANNEL', '')
    if not secret or not token or not channel:
        raise ValueError('configure SLACK_AS_HUMAN_SECRET, SLACK_BOT_TOKEN and SLACK_CHANNEL first')
    web = AsyncWebClient(token=token)
    me = await web.auth_test()
    # Slack does not emit app_mention for bots: the listener recognizes this mention
    # in a message event, even when this is the first message of a new thread.
    if not thread and not MENTION_RE.search(text):
        text = f'<@{me["user_id"]}> {text}'
    response = await web.chat_postMessage(
        channel=channel, thread_ts=thread,
        text=f'[as-human:{secret}] {text}',
    )
    return response['ts']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default='', help='<PREFIX>_SLACK_* namespace, as in slack-pair')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='initialize the secret without showing it')
    sender = commands.add_parser('send', help='post a conversation check')
    sender.add_argument('--thread', help='reply in this Slack thread ts')
    sender.add_argument('--channel', help='override the configured channel')
    sender.add_argument('text')
    args = parser.parse_args()
    load_harness_env()
    try:
        if args.command == 'init':
            p = f'{args.prefix}_' if args.prefix else ''
            init_secret(p + 'SLACK_AS_HUMAN_SECRET')
        else:
            print(asyncio.run(send(args.prefix, args.text, thread=args.thread, channel=args.channel)))
    except Exception:
        # Slack exceptions may contain the submitted request, including the marker.
        print('as-human command failed; check configuration and Slack connectivity', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
