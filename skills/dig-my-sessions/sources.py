"""sources.py — read each coding agent's local session history into one shape.

Every agent keeps its history somewhere different, in a different format. This module is
the only place that knows those formats; everything downstream sees `Session` records.

  claude    ~/.claude/projects/<dir>/<id>.jsonl                   (CLAUDE_CONFIG_DIR)
  codex     ~/.codex/sessions/**/rollout-*.jsonl                   (CODEX_HOME)
  opencode  ~/.local/share/opencode/opencode.db                    (XDG_DATA_HOME)
  cursor    <Cursor user dir>/globalStorage/state.vscdb            (the IDE's agent chats)
  gemini    ~/.gemini/tmp/<name>/chats/session-*.json[l]           (GEMINI_DIR)

Only what the human typed is kept as a prompt. Each agent wraps the user turn in injected
context (environment blocks, skill listings, slash-command bodies); left in, that
machinery is the most repetitive text in the archive and clusters into fake chores.

Read-only, offline, stdlib only. SQLite stores are opened with mode=ro.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

AGENTS = ("claude", "codex", "opencode", "cursor", "gemini")

# Text an agent injects into the user turn. None of it was typed by the human.
SYNTHETIC_PREFIXES = (
    "<system-reminder>", "<local-command", "<command-name>", "<command-message>",
    "<command-args>", "Caveat: The messages below", "[Request interrupted", "API Error",
    "<user-prompt-submit-hook>", "<bash-stdout>", "<bash-input>", "<task-notification>",
    "Stop hook feedback:", "[Image: source:", "Base directory for this skill:",
    "This session is being continued from a previous conversation",
    "Your response above was stopped by a safety classifier",
    # Codex
    "<environment_context>", "<user_instructions>", "<skills_instructions>",
    "<permissions instructions>", "<turn_aborted>", "# AGENTS.md instructions",
    # Gemini
    "<session_context>",
)

# Openers that a program wrote, not a person. A scheduled skill run, an orchestrator
# dispatching a worker, a smoke test. These recur by construction, so left in they top
# every "recurring chore" ranking.
AUTOMATED_OPENERS = (
    re.compile(r"^\s*run the [\w:./-]+ skill\b", re.I),
    re.compile(r"\byou are an? (dispatched |delegated )?(worker|subagent)\b", re.I),
    re.compile(r"\bcarry out this task from my [\w -]*coordinator\b", re.I),
    re.compile(r"^\s*reply (with )?(only )?\W?ok\W?\s*$", re.I),
    re.compile(r"^\s*reply with (exactly|only)\b", re.I),
    re.compile(r"^\s*(this is an? )?(integration|smoke|e2e) test\b", re.I),
)


@dataclass
class Session:
    agent: str
    id: str
    project: str  # the directory the agent ran in, as an absolute path when known
    path: str  # where the record lives, for reading it in full
    first: datetime
    last: datetime
    prompts: list[tuple[datetime, str]] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    automated: str | None = None  # why this session is a program's, not a person's
    resume: str | None = None
    text: Callable[[], str] = lambda: ""  # the whole searchable transcript, read lazily

    @property
    def opener(self) -> str | None:
        """The errand. Later turns are steering; a short first turn ("계속") is a resume."""
        for _, t in self.prompts:
            if len(t) >= 25:
                return t
        return None


@dataclass
class Window:
    since: datetime
    until: datetime
    include: tuple[str, ...] = ()  # project substrings, any of which must match
    exclude: tuple[str, ...] = ()

    def has(self, dt: datetime | None) -> bool:
        return dt is not None and self.since <= dt <= self.until

    def wants(self, project: str) -> bool:
        p = project.lower()
        if self.include and not any(s.lower() in p for s in self.include):
            return False
        return not any(s.lower() in p for s in self.exclude)


def is_typed(text: str) -> bool:
    return bool(text) and not text.startswith(SYNTHETIC_PREFIXES)


# A session whose working directory is a temp dir is an agent testing something (a
# scratchpad, a throwaway fixture), not a person's errand.
TEMP_DIRS = tuple(str(Path(p)) + os.sep for p in {
    "/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
    os.environ.get("TMPDIR", "/tmp").rstrip("/\\") or "/tmp"})


def automated_reason(s: Session) -> str | None:
    if s.automated:
        return s.automated
    project = s.project.rstrip("/\\") + os.sep
    if project.startswith(TEMP_DIRS) and not project.startswith(str(Path.home()) + os.sep):
        return "ran in a temp directory"
    op = s.opener or (s.prompts[0][1] if s.prompts else "")
    for rx in AUTOMATED_OPENERS:
        if rx.search(op):
            return f"opener matches {rx.pattern!r}"
    return None


def parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _mtime(p: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _jsonl(path: Path) -> Iterator[dict]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sqlite(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _finish(s: Session, window: Window) -> Session | None:
    """Keep a session only if it has activity inside the window and a project we want."""
    s.prompts = [(dt, t) for dt, t in s.prompts if window.has(dt)]
    if not (window.has(s.first) or window.has(s.last) or s.prompts):
        return None
    if not window.wants(s.project):
        return None
    s.automated = automated_reason(s)
    return s


# ---------------------------------------------------------------- claude


def _claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _claude_text(path: Path) -> Callable[[], str]:
    def read() -> str:
        keep = []
        for line in _read(path).splitlines():
            # Harness-injected context is not session activity. The skills listing alone
            # made every word of every skill description match the whole archive.
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type") if isinstance(d, dict) else None
            if kind == "system":
                continue
            if kind == "attachment" and (d.get("attachment") or {}).get("type") \
                    not in ("edited_text_file", "nested_memory"):
                continue
            keep.append(line)
        return "\n".join(keep)
    return read


def claude(window: Window) -> Iterator[Session]:
    root = _claude_root()
    if not root.is_dir():
        return
    for proj_dir in sorted(root.iterdir()):
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            m = _mtime(f)
            if m is None or m < window.since:
                continue
            s = Session("claude", f.stem, proj_dir.name, str(f), m, m,
                        resume=f"claude --resume {f.stem}", text=_claude_text(f))
            times = []
            for d in _jsonl(f):
                if d.get("isSidechain"):
                    continue
                dt = parse_ts(d.get("timestamp"))
                if dt is None:
                    continue
                times.append(dt)
                if d.get("cwd") and s.project == proj_dir.name:
                    s.project = d["cwd"]
                if d.get("type") == "assistant":
                    content = (d.get("message") or {}).get("content")
                    if isinstance(content, list):
                        s.tools.update(b.get("name") for b in content
                                       if isinstance(b, dict) and b.get("type") == "tool_use")
                    continue
                if d.get("type") != "user":
                    continue
                src = d.get("promptSource")
                if src == "system":
                    continue
                if src == "sdk" or str(d.get("entrypoint", "")).startswith("sdk"):
                    s.automated = s.automated or f"started by the SDK ({d.get('entrypoint')})"
                content = (d.get("message") or {}).get("content")
                # A typed prompt arrives as a plain string. A TEXT-ONLY list is always
                # machinery (an expanded slash-command body, a tool_result); a list is
                # the user's only when it carries an image.
                if isinstance(content, list):
                    if not any(isinstance(b, dict) and b.get("type") == "image" for b in content):
                        continue
                    content = "\n".join(b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(content, str) and is_typed(content.strip()):
                    s.prompts.append((dt, content.strip()))
            if times:
                s.first, s.last = min(times), max(times)
            if (s := _finish(s, window)) is not None:
                yield s


# ---------------------------------------------------------------- codex


def _codex_text(path: Path) -> Callable[[], str]:
    def read() -> str:
        keep = []
        for d in _jsonl(path):
            kind = d.get("type")
            p = d.get("payload") or {}
            # Instructions, skill listings and environment snapshots repeat in every file;
            # `compacted` is a copy of history already read, skill listing included.
            if kind in ("session_meta", "world_state", "turn_context", "token_usage_record",
                        "compacted"):
                continue
            if kind == "response_item" and p.get("type") == "message" \
                    and p.get("role") in ("developer", "system"):
                continue
            if kind == "event_msg" and p.get("type") in ("token_count", "task_started"):
                continue
            keep.append(json.dumps(d, ensure_ascii=False))
        return "\n".join(keep)
    return read


def _codex_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def codex(window: Window) -> Iterator[Session]:
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    if not root.is_dir():
        return
    for f in sorted(root.rglob("rollout-*.jsonl")):
        m = _mtime(f)
        if m is None or m < window.since:
            continue
        s = Session("codex", f.stem, "", str(f), m, m, text=_codex_text(f))
        # Codex has recorded the user turn three ways across versions. Take the first
        # that the file has, so one prompt is never counted twice.
        typed: dict[str, list[tuple[datetime, str]]] = {"event": [], "item": [], "response": []}
        times = []
        for d in _jsonl(f):
            dt = parse_ts(d.get("timestamp"))
            p = d.get("payload") or {}
            kind = d.get("type")
            if kind == "session_meta":
                s.id = p.get("id") or p.get("session_id") or s.id
                s.project = p.get("cwd") or s.project
                if p.get("source") == "exec" or p.get("originator") == "codex_exec":
                    s.automated = "non-interactive `codex exec`"
                continue
            if dt is None:
                continue
            times.append(dt)
            ptype = p.get("type")
            if kind == "turn_context" and not s.project:
                s.project = p.get("cwd") or ""
            elif kind == "event_msg" and ptype == "user_message":
                typed["event"].append((dt, str(p.get("message") or "")))
            elif kind == "event_msg" and ptype == "item_completed" \
                    and (p.get("item") or {}).get("type") == "UserMessage":
                typed["item"].append((dt, _codex_message_text(p["item"].get("content"))))
            elif kind == "response_item" and ptype == "message" and p.get("role") == "user":
                typed["response"].append((dt, _codex_message_text(p.get("content"))))
            elif kind == "response_item" and ptype in ("function_call", "custom_tool_call",
                                                        "local_shell_call"):
                s.tools[p.get("name") or ptype] += 1
        for key in ("event", "item", "response"):
            if typed[key]:
                s.prompts = [(dt, t.strip()) for dt, t in typed[key] if is_typed(t.strip())]
                break
        if times:
            s.first, s.last = min(times), max(times)
        s.resume = f"codex resume {s.id}"
        if (s := _finish(s, window)) is not None:
            yield s


# ---------------------------------------------------------------- opencode


def opencode(window: Window) -> Iterator[Session]:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    db_path = data / "opencode" / "opencode.db"
    con = _sqlite(db_path)
    if con is None:
        return
    try:
        rows = con.execute(
            "select id, directory, time_created, time_updated from session"
            " where parent_id is null and time_updated >= ?",
            (int(window.since.timestamp() * 1000),)).fetchall()
        parts = con.execute(
            "select p.session_id, p.time_created, json_extract(m.data, '$.role'), p.data"
            " from part p join message m on m.id = p.message_id"
            " where p.time_created >= ? order by p.time_created",
            (int(window.since.timestamp() * 1000),)).fetchall()
    except sqlite3.Error as e:
        print(f"opencode: cannot read {db_path}: {e}", file=sys.stderr)
        return
    finally:
        con.close()
    by_session: dict[str, list] = {}
    for sid, created, role, raw in parts:
        by_session.setdefault(sid, []).append((created, role, raw))
    for sid, directory, created, updated in rows:
        texts: list[str] = []
        s = Session("opencode", sid, directory or "", f"{db_path}#{sid}",
                    parse_ts(created), parse_ts(updated),
                    resume=f"opencode --session {sid}", text=lambda t=texts: "\n".join(t))
        for ts, role, raw in by_session.get(sid, ()):
            try:
                p = json.loads(raw)
            except ValueError:
                texts.append(raw)
                continue
            # Decoded, so text stored with \u escapes still matches what the user types.
            texts.append(json.dumps(p, ensure_ascii=False))
            if role == "user" and p.get("type") == "text" and not p.get("synthetic"):
                text = str(p.get("text") or "").strip()
                if is_typed(text):
                    s.prompts.append((parse_ts(ts), text))
            elif role == "assistant" and p.get("type") == "tool":
                s.tools[p.get("tool") or "tool"] += 1
        if (s := _finish(s, window)) is not None:
            yield s


# ---------------------------------------------------------------- cursor


def _cursor_user_dir() -> Path:
    if os.environ.get("CURSOR_USER_DIR"):
        return Path(os.environ["CURSOR_USER_DIR"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Cursor" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Cursor" / "User"


def _cursor_folders(user_dir: Path) -> dict[str, str]:
    """composerId → workspace folder, from each workspace's own state store."""
    out: dict[str, str] = {}
    for ws in (user_dir / "workspaceStorage").glob("*/"):
        try:
            folder = json.loads(_read(ws / "workspace.json") or "{}").get("folder", "")
        except ValueError:
            continue
        con = _sqlite(ws / "state.vscdb")
        if con is None:
            continue
        try:
            row = con.execute(
                "select value from ItemTable where key = 'composer.composerData'").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        try:
            composers = json.loads(row[0]).get("allComposers", []) if row and row[0] else []
        except ValueError:
            composers = []
        path = folder[7:] if folder.startswith("file://") else folder
        for c in composers:
            if isinstance(c, dict) and c.get("composerId"):
                out[c["composerId"]] = path
    return out


