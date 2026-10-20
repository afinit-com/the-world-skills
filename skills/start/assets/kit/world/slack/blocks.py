"""Block Kit for what the harness posts: a headline, grouped facts, then context.

Everything the kit posts goes through here, and so can a harness's own posts:

    from world.slack.blocks import card

    text, blocks = card(
        "✅ 승인 받음 — r12 배포 대기",
        fields={"결과 파일": "`reports/r12.csv`", "모델": "`model-r12` v1"},
        bullets=["모델 push 완료", "창이 열리면 바로 배포"],
        context="monitor · 다음 확인 10분 뒤",
    )
    await listener.post(text, blocks=blocks)

`text` is the notification and accessibility fallback Slack shows where blocks are not
rendered (a push notification, a screen reader), so it says the same thing in plain
lines. `blocks` is what people see.

Slack's limits, which every builder here keeps so a post is never refused: a section
holds at most 3000 characters, a field 2000 and a section 10 fields, a header 150
characters of plain text, a context block 10 elements, an actions block 25 elements
(the kit allows 5 buttons), and a message 50 blocks. `post()` splits a longer list of
blocks across messages in the same thread.

Pure functions, no Slack client: importable from harness code and tests alike.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..types import BUTTON_LABEL_MAX, MAX_BUTTONS, Button

SECTION_CHARS = 3000
FIELD_CHARS = 2000
MAX_FIELDS = 10
HEADER_CHARS = 150
MAX_CONTEXT_ELEMENTS = 10
MAX_BLOCKS = 50
# The plain `text` beside the blocks. Slack truncates it far later, but a notification
# longer than this is not read anyway, and it keeps one message under the old chunk size.
TEXT_CHARS = 3900

_FENCE_LINE = re.compile(r"^\s{0,3}```")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_BOLD_LINE = re.compile(r"^\*[^*\n]+\*:?$")
# Characters that already carry mrkdwn meaning; a headline holding any of them is shown
# as written rather than wrapped in another pair of asterisks.
_MARKUP = re.compile(r"[*_`~<>]")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def section(text: str) -> dict:
    """One mrkdwn section, cut to Slack's 3000 characters (with a closed code fence)."""
    if len(text) > SECTION_CHARS:
        text = _cut(text, SECTION_CHARS - 4)
        if text.count("```") % 2:
            text += "\n```"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}}


def header(text: str) -> dict:
    """A header block: large plain text, one line, at most 150 characters."""
    line = " ".join(text.split()) or " "
    return {"type": "header", "text": {"type": "plain_text", "text": _cut(line, HEADER_CHARS),
                                       "emoji": True}}


def divider() -> dict:
    return {"type": "divider"}


def context(*items: str) -> dict:
    """A context block: small grey lines under a message (a footer, a link, a time)."""
    elements = [{"type": "mrkdwn", "text": _cut(item, SECTION_CHARS)}
                for item in items if item and item.strip()][:MAX_CONTEXT_ELEMENTS]
    return {"type": "context", "elements": elements or [{"type": "mrkdwn", "text": " "}]}


def bullets(items: Iterable[str]) -> str:
    """`items` as a bulleted mrkdwn list, one fact per line."""
    return "\n".join(f"• {item.strip()}" for item in items if item and item.strip())


def fields(pairs: Mapping[str, Any] | Sequence[tuple[str, Any]]) -> list[dict]:
    """Label/value pairs as section fields (two columns on desktop), 10 per section.
    Values are mrkdwn: put ids and paths in backticks."""
    items = list(pairs.items()) if isinstance(pairs, Mapping) else list(pairs)
    cells = [{"type": "mrkdwn", "text": _cut(f"*{label}*\n{value}", FIELD_CHARS)}
             for label, value in items if value is not None and str(value) != ""]
    return [{"type": "section", "fields": cells[i:i + MAX_FIELDS]}
            for i in range(0, len(cells), MAX_FIELDS)]


