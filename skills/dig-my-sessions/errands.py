"""errands.py — `dig.py chores --errands`: errands, each with a fingerprint of the work done.

An opener alone undersells a session two ways. A session left open for weeks holds many
errands, and only the first one has an opener. And two sessions doing the same chore are
often worded nothing alike ("이어서 보자" / "what is still open?") while the work they did
— the same commands, the same services — is identical.

So a session is cut into errands wherever the person was away for `gap` hours, and each
errand carries a fingerprint of the tool calls made while it ran: shell command heads,
skills, MCP servers, URL hosts and the kinds of file edited.

Two fingerprints per errand, because not all of it may leave the machine:

- `local`: everything above, used only on this machine: to group errands that look alike
  (`look_alikes`, offline) and to propose the pairs `--jev` asks about.
- `shared`: what Jev may see, drawn from a fixed public vocabulary and nothing else:
  built-in tool names (PUBLIC_TOOLS) and well-known commands with their well-known
  subcommands (PUBLIC_COMMANDS). Never skills, MCP servers, hosts, paths, file names or
  command arguments. How often a word recurs is no proof it is public: a recurring chore
  repeats its private host as faithfully as its `git fetch`.

Tool calls are read for Claude Code and Codex. The other agents' errands keep their tool
names only, which still separates a chat from a session that edited files.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from pathlib import Path
from urllib.parse import urlparse

import sources

GAP_HOURS = 3.0
WORDS_SAME = 0.34  # opener similarity at which two errands look alike (as verbatim repeats)
WORK_SAME = 0.5    # fingerprint similarity at which two errands look alike
WORK_MIN = 3       # an errand that touched fewer things than this says too little to compare
OPENER_MIN = 25  # as Session.opener: a short first turn ("계속") is a resume, not an errand
LOOKBACK = timedelta(minutes=1)  # the first prompt's own tool calls can carry its timestamp

# Built-in tool names of the agents read here. Anything else (a custom tool, an MCP tool
# under another naming scheme) may carry a private name and never leaves the machine.
PUBLIC_TOOLS = frozenset({
    # Claude Code
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS",
    "WebFetch", "WebSearch", "Agent", "Task", "Skill", "TodoWrite", "ToolSearch",
    "AskUserQuestion",
    # Codex
    "exec_command", "write_stdin", "apply_patch", "shell", "local_shell_call", "update_plan",
    "view_image", "web_search",
    # opencode
    "bash", "read", "write", "edit", "glob", "grep", "list", "webfetch", "task", "todowrite",
    "skill",
    # Cursor
    "read_file_v2", "edit_file_v2", "run_terminal_command_v2", "ripgrep_raw_search",
    "glob_file_search",
    # Antigravity CLI
    "view_file", "run_command",
})

# Well-known executables, each with the subcommands that may be named alongside it. An
# empty set shares the executable alone: whatever follows `ssh` or `mysql` is a target.
_GIT = {"add", "am", "apply", "bisect", "blame", "branch", "check-ignore", "checkout",
        "cherry-pick", "clean", "clone", "commit", "config", "diff", "fetch", "grep", "init",
        "log", "ls-files", "ls-remote", "ls-tree", "merge", "mv", "pull", "push", "rebase",
        "reflog", "remote", "reset", "restore", "rev-parse", "revert", "rm", "show", "stash",
        "status", "switch", "tag", "worktree"}
_JS = {"add", "audit", "build", "ci", "dev", "exec", "install", "lint", "ls", "outdated",
       "publish", "remove", "run", "start", "test", "update", "view"}
PUBLIC_COMMANDS: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in {
    "git": _GIT,
    "gh": {"api", "auth", "browse", "gist", "issue", "pr", "release", "repo", "run",
           "search", "workflow"},
    "npm": _JS, "pnpm": _JS, "yarn": _JS, "bun": _JS,
    "docker": {"build", "compose", "exec", "images", "logs", "ps", "pull", "push", "rm",
               "run", "stop"},
    "kubectl": {"apply", "delete", "describe", "exec", "get", "logs", "port-forward",
                "rollout"},
    "cargo": {"add", "build", "check", "clippy", "fmt", "install", "run", "test"},
    "go": {"build", "get", "mod", "run", "test", "vet"},
    "pip": {"install", "list", "show", "uninstall"}, "pip3": {"install", "list", "show"},
    "uv": {"add", "lock", "pip", "run", "sync", "venv"},
    "poetry": {"add", "install", "lock", "run"},
    "brew": {"info", "install", "list", "services", "uninstall", "upgrade"},
    "vercel": {"deploy", "env", "link", "logs", "ls", "project", "whoami"},
    "orca": {"linear", "open", "orchestration", "skills", "status", "tab", "terminal",
             "worktree"},
    "claude": {"config", "mcp", "plugin"}, "codex": {"exec", "mcp", "plugin", "resume"},
    "terraform": {"apply", "init", "plan", "validate"},
    "helm": {"install", "list", "upgrade"},
    **{bare: set() for bare in (
        "aws", "az", "bash", "black", "curl", "deno", "eslint", "ffmpeg", "ffprobe",
        "firebase", "gcloud", "gradle", "launchctl", "make", "mvn", "mypy", "mysql", "node",
        "openssl", "osascript", "pgrep", "pkill", "prettier", "psql", "pytest", "python",
        "python3", "redis-cli", "rsync", "ruff", "scp", "shasum", "sips", "sqlite3", "ssh",
        "sudo", "supabase", "swift", "tar", "tmux", "tsc", "unittest", "unzip", "vitest",
        "wget", "wrangler", "xcodebuild", "zip", "zsh")},
}.items()}

SHELL_TOOLS = {"Bash", "bash", "shell", "exec_command", "local_shell_call", "shell_command",
               "run_command", "run_terminal_command_v2"}
PATCH_TOOLS = {"apply_patch"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "edit", "write"}
SHELLS = {"bash", "sh", "zsh"}
# Prefixes that run the next word: `sudo git push` is `git push`. Followed by an option,
# they are named alone — the option's value (`sudo -u alice`) must not become the head.
WRAPPERS = {"sudo", "env", "nohup", "time", "exec", "npx", "bunx"}
RUNNERS = {("uv", "run"), ("poetry", "run"), ("pnpm", "exec"), ("pnpm", "dlx"),
           ("npm", "exec"), ("yarn", "dlx")}
# Navigation, text plumbing and shell grammar: present in every errand, telling none apart.
IGNORED = {
    "cd", "ls", "cat", "echo", "head", "tail", "sed", "grep", "rg", "find", "wc", "pwd",
    "mkdir", "true", "false", "sleep", "awk", "sort", "uniq", "printf", "test", "jq", "xargs",
    "tee", "cp", "mv", "rm", "touch", "chmod", "cut", "tr", "date", "stat", "file", "which",
    "command", "type", "diff", "du", "df", "ps", "lsof", "kill", "open", "[", "[[",
    "for", "do", "done", "if", "then", "else", "elif", "fi", "while", "until", "case", "esac",
    "function", "return", "exit", "export", "local", "set", "unset", "source", ".",
}
WORD_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
SEPARATORS = {";", "&", "&&", "|", "||", "\n", "(", ")", "|&", ";;"}
SHELL_FLAGS = ("-c", "-lc", "-ic")


def _tokens(cmd: str) -> list[str] | None:
    """Shell words and operators, quotes respected; None when the command won't parse."""
    lex = shlex.shlex(HEREDOC_RE.sub(" ", cmd), posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _simple_commands(toks: list[str]):
    """Split at operators; drop redirect targets (`> out` names a file, not a command)."""
    cur, skip = [], False
    for t in toks:
        if skip:
            skip = False
        elif t in SEPARATORS:
            if cur:
                yield cur
            cur = []
        elif t and set(t) <= set("<>&"):
            skip = True
        else:
            cur.append(t)
    if cur:
        yield cur


def command_heads(cmd: str, depth: int = 0) -> list[str]:
    """`cd x && git fetch origin | tee log` -> ['git fetch']. Each entry is an executable,
    plus the word right after it when that word is not an option. Heredoc bodies, quoted
    arguments and redirect targets never become heads; `bash -lc '…'` is unwrapped."""
    toks = _tokens(cmd)
    if toks is None:
        return []
    out = []
    for words in _simple_commands(toks):
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]  # FOO=1 cmd
        while words:
            name = Path(words[0]).name
            if name in SHELLS and len(words) > 2 and words[1] in SHELL_FLAGS:
                if depth < 2:
                    out.extend(command_heads(words[2], depth + 1))
                words = []
            elif name in WRAPPERS or (len(words) > 1 and (name, words[1]) in RUNNERS):
                step = 1 if name in WRAPPERS else 2
                if len(words) <= step or words[step].startswith("-"):
                    out.append(" ".join([name] + words[1:step]))
                    words = []
                else:
                    words = words[step:]
                continue
            elif name in ("python", "python3") and len(words) > 2 and words[1] == "-m":
                words = words[2:]
                continue
            break
        if not words:
            continue
        head = Path(words[0]).name
        if head in IGNORED or not WORD_RE.match(head):
            continue
        sub = words[1] if len(words) > 1 and WORD_RE.match(words[1]) else None
        out.append(f"{head} {sub}" if sub else head)
    return out


