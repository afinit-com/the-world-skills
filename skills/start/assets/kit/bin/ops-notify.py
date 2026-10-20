#!/usr/bin/env python3
"""ops-notify — post one line to the harness's Slack channel, for the watchdog.

    python bin/ops-notify.py --token-env OBS_SLACK_BOT_TOKEN --channel-env OBS_SLACK_CHANNEL \
        [--thread-ts TS] "text"

With --thread-ts the line is a reply in that thread; the watchdog keeps one thread per
incident. If Slack says the thread is gone (its parent was deleted) the line is posted
as a new message instead. Any other failure, a timeout included, is only a failure: Slack
may have taken a reply whose answer was lost, and a second copy as a new message would
split the thread. On success the ts of the thread it landed in is printed on stdout: the
given --thread-ts, or the new message's own ts, which the watchdog keeps as the
incident's thread from then on.

Only scripts/<name>-watchdog calls this, and only when bin/ops-kit.py rendered it with
--notify. It is operator tooling, not an agent capability: no agent can reach it, and it
posts exactly the text it is given. Standard library only (urllib), so it works with the
harness's interpreter whatever the venv holds. The token comes from env/harness.env
using the harness value parser and is never printed. File values outrank shell exports,
just as they do under the ops launcher. A failure exits non-zero
with the reason, which the watchdog writes to logs/watchdog.log.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://slack.com/api/chat.postMessage"
# Slack's answers for a reply whose thread no longer takes replies.
THREAD_GONE = frozenset({"thread_not_found", "message_not_found", "cannot_reply_to_message"})


def build_request(token: str, channel: str, text: str,
                  thread_ts: str | None = None) -> urllib.request.Request:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )


def main(argv: list[str] | None = None, opener=urllib.request.urlopen) -> int:
    ap = argparse.ArgumentParser(description="Post one line to the harness's Slack channel.")
    ap.add_argument("--token-env", required=True)
    ap.add_argument("--channel-env", required=True)
    ap.add_argument("--thread-ts", help="reply in this thread (a message ts)")
    ap.add_argument("text")
    args = ap.parse_args(argv)

    env = runpy.run_path(str(ROOT / "world" / "env.py"))
    # The launcher gives the file precedence too; keep the caller's environment intact.
    values = {**os.environ, **env["parse_env_file"](env["env_path"]())}
    token, channel = values.get(args.token_env), values.get(args.channel_env)
    missing = [n for n, v in ((args.token_env, token), (args.channel_env, channel)) if not v]
    if missing:
        print(f"ops-notify: {', '.join(missing)} not set in env/harness.env; not posted",
              file=sys.stderr)
        return 1
    if args.thread_ts:
        ts, error, refusal = post(opener, build_request(token, channel, args.text, args.thread_ts))
        if not error:
            print(args.thread_ts)
            return 0
        if refusal not in THREAD_GONE:
            print(f"ops-notify: the reply in thread {args.thread_ts} was not posted ({error})",
                  file=sys.stderr)
            return 1
        print(f"ops-notify: the reply in thread {args.thread_ts} was not posted ({error}); "
              "posting it as a new message", file=sys.stderr)
    ts, error, _ = post(opener, build_request(token, channel, args.text))
    if error:
        print(f"ops-notify: {error}; not posted", file=sys.stderr)
        return 1
    print(ts)
    return 0


def post(opener, request: urllib.request.Request) -> tuple[str, str | None, str]:
    """(the message's ts, None, "") once Slack took it, or ("", why it did not, Slack's
    error code when Slack answered with one)."""
    try:
        with opener(request, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "", f"Slack unreachable ({exc})", ""
    if not body.get("ok"):
        refusal = str(body.get("error") or "")
        return "", f"Slack refused it ({refusal or 'no error given'})", refusal
    return str(body.get("ts") or ""), None, ""


if __name__ == "__main__":
    raise SystemExit(main())
