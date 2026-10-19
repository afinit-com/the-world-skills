"""world — The World harness runtime.

Build always-on, Slack-native multi-agent watchdog harnesses with themed bots.

A harness project declares everything in plain Python:

    from world import Harness, Agent, Persona, PollTrigger, Signal, SlackConfig

    h = Harness(name="my-bots", main_persona=Persona(key="probe", display="Probe"))

    from abilities.drift_watch import agent as drift_watch
    h.register(drift_watch.AGENT)

    if __name__ == "__main__":
        h.main()

Scaffolded and extended by project-local skills in .claude/skills or .agents/skills:
/the-world:start (survey + world + project), `agent.md` (new agent + Slack persona),
`tool.md` (scoped in-process MCP tools).

Importing world loads the harness env file (env/harness.env at the repo root) as a side effect. That
is deliberate. Loading it inside Harness.__init__ would make correctness depend on a
harness constructing its Harness BEFORE importing any ability, because abilities
read env at module level:

    JIRA_SITE = os.environ.get("JIRA_SITE", "your-org.atlassian.net")

Every harness happened to be ordered that way, but nothing enforced it, and any
linter that hoists imports to the top of the file would have silently reverted each
of those reads to its hardcoded default. The default and the real value are equal
today, so nothing would have failed loudly. Loading at import time makes the
ordering irrelevant: you cannot read `world`-adjacent env without importing world.
"""

import os
import sys

# A redirected Windows console (a pipe, a log file, a scheduled task) writes in the
# legacy code page, which cannot encode Korean, emoji, or even the em dash in the
# startup line, and the first such print raises UnicodeEncodeError.
if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

from .context import current_asker  # noqa: E402
from .env import load_harness_env
from .slack.approval_text import looks_like_decision  # noqa: E402

load_harness_env()  # before any module-level `os.environ.get` in an ability or tool

from .harness import Harness, TickTask  # noqa: E402 — must follow load_harness_env()
from .profile import SUPPORTED_PROVIDERS  # noqa: E402

if "codex" in SUPPORTED_PROVIDERS:  # a Claude-only install ships no Codex runner
    from .runners.codex import CodexRunner  # noqa: E402
from .toolkit import (  # noqa: E402
    make_approval_gated,
    make_list_findings,
    make_list_reports,
    make_mention_tool,
    make_handoff_tool,
    make_teammate_mention_tool,
    make_read_finding,
    make_read_report,
    make_repo_readers,
    make_tool_server,
    make_write_ask,
    make_write_finding,
    make_write_report,
    result,
    run_cmd,
    tool,
    truncate,
)
from .tools import RunEvent, ToolResult, ToolSpec  # noqa: E402
from .types import (  # noqa: E402
    Agent,
    Button,
    ApprovalPhrases,
    DiscordConfig,
    MentionTrigger,
    Persona,
    PluginMarketplace,
    PollTrigger,
    Relationship,
    RunResult,
    Schedule,
    ScheduleTrigger,
    Signal,
    SlackConfig,
    StatusPhrases,
)

__all__ = [
    "current_asker",
    "load_harness_env",
    "looks_like_decision",
    "Agent",
    "Button",
    "ApprovalPhrases",
    "DiscordConfig",
    "Harness",
    "MentionTrigger",
    "Persona",
    "PluginMarketplace",
    "PollTrigger",
    "Relationship",
    "RunResult",
    "RunEvent",
    "Schedule",
    "ScheduleTrigger",
    "Signal",
    "SlackConfig",
    "StatusPhrases",
    "TickTask",
    "ToolSpec",
    "ToolResult",
    "make_approval_gated",
    "make_list_reports",
    "make_mention_tool",
    "make_handoff_tool",
    "make_teammate_mention_tool",
    "make_tool_server",
    "make_write_ask",
    "make_write_report",
    "make_read_report",
    "make_write_finding",  # the 0.2.6 names, kept for harnesses that import them
    "make_read_finding",
    "make_list_findings",
    "make_repo_readers",
    "result",
    "run_cmd",
    "tool",
    "truncate",
]
if "codex" in SUPPORTED_PROVIDERS:
    __all__.append("CodexRunner")