def public_command(head: str) -> str | None:
    """The shareable form of a local command head, or None if none of it is public."""
    exe, _, sub = head.partition(" ")
    allowed = PUBLIC_COMMANDS.get(exe)
    if allowed is None:
        return None
    return f"{exe} {sub}" if sub in allowed else exe


def hosts(text: str) -> set[str]:
    out = set()
    for u in URL_RE.findall(text or ""):
        try:
            h = urlparse(u).hostname or ""
        except ValueError:
            continue
        if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", h):
            out.add(".".join(h.split(".")[-2:]))
    return out


def _arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _shell_text(raw) -> str | None:
    args = _arguments(raw)
    cmd = args.get("command") or args.get("cmd") or args.get("CommandLine")
    if cmd is None and isinstance(raw, str) and not args:
        cmd = raw  # a freeform shell tool's input is the command itself
    if isinstance(cmd, list):
        argv = [str(c) for c in cmd]
        if len(argv) > 2 and Path(argv[0]).name in SHELLS and argv[1] in SHELL_FLAGS:
            return argv[2]
        return shlex.join(argv)
    return cmd if isinstance(cmd, str) else None


def add_call(fp: Counter, name: str | None, raw) -> None:
    name = name or "?"
    if name.startswith("mcp__"):
        server = name.split("__")[1] if name.count("__") >= 2 else name[5:]
        server = server.removeprefix("claude_ai_").removeprefix("plugin_")
        fp[f"mcp:{server.lower().replace('_', '-')}"] += 1
        return
    fp[f"tool:{name}"] += 1
    if name in SHELL_TOOLS:
        cmd = _shell_text(raw)
        if cmd:
            for h in command_heads(cmd):
                fp[f"cmd:{h}"] += 1
            for h in hosts(cmd):
                fp[f"host:{h}"] += 1
        return
    if name in PATCH_TOOLS:
        # A patch is file content. Only its file headers are read, for the kind of file.
        args = _arguments(raw)
        text = raw if isinstance(raw, str) else str(args.get("input") or args.get("patch") or "")
        for target in PATCH_FILE_RE.findall(text):
            if Path(target.strip()).suffix:
                fp[f"edits:{Path(target.strip()).suffix.lower()}"] += 1
        return
    args = _arguments(raw)
    if name == "Skill" and isinstance(args.get("skill"), str):
        fp[f"skill:{args['skill'].split(':')[-1].lower()}"] += 1
    for h in hosts(str(args.get("url") or "")):
        fp[f"host:{h}"] += 1
    if name in EDIT_TOOLS:
        target = str(args.get("file_path") or args.get("filePath") or "")
        if Path(target).suffix:
            fp[f"edits:{Path(target).suffix.lower()}"] += 1


