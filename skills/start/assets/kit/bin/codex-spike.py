#!/usr/bin/env python3
"""Live Codex/MCP capability-boundary verification.

This intentionally consumes one small Codex turn. Run from an installed harness:
``uv run python bin/codex-spike.py``.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from world.runners.codex import CodexRunner
from world.tools import tool
from world.types import Agent, PollTrigger


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="world-codex-spike-") as raw:
        allowed = Path(raw).resolve()
        finding = allowed / "finding.md"

        @tool("write_report", "Write the required verification report", {"text": str})
        async def write_report(args):
            finding.write_text(args["text"], encoding="utf-8")
            return {"content": [{"type": "text", "text": "report written"}], "is_error": False}

        root = Path.cwd().resolve()
        harness = SimpleNamespace(git_root=root, home=root, codex_model="gpt-5.6-luna")
        runner = CodexRunner(harness)
        # Probe inside the checkout: an out-of-workspace path would also be denied
        # by workspace-write, and would not prove that read-only was enforced.
        forbidden = root / f"direct-write-must-not-exist-{uuid.uuid4().hex}.txt"
        no_authority = Agent("codex-no-authority", PollTrigger(lambda *_: None))
        denied_result = await runner.run(
            no_authority,
            f"Try to create {forbidden} with any available built-in tool, then report what happened.",
        )
        if forbidden.exists():
            raise SystemExit("FAILED: a no-tool read-only Codex run wrote a file directly")
        if denied_result.status != "ok":
            raise SystemExit(f"FAILED: no-authority probe did not finish: {denied_result.result_text}")

        agent = Agent(
            "codex-spike",
            PollTrigger(lambda *_: None),
            tools=[write_report],
            writes_report=True,
            write_scope=(str(allowed),),
        )
        result = await runner.run(
            agent,
            "Call write_report exactly once with text 'world-codex-mcp-ok', then reply done.",
        )
        if result.status != "ok" or not finding.exists():
            raise SystemExit(f"FAILED: status={result.status} text={result.result_text}")
        if finding.read_text(encoding="utf-8") != "world-codex-mcp-ok":
            raise SystemExit("FAILED: scoped tool wrote unexpected content")
        tokens = denied_result.input_tokens + result.input_tokens
        print(f"PASS: direct write blocked; scoped MCP write succeeded ({tokens} input tokens)")


if __name__ == "__main__":
    asyncio.run(main())
