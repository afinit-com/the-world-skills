#!/usr/bin/env python3
"""Send Slack conversation checks using this harness's bot token (Slack only)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slack_sdk.web.async_client import AsyncWebClient

from world.env import env_path, load_harness_env, parse_env_file
from world.slack.as_human import MIN_SECRET, configured_secret
from world.slack.listener import MENTION_RE


def init_secret(key: str) -> None:
    """Keep an existing value and file mode; never print the credential.

    The parser keeps a key's first value, so an empty first declaration is filled in
    place: appending another line would leave the empty one in charge.
    """
    path = env_path()
    values = parse_env_file(path)
    if values.get(key) or os.environ.get(key):
        print('as-human secret already configured')
        return
    secret = secrets.token_urlsafe(32)
    if key in values:
        with open(path, encoding='utf-8', newline='') as stream:
            lines = stream.read().splitlines(keepends=True)
        declaration = re.compile(rf'^(\s*(?:export\s+)?{key}\s*=)(.*?)(\r?\n)?$')
        for index, line in enumerate(lines):
            match = declaration.match(line)
            if match:
                # An empty value is only blanks or empty quotes: nothing else to keep.
                lines[index] = match.group(1) + secret + (match.group(3) or '')
                break
        # Rewriting the open file keeps its mode and owner.
        with open(path, 'r+', encoding='utf-8', newline='') as stream:
            stream.write(''.join(lines))
            stream.truncate()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a+', encoding='utf-8') as stream:
            stream.seek(0)
            text = stream.read()
            stream.write(('' if not text or text.endswith('\n') else '\n') + f'{key}={secret}\n')
    print('as-human secret initialized; restart the harness to load it')


async def mentions_a_bot(text: str, main_user: str, main_token: str) -> bool:
    """The main bot, or a persona app whose xoxb token is in this environment. Telling
    any other user apart would need users:read, which the kit's apps do not request."""
    mentioned = set(MENTION_RE.findall(text))
    if main_user in mentioned or not mentioned:
        return main_user in mentioned
    for token in {v for v in os.environ.values() if v.startswith('xoxb-') and v != main_token}:
        with contextlib.suppress(Exception):  # a stale persona token must not stop the check
            if (await AsyncWebClient(token=token).auth_test())['user_id'] in mentioned:
                return True
    return False


async def send(prefix: str, text: str, *, thread: str | None = None,
               channel: str | None = None) -> str:
    p = f'{prefix}_' if prefix else ''
    secret = configured_secret(p + 'SLACK_AS_HUMAN_SECRET')
    token = os.environ.get(p + 'SLACK_BOT_TOKEN', '')
    channel = channel or os.environ.get(p + 'SLACK_CHANNEL', '')
    if not secret or not token or not channel:
        raise ValueError(f'configure SLACK_AS_HUMAN_SECRET (at least {MIN_SECRET} characters), '
                         'SLACK_BOT_TOKEN and SLACK_CHANNEL first')
    web = AsyncWebClient(token=token)
    me = await web.auth_test()
    # Slack does not emit app_mention for bots: the listener recognizes this mention
    # in a message event, even when this is the first message of a new thread. Mentioning
    # only a person would reach no bot at all, so the main bot is added then too.
    if not thread and not await mentions_a_bot(text, me['user_id'], token):
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
