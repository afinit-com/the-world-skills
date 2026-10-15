"""Small channel boundary used by the orchestrator."""

from __future__ import annotations

import asyncio
import codecs
import os
import stat
from pathlib import Path
from typing import Any, Callable, Protocol

from ..env import redact
from ..process import task as tasks

CARD_EXCERPT_CHARS = 300
# The status line carries the evidence as one line, so it gets a line's worth of room.
CARD_EVIDENCE_CHARS = 200
# Everything under the headline. Slack rejects a section over 3000 characters and the
# chunking in post() cannot shorten a block, and Discord splits a message at 2000; a
# 120-char headline plus this plus a run footer stays inside both.
CARD_BODY_CHARS = 1600
FILE_PREVIEW_BYTES = 64 * 1024


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


def _excerpt(text: str) -> str:
    """The head of what a run said, marked when it is not all of it, a cut code block
    closed again. Redacted before the cut, so a secret is never cut short of what
    redact() recognises."""
    text = redact(text)
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


def _file_preview(home: Path, record: Any, result: Any) -> str:
    """First current-run artifact inside this task's work/, never a directory scan.

    Completion supplies artifacts in sorted order. Skip legacy logs/findings and other
    tasks, then use one primary path; an unusable primary falls back to the final message.
    Read a bounded UTF-8 prefix, refusing binary/control bytes and non-regular files.
    """
    work = tasks.task_work_dir(home, record.id)
    if work is None or result is None:
        return ""
    work = home.resolve() / work.relative_to(home)
    for name in result.artifacts:
        try:
            path = (home / name).resolve()
            if not path.is_relative_to(work):
                continue
            if not path.is_file():
                return ""
            # Nonblocking prevents a FIFO from hanging the announcer before fstat.
            # Windows has neither flag; resolution and the regular-file checks still apply.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    return ""
                data = source.read(FILE_PREVIEW_BYTES)
            # A multibyte character split by the read limit is not a binary file.
            text = codecs.getincrementaldecoder("utf-8-sig")().decode(
                data, final=len(data) < FILE_PREVIEW_BYTES)
            if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
                return ""
            lines = text.strip().splitlines()
            in_code = False
            for i, line in enumerate(lines):
                if not in_code and i > 0 and line.startswith("## "):
                    return "\n".join(lines[:i]).strip()
                if line.lstrip().startswith("```"):
                    in_code = not in_code
            return "\n".join(lines).strip()
        except (OSError, ValueError, RuntimeError):  # Path.resolve symlink loops on Python 3.11/3.12
            return ""
    return ""


def task_card_lines(home: Path, record: Any, result: Any, *,
                    render: Callable[[str], str] = str, quote_code: bool = True,
                    announce: str = "result") -> list[str]:
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
    if announce == "evidence" and evidence:
        text = ""
    elif announce == "file":
        text = _file_preview(home, record, result) or text
    if not text:
        quoted = _quote("\n".join(evidence), quote_code)
        return _fit([status, *quoted], CARD_BODY_CHARS,
                    _QUOTED_FENCE if quote_code else "```")
    if evidence:
        status += " · " + _one_line(" · ".join(evidence), CARD_EVIDENCE_CHARS)
    excerpt = render(_excerpt(text))
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