def cursor(window: Window) -> Iterator[Session]:
    user_dir = _cursor_user_dir()
    db_path = user_dir / "globalStorage" / "state.vscdb"
    con = _sqlite(db_path)
    if con is None:
        return
    try:
        composers = con.execute(
            "select key, value from cursorDiskKV where key like 'composerData:%'").fetchall()
        bubbles = con.execute(
            "select key, value from cursorDiskKV where key like 'bubbleId:%'").fetchall()
    except sqlite3.Error as e:
        print(f"cursor: cannot read {db_path}: {e}", file=sys.stderr)
        return
    finally:
        con.close()
    by_composer: dict[str, dict[str, dict]] = {}
    for key, raw in bubbles:
        _, cid, bid = (key.split(":") + ["", ""])[:3]
        try:
            by_composer.setdefault(cid, {})[bid] = json.loads(raw) if raw else {}
        except ValueError:
            continue
    folders = _cursor_folders(user_dir)
    for key, raw in composers:
        try:
            c = json.loads(raw) if raw else None
        except ValueError:
            continue
        if not isinstance(c, dict):
            continue
        cid = c.get("composerId") or key.split(":", 1)[1]
        uri = (c.get("workspaceIdentifier") or {}).get("uri") or {}
        project = uri.get("fsPath") or folders.get(cid, "")
        mine = by_composer.get(cid, {})
        order = [h.get("bubbleId") for h in c.get("fullConversationHeadersOnly") or []
                 if isinstance(h, dict)] or list(mine)
        texts: list[str] = []
        s = Session("cursor", cid, project, f"{db_path}#composerData:{cid}",
                    parse_ts(c.get("createdAt")), parse_ts(c.get("lastUpdatedAt")),
                    text=lambda t=texts: "\n".join(t))
        if s.first is None:
            continue
        s.last = s.last or s.first
        for bid in order:
            b = mine.get(bid) or {}
            text = str(b.get("text") or "").strip()
            if text:
                texts.append(text)
            tool = b.get("toolFormerData") or {}
            if tool.get("name"):
                s.tools[tool["name"]] += 1
                texts.append(json.dumps(tool, ensure_ascii=False)[:4000])
            if b.get("type") == 1 and is_typed(text):
                s.prompts.append((parse_ts(b.get("createdAt")) or s.first, text))
        if (s := _finish(s, window)) is not None:
            yield s


