"""Small channel boundary used by the orchestrator."""

from __future__ import annotations

import asyncio
import codecs
import fnmatch
import heapq
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from ..env import redact, redact_file
from ..process import task as tasks

CARD_EXCERPT_CHARS = 300
# The status line carries the evidence as one line, so it gets a line's worth of room.
CARD_EVIDENCE_CHARS = 200
# Everything under the headline. Slack rejects a section over 3000 characters and the
# chunking in post() cannot shorten a block, and Discord splits a message at 2000; a
# 120-char headline plus this plus a run footer stays inside both.
CARD_BODY_CHARS = 1600
FILE_PREVIEW_BYTES = 64 * 1024
# The default report pick, best first; every other suffix needs Agent.announce_file.
REPORT_SUFFIXES = {".md": 0, ".markdown": 0, ".txt": 1}
# Matching candidates retained/read per card; discovery still walks work/.
REPORT_CANDIDATES = 500
# A report is read only through descriptor-relative, no-follow opens. Native Windows has
# neither, so there announce="file" always shows the result (Harness warns at startup).
FILE_PREVIEW_SUPPORTED = (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
                          and all(getattr(os, name, 0) for name in
                                  ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")))
# A fenced code block's opening or closing line: 3+ backticks or tildes, up to 3 spaces in.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


# (persona key, icon_url) pairs already warned about: a broken icon is a standing
# condition, so one line per process says it, not one per message.
_BROKEN_ICONS_WARNED: set[tuple[str, str]] = set()


def posting_icon(persona: Any, log: Callable[[str], None]) -> str:
    """persona.chat_icon for a message about to go out, or "" when its icon_url is not a
    URL. Persona refuses such a value when it is built, but a harness that sets icon_url
    afterwards (from a settings file) skips that check, and then every post by that
    persona raised: replies, cards and handoffs all gone over a picture. Say so once and
    post without it."""
    try:
        return persona.chat_icon
    except ValueError as exc:
        key = (persona.key, persona.icon_url)
        if key not in _BROKEN_ICONS_WARNED:
            _BROKEN_ICONS_WARNED.add(key)
            log(f"{exc} — posting without an icon until it is fixed")
        return ""


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# Card lines under the status line are quoted, so a fence that closes one is too.
_QUOTED_FENCE = "> ```"


def _closed(cut: str, fence: str = "```") -> str:
    """A cut that ends inside a code block, closed again with `fence`: Slack shows an
    unclosed ``` as literal backticks, and on Discord it would run on into the footer
    under the card."""
    return cut + "\n" + fence if cut.count("```") % 2 else cut


def _fit(lines: list[str], limit: int, fence: str = _QUOTED_FENCE) -> list[str]:
    """Keep lines while they fit; cut the first that does not, marked, and stop. The cut
    keeps room to close a code block it leaves open, as a quoted line."""
    out: list[str] = []
    used = 0
    for line in lines:
        room = limit - used - (1 if out else 0)
        if len(line) <= room:
            out.append(line)
            used += len(line) + (1 if len(out) > 1 else 0)
            continue
        room -= len("\n" + fence)
        if room > 1:
            out.append(line[: room - 1] + "…")
        closed = _closed("\n".join(out), fence)
        while len(closed) > limit:  # cut at a line edge with no room left to close
            out.pop()
            closed = _closed("\n".join(out), fence)
        return closed.split("\n")
    return out


def _excerpt(text: str, *, file: bool = False) -> str:
    """The head of what a run said, marked when it is not all of it, a cut code block
    closed again. Redacted before the cut, so a secret is never cut short of what
    redact() recognises."""
    text = redact_file(text) if file else redact(text)
    if len(text) <= CARD_EXCERPT_CHARS:
        return text
    return _closed(text[: CARD_EXCERPT_CHARS - 1] + "…")


def _quote(text: str, quote_code: bool) -> list[str]:
    lines = []
    in_code = False
    for line in text.splitlines():
        fence = line.count("```") % 2
        # Slack treats quote prefixes inside fences as literal code. Keep the whole
        # block outside the quote, including both fences; Discord keeps its nesting.
        # A line that opens and closes ``` itself is not a block and stays quoted.
        lines.append(line if not quote_code and (in_code or fence) else f"> {line}")
        if fence:
            in_code = not in_code
    return lines


def _opening(text: str) -> str:
    """A report's opening: what comes before its first `## ` section.

    Closed YAML front matter (`---` … `---`) is metadata, not the opening. A leading `# `
    title is skipped, since it repeats the card's headline. When what follows starts with
    `## ` (a report shaped `# title`, then `## Summary`), that first section is the
    opening, up to the next `## `. Headings inside fenced code do not count.
    """
    lines = text.lstrip("\r\n").splitlines()
    if lines and lines[0].rstrip() == "---":  # YAML front matter, when it is closed
        end = next((i for i, line in enumerate(lines[1:], 1)
                    if line.rstrip() in ("---", "...")), None)
        if end is not None:
            lines = lines[end + 1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    if lines and (lines[0] == "#" or lines[0].startswith(("# ", "#\t"))):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines[:_first_section(lines)]).strip()


def _first_section(lines: list[str]) -> int:
    """Index of the first `## ` line after the first, outside ``` or ~~~ fenced code; else
    the end. A fence that is never closed is text, so it cannot hide the sections after it."""
    markers = [_FENCE.match(line) for line in lines]
    # longest[i][char]: the longest bare closing run of that char at line i or later.
    longest = [{"`": 0, "~": 0} for _ in range(len(lines) + 1)]
    for i in range(len(lines) - 1, -1, -1):
        longest[i] = dict(longest[i + 1])
        marker = markers[i]
        if marker and not marker.group(2).strip():
            run = marker.group(1)
            longest[i][run[0]] = max(longest[i][run[0]], len(run))
    fence = ""
    for i, line in enumerate(lines):
        if not fence and i > 0 and line.startswith("## "):
            return i
        marker = markers[i]
        if not marker:
            continue
        run = marker.group(1)
        if not fence:
            if longest[i + 1][run[0]] >= len(run):
                fence = run
        elif run[0] == fence[0] and len(run) >= len(fence) and not marker.group(2).strip():
            fence = ""
    return len(lines)


def _attempt_started(home: Path, record: Any) -> int:
    """Claim time in epoch nanoseconds; legacy events retain their stored precision."""
    claims = [e for e in tasks.read_events(home, record.id)
              if e.get("kind") == "transition" and e.get("to") == "running"]
    return int(claims[-1].get("started_ns",
               tasks.to_epoch(claims[-1].get("at", "")) * 1_000_000_000)) if claims else 0


def _walk(top: Path):
    for root, _dirs, files in os.walk(top):  # never descends a symlinked folder
        for name in files:
            yield Path(root) / name


def _report_candidates(home: Path, record: Any, result: Any, work: Path, pattern: str = "") -> dict[str, Path]:
    """This attempt's files inside work/, keyed by their path relative to it.

    That is the run's artifacts (a new folder contributes the files in it), plus any file
    in work/ modified since this attempt was claimed: a retry reuses work/, so a report it
    rewrites is not new and is not an artifact. Older files are never candidates.
    """
    found: dict[str, Path] = {}
    keys: dict[str, tuple] = {}
    try:
        real_home = str(home.resolve())
    except (OSError, RuntimeError):  # Path.resolve symlink loops on Python 3.11/3.12
        return found
    # Plain string prefixes: this runs once per file of a possibly huge artifact folder.
    home_prefix = os.path.join(os.path.abspath(home), "")
    work_prefix = os.path.join(str(work), "")

    def add(path: Path) -> None:
        absolute = os.path.abspath(path)
        if absolute.startswith(home_prefix):
            absolute = os.path.join(real_home, absolute[len(home_prefix):])
        if not absolute.startswith(work_prefix) or absolute == work_prefix:
            return
        rel = absolute[len(work_prefix):].replace(os.sep, "/")
        key = _report_key(rel, pattern)
        if key is None or rel in found:
            return
        found[rel], keys[rel] = Path(absolute), key
        if len(found) >= 2 * REPORT_CANDIDATES:
            trim()

    def trim() -> None:
        # Keep the best matches, independent of artifact/discovery order. Trimming in
        # batches, on keys computed once, keeps this O(N log N) for N matching files.
        keep = set(heapq.nsmallest(REPORT_CANDIDATES, keys, key=keys.__getitem__))
        for rel in [rel for rel in found if rel not in keep]:
            del found[rel], keys[rel]

    for name in result.artifacts:
        path = home / name
        try:
            if not path.resolve().is_relative_to(work):
                continue
        except (OSError, RuntimeError):
            continue
        if path.is_dir() and not path.is_symlink():
            for child in _walk(path):
                add(child)
        else:
            add(path)
    started = _attempt_started(home, record)
    if started:
        for child in _walk(work):
            try:
                rel = child.relative_to(work).as_posix()
                if rel in found or _report_key(rel, pattern) is None:
                    continue
                if child.stat().st_mtime_ns >= started:
                    add(child)
            except OSError:
                continue
    if len(found) > REPORT_CANDIDATES:
        trim()
    return found


def _report_key(rel: str, pattern: str) -> tuple | None:
    """Where `rel` (relative to work/) sorts among reports, or None if it is not one. A
    named pattern is matched against it; without one, Markdown comes first, then plain
    text, top level before folders, and hidden files, logs, data and everything else are
    left out."""
    if pattern:
        return (rel,) if fnmatch.fnmatchcase(rel, pattern) else None
    path = PurePosixPath(rel)
    suffix = path.suffix.lower()
    if suffix in REPORT_SUFFIXES and not any(part.startswith(".") for part in path.parts):
        return (REPORT_SUFFIXES[suffix], len(path.parts), rel.casefold(), rel)
    return None


def _report_order(found: dict[str, Path], pattern: str) -> list[Path]:
    """The reports to try, in _report_key order."""
    ranked = [(key, path) for rel, path in found.items()
              if (key := _report_key(rel, pattern)) is not None]
    return [path for _key, path in sorted(ranked)]


def _read_opening(path: Path, work_fd: int) -> str:
    """Open each relative component without following links, anchored to work_fd."""
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts):
        return ""
    parent = os.dup(work_fd)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        checked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(checked.st_mode):
            return ""
        # Nonblocking prevents a FIFO swapped in after stat from hanging before fstat.
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1
                    or (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino)):
                return ""
            data = source.read(FILE_PREVIEW_BYTES)
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(
            data, final=len(data) < FILE_PREVIEW_BYTES)
    except (OSError, ValueError):
        return ""
    finally:
        os.close(parent)
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        return ""
    return _opening(text)