# card() takes parameters with these names, so it reaches the builders through aliases.
_fields_blocks, _bullet_text, _context_block = fields, bullets, context


def link_button(label: str, url: str, *, style: str = "") -> dict:
    """A button that opens `url`. The only kind that needs no click handler, so the only
    kind harness code gets from here; the kit's ask/show/dismiss buttons are declared on
    `Agent.buttons` and rendered for the task they belong to."""
    return _element(Button("link", label, url=url, style=style))


def _element(button: Button | Mapping) -> dict:
    if isinstance(button, Mapping):
        element = dict(button)
        label = element.get("text", {}).get("text", "")
        if not label or len(label) > BUTTON_LABEL_MAX:
            raise ValueError(f"button label must be 1-{BUTTON_LABEL_MAX} chars: {label!r}")
        return element
    if button.kind != "link":
        raise ValueError(f"button {button.label!r}: only link buttons can be rendered here; "
                         "ask/show/dismiss belong on Agent.buttons")
    element = {"type": "button", "text": {"type": "plain_text", "text": button.label},
               "url": button.url}
    if button.style:
        element["style"] = button.style
    return element


def actions(buttons: Sequence[Button | Mapping]) -> dict:
    """An actions block, under the kit's button rules: at most five, at most one primary,
    each label 1-75 characters."""
    elements = [_element(b) for b in buttons]
    for i, element in enumerate(elements):
        # Slack refuses an actions block with two identical action_ids.
        element.setdefault("action_id", f"world_link_{i}")
    if len(elements) > MAX_BUTTONS:
        raise ValueError(f"at most {MAX_BUTTONS} buttons, got {len(elements)}")
    if sum(1 for e in elements if e.get("style") == "primary") > 1:
        raise ValueError("at most one primary button")
    return {"type": "actions", "elements": elements}


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs, with a fenced code block kept whole even when it
    contains blank lines."""
    out: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if current:
                out.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        out.append("\n".join(current))
    return out


def _split_long(paragraph: str, limit: int = SECTION_CHARS) -> list[str]:
    """A paragraph over `limit` as pieces at line boundaries (a line longer than a piece
    is cut); a code block cut in two is closed and reopened so neither half renders as
    prose."""
    if len(paragraph) <= limit:
        return [paragraph]
    room = limit - 8  # space for a closing and a reopening fence
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    fence_open = False
    for line in paragraph.splitlines():
        for chunk in [line[i:i + room] for i in range(0, len(line), room)] or [""]:
            if current and size + len(chunk) + 1 > room:
                pieces.append("\n".join(current + (["```"] if fence_open else [])))
                current = ["```"] if fence_open else []
                size = sum(len(x) + 1 for x in current)
            current.append(chunk)
            size += len(chunk) + 1
        if _FENCE_LINE.match(line):
            fence_open = not fence_open
    if current:
        pieces.append("\n".join(current))
    return pieces


def _headline(line: str) -> str:
    stripped = line.strip()
    if _MARKUP.search(stripped) or stripped.startswith(">"):
        return stripped
    return f"*{stripped}*"


def text_blocks(text: str, *, headline: bool = True) -> list[dict]:
    """Free text (a persona's reply, a report, a notice) as sections instead of one blob.

    Each paragraph becomes its own section, a paragraph over 3000 characters is split at
    line boundaries, and a fenced code block stays whole. With `headline`, a short first
    line followed by more is set apart in bold as the message's one-line outcome. A
    paragraph that opens with a bold heading line (`*Heading*`, which is what
    `slack_mrkdwn` makes of `## Heading`) gets a divider above it."""
    paragraphs = _paragraphs(text or "")
    if not paragraphs:
        return []
    if headline:
        first, _, rest = paragraphs[0].partition("\n")
        more = bool(rest.strip()) or len(paragraphs) > 1
        if (more and len(first.strip()) <= HEADER_CHARS and not _FENCE_LINE.match(first)
                and not _LIST_ITEM.match(first)):
            paragraphs = [_headline(first), *([rest] if rest.strip() else []), *paragraphs[1:]]
    blocks: list[dict] = []
    for i, paragraph in enumerate(paragraphs):
        if i > 1 and _BOLD_LINE.match(paragraph.splitlines()[0].strip()):
            blocks.append(divider())
        blocks.extend(section(piece) for piece in _split_long(paragraph))
    return blocks


def card(
    title: str,
    *,
    fields: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    bullets: Iterable[str] | None = None,
    body: str = "",
    context: str | Sequence[str] | None = None,
    buttons: Sequence[Button | Mapping] | None = None,
    headline: str = "header",
) -> tuple[str, list[dict]]:
    """A structured message: (`text` fallback, `blocks`).

    title     the one-line outcome. `headline="header"` shows it as a header block
              (large, plain text); `headline="bold"` as a bold section line, which keeps
              mrkdwn such as a mention or `code` in it.
    fields    label → value facts (what changed, numbers, ids in backticks).
    bullets   short lines, one fact each.
    body      free text under the facts, split into sections like a reply.
    context   small grey lines at the bottom: a footer, a link, a time.
    buttons   link buttons (`link_button`, `Button.link`) or ready Slack elements.

    A divider separates the facts from the body, and the body from the context."""
    text_lines = [title.strip()]
    blocks: list[dict] = []
    if title.strip():
        blocks.append(header(title) if headline == "header" else section(_headline(title)))
    facts: list[dict] = []
    if fields:
        facts += _fields_blocks(fields)
        pairs = list(fields.items()) if isinstance(fields, Mapping) else list(fields)
        text_lines += [f"{label}: {value}" for label, value in pairs
                       if value is not None and str(value) != ""]
    listed = _bullet_text(bullets or [])
    if listed:
        facts.append(section(listed))
        text_lines.append(listed)
    blocks += facts
    if body and body.strip():
        if facts:
            blocks.append(divider())
        blocks += text_blocks(body, headline=False)
        text_lines.append(body.strip())
    if buttons:
        blocks.append(actions(buttons))
    notes = [context] if isinstance(context, str) else list(context or [])
    notes = [n for n in notes if n and n.strip()]
    if notes:
        blocks.append(divider())
        blocks.append(_context_block(*notes))
        text_lines += notes
    return _cut("\n".join(line for line in text_lines if line), TEXT_CHARS), blocks


def has_text(blocks: Sequence[dict]) -> bool:
    """Whether `blocks` show any words, or are only buttons and dividers."""
    return any(b.get("type") in ("section", "header", "rich_text", "context", "markdown")
               for b in blocks)


def plain_text(blocks: Sequence[dict]) -> str:
    """A readable plain rendering of `blocks`, for a message's `text` fallback."""
    lines: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind in ("section", "header"):
            if block.get("text"):
                lines.append(block["text"].get("text", ""))
            lines += [cell.get("text", "").replace("\n", ": ", 1) for cell in block.get("fields", [])]
        elif kind == "context":
            lines += [e.get("text", "") for e in block.get("elements", []) if e.get("text")]
    return _cut("\n".join(line for line in lines if line.strip()), TEXT_CHARS)


def split_messages(blocks: Sequence[dict], limit: int = MAX_BLOCKS) -> list[list[dict]]:
    """`blocks` as consecutive messages of at most `limit` blocks each, never starting a
    message with a divider."""
    out: list[list[dict]] = []
    current: list[dict] = []
    for block in blocks:
        if len(current) >= limit:
            out.append(current)
            current = []
        if not current and block.get("type") == "divider":
            continue
        current.append(block)
    if current:
        out.append(current)
    return out


__all__ = [
    "MAX_BLOCKS", "SECTION_CHARS", "actions", "bullets", "card", "context", "divider",
    "fields", "has_text", "header", "link_button", "plain_text", "section",
    "split_messages", "text_blocks",
]