# ---------------------------------------------------------------- gemini


def _gemini_messages(path: Path) -> tuple[dict, list[dict]]:
    """Gemini wrote one JSON document per session, then moved to an append log whose
    `$set` lines replace the message list. Read both into (header, messages)."""
    if path.suffix == ".json":
        try:
            d = json.loads(_read(path) or "{}")
        except ValueError:
            return {}, []
        return d, list(d.get("messages") or [])
    header, messages = {}, {}
    for d in _jsonl(path):
        if "sessionId" in d and "messages" not in d and "type" not in d:
            header = d
        elif isinstance(d.get("$set"), dict) and "messages" in d["$set"]:
            messages = {m.get("id"): m for m in d["$set"]["messages"] if isinstance(m, dict)}
        elif "type" in d and "id" in d:
            messages[d["id"]] = d
    return header, list(messages.values())


def _gemini_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def gemini(window: Window) -> Iterator[Session]:
    home = Path(os.environ.get("GEMINI_DIR") or Path.home() / ".gemini")
    try:
        names = json.loads(_read(home / "projects.json") or "{}").get("projects", {})
    except ValueError:
        names = {}
    folder_of = {v: k for k, v in names.items()}
    for f in sorted((home / "tmp").glob("*/chats/session-*.json*")):
        m = _mtime(f)
        if m is None or m < window.since:
            continue
        header, messages = _gemini_messages(f)
        sid = header.get("sessionId") or f.stem
        texts: list[str] = []
        s = Session("gemini", sid, folder_of.get(f.parent.parent.name, f.parent.parent.name),
                    str(f), parse_ts(header.get("startTime")) or m,
                    parse_ts(header.get("lastUpdated")) or m, text=lambda t=texts: "\n".join(t))
        for msg in sorted(messages, key=lambda x: str(x.get("timestamp"))):
            text = _gemini_text(msg.get("content")).strip()
            if msg.get("type") != "info":
                texts.append(text)
            for call in msg.get("toolCalls") or []:
                if isinstance(call, dict):
                    s.tools[call.get("name") or "tool"] += 1
                    texts.append(json.dumps(call, ensure_ascii=False)[:4000])
            if msg.get("type") == "user" and is_typed(text):
                s.prompts.append((parse_ts(msg.get("timestamp")) or s.first, text))
        if (s := _finish(s, window)) is not None:
            yield s


READERS = {"claude": claude, "codex": codex, "opencode": opencode, "cursor": cursor,
           "gemini": gemini}


def sessions(window: Window, agents=AGENTS) -> list[Session]:
    out: list[Session] = []
    for name in agents:
        out.extend(READERS[name](window))
    return out
