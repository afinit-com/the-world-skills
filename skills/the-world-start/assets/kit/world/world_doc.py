"""What does not belong in WORLD.md, found without a model.

WORLD.md is sent with every mention turn, to every persona. It holds the world: the cast,
how they relate, the world's words, what is watched in the world's terms. An install log,
a command, a path, or "the process runs with --no-slack" in it is read as a standing fact
long after it stops being one, and costs tokens on every turn. Those belong in README.md
(how to operate), settings.json (values), or the generated STATUS.md (current state).

Whether a model keeps them out was left to the prompt, and one install of two did not.
These checks are the deterministic half: `bin/smoke.py` and harness startup report them.
"""

from __future__ import annotations

import re

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a code block", re.compile(r"^\s*```", re.M)),
    ("an absolute path", re.compile(r"(?<![\w.])(?:/Users/|/home/|~/|[A-Za-z]:\\)")),
    ("a shell command", re.compile(r"(?:^|[`\s])(?:uv run|python3? |npm (?:run|i)|pip "
                                   r"|git (?:pull|push|clone)|slack (?:deploy|login))")),
    ("a process id", re.compile(r"\bPID\b|\.pid\b", re.I)),
    ("an env variable name", re.compile(r"\b[A-Z][A-Z0-9_]*_(?:TOKEN|SLACK_ID|CHANNEL|API_KEY|SECRET)\b")),
    ("an install or operations heading",
     re.compile(r"^#{1,6}\s.*(?:설치|운영|연결 절차|현재 상태|install|setup|operation|status\b)", re.I | re.M)),
)


def world_doc_problems(text: str) -> list[str]:
    """One line per kind of thing found, naming the first line it is on."""
    found = []
    for what, pattern in _RULES:
        match = pattern.search(text)
        if match:
            found.append((text.count("\n", 0, match.start()) + 1, what))
    return [f"line {line}: {what}; move it to README.md, settings.json, or leave it to STATUS.md"
            for line, what in sorted(found)]