def _claude_calls(path: str):
    for d in sources._jsonl(Path(path)):
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        dt = sources.parse_ts(d.get("timestamp"))
        for b in (d.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                yield dt, b.get("name"), b.get("input")


def _codex_calls(path: str):
    for d in sources._jsonl(Path(path)):
        p = d.get("payload") or {}
        if d.get("type") == "response_item" and p.get("type") in (
                "function_call", "custom_tool_call", "local_shell_call"):
            yield (sources.parse_ts(d.get("timestamp")), p.get("name") or p.get("type"),
                   p.get("arguments") or p.get("input") or p.get("action"))


CALLS = {"claude": _claude_calls, "codex": _codex_calls}


def split(s, gap: float) -> list[dict]:
    """A session's typed prompts, cut wherever the person was away for `gap` hours. A run
    with no real opener ("계속", "fix") continues the errand before it, tool calls and all."""
    prompts = sorted(s.prompts, key=lambda p: p[0])
    if not prompts:
        return []
    runs, cur = [], [prompts[0]]
    for prev, p in zip(prompts, prompts[1:]):
        if p[0] - prev[0] >= timedelta(hours=gap):
            runs.append(cur)
            cur = []
        cur.append(p)
    runs.append(cur)
    merged, pending = [], []
    for run in runs:
        if any(len(t) >= OPENER_MIN for _, t in run):
            merged.append(pending + run)
            pending = []
        elif merged:
            merged[-1].extend(run)
        else:
            pending.extend(run)  # a stub before the first real errand belongs to it
    out = []
    for i, run in enumerate(merged):
        end = merged[i + 1][0][0] if i + 1 < len(merged) else s.last + timedelta(seconds=1)
        out.append({"start": run[0][0], "end": end, "turns": len(run),
                    "opener": next(t for _, t in run if len(t) >= OPENER_MIN)})
    return out


def build(sessions_: list, short, gap: float = GAP_HOURS, window=None) -> list[dict]:
    """One row per errand, in the shape `jev.run` takes. `short` shortens a project path;
    `window`, when given, bounds which tool calls count (a transcript runs past it)."""
    rows, seen = [], {}
    for s in sessions_:
        calls = list(CALLS[s.agent](s.path)) if s.agent in CALLS else []
        for n, e in enumerate(split(s, gap)):
            # Each call belongs to exactly one errand: [start, next start), with the first
            # errand reaching back a minute for calls stamped just before its prompt.
            lo, hi = (e["start"] - LOOKBACK if n == 0 else e["start"]), e["end"]
            if window is not None:
                lo, hi = max(lo, window.since), min(hi, window.until)
            fp = Counter()
            if calls:
                for dt, name, raw in calls:
                    if dt and lo <= dt < hi:
                        add_call(fp, name, raw)
            else:
                fp.update({f"tool:{t}": c for t, c in s.tools.items() if t})
            # Resuming writes a new transcript carrying the old history, timestamps and
            # all; the same opener at the same minute is that copy. Merge, don't drop.
            key = (s.agent, s.project, " ".join(e["opener"].split())[:120],
                   e["start"].strftime("%Y%m%d%H%M"))
            if key in seen:
                row = seen[key]
                row["turns"] = max(row["turns"], e["turns"])
                for k, v in fp.items():
                    row["local"][k] = max(row["local"].get(k, 0), v)
                continue
            row = {
                "id": f"{s.id}@{e['start']:%Y%m%d%H%M}",
                "session": s.id,
                "agent": s.agent,
                "date": e["start"].astimezone().strftime("%Y-%m-%d %H:%M"),
                "project": short(s.project),
                "turns": e["turns"],
                "opener": e["opener"][:400],
                "local": dict(fp),
                "path": s.path,
            }
            seen[key] = row
            rows.append(row)
    rows.sort(key=lambda r: r["date"])
    for r in rows:
        r["shared"] = shared(r["local"])
    return rows


def shared(local: dict) -> dict:
    """The part of a local fingerprint that may leave the machine: the fixed vocabulary."""
    ranked = Counter(local).most_common()
    tools = [k[5:] for k, _ in ranked if k.startswith("tool:") and k[5:] in PUBLIC_TOOLS]
    did = []
    for k, _ in ranked:
        if k.startswith("cmd:"):
            word = public_command(k[4:])
            if word and word not in did:
                did.append(word)
    return {"tools": tools[:5], "did": did[:12]}


def pairing_features(row: dict) -> set[str]:
    """What candidate pairing compares, locally: everything the errand touched, once."""
    return {k for k in row["local"] if not k.startswith("tool:")}


HANGUL = re.compile(r"[가-힣]{2,}")


def _features(text: str, tokenize) -> set[str]:
    """Words, plus character bigrams of Korean words: 배포확인 and 배포점검 share no word
    but share 배포, and a word-only comparison never pairs them."""
    out = set(tokenize(text))
    for w in HANGUL.findall(text):
        out.update("§" + w[i:i + 2] for i in range(len(w) - 1))
    return out


def _idf(sets: list[set]) -> dict:
    df = Counter(t for ts in sets for t in ts)
    n = len(sets) or 1
    return {t: math.log(1 + n / c) for t, c in df.items()}


def opener_weights(rows, tokenize):
    toks = [_features(r["opener"], tokenize) for r in rows]
    return toks, _idf(toks)


def work_weights(rows):
    fps = [pairing_features(r) if "local" in r else set() for r in rows]
    return fps, _idf(fps)


def similarity(a: set, b: set, idf: dict) -> float:
    inter = sum(idf[t] for t in a & b)
    union = sum(idf[t] for t in a | b)
    return inter / union if union else 0.0


def union_groups(n: int, edges) -> list[list[int]]:
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


def summarize(rows: list[dict], members: list[int]) -> dict:
    """What code can count about a group of errands: sessions, distinct days, cadence."""
    dates = [datetime.strptime(rows[i]["date"], "%Y-%m-%d %H:%M") for i in members]
    sessions = {rows[i].get("session", rows[i]["id"]) for i in members}
    days = {d.date() for d in dates}
    return {
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
        "members": [{"date": rows[i]["date"], "agent": rows[i]["agent"],
                     "project": rows[i]["project"], "id": rows[i]["id"],
                     "opener": rows[i]["opener"][:280],
                     "work": [k for k, _ in Counter(rows[i].get("local") or {}).most_common()
                              if not k.startswith("tool:")][:8]}
                    for i in sorted(members)],
    }


def recurs(group: dict, min_sessions: int) -> bool:
    """A chore recurs across sessions or across days: one session left open for a month,
    coming back to the same errand on three days, is recurrence; three errands of one
    afternoon are not."""
    return max(group["sessions"], group["days"]) >= min_sessions


def look_alikes(rows: list[dict], tokenize, *, min_sessions: int, words: float = WORDS_SAME,
                work: float = WORK_SAME) -> dict:
    """Errands that look like the same chore, by code alone and on this machine: openers
    worded alike, or fingerprints alike (the same chore worded nothing alike still runs
    the same commands against the same services). Candidates for the reader to judge,
    not verdicts: code cannot tell a chore from two errands that merely touched the same
    things."""
    toks, idf = opener_weights(rows, tokenize)
    fps, fidf = work_weights(rows)
    edges = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if (similarity(toks[i], toks[j], idf) >= words
                    or (len(fps[i]) >= WORK_MIN and len(fps[j]) >= WORK_MIN
                        and similarity(fps[i], fps[j], fidf) >= work)):
                edges.append((i, j))
    groups = [summarize(rows, m) for m in union_groups(len(rows), edges) if len(m) > 1]
    # Distinct days before session count: sessions minutes apart on one day are one
    # sitting (one prompt retried), not a chore that comes back.
    groups.sort(key=lambda g: (-g["days"], -g["errands"]))
    return {
        "errands": len(rows),
        "sessions": len({r.get("session", r["id"]) for r in rows}),
        "look_alike": [g for g in groups if recurs(g, min_sessions)],
        "below_min_sessions": [g for g in groups if not recurs(g, min_sessions)],
    }


def print_report(e: dict) -> None:
    print(f"\n## errands that look alike ({e['errands']} errands in {e['sessions']} "
          f"sessions; by wording or by the work done — judge them, code only proposes)\n")
    for g in e["look_alike"]:
        print(f"· {g['errands']} errands in {g['sessions']} sessions / {g['days']} days"
              f" · {g['first']} → {g['last']}"
              f" · {'ONE SITTING' if g['one_sitting'] else g['cadence'] or '-'}"
              f" · {'/'.join(g['agents'])} · {', '.join(g['projects'][:3])}")
        for m in g["members"][-3:]:
            print(f"    {m['date']} {' '.join(m['opener'].split())[:150]}")
            if m["work"]:
                print(f"        did: {', '.join(m['work'][:6])}")
