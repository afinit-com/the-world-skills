#!/usr/bin/env python3
"""Keep GitHub's traffic numbers past the 14 days GitHub shows.

    GH_TOKEN=... python traffic.py OWNER/REPO OUT_DIR

Views and clones come per day and are merged into OUT_DIR/daily.json by date, so a day
seen twice keeps one entry and the record grows past GitHub's window. Referrers and
popular paths come only as a 14-day total; each run saves that total as a dated
snapshot. Stars, forks and watchers are a snapshot per run day. Stdlib only.
The traffic endpoints need a token with Administration: read on the repository.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path


def get(path: str):
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    repo, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    daily_path = out / "daily.json"
    daily = json.loads(daily_path.read_text(encoding="utf-8")) if daily_path.exists() else {}
    for kind in ("views", "clones"):
        for day in get(f"/repos/{repo}/traffic/{kind}")[kind]:
            date = day["timestamp"][:10]
            daily.setdefault(date, {})[kind] = {"count": day["count"], "uniques": day["uniques"]}
    info = get(f"/repos/{repo}")
    daily.setdefault(today, {})["repo"] = {k: info[k] for k in
                                           ("stargazers_count", "forks_count", "subscribers_count",
                                            "open_issues_count")}
    daily_path.write_text(json.dumps(dict(sorted(daily.items())), indent=1) + "\n", encoding="utf-8")
    for name, path in (("referrers", "popular/referrers"), ("paths", "popular/paths")):
        folder = out / name
        folder.mkdir(exist_ok=True)
        (folder / f"{today}.json").write_text(
            json.dumps(get(f"/repos/{repo}/traffic/{path}"), indent=1) + "\n", encoding="utf-8")
    print(f"{repo}: {len(daily)} days on record, latest {max(daily)}")


if __name__ == "__main__":
    main()
