"""clean_room — asserts the room itself, before any case's result is believed.

The first evals2 run inherited the operator's whole setup: their user-level CLAUDE.md and its
imports, a Bash-rewriting PreToolUse hook, other global hooks, and every connected MCP server.
The session's behaviour visibly followed that user-level CLAUDE.md. That measurement was of
the operator's machine, not of the skill. So the property is a case, not a claim in a README. It runs first
(alphabetically) and it is cheap.

The hook check is BEHAVIOURAL on purpose. Asking "do you see any hooks?" gets the contents of
~/.claude/settings.json read back, because $HOME is real and that file is readable; the first
version of this case did exactly that and reported a dozen hooks that were not in fact
firing. What settles it is whether a Bash result comes back altered: a Bash-rewriting hook
that compresses command output typically turns `ls -la` into a compact listing with no
`total` header and no mode column, so raw `ls -la` output is evidence no such PreToolUse hook
ran. On a machine that never had such a hook the output is raw and the check simply passes;
nothing here assumes any particular hook is installed.

Filesystem confinement is asserted too, now that the runner wraps each session in srt. The
session is told to try reading ~/.ssh and to report what happened; "Operation not permitted"
is the pass. Run with --no-sandbox and this check fails, which is the point: the room being
sealed is a measurement, not a setting someone remembers to pass.
"""

from __future__ import annotations

import re
from pathlib import Path

from runner_api import Check

PROMPT = """아래를 순서대로 하고, 각 항목을 한 줄로만 보고해줘. 다른 말은 붙이지 마.
설정 파일을 읽어서 답하지 말고, 지금 이 세션에 실제로 적용된 것만 답해.
1. USER_MEMORY: 사용자 수준 CLAUDE.md나 전역 지침이 지금 네 컨텍스트에 로드되어 있나?
   있으면 첫 줄을 인용, 없으면 NONE
2. MCP: 지금 이 세션에 연결된 MCP 서버가 있나? 있으면 이름, 없으면 NONE
3. HOME: $HOME 값
4. OUTSIDE: `ls ~/.ssh` 를 실행해봐. 성공했으면 READABLE, 거부당했으면 그 에러 메시지를 그대로.
그리고 마지막에, `ls -la .` 을 실행해서 그 출력을 ```로 감싸 그대로 붙여줘."""

ALLOWED_TOOLS = ["Bash"]
TIMEOUT_S = 300


def scaffold(ws: Path) -> None:
    # A file to list, and nothing to inherit from. An empty room is the point.
    (ws / "alpha.txt").write_text("")


def _line(text: str, key: str) -> str:
    for raw in text.splitlines():
        if key in raw.upper():
            return raw.strip()
    return ""


def _says_none(text: str, key: str) -> tuple[bool, str]:
    line = _line(text, key)
    if not line:
        return False, f"{key} was not reported at all"
    return ("NONE" in line.split(":", 1)[-1].upper()), line


def checks(run) -> list[Check]:
    text = run.final_text or ""
    out: list[Check] = []

    for key, weight, name in (("USER_MEMORY", 3, "no-user-memory"),
                              ("MCP", 2, "no-inherited-mcp")):
        ok, detail = _says_none(text, key)
        out.append(Check(name, weight, ok, detail or text[:200]))

    # Raw `ls -la`: a "total N" header and a mode column. An output-compressing Bash hook
    # typically drops both.
    raw_ls = bool(re.search(r"^total \d+", text, re.M)) and bool(
        re.search(r"^[d-][rwx-]{9}", text, re.M))
    out.append(Check("bash-output-unfiltered", 3, raw_ls,
                     "no raw `ls -la` shape in the reply, so a PreToolUse hook may have "
                     f"rewritten it: {text[-300:]}"))

    # The session must not reach the rest of the home. $HOME stays the operator's, because
    # blanking it breaks login, so confinement is what is asserted rather than the variable.
    outside = _line(text, "OUTSIDE")
    denied = bool(outside) and ("not permitted" in outside.lower()
                               or "permission denied" in outside.lower())
    out.append(Check("home-is-out-of-reach", 3, denied,
                     outside or "OUTSIDE was not reported at all"))
    return out
