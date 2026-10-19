#!/usr/bin/env python3
"""dig.py — dig through every coding agent's local session history.

Two questions, one index:

  python3 dig.py find PAY-881                      # which session touched this?
  python3 dig.py find checkout retry --since 2026-08-01 --agent codex
  python3 dig.py chores --days 90                  # what does the human keep doing by hand?
  python3 dig.py chores --project my-app --exclude-project work --json out.json
  python3 dig.py chores --days 120 --errands       # + errands grouped by wording or work done
  python3 dig.py chores --days 120 --jev           # + Jev labels and same-chore grouping

History is read from Claude Code, Codex, opencode, Cursor, Antigravity CLI (`agy`) and the
legacy Gemini CLI, whichever are on this machine (`--agent` narrows it). The archive is
far too big for a model to read; this script does the deterministic reduction and makes
no judgment. Which chores are worth automating is the caller's.

Everything is offline and read-only, except `--jev`, which sends redacted openers and
their shared fingerprints (errands.py) to TypeSafe's API and needs TYPESAFE_API_KEY.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import errands  # noqa: E402
import sources  # noqa: E402

# Pure acknowledgements / steering grunts: real typed text, but not a chore.
NOISE_EXACT = {
    "ㅇㅇ", "ㅇㅋ", "ㄱㄱ", "ㅇ", "ok", "okay", "네", "넵", "웅", "웅웅", "그래", "좋아",
    "고마워", "감사", "감사합니다", "굿", "good", "nice", "yes", "no", "아니", "아니야",
    "계속", "continue", "go", "gogo", "진행", "진행해", "해줘", "ㅋㅋ", "ㅋㅋㅋ", "y", "n",
    "stop", "그만", "wait", "잠깐", "다시", "again", "next", "다음",
}

# Crude Korean morphology: strip trailing particles/endings so 슬랙을/슬랙에서/슬랙이
# collapse to one token. Longest-first so 에서 wins over 에.
JOSA = (
    "에서는", "에게서", "으로는", "이라는", "라는", "에서", "에게", "한테", "으로", "까지",
    "부터", "이랑", "처럼", "보다", "마다", "조차", "밖에", "이나", "은", "는", "이", "가",
    "을", "를", "에", "의", "로", "도", "만", "와", "과", "랑", "요",
)
VERB_ENDINGS = (
    "해줄래", "해줘야", "시켜줘", "해주세요", "해야돼", "해야해", "하고싶어", "해볼까",
    "해줘", "해줌", "해봐", "해서", "했어", "했던", "하는", "하고", "하자", "할까", "해야",
    "된다", "됐어", "돼야", "인가", "인지", "이야", "야",
)

STOP = {
    "the", "a", "an", "and", "or", "but", "if", "then", "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "been", "it", "its", "to", "of", "in", "on", "for",
    "with", "as", "at", "by", "from", "up", "out", "so", "do", "does", "did", "can",
    "could", "should", "would", "will", "just", "not", "no", "yes", "you", "i", "me",
    "my", "we", "us", "your", "there", "here", "what", "when", "where", "which", "who",
    "how", "why", "all", "any", "some", "more", "most", "other", "into", "than", "too",
    "very", "now", "also", "let", "get", "got", "make", "made", "want", "need", "like",
    "please", "thanks", "ok", "okay",
    "그리고", "그럼", "근데", "그거", "이거", "저거", "여기", "거기", "저기", "이제", "다시",
    "다시한번", "일단", "먼저", "다음", "지금", "아까", "혹시", "그냥", "좀", "다", "더",
    "각", "안", "잘", "왜", "뭐", "뭔가", "어떤", "무슨", "같은", "같이", "대해", "대한",
    "위해", "통해", "관련", "부분", "내용", "경우", "때문", "해서", "하면", "되는", "있는",
    "없는", "있어", "없어", "된", "될", "수", "것", "게", "거", "말고", "말",
}

WORD_RE = re.compile(r"[0-9a-zA-Z_가-힣][0-9a-zA-Z_가-힣\-./]*")
CODEBLOCK_RE = re.compile(r"```.*?```", re.S)
URL_RE = re.compile(r"https?://\S+")
PATH_RE = re.compile(r"(/[\w.\-]+){2,}")
NUM_RE = re.compile(r"\b\d[\d,._]*\b")
SLASH_RE = re.compile(r"^/([a-z0-9][\w:-]*)(?![\w:./-])", re.I)  # not /Users/… paths
HOME = str(Path.home())


def short(project: str) -> str:
    return "~" + project[len(HOME):] if project.startswith(HOME) else project


def strip_suffixes(tok: str) -> str:
    for group in (VERB_ENDINGS, JOSA):
        for suf in group:
            if len(tok) > len(suf) + 1 and tok.endswith(suf):
                tok = tok[: -len(suf)]
                break
    return tok


def tokenize(text: str) -> set[str]:
    t = CODEBLOCK_RE.sub(" ", text)
    t = URL_RE.sub(" url ", t)
    t = PATH_RE.sub(" path ", t)
    t = NUM_RE.sub(" n ", t)
    out = set()
    for m in WORD_RE.finditer(t.lower()):
        tok = strip_suffixes(m.group(0).strip("-./"))
        if len(tok) < 2 or tok in STOP or tok.isdigit():
            continue
        out.add(tok)
    return out


# ---------------------------------------------------------------- window


def day(value: str, end: bool = False) -> datetime:
    d = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(d, time.max if end else time.min).astimezone(timezone.utc)


def window_from(a) -> sources.Window:
    now = datetime.now(timezone.utc)
    until = day(a.until, end=True) if a.until else now
    since = day(a.since) if a.since else until - timedelta(days=a.days)
    if since > until:
        sys.exit("--since is after --until")
    return sources.Window(since, until, tuple(a.project or ()), tuple(a.exclude_project or ()))


def agents_from(a) -> tuple[str, ...]:
    if not a.agent:
        return sources.AGENTS
    names = [n.strip().lower() for v in a.agent for n in v.split(",") if n.strip()]
    unknown = sorted(set(names) - set(sources.AGENTS))
    if unknown:
        sys.exit(f"unknown agent {', '.join(unknown)}; choose from {', '.join(sources.AGENTS)}")
    return tuple(dict.fromkeys(names))


def common_args(ap: argparse.ArgumentParser, days: int) -> None:
    ap.add_argument("--days", type=int, default=days, help=f"look back this far (default {days})")
    ap.add_argument("--since", help="YYYY-MM-DD; overrides --days")
    ap.add_argument("--until", help="YYYY-MM-DD (default: now)")
    ap.add_argument("--agent", action="append",
                    help=f"repeatable or comma-separated: {','.join(sources.AGENTS)} (default all)")
    ap.add_argument("--project", action="append",
                    help="keep sessions whose directory contains this (repeatable, any matches)")
    ap.add_argument("--exclude-project", action="append",
                    help="drop sessions whose directory contains this (repeatable)")


# ---------------------------------------------------------------- find


def find_sessions(terms: list[str], window, agents, top: int) -> list[dict]:
    """Locate the session that touched something. Answers "그거 어느 세션이었지?".

    Matches the WHOLE transcript, not just typed prompts: a ticket id or a branch name
    usually appears in tool output, never in what the user typed.
    """
    needles = [t.lower() for t in terms if t]
    hits = []
    for s in sources.sessions(window, agents):
        text = s.text()
        low = text.lower()
        counts = [low.count(n) for n in needles]
        if not all(counts):
            continue
        i = low.find(needles[0])
        snippet = " ".join(text[max(0, i - 90):i + 130].split())
        hits.append({
            "agent": s.agent,
            "session": s.id,
            "project": s.project,
            "path": s.path,
            "date": s.first.astimezone().strftime("%Y-%m-%d %H:%M"),
            "matches": sum(counts),
            "opener": " ".join((s.opener or "(no opener)").split())[:220],
            "snippet": snippet[:220],
            "resume": s.resume,
        })
    # Relevance before recency: the session that worked on a thing mentions it dozens of
    # times, while today's session mentions it once in passing.
    hits.sort(key=lambda h: (h["matches"], h["date"]), reverse=True)
    return hits[:top]


def cmd_find(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="dig.py find")
    ap.add_argument("terms", nargs="+", help="every term must appear in the session")
    common_args(ap, 365)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args(argv)
    hits = find_sessions(a.terms, window_from(a), agents_from(a), a.top)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(hits, ensure_ascii=False, indent=2))
        print(f"wrote {a.json_out}: {len(hits)} sessions")
        return 0 if hits else 1
    if not hits:
        print(f"no session matched {a.terms} in the window")
        return 1
    for h in hits:
        print(f"{h['date']} · {h['agent']} · {short(h['project'])} · {h['matches']} hits")
        if h["resume"]:
            print(f"    resume: {h['resume']}")
        print(f"    opener: {h['opener'][:150]}")
        if h["snippet"]:
            print(f"    match:  …{h['snippet'][:150]}…")
        print(f"    file:   {h['path']}")
    return 0


# ---------------------------------------------------------------- chores


def cluster(records, min_sessions: int, threshold: float):
    """Greedy near-duplicate clustering over token sets. Lexical: it only catches
    re-typed requests. An inverted index on rare tokens keeps it near-linear."""
    docs, df = [], Counter()
    for rec in records:
        toks = tokenize(rec["text"])
        if len(toks) < 3:
            continue
        rec["tokens"] = toks
        docs.append(rec)
        df.update(toks)
    n = len(docs) or 1
    common = {t for t, c in df.items() if c / n > 0.25}  # own vocabulary carries no signal

    index, clusters = defaultdict(list), []
    for doc in docs:
        rare = doc["tokens"] - common
        if len(rare) < 2:
            continue
        best, best_score, seen = None, 0.0, set()
        for tok in rare:
            for ci in index[tok]:
                if ci in seen:
                    continue
                seen.add(ci)
                other = clusters[ci]["centroid"]
                inter = len(rare & other)
                if inter and inter / len(rare | other) > best_score:
                    best, best_score = ci, inter / len(rare | other)
        if best is not None and best_score >= threshold:
            c = clusters[best]
            c["members"].append(doc)
            c["centroid"] &= rare if len(c["members"]) > 2 else c["centroid"] | rare
            for tok in c["centroid"]:
                if best not in index[tok]:
                    index[tok].append(best)
        else:
            clusters.append({"centroid": set(rare), "members": [doc]})
            for tok in rare:
                index[tok].append(len(clusters) - 1)

    idf = {t: math.log(n / (1 + c)) for t, c in df.items()}
    out = []
    for c in clusters:
        members = sorted(c["members"], key=lambda m: m["dt"])
        if len({m["session"] for m in members}) < min_sessions:
            continue
        shared = set.intersection(*(m["tokens"] for m in members))
        out.append({
            "size": len(members),
            "sessions": len({m["session"] for m in members}),
            "agents": sorted({m["agent"] for m in members}),
            "projects": [short(p) for p, _ in Counter(m["project"] for m in members).most_common()],
            "first": members[0]["dt"].date().isoformat(),
            "last": members[-1]["dt"].date().isoformat(),
            "keywords": sorted(shared, key=lambda t: -idf.get(t, 0))[:8],
            "examples": [{"date": m["dt"].date().isoformat(), "text": m["text"][:280]}
                         for m in members[-5:]],
        })
    out.sort(key=lambda c: (-c["sessions"], -c["size"]))
    return out


def digest(sessions_: list) -> list[dict]:
    """One row per session: the opener states the errand, the tool profile says what the
    work touched. Resuming writes a new transcript carrying the old history, so the same
    errand can appear two or three times; collapse on (agent, project, opener)."""
    rows, seen = [], {}
    for s in sessions_:
        if not s.opener:
            continue
        key = (s.agent, s.project, " ".join(s.opener.split())[:120])
        span = round((s.last - s.first).total_seconds() / 60)
        entry = {
            "id": s.id,
            "agent": s.agent,
            "date": s.first.astimezone().strftime("%Y-%m-%d %H:%M"),
            "project": short(s.project),
            "turns": len(s.prompts),
            # Wall-clock span, not effort: a session left open overnight spans days.
            "span": f"{span}m" if span < 600 else f"{round(span / 1440, 1)}d",
            "tools": [t for t, _ in s.tools.most_common(6) if t],
            "opener": s.opener[:400],
            "path": s.path,
        }
        prev = seen.get(key)
        if prev is None:
            seen[key] = entry
            rows.append(entry)
        else:
            prev["turns"] = max(prev["turns"], entry["turns"])
            prev["date"] = min(prev["date"], entry["date"])
    rows.sort(key=lambda d: d["date"])
    return rows


def cmd_chores(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="dig.py chores")
    common_args(ap, 180)
    ap.add_argument("--min-sessions", type=int, default=3,
                    help="a chore must recur across at least this many sessions (with "
                         "--errands/--jev: sessions or distinct days)")
    ap.add_argument("--threshold", type=float, default=0.34)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--include-automated", action="store_true",
                    help="keep sessions a program started (SDK, codex exec, scheduled runs)")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--errands", action="store_true",
                    help="cut sessions into errands and group those that look alike, by "
                         "wording or by the work done (offline)")
    ap.add_argument("--jev", action="store_true",
                    help="label and group errands with Jev (sends redacted openers and a "
                         "fixed-vocabulary fingerprint to TypeSafe)")
    ap.add_argument("--jev-dry-run", action="store_true",
                    help="show what --jev would send, and send nothing")
    ap.add_argument("--jev-same", type=float, default=0.7,
                    help="Noul probability at which two errands count as the same chore")
    ap.add_argument("--jev-candidates", type=int, default=5,
                    help="nearest errands asked about per errand, by words and by fingerprint")
    ap.add_argument("--errand-gap", type=float, default=errands.GAP_HOURS,
                    help="hours away that end one errand and start the next")
    a = ap.parse_args(argv)
    if a.errand_gap <= 0:
        ap.error("--errand-gap must be a positive number of hours")

    window, agents = window_from(a), agents_from(a)
    found = sources.sessions(window, agents)
    automated = [s for s in found if s.automated]
    kept = found if a.include_automated else [s for s in found if not s.automated]

    slash, records = Counter(), []
    for s in kept:
        for dt, text in s.prompts:
            m = SLASH_RE.match(text)
            if m:
                slash[m.group(1).lower()] += 1
                if len(text) < 60:  # a bare slash invocation is usage data, not a chore
                    continue
            if len(text) < 12 or text.strip().lower() in NOISE_EXACT:
                continue
            records.append({"agent": s.agent, "project": s.project, "session": s.id,
                            "dt": dt, "text": text})

    rows = digest(kept)
    clusters = cluster(records, a.min_sessions, a.threshold)
    result = {
        "window": {"since": window.since.date().isoformat(),
                   "until": window.until.date().isoformat(),
                   "agents": list(agents), "projects": list(window.include),
                   "excluded": list(window.exclude)},
        "scanned": {
            "sessions": len(found),
            "by_agent": dict(Counter(s.agent for s in found)),
            "automated_dropped": 0 if a.include_automated else len(automated),
            "sessions_with_errand": len(rows),
            "prompts": len(records),
            "projects": len({s.project for s in kept}),
        },
        "existing_slash_usage": dict(slash.most_common(25)),
        "sessions": rows,
        "verbatim_repeats": clusters[: a.top],
    }

    if a.errands or a.jev or a.jev_dry_run:
        errand_rows = errands.build(kept, short, a.errand_gap, window)
        # With --jev too, so what Jev adds over code alone shows side by side.
        result["errands"] = errands.look_alikes(errand_rows, tokenize,
                                                min_sessions=a.min_sessions)
    if a.jev or a.jev_dry_run:
        import jev
        result["jev"] = jev.run(errand_rows, tokenize, min_sessions=a.min_sessions,
                                same=a.jev_same, k=a.jev_candidates, dry_run=a.jev_dry_run)
        if result["jev"].get("error"):
            print(f"jev: {result['jev']['error']}", file=sys.stderr)

    if a.json_out:
        Path(a.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                               default=str))
        print(f"wrote {a.json_out}: {len(rows)} session errands, {len(clusters)} "
              f"verbatim-repeat clusters, {len(records)} prompts")
        return 0
    print_chores(result, a.top)
    return 0


def print_chores(result: dict, top: int) -> None:
    s, w = result["scanned"], result["window"]
    by = ", ".join(f"{k} {v}" for k, v in s["by_agent"].items()) or "none"
    print(f"# {s['sessions_with_errand']} session errands · {s['prompts']} typed prompts · "
          f"{s['projects']} projects · {w['since']} → {w['until']}")
    print(f"# sessions by agent: {by}; automated dropped: {s['automated_dropped']}")
    if result["existing_slash_usage"]:
        print("# slash usage: " + ", ".join(
            f"/{k}×{v}" for k, v in list(result["existing_slash_usage"].items())[:12]))
    print("\n## session errands (opening prompt = what the session was for)\n")
    for d in result["sessions"]:
        print(f"{d['date']} · {d['agent']} · {d['project']} · {d['turns']}t/{d['span']} · "
              f"{','.join(d['tools'][:4])}")
        print(f"    {' '.join(d['opener'].split())[:260]}")
    if result["verbatim_repeats"]:
        print("\n## verbatim repeats (same request re-typed — cheapest to script)\n")
        for c in result["verbatim_repeats"][:top]:
            print(f"· {c['sessions']} sessions · {'/'.join(c['agents'])} · "
                  f"{' '.join(c['keywords'][:5])}")
            print(f"    {' '.join(c['examples'][-1]['text'].split())[:160]}")
    if result.get("errands"):
        errands.print_report(result["errands"])
    j = result.get("jev")
    if j:
        import jev
        jev.print_report(j)


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "find":
        return cmd_find(argv[1:])
    if argv and argv[0] == "chores":
        argv = argv[1:]
    return cmd_chores(argv)


if __name__ == "__main__":
    raise SystemExit(main())
