"""jev.py — `dig.py chores --jev`: narrow judgments by Jev, everything countable by code.

A labelling trial settled the split:

- kind (Choice) was useful: recurring_chore / one_off / exploration / other.
- a 0–4 "how automatable" Score was not. It sees one opener, and how much a chore is
  worth automating comes from how often it recurs, which one line cannot show. Its top
  scores were agent-written test prompts and one-off dev work that merely read as
  scriptable.

So recurrence is never asked as a score. Code picks candidate pairs, Jev answers one
yes/no per pair ("the same chore?", a Noul), and code unions the yeses and counts
sessions, days, agents and projects. Sessions a program started are dropped before
anything is sent (in sources.py).

What is compared is an errand (errands.py), not a session: a long session is cut where
the person was away, and each errand carries a fingerprint of the work done. Openers of
the same chore are often worded nothing alike while the commands run are identical, so
candidate pairs come from opener words OR the local fingerprint.

What leaves the machine: each errand's opener, redacted (credentials, cookies, auth
headers, key/token values, emails, URLs, filesystem paths) and cut to 600 characters,
followed by its shared fingerprint: built-in tool names and well-known commands, from the
fixed vocabulary in errands.py. Skills, MCP servers, hosts, file names and command
arguments the agent used stay on the machine; the opener is otherwise the person's own
words, as typed.

  POST {TYPESAFE_BASE_URL:-https://api.typesafe.ai}/v1/systemone
  Authorization: Bearer $TYPESAFE_API_KEY
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from statistics import median

import errands

MAX_CHARS = 600
WORKERS = 8
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

KIND = {
    "type": "choice",
    "instructions": "This is the opening request a person typed to a coding agent. "
                    "What kind of work is it?",
    "criteria": {
        "recurring_chore": "Routine work the person likely does again and again: the same "
                           "procedure, check, sync, cleanup, report or update, with little "
                           "new thinking each time.",
        "one_off": "A specific piece of work done once: build, fix or change one "
                   "particular thing.",
        "exploration": "Learning, research, a question, brainstorming or design "
                       "discussion.",
        "other": "A greeting, a test of the tool, or anything else.",
    },
}
SAME = ("Is `other_request` the same recurring chore as the request in the state: the "
        "same procedure on the same kind of target, so one script or skill would do both, "
        "even if it is worded differently? Each side shows what the person asked and what "
        "the agent then did (tools, commands, services).")

# Order matters: a header line goes before the bare-token rules eat its value.
REDACTIONS = (
    (re.compile(r"(?im)^\s*(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
                r"\s*:.*$"), r"\1: [redacted]"),
    (re.compile(r"(?i)(-H\s*['\"]?\s*(?:authorization|cookie|x-api-key)\s*:)[^'\"\n]*"),
     r"\1 [redacted]"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|secret|token|passw(?:or)?d|session|cookie|"
                r"auth)[\w-]*)\s*[:=]\s*['\"]?[^\s'\"]+"), r"\1=[redacted]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[key]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}"), "[key]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}"), "[key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[key]"),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "[key]"),  # JWT
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b(?:https?|wss?|ftp)://\S+"), "[url]"),
    (re.compile(r"(?<![\w.:/-])(?:~/[\w.@+-]+(?:/[\w.@+-]*)*|"
                r"/[\w.@+-]+(?:/[\w.@+-]*)+|[A-Za-z]:\\[^\s'\"]+)"), "[path]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/_-]{40,}={0,2}"), "[key]"),
)


def redact(text: str) -> str:
    for rx, repl in REDACTIONS:
        text = rx.sub(repl, text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def state_of(row: dict) -> str:
    """What Jev sees of one errand: the redacted opener, then its shared fingerprint."""
    asked = redact(row["opener"])
    shared = row.get("shared") or {}
    if not (shared.get("tools") or shared.get("did")):
        return asked
    # The fingerprint is fixed vocabulary already; redacting it too is the floor under
    # that, should a vocabulary entry ever look like a credential.
    tools = redact(", ".join(shared.get("tools") or [])) or "-"
    did = redact(", ".join(shared.get("did") or [])) or "-"
    return f"asked: {asked}\ntools: {tools} | did: {did}"


class JevError(RuntimeError):
    pass


def _tls() -> ssl.SSLContext:
    """A python.org build on macOS ships without CA certificates until its "Install
    Certificates" step is run; fall back to certifi or the system bundle, never to an
    unverified connection."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca"):
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.isfile(bundle):
            return ssl.create_default_context(cafile=bundle)
    return ctx


