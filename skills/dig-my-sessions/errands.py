"""errands.py — what `--jev` compares: errands, each with a fingerprint of the work done.

An opener alone undersells a session two ways. A session left open for weeks holds many
errands, and only the first one has an opener. And two sessions doing the same chore are
often worded nothing alike ("이어서 보자" / "what is still open?") while the work
they did — the same commands, the same services — is identical.

So a session is cut into errands wherever the person was away for `gap` hours, and each
errand carries a fingerprint of the tool calls made while it ran: shell command heads
(`git fetch`, `gh pr`), MCP servers, skills, URL hosts and the kinds of file edited.

Two fingerprints per errand, because not all of it may leave the machine:

- `local`: everything above, used only on this machine to propose candidate pairs.
- `shared`: what Jev may see. Never hosts, file kinds or anything path-like; a command,
  skill or MCP server only once it appears in at least `SHARE_MIN_ERRANDS` errands, so a
  one-off name (a handle, a file name, a run id) stays here.

Tool calls are read for Claude Code and Codex. The other agents' errands keep their tool
names only, which still separates a chat from a session that edited files.
"""

from __future__ import annotations

import json
import re
import shlex
from collections import Counter
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import sources

GAP_HOURS = 3.0
SHARE_MIN_ERRANDS = 3
OPENER_MIN = 25  # as Session.opener: a short first turn ("계속") is a resume, not an errand

URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
LONG_QUOTED_RE = re.compile(r"(['\"])(?:\\.|(?!\1).){80,}?\1", re.S)
# The executable shape a shared command head must have: no path, extension or long number.
COMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
WRAPPERS = {"sudo", "env", "time", "nohup", "uv", "npx", "bunx", "pnpm", "python", "python3",
            "bash", "sh", "zsh", "run", "exec", "x", "dlx"}
# Navigation, text plumbing and shell grammar: present in every errand, telling none apart.
IGNORED = {
    "cd", "ls", "cat", "echo", "head", "tail", "sed", "grep", "rg", "find", "wc", "pwd",
    "mkdir", "true", "false", "sleep", "awk", "sort", "uniq", "printf", "test", "jq", "xargs",
    "tee", "cp", "mv", "rm", "touch", "chmod", "cut", "tr", "date", "stat", "file", "which",
    "command", "type", "diff", "du", "df", "ps", "lsof", "kill", "open", "node",
    "for", "do", "done", "if", "then", "else", "elif", "fi", "while", "until", "case", "esac",
    "function", "return", "exit", "export", "local", "set", "unset", "source",
    "import", "from", "def", "async", "await", "assert", "print", "class", "with", "try",
    "except", "const", "let", "var", "null", "break", "continue", "not", "eval", "wait",
    "warn", "error", "timeout",
}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "edit", "write"}


def command_heads(cmd: str) -> list[str]:
    """`cd x && git fetch origin | tee log` -> ['git fetch']. Heredoc bodies and long
    quoted scripts are dropped first: their lines are code, not commands."""
    cmd = HEREDOC_RE.sub(" ", cmd)
    cmd = LONG_QUOTED_RE.sub("''", cmd)
    out = []
    for part in re.split(r"&&|\|\||;|\||\n", cmd):
        try:
            toks = shlex.split(part, comments=True)
        except ValueError:
            toks = part.split()
        while toks and (toks[0] in WRAPPERS or toks[0].startswith("-") or "=" in toks[0]):
            toks = toks[1:]
        if not toks:
            continue
        head = Path(toks[0]).name
        if head in IGNORED or not COMMAND_RE.match(head):
            continue
        sub = next((t for t in toks[1:3] if COMMAND_RE.match(t)), None)
        out.append(f"{head} {sub}" if sub else head)
    return out


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
            return parsed if isinstance(parsed, dict) else {"cmd": raw}
        except ValueError:
            return {"cmd": raw}
    return {}


def add_call(fp: Counter, name: str | None, raw) -> None:
    name = name or "?"
    if name.startswith("mcp__"):
        server = name.split("__")[1] if name.count("__") >= 2 else name[5:]
        server = server.removeprefix("claude_ai_").removeprefix("plugin_")
        fp[f"mcp:{server.lower()}"] += 1
    else:
        fp[f"tool:{name}"] += 1
    args = _arguments(raw)
    cmd = args.get("command") or args.get("cmd") or args.get("CommandLine")
    if isinstance(cmd, list):
        cmd = " ".join(map(str, cmd[2:] if cmd[:1] in (["bash"], ["zsh"], ["sh"]) else cmd))
    if isinstance(cmd, str):
        for h in command_heads(cmd):
            fp[f"cmd:{h}"] += 1
        for h in hosts(cmd):
            fp[f"host:{h}"] += 1
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
    """A session's typed prompts, cut wherever the person was away for `gap` hours."""
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
    out = []
    for i, run in enumerate(runs):
        opener = next((t for _, t in run if len(t) >= OPENER_MIN), None)
        if opener is None:
            continue
        end = runs[i + 1][0][0] if i + 1 < len(runs) else s.last + timedelta(seconds=1)
        out.append({"start": run[0][0], "end": end, "opener": opener, "turns": len(run)})
    return out


def build(sessions_: list, short, gap: float = GAP_HOURS) -> list[dict]:
    """One row per errand, in the shape `jev.run` takes. `short` shortens a project path."""
    rows, seen = [], set()
    for s in sessions_:
        calls = list(CALLS[s.agent](s.path)) if s.agent in CALLS else []
        for e in split(s, gap):
            # Resuming writes a new transcript carrying the old history; keep one copy.
            key = (s.agent, s.project, " ".join(e["opener"].split())[:120])
            if key in seen:
                continue
            seen.add(key)
            fp = Counter()
            if calls:
                lo = e["start"] - timedelta(minutes=1)
                for dt, name, raw in calls:
                    if dt and lo <= dt < e["end"]:
                        add_call(fp, name, raw)
            else:
                fp.update({f"tool:{t}": c for t, c in s.tools.items() if t})
            rows.append({
                "id": f"{s.id}@{e['start']:%Y%m%d%H%M}",
                "session": s.id,
                "agent": s.agent,
                "date": e["start"].astimezone().strftime("%Y-%m-%d %H:%M"),
                "project": short(s.project),
                "turns": e["turns"],
                "opener": e["opener"][:400],
                "local": dict(fp),
                "path": s.path,
            })
    rows.sort(key=lambda r: r["date"])
    share(rows)
    return rows


def share(rows: list[dict], min_errands: int = SHARE_MIN_ERRANDS) -> None:
    """Set each row's `shared` fingerprint: the vocabulary common across the scan."""
    seen_in = Counter(k for r in rows for k in r["local"])
    for r in rows:
        tools = [k[5:] for k, _ in Counter(r["local"]).most_common()
                 if k.startswith("tool:") and not k.startswith("tool:mcp")][:5]
        did = [k for k, c in Counter(r["local"]).most_common()
               if k.split(":", 1)[0] in ("cmd", "mcp", "skill")
               and seen_in[k] >= min_errands
               and all(COMMAND_RE.match(w) for w in k.split(":", 1)[1].split())]
        r["shared"] = {"tools": tools, "did": [k.split(":", 1)[1] for k in did[:12]]}


def pairing_features(row: dict) -> set[str]:
    """What candidate pairing compares, locally: a feature seen once in an errand is
    incidental; services, skills and hosts count once."""
    return {k for k, c in row["local"].items() if not k.startswith("tool:")
            and (c >= 2 or k.split(":", 1)[0] in ("mcp", "skill", "host"))}
