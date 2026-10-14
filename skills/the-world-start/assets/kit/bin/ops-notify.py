#!/usr/bin/env python3
"""ops-notify — post one line to the harness's Slack channel, for the watchdog.

    python bin/ops-notify.py --token-env OBS_SLACK_BOT_TOKEN --channel-env OBS_SLACK_CHANNEL "text"

Only scripts/<name>-watchdog calls this, and only when bin/ops-kit.py rendered it with
--notify. It is operator tooling, not an agent capability: no agent can reach it, and it
posts exactly the text it is given. Standard library only (urllib), so it works with the
harness's interpreter whatever the venv holds. The token comes from env/harness.env
through the same loader the harness uses and is never printed. A failure exits non-zero
with the reason, which the watchdog writes to logs/watchdog.log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://slack.com/api/chat.postMessage"


def build_request(token: str, channel: str, text: str) -> urllib.request.Request:
    return urllib.request.Request(
        API,
        data=json.dumps({"channel": channel, "text": text}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )


def main(argv: list[str] | None = None, opener=urllib.request.urlopen) -> int:
    ap = argparse.ArgumentParser(description="Post one line to the harness's Slack channel.")
    ap.add_argument("--token-env", required=True)
    ap.add_argument("--channel-env", required=True)
    ap.add_argument("text")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from world.env import load_harness_env

    load_harness_env()
    token, channel = os.environ.get(args.token_env), os.environ.get(args.channel_env)
    missing = [n for n, v in ((args.token_env, token), (args.channel_env, channel)) if not v]
    if missing:
        print(f"ops-notify: {', '.join(missing)} not set in env/harness.env; not posted",
              file=sys.stderr)
        return 1
    try:
        with opener(build_request(token, channel, args.text), timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"ops-notify: Slack unreachable ({exc}); not posted", file=sys.stderr)
        return 1
    if not body.get("ok"):
        print(f"ops-notify: Slack refused it ({body.get('error', 'no error given')}); not posted",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