def ask(state, questions: dict, key: str, base: str, attempts: int = 5) -> dict:
    body = json.dumps({"state": state, "model": MODEL, "questions": questions},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/systemone", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    ctx = _tls()
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < attempts - 1:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise JevError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < attempts - 1 and not isinstance(getattr(e, "reason", None),
                                                         ssl.SSLError):
                time.sleep(min(30, 2 ** attempt))
                continue
            raise JevError(f"cannot reach {base}: {e}") from None
    raise JevError("unreachable")


HANGUL = re.compile(r"[가-힣]{2,}")


def _features(text: str, tokenize) -> set[str]:
    """Words, plus character bigrams of Korean words: 배포확인 and 배포점검 share no word
    but share 배포, and a word-only comparison never proposes them as a pair."""
    out = set(tokenize(text))
    for w in HANGUL.findall(text):
        out.update("§" + w[i:i + 2] for i in range(len(w) - 1))
    return out


def _weights(rows, tokenize):
    toks = [_features(r["opener"], tokenize) for r in rows]
    df = Counter(t for ts in toks for t in ts)
    n = len(rows) or 1
    return toks, {t: math.log(1 + n / c) for t, c in df.items()}


def _similarity(a: set, b: set, idf: dict) -> float:
    inter = sum(idf[t] for t in a & b)
    union = sum(idf[t] for t in a | b)
    return inter / union if union else 0.0


def candidate_pairs(rows, tokenize, pool: list[int], recurring: list[int], k: int,
                    floor: float = 0.05, all_pairs_up_to: int = 40, fp_floor: float = 0.15):
    """Which pairs Jev is asked about. Code proposes, Jev disposes: a pair never asked
    about can never be grouped, so proposing is generous.

    - every errand in the pool, against its k most similar others by opener words
    - and against its k most similar by local fingerprint: the same chore worded
      differently still runs the same commands
    - errands labelled recurring_chore, against every other one, while there are few
      enough of them (40 → 780 pairs, a few cents)
    """
    toks, idf = _weights(rows, tokenize)
    fps = [errands.pairing_features(r) if "local" in r else set() for r in rows]
    fdf = Counter(t for f in fps for t in f)
    fidf = {t: math.log(1 + len(rows) / c) for t, c in fdf.items()}
    pairs = set()
    for i in pool:
        near = sorted(((_similarity(toks[i], toks[j], idf), j) for j in pool if j != i),
                      reverse=True)
        pairs.update((min(i, j), max(i, j)) for score, j in near[:k] if score >= floor)
        if fps[i]:
            near = sorted(((_similarity(fps[i], fps[j], fidf), j) for j in pool
                           if j != i and fps[j]), reverse=True)
            pairs.update((min(i, j), max(i, j)) for score, j in near[:k] if score >= fp_floor)
    if len(recurring) <= all_pairs_up_to:
        pairs.update((a, b) for a in recurring for b in recurring if a < b)
    asks: dict[int, list[int]] = {}
    for a, b in sorted(pairs):
        asks.setdefault(a, []).append(b)
    return asks


def _groups(n: int, edges) -> list[list[int]]:
    parent = list(range(n))

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[root(a)] = root(b)
    out: dict[int, list[int]] = {}
    for i in range(n):
        out.setdefault(root(i), []).append(i)
    return list(out.values())


def _cadence(dates: list[datetime]) -> str | None:
    days = sorted({d.date() for d in dates})
    if len(days) < 2:
        return None
    gap = median((b - a).days for a, b in zip(days, days[1:]))
    return f"every ~{gap:g}d"


def run(rows: list[dict], tokenize, *, min_sessions: int, same: float, k: int,
        dry_run: bool) -> dict:
    states = [state_of(r) for r in rows]
    tokens_est = sum(len(s) for s in states) // 2 + 120 * len(states)
    plan = {
        "model": MODEL,
        "errands": len(rows),
        "sessions": len({r.get("session", r["id"]) for r in rows}),
        "step1_requests": len(rows),
        "step1_tokens_estimate": tokens_est,
        "sent": "each errand's opener, redacted and cut to 600 chars, with built-in tool "
                "names and well-known commands from a fixed vocabulary",
        "sample": states[:3],
    }
    if dry_run:
        return {"dry_run": True, "plan": plan}
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        return {"error": "TYPESAFE_API_KEY is not set; nothing was sent", "plan": plan}
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
    usage = Counter()

    def call(state, questions):
        r = ask(state, questions, key, base)
        usage.update({k_: v for k_, v in (r.get("usage") or {}).items()
                      if isinstance(v, int)})
        return r.get("answers") or {}

    try:
        # 1. kind, one request per errand.
        with ThreadPoolExecutor(WORKERS) as ex:
            kinds = list(ex.map(lambda s: call(s, {"kind": KIND}).get("kind") or {}, states))
        # 2. "same chore?" for each errand's nearest neighbours. Every kind stays in the
        #    pool: a status check reads as exploration as often as it reads as a chore,
        #    and an errand left out here can never be grouped.
        pool = list(range(len(rows)))
        recurring = [i for i, kd in enumerate(kinds) if kd.get("choice") == "recurring_chore"]
        asks = candidate_pairs(rows, tokenize, pool, recurring, k)

        def same_chore(item):
            i, js = item
            qs = {f"same_{j}": {"type": "noul", "instructions": {
                "other_request": states[j], "question": SAME}} for j in js}
            ans = call(states[i], qs)
            return [(i, j, (ans.get(f"same_{j}") or {}).get("noul", 0.0)) for j in js]

        # One request per errand, its candidates as parallel questions in it, chunked
        # so a request stays well inside the context limit.
        batches = [(i, js[n:n + 16]) for i, js in asks.items() for n in range(0, len(js), 16)]
        with ThreadPoolExecutor(WORKERS) as ex:
            judged = [p for batch in ex.map(same_chore, batches) for p in batch]
    except JevError as e:
        return {"error": str(e), "plan": plan, "usage": dict(usage)}

    # 3. Counting is code's.
    edges = [(i, j) for i, j, p in judged if p >= same]
    groups = []
    for members in _groups(len(rows), edges):
        if len(members) < 2 and kinds[members[0]].get("choice") != "recurring_chore":
            continue
        dates = [datetime.strptime(rows[i]["date"], "%Y-%m-%d %H:%M") for i in members]
        sessions = {rows[i].get("session", rows[i]["id"]) for i in members}
        days = {d.date() for d in dates}
        p_recur = sum(kinds[i].get("probabilities", {}).get("recurring_chore", 0.0)
                      for i in members) / len(members)
        groups.append({
            "sessions": len(sessions),
            "errands": len(members),
            "days": len(days),
            "first": min(dates).date().isoformat(),
            "last": max(dates).date().isoformat(),
            "cadence": _cadence(dates),
            # Retries minutes apart, or errands of one session on one day: not recurrence.
            "one_sitting": len(members) > 1 and (
                max(dates) - min(dates) < timedelta(hours=3)
                or (len(sessions) == 1 and len(days) == 1)),
            "agents": sorted({rows[i]["agent"] for i in members}),
            "projects": [p for p, _ in Counter(rows[i]["project"] for i in members).most_common()],
            "kinds": dict(Counter(kinds[i].get("choice") for i in members)),
            "p_recurring": round(p_recur, 2),
            "members": [{"date": rows[i]["date"], "agent": rows[i]["agent"],
                         "project": rows[i]["project"], "id": rows[i]["id"],
                         "opener": rows[i]["opener"][:280]} for i in sorted(members)],
        })
    # Distinct days before session count: sessions minutes apart on one day are one
    # sitting (one prompt retried), not a chore that comes back.
    # A chore recurs across sessions or across days: one session left open for a month,
    # coming back to the same errand on three days, is recurrence; three errands of one
    # afternoon are not.
    groups.sort(key=lambda g: (-g["days"], -g["errands"], -g["p_recurring"]))

    def recurs(g):
        return max(g["sessions"], g["days"]) >= min_sessions

    shortlist = [g for g in groups if recurs(g)]
    single = [g for g in groups if g["errands"] == 1][:10]
    return {
        "plan": plan,
        "usage": dict(usage),
        "requests": len(rows) + len(batches),
        "pairs_asked": len(judged),
        "pairs_same": len(edges),
        "kinds": dict(Counter(kd.get("choice") for kd in kinds)),
        "labels": [{"id": r["id"], "kind": kd.get("choice"),
                    "confidence": kd.get("confidence")} for r, kd in zip(rows, kinds)],
        "shortlist": shortlist,
        "below_min_sessions": [g for g in groups if g["errands"] > 1 and not recurs(g)],
        "recurring_seen_once": single,
    }


def print_report(j: dict) -> None:
    print("\n## jev")
    if j.get("dry_run") or j.get("error"):
        p = j["plan"]
        if j.get("error"):
            print(f"\nerror: {j['error']}")
        print(f"\nwould send {p['step1_requests']} requests to {p['model']} "
              f"(~{p['step1_tokens_estimate']} input tokens) before the same-chore step; "
              f"sent: {p['sent']}. sample:")
        for s in p["sample"]:
            print(f"    {s[:200]}")
        return
    u = j.get("usage", {})
    print(f"\n{j['requests']} requests · {u.get('input_tokens', '?')} input tokens · "
          f"kinds {j['kinds']} · {j['pairs_same']}/{j['pairs_asked']} pairs judged the same")
    for title, key in (("shortlist (same chore, enough sessions or days)", "shortlist"),
                       ("recurring, but under --min-sessions", "below_min_sessions"),
                       ("labelled recurring, seen once", "recurring_seen_once")):
        if not j.get(key):
            continue
        print(f"\n### {title}\n")
        for g in j[key]:
            print(f"· {g['errands']} errands in {g['sessions']} sessions / {g['days']} days"
                  f" · {g['first']} → {g['last']}"
                  f" · {'ONE SITTING' if g['one_sitting'] else g['cadence'] or '-'}"
                  f" · {'/'.join(g['agents'])} · "
                  f"{', '.join(g['projects'][:3])} · p_recurring {g['p_recurring']}")
            for m in g["members"][-3:]:
                print(f"    {m['date']} {' '.join(m['opener'].split())[:150]}")