def _file_preview(home: Path, record: Any, result: Any, pattern: str = "") -> str:
    """The opening of this run's report inside this task's work/.

    Candidates are this attempt's files (_report_candidates), never another task's or the
    legacy logs/ and findings/. `pattern` (Agent.announce_file) names the report; without
    it, _report_order ranks Markdown, then plain text. The first that reads as non-empty
    text wins; none falls back to the final message.
    """
    work = tasks.task_work_dir(home, record.id)
    if work is None or result is None:
        return ""
    work = home.resolve() / work.relative_to(home)
    # Native Windows lacks dir_fd/O_NOFOLLOW: fail closed to the result card rather
    # than claiming a path-based check can bind containment to the actual open file.
    if not FILE_PREVIEW_SUPPORTED:
        return ""
    try:
        work_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        for path in _report_order(_report_candidates(home, record, result, work, pattern), pattern):
            opening = _read_opening(path.relative_to(work), work_fd)
            if opening:
                return opening
    finally:
        os.close(work_fd)
    return ""


def task_card_lines(home: Path, record: Any, result: Any, *,
                    render: Callable[[str], str] = str, quote_code: bool = True,
                    announce: str = "result", announce_file: str = "") -> list[str]:
    """The lines under a task card's headline: a status line, then what the run said.

    What the agent wrote leads. The default completion check records the same evidence
    for every run ("<agent> 실행 완료 · work/ 산출물 <file>"), and when that evidence was
    the body, a scheduled briefing reached the channel as "it finished" with the briefing
    itself one button away. So when the run left a result_text, the evidence shrinks onto
    the status line, still visible; only a run with nothing to say gets it as the body.

    `announce` selects the default result, the completion evidence, or the primary
    artifact's opening (falling back to result). `render` turns result/file text into
    the channel's own markup before it is quoted: Slack's is not the GitHub markdown a
    model writes, and Discord's is. Evidence keeps the existing evidence-only path.
    """
    status = f"`{record.id}` · {record.status} · {record.agent}"
    verdict = next(
        (e for e in reversed(tasks.read_events(home, record.id)) if e.get("kind") == "verify"),
        None,
    )
    # Redacted like the excerpt, and before either cut below: a check that quotes a failed
    # command can carry its token, and a token cut short may no longer match redact().
    evidence = [redact(str(item)) for item in (verdict or {}).get("evidence") or []][:4]
    text = (result.result_text or "").strip() if result is not None else ""
    file_text = ""
    if announce == "evidence" and evidence:
        text = ""
    elif announce == "file":
        file_text = _file_preview(home, record, result, announce_file)
        text = file_text or text
    if not text:
        quoted = _quote("\n".join(evidence), quote_code)
        return _fit([status, *quoted], CARD_BODY_CHARS,
                    _QUOTED_FENCE if quote_code else "```")
    if evidence:
        status += " · " + _one_line(" · ".join(evidence), CARD_EVIDENCE_CHARS)
    excerpt = render(_excerpt(text, file=bool(file_text)))
    return _fit([status, *_quote(excerpt, quote_code)], CARD_BODY_CHARS,
                _QUOTED_FENCE if quote_code else "```")


class ConversationGateway(Protocol):
    enabled: bool
    ready: asyncio.Event

    async def start(self, stop: asyncio.Event) -> None: ...
    async def post(self, text: str, **kwargs: Any) -> Any: ...
    async def announce_task(self, record: Any, result: Any, persona: Any) -> Any: ...


class NullGateway:
    """Poll-only gateway used without importing a channel SDK."""

    enabled = False

    def __init__(self) -> None:
        self.ready = asyncio.Event()

    async def start(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def post(self, text: str, **kwargs: Any) -> None:
        return None

    async def announce_task(self, record: Any, result: Any, persona: Any) -> None:
        return None
