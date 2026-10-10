#!/usr/bin/env python3
"""scan — collect the evidence for a World harness risk audit, without touching it.

    python3 scan.py <harness-dir>                # human-readable, grouped by category
    python3 scan.py <harness-dir> --json         # the same findings as JSON

Every check ends in one of three words, in line with docs/philosophy.md ("say what's
closed and what isn't — don't claim zero"):

    closed        the scanner found the boundary in code and nothing reopening it
    open          it found a concrete way through, with a file:line and a repro
    not_verified  it could not decide from the code alone — a human or the model reads it

Read-only by construction. The harness is parsed with `ast`, never imported: importing
harness.py runs `import world`, which loads env/harness.env and constructs tools, and an
audit that executes the thing it audits has already trusted it. The only subprocesses
are `git ls-files` and `git check-ignore`. Secret values are never printed — a hit shows
its kind and its first four characters.

A pattern scanner sees patterns. Its `closed` means "nothing matched", so SKILL.md has the
model read each tool module and every `open` item before the report goes out.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

CATEGORIES = (
    ("authority", "Bot authority that leaves the defaults"),
    ("tools", "Tools whose scope is not really closed"),
    ("outward", "Outward actions and approval"),
    ("content", "Content-borne manipulation"),
    ("self_config", "Self-reconfiguration"),
    ("secrets", "Secrets and environment"),
    ("silent", "Silent failure"),
)

# Directories that are not the harness's own Python: the kit runtime (audited by
# comparison with the bundled copy instead), virtualenvs, and run output.
SKIP_DIRS = {"world", ".venv", "venv", ".git", "node_modules", "__pycache__", "tests",
             "bin", ".claude", ".codex", "tasks", "logs", "findings", ".the-world"}
# Run output and records are scanned for secrets even though they hold no code.
SECRET_SKIP_DIRS = {"world", ".venv", "venv", ".git", "node_modules", "__pycache__"}
MAX_SECRET_SCAN_BYTES = 2_000_000

ORDINARY_LANES = ("tasks/{task}/work",)
# Paths whose writer can change what constrains it: hooks, settings, the scope table,
# the harness's own code, its credentials, and the records read back into prompts.
SENSITIVE = (".claude", ".codex", ".the-world", "harness.py", "abilities", "tools",
             "world", "env", "ledger.jsonl", "state.json", "pyproject.toml", "AGENTS.md",
             "WORLD.md")
CLAUDE_HOOKS = {
    "world-scope-guard.py": "Edit|Write|NotebookEdit",
    "durable-config-guard.py": "Edit|Write|NotebookEdit",
    "world-integrity-guard.py": "Bash|Edit|Write|NotebookEdit",
    "world-git-guard.py": "Bash",
}

BOUNDARY_PARAMS = {"cwd", "repo", "repository", "host", "hostname", "remote", "url",
                   "endpoint", "base_url", "server", "account", "project", "org", "owner",
                   "workspace", "bucket", "channel", "database", "db", "domain",
                   "namespace", "cluster", "branch"}
PATH_PARAMS = {"path", "file", "filename", "filepath", "dir", "directory", "target",
               "dest", "destination", "src", "source", "rel", "relpath"}
CONTAINMENT = re.compile(r"_resolve_in|_within|is_relative_to|relative_to\(|commonpath")
RESOLVES = re.compile(r"\.resolve\(|realpath")
OUTWARD = re.compile(
    r"\brequests\.(?:post|put|patch|delete)\b|\bhttpx\.(?:post|put|patch|delete)\b"
    r"|\.(?:post|put|patch|delete)\(|method\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)"
    r"|urlopen\(|chat_(?:postMessage|update|delete)|files_upload|reactions_add"
    r"|['\"]push['\"]|['\"]merge['\"]"
    r"|['\"](?:pr|issue)['\"]\s*,\s*['\"](?:create|merge|close|edit|comment|review)"
    r"|\bsmtplib\b|\.(?:put_object|delete_object|send_email|sendmail)\("
)
OUTSIDE_INPUT = re.compile(
    r"\brequests\.|\bhttpx\.|urlopen\(|['\"]gh['\"]|\bjira\b|conversations_history"
    r"|search_messages|\bimaplib\b|feedparser|api\.github\.com|/rest/api/",
    re.IGNORECASE,
)
DENYLIST_NAME = re.compile(r"DENY|DENIED|BLOCK|BLACKLIST|FORBID|BANNED|DISALLOW", re.I)
SHELLS = {"sh", "bash", "zsh", "/bin/sh", "/bin/bash", "/bin/zsh", "cmd", "powershell",
          "pwsh"}
SECRET_PATTERNS = (
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}")),
    ("Slack app token", re.compile(r"\bxapp-[0-9A-Za-z-]{10,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{20,}|\bgithub_pat_[0-9A-Za-z_]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{32,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Atlassian token", re.compile(r"\bATATT[0-9A-Za-z_=-]{20,}")),
)
TOKENISH_KEY = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|PASS\b|CREDENTIAL|PAT\b", re.I)

# Lines in the kit whose absence reopens a boundary the kit closes. If a harness's copy
# of world/ was edited, the audit cannot assume the kit's guarantee — these are the ones
# worth checking line for line.
# The older approval gate: skipped entirely when no approver was configured.
OPEN_APPROVER_GATE = "if slack.approver and not slack.is_approver(clicker)"

KIT_INVARIANTS = (
    ("outward", "world/slack/approvals.py", "slack.is_approver(clicker)",
     "approve/deny clicks are checked against the approver"),
    ("authority", "world/runners/claude.py", "strict_mcp_config=True",
     "Claude spawns load only the harness's own MCP server"),
    ("authority", "world/runners/codex.py", '"--sandbox", "read-only"',
     "Codex spawns run in a read-only sandbox"),
    ("silent", "world/process/spawn.py", 'status="blocked"',
     "a writes_finding run with no artifact is recorded as blocked, not done"),
)


@dataclass
class Finding:
    category: str
    check: str
    status: str                     # closed | open | not_verified
    detail: str
    where: list[str] = field(default_factory=list)
    repro: str = ""


@dataclass
class Module:
    path: Path
    rel: str
    dotted: str
    source: str
    tree: ast.Module


@dataclass
class AgentDecl:
    module: Module
    node: ast.Call
    name: str
    kw: dict

    @property
    def where(self) -> str:
        return f"{self.module.rel}:{self.node.lineno}"


@dataclass
class ToolDef:
    module: Module
    node: ast.AST
    name: str
    func: str
    factory: str                    # enclosing function name, "" at module level
    schema: ast.AST | None

    @property
    def where(self) -> str:
        return f"{self.module.rel}:{self.node.lineno}"


UNKNOWN = object()


def lit(node):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return UNKNOWN


def callee(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def idents(node: ast.AST | None) -> set[str]:
    """Every name and attribute a tools expression mentions."""
    if node is None:
        return set()
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def segment(module: Module, node: ast.AST) -> str:
    return ast.get_source_segment(module.source, node) or ""


class Harness:
    def __init__(self, root: Path, kit: Path | None) -> None:
        self.root = root
        self.kit = kit
        self.modules: list[Module] = []
        self.by_dotted: dict[str, Module] = {}
        self.parse_errors: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts[:-1]):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                self.parse_errors.append(f"{rel}: {exc}")
                continue
            dotted = ".".join(rel.with_suffix("").parts)
            if dotted.endswith(".__init__"):
                dotted = dotted[: -len(".__init__")]
            module = Module(path, rel.as_posix(), dotted, source, tree)
            self.modules.append(module)
            self.by_dotted[dotted] = module
        self.agents = self._agents()
        self.tools = self._tools()
        self.harness_calls = self._calls("Harness")
        self.slack_calls = self._calls("SlackConfig")
        self.persona_calls = self._calls("Persona")
        self.env = self._env()

    # --- collection --------------------------------------------------------------

    def _calls(self, name: str) -> list[tuple[Module, ast.Call]]:
        return [(m, node) for m in self.modules for node in ast.walk(m.tree)
                if isinstance(node, ast.Call) and callee(node) == name]

    def _agents(self) -> list[AgentDecl]:
        agents = []
        for module, node in self._calls("Agent"):
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            name = lit(kw.get("name"))
            agents.append(AgentDecl(module, node, name if isinstance(name, str) else
                                    f"<agent at line {node.lineno}>", kw))
        return agents

    def _tools(self) -> list[ToolDef]:
        found = []

        def visit(module: Module, node: ast.AST, factory: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in child.decorator_list:
                        if isinstance(dec, ast.Call) and callee(dec) == "tool" and dec.args:
                            name = lit(dec.args[0])
                            found.append(ToolDef(
                                module, child, name if isinstance(name, str) else child.name,
                                child.name, factory,
                                dec.args[2] if len(dec.args) > 2 else None))
                    visit(module, child, child.name)
                else:
                    visit(module, child, factory)

        for module in self.modules:
            visit(module, module.tree, "")
        return found

    def _env(self) -> dict[str, str]:
        """Names → values from env/harness.env. Values stay in memory, never printed."""
        path = self.root / "env" / "harness.env"
        values: dict[str, str] = {}
        if not path.is_file():
            return values
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            values[key.strip()] = value.strip().strip("'\"")
        return values

    # --- resolution ----------------------------------------------------------------

    def resolve(self, module: Module, node: ast.AST | None, depth: int = 0) -> list[tuple[Module, ast.AST]]:
        """The node plus, one hop out, the module-level assignment of each bare name in it
        (`tools=TOOLS`, `write_scope=SCOPE`), following `from x import NAME` into the
        harness's own modules."""
        if node is None:
            return []
        out = [(module, node)]
        if depth > 1:
            return out
        for name in [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]:
            target = self._assignment(module, name)
            if target is not None:
                out += self.resolve(target[0], target[1], depth + 1)
        return out

    def _assignment(self, module: Module, name: str):
        for stmt in module.tree.body:
            if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
                return module, stmt.value
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                    and stmt.target.id == name and stmt.value is not None:
                return module, stmt.value
            if isinstance(stmt, ast.ImportFrom) and stmt.module:
                for alias in stmt.names:
                    if (alias.asname or alias.name) == name:
                        other = self.by_dotted.get(stmt.module)
                        if other is not None and other is not module:
                            return self._assignment(other, alias.name)
        return None

    def value(self, agent: AgentDecl, key: str):
        node = agent.kw.get(key)
        if node is None:
            return None
        direct = lit(node)
        if direct is not UNKNOWN:
            return direct
        for module, resolved in self.resolve(agent.module, node)[1:]:
            got = lit(resolved)
            if got is not UNKNOWN:
                return got
        return UNKNOWN

    def tool_idents(self, module: Module, node: ast.AST | None) -> set[str]:
        names: set[str] = set()
        for _, resolved in self.resolve(module, node):
            names |= idents(resolved)
        return names

    def held_by(self, tool: ToolDef, names: set[str]) -> bool:
        return bool({tool.func, tool.factory} & names - {""}) or (
            tool.module.dotted.split(".")[-1] in names)

    def tool_source(self, tool: ToolDef) -> str:
        """The tool's own body plus, one level deep, same-module helpers it calls."""
        text = segment(tool.module, tool.node)
        called = {callee(n) for n in ast.walk(tool.node) if isinstance(n, ast.Call)}
        for stmt in ast.walk(tool.module.tree):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and stmt.name in called and stmt is not tool.node:
                text += "\n" + segment(tool.module, stmt)
        return text

    # --- facts the checks share ------------------------------------------------------

    @property
    def provider(self) -> str:
        for _, call in self.harness_calls:
            for k in call.keywords:
                if k.arg == "provider" and isinstance(lit(k.value), str):
                    return lit(k.value)
        return "claude"

    def harness_kw(self, key: str):
        for module, call in self.harness_calls:
            for k in call.keywords:
                if k.arg == key:
                    return module, k.value
        return None

    def slack_env_name(self, key: str, default: str | None) -> str | None:
        for _, call in self.slack_calls:
            for k in call.keywords:
                if k.arg == key:
                    got = lit(k.value)
                    return got if isinstance(got, str) else default
        return default

    def gated_names(self) -> set[str]:
        """Tools wrapped by make_approval_gated, plus everything handed to a persona's
        mention_tools — the kit's mention responder wraps those itself."""
        names: set[str] = set()
        for module, call in self._calls("make_approval_gated"):
            if call.args:
                names |= self.tool_idents(module, call.args[0])
        for module, call in self.persona_calls:
            for k in call.keywords:
                if k.arg == "mention_tools":
                    names |= self.tool_idents(module, k.value)
        return names

    def git(self, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None


# --- checks ----------------------------------------------------------------------------


def schema_params(tool: ToolDef) -> dict[str, ast.AST]:
    node = tool.schema
    if not isinstance(node, ast.Dict):
        return {}
    params = {lit(k): v for k, v in zip(node.keys, node.values) if k is not None}
    props = params.get("properties")
    if isinstance(props, ast.Dict):
        params = {lit(k): v for k, v in zip(props.keys, props.values) if k is not None}
    return {k: v for k, v in params.items() if isinstance(k, str)}


def bounded(value: ast.AST) -> bool:
    """A JSON-schema value with an enum or const is a closed choice, not a free string."""
    return isinstance(value, ast.Dict) and any(
        lit(k) in ("enum", "const") for k in value.keys if k is not None)


def classify_scope(entry: str, agent_name: str) -> tuple[str, str]:
    """('ok' | 'wide' | 'sensitive' | 'unusual', why)."""
    clean = entry.strip()
    if clean in ("", ".", "./", "*", "**") or any(ch in clean for ch in "*?["):
        return "wide", "covers the whole harness or a glob"
    if clean.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", clean):
        return "wide", "is an absolute path outside the harness"
    if ".." in Path(clean).parts:
        return "wide", "climbs out of the harness with '..'"
    top = Path(clean).parts[0]
    if top in SENSITIVE or any(clean == s or clean.startswith(s + "/") for s in SENSITIVE):
        return "sensitive", f"reaches `{top}`, which configures or records the harness itself"
    if clean.startswith(ORDINARY_LANES) or clean in (f"logs/{agent_name}",
                                                       f"findings/{agent_name}") \
            or clean.startswith((f"logs/{agent_name}/", f"findings/{agent_name}/")):
        return "ok", ""
    return "unusual", "is wider than the ordinary `tasks/{task}/work` lane"


# The call that makes mutates=True mean something. Before it, both runners
# spawned every agent at git_root and the flag was a comment; a harness still carrying a
# world/ from then is audited against that older behavior.
SPAWN_WORKTREE = "worktrees.run_worktree("


def spawns_in_worktree(h: Harness) -> bool:
    spawn = h.root / "world/process/spawn.py"
    return spawn.is_file() and SPAWN_WORKTREE in spawn.read_text(encoding="utf-8", errors="replace")


def check_authority(h: Harness, out: list[Finding]) -> dict[str, dict]:
    """Returns per-agent facts the later checks reuse."""
    facts: dict[str, dict] = {}
    if not h.agents:
        out.append(Finding("authority", "agents found", "not_verified",
                           "No Agent(...) declarations were found in the harness's own "
                           "Python, so no authority could be read. Check that harness.py "
                           "registers its workers from files under this directory."))
        return facts
    gated = h.gated_names()
    for agent in h.agents:
        tool_names = h.tool_idents(agent.module, agent.kw.get("tools"))
        held = [t for t in h.tools if h.held_by(t, tool_names)]
        facts[agent.name] = {
            "agent": agent,
            "held": held,
            "tool_names": tool_names,
            "outward_ungated": [t for t in held if OUTWARD.search(h.tool_source(t))
                                and not ({t.func, t.factory} & gated)],
        }

    axes = (
        ("allow_bash", "raw Bash: any command, in the main checkout, with the harness's "
                       "whole environment", "ask it to run `ls ..` through Bash"),
        ("task_allowed", "spawning sub-agents, which inherit nothing the kit can fence "
                         "per agent", "ask it to delegate any step to a sub-agent"),
        ("allow_plugins", "the harness's installed plugins, whose own hooks and MCP "
                          "servers strict_mcp_config does not block",
         "list the plugins it loads (`Harness(plugins=...)`) and read each one's hooks"),
    )
    for key, what, how in axes:
        hits, unknown = [], []
        for agent in h.agents:
            got = h.value(agent, key)
            if got is True:
                hits.append(agent)
            elif got is UNKNOWN:
                unknown.append(agent)
            facts[agent.name][key] = got is True
        for agent in hits:
            out.append(Finding("authority", key, "open",
                               f"`{agent.name}` sets {key}=True: {what}.",
                               [agent.where],
                               f"Open {agent.where}; to see it live, {how}."))
        for agent in unknown:
            out.append(Finding("authority", key, "not_verified",
                               f"`{agent.name}` sets {key} from an expression the scanner "
                               "cannot evaluate; read it.", [agent.where]))
        if not hits and not unknown:
            out.append(Finding("authority", key, "closed",
                               f"No agent sets {key}=True ({len(h.agents)} agents read)."))

    plugins = h.harness_kw("plugins")
    mention_plugins = h.harness_kw("mention_plugins")
    has_plugins = plugins is not None and lit(plugins[1]) not in ([], (), None)
    if has_plugins and (mention_plugins is None or lit(mention_plugins[1]) is not False):
        module, node = plugins
        out.append(Finding("authority", "mention_plugins", "open",
                           "The harness syncs plugin marketplaces and leaves "
                           "`mention_plugins` at its default True, so the Slack mention "
                           "responder loads every enabled plugin — hooks and MCP servers "
                           "included — for any message in the channel.",
                           [f"{module.rel}:{node.lineno}"],
                           "Mention the main persona with a request one of the enabled "
                           "plugins' skills matches; it runs."))
    else:
        out.append(Finding("authority", "mention_plugins", "closed",
                           "The mention responder loads no plugins."
                           if not has_plugins else
                           "Plugins are synced but `mention_plugins=False`."))

    for agent in h.agents:
        f = facts[agent.name]
        scope = h.value(agent, "write_scope")
        f["scope"] = scope if isinstance(scope, (tuple, list)) else ()
        f["scope_unusual"] = False
        f["self_config"] = []
        if scope is None or (isinstance(scope, (tuple, list)) and not scope):
            out.append(Finding("authority", "write_scope", "open",
                               f"`{agent.name}` declares no write_scope. The kit requires "
                               "one, and `tasks/{task}/work` is the ordinary lane; with "
                               "none, nothing records where it may write.", [agent.where],
                               f"Open {agent.where}; `.the-world/agent-scope.json` lists "
                               "it with an empty scope after the harness starts."))
        elif scope is UNKNOWN:
            out.append(Finding("authority", "write_scope", "not_verified",
                               f"`{agent.name}`'s write_scope is computed; read it.",
                               [agent.where]))
        else:
            for entry in scope:
                kind, why = classify_scope(str(entry), agent.name)
                if kind == "wide":
                    out.append(Finding("authority", "write_scope", "open",
                                       f"`{agent.name}`'s write_scope entry `{entry}` {why}.",
                                       [agent.where],
                                       f"Give its write tool the path `{entry}/../x` or a "
                                       "file elsewhere in the harness; the scope allows it."))
                elif kind == "sensitive":
                    f["self_config"].append(entry)
                elif kind == "unusual":
                    f["scope_unusual"] = True
                    out.append(Finding("authority", "write_scope", "not_verified",
                                       f"`{agent.name}`'s write_scope entry `{entry}` {why}; "
                                       "confirm its prompt needs it.", [agent.where]))

        writers = [c for _, node in h.resolve(agent.module, agent.kw.get("tools"))
                   for c in ast.walk(node)
                   if isinstance(c, ast.Call) and callee(c) == "make_write_finding"]
        other_writers = [t for t in f["held"] if re.search(r"write|edit|create|save|put",
                                                           t.name, re.I)]
        if f["scope"] and not writers and not other_writers:
            out.append(Finding("authority", "write tool bound to scope", "open",
                               f"`{agent.name}` declares write_scope {list(f['scope'])} but "
                               "holds no write tool bound to it. The declaration grants "
                               "nothing: the agent either cannot write at all or writes "
                               "through something the scope does not describe.",
                               [agent.where],
                               f"Read its `tools=` at {agent.where}: no "
                               "make_write_finding(...) bound to these roots."))
        for call in writers:
            roots = call.args[1] if len(call.args) > 1 else None
            declared = agent.kw.get("write_scope")
            same_source = roots is not None and declared is not None and \
                segment(agent.module, roots) == segment(agent.module, declared)
            roots_value = lit(roots)
            if not same_source and roots_value is not UNKNOWN and roots_value is not None \
                    and tuple(roots_value) != tuple(f["scope"]):
                out.append(Finding("authority", "write tool bound to scope", "open",
                                   f"`{agent.name}`'s write tool is bound to "
                                   f"{list(roots_value)} but its write_scope says "
                                   f"{list(f['scope'])}. The tool is what enforces; the "
                                   "declaration is what people read.",
                                   [f"{agent.module.rel}:{call.lineno}"],
                                   "Call its write_finding with a path under a root the "
                                   "tool has and the scope lacks."))
            elif roots_value is UNKNOWN and not same_source:
                out.append(Finding("authority", "write tool bound to scope", "not_verified",
                                   f"`{agent.name}`'s write tool roots are computed; "
                                   "compare them with its write_scope by hand.",
                                   [f"{agent.module.rel}:{call.lineno}"]))

        mutates = h.value(agent, "mutates")
        f["mutates"] = mutates is True
        if mutates is True and spawns_in_worktree(h):
            # The kit cuts the worktree at spawn and refuses the run without one,
            # so the question is no longer "does anything make a worktree" but "does
            # anything write somewhere other than the cwd it was given". A cwd moves Bash
            # and relative paths; it does not move a tool bound to an absolute path.
            repo_writers = [t for t in other_writers
                            if "worktree" not in segment(t.module, t.node)]
            if f.get("allow_bash"):
                out.append(Finding("authority", "mutates", "not_verified",
                                   f"`{agent.name}` has mutates=True and Bash. The kit "
                                   "spawns it in its own worktree (world/process/spawn.py "
                                   "→ worktrees.run_worktree), but a cwd is not a sandbox: "
                                   "Bash can still reach the main checkout by an absolute "
                                   "path or `cd`. Git writes are refused by "
                                   "world-git-guard either way; confirm its prompt keeps "
                                   "other writes inside the worktree.",
                                   [agent.where],
                                   "Ask it to run `pwd` through Bash: it reports "
                                   "`.claude/worktrees/<agent>-<task id>`."))
            elif repo_writers:
                out.append(Finding("authority", "mutates", "not_verified",
                                   f"`{agent.name}` has mutates=True and runs in its own "
                                   "worktree, but "
                                   f"{', '.join(f'`{t.name}`' for t in repo_writers)} "
                                   "never mention a worktree. A tool's paths are bound when "
                                   "it is built and do not follow the run's cwd; confirm "
                                   "each writes under world.context.current_worktree() or "
                                   "the task's work/, not the main checkout.",
                                   [t.where for t in repo_writers]))
            else:
                out.append(Finding("authority", "mutates", "closed",
                                   f"`{agent.name}` has mutates=True; the kit spawns it in "
                                   "its own worktree and refuses the run without one, and "
                                   "nothing it holds writes elsewhere by name.",
                                   [agent.where]))
        elif mutates is True:
            # An older kit: every agent is spawned at the git root, whatever
            # mutates says. Kept so an audit of an older harness still tells the truth.
            module_text = "\n".join(segment(t.module, t.node) for t in f["held"])
            module_text += agent.module.source
            if f.get("allow_bash"):
                out.append(Finding("authority", "mutates", "open",
                                   f"`{agent.name}` has mutates=True and Bash. This "
                                   "harness's kit predates spawn-time worktrees: it spawns "
                                   "every agent with cwd at the harness's git root "
                                   "(world/runners/claude.py `cwd=str(harness.git_root)`), "
                                   "so its Bash runs in the main checkout, not a worktree. "
                                   "Updating world/ to the current kit closes this.",
                                   [agent.where],
                                   "Ask it to run `git status` through Bash: it reports the "
                                   "main checkout."))
            elif "worktree" not in module_text:
                out.append(Finding("authority", "mutates", "open",
                                   f"`{agent.name}` has mutates=True, but nothing it holds "
                                   "creates or targets a worktree, and this harness's kit "
                                   "predates spawn-time worktrees, so it does not move an "
                                   "agent into one by itself; its tools decide where it "
                                   "writes, and these are bound to the harness.",
                                   [agent.where],
                                   "Read each of its tools' bound paths; none is a "
                                   "worktree."))
            else:
                out.append(Finding("authority", "mutates", "not_verified",
                                   f"`{agent.name}` has mutates=True and its tools mention "
                                   "worktrees; confirm every write and git call is bound to "
                                   "the worktree path and never to the main checkout.",
                                   [agent.where]))
    if not any(f.get("mutates") for f in facts.values()):
        out.append(Finding("authority", "mutates", "closed", "No agent sets mutates=True."))
    return facts


def check_tools(h: Harness, out: list[Finding]) -> None:
    hits: list[Finding] = []
    for module in h.modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Assign) and node in module.tree.body:
                for target in node.targets:
                    if isinstance(target, ast.Name) and DENYLIST_NAME.search(target.id) \
                            and isinstance(node.value, (ast.List, ast.Tuple, ast.Set,
                                                        ast.Call, ast.Dict)):
                        hits.append(Finding(
                            "tools", "denylist", "open",
                            f"`{target.id}` is a list of what to refuse. A denylist is a "
                            "promise to have named every dangerous case; the next unnamed "
                            "flag walks past it. Enumerate what is allowed instead.",
                            [f"{module.rel}:{node.lineno}"],
                            "Find an argument the list does not name (a new flag or "
                            "subcommand) and pass it to the tool."))
            if not isinstance(node, ast.Call):
                continue
            where = f"{module.rel}:{node.lineno}"
            name = callee(node)
            if any(k.arg == "shell" and lit(k.value) is True for k in node.keywords):
                hits.append(Finding("tools", "shell=True", "open",
                                    "A subprocess runs through a shell, so metacharacters in "
                                    "any interpolated value are commands.", [where],
                                    "Pass `x; ls ..` as the value that reaches this call."))
                continue
            owner = node.func.value.id if isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) else ""
            if (owner == "os" and name in ("system", "popen")) or \
                    name in ("create_subprocess_shell", "getoutput", "getstatusoutput"):
                hits.append(Finding("tools", "shell=True", "open",
                                    f"`{owner + '.' if owner else ''}{name}` runs its "
                                    "argument through a shell.", [where],
                                    "Pass `x; ls ..` as the value that reaches this call."))
                continue
            if name == "split" and owner == "shlex" and node.args and \
                    not isinstance(node.args[0], ast.Constant):
                hits.append(Finding("tools", "string-built command", "open",
                                    "A command line is built as a string and split back "
                                    "into argv, so a value with spaces becomes extra "
                                    "arguments.", [where],
                                    "Pass a value containing ` --some-flag` to it."))
                continue
            runs = (owner == "subprocess" and name in ("run", "Popen", "call", "check_call",
                                                       "check_output")) or \
                name in ("create_subprocess_exec", "run_cmd")
            if not runs or not node.args:
                continue
            first = node.args[0]
            split_string = isinstance(first, ast.Call) and callee(first) == "split" and \
                isinstance(first.func, ast.Attribute) and isinstance(
                    first.func.value, (ast.JoinedStr, ast.BinOp, ast.Call))
            if isinstance(first, (ast.JoinedStr, ast.BinOp)) or split_string or (
                    isinstance(first, ast.Call) and callee(first) == "format") or (
                    isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and " " in first.value and owner == "subprocess"):
                hits.append(Finding("tools", "string-built command", "open",
                                    "A command is built as one string instead of an argv "
                                    "list.", [where],
                                    "Pass a value containing a space and a flag to it."))
                continue
            argv = first.elts if isinstance(first, (ast.List, ast.Tuple)) else node.args
            if len(argv) > 1 and lit(argv[0]) in SHELLS and lit(argv[1]) in ("-c", "/c",
                                                                              "-Command"):
                hits.append(Finding("tools", "shell=True", "open",
                                    f"argv starts with `{lit(argv[0])} {lit(argv[1])}`: a "
                                    "shell by another name.", [where],
                                    "Pass `x; ls ..` as the value that reaches this call."))
    out.extend(hits)
    for check in ("shell=True", "string-built command", "denylist"):
        if not any(f.check == check for f in hits):
            out.append(Finding("tools", check, "closed",
                               f"None found in the harness's own Python "
                               f"({len(h.modules)} files)."))

    params_open = paths_open = 0
    for tool in h.tools:
        source = h.tool_source(tool)
        for param, value in schema_params(tool).items():
            low = param.lower()
            if low in BOUNDARY_PARAMS and not bounded(value):
                params_open += 1
                out.append(Finding("tools", "parameter reopens the boundary", "open",
                                   f"Tool `{tool.name}` takes `{param}` as a free value, so "
                                   "the caller chooses which "
                                   f"{low} it acts on. Bind it at construction or make it "
                                   "an enum.", [tool.where],
                                   f"Call `{tool.name}` with a `{param}` it was never meant "
                                   "for."))
            elif low in PATH_PARAMS:
                contained = CONTAINMENT.search(source) and RESOLVES.search(source)
                if not contained:
                    paths_open += 1
                    out.append(Finding("tools", "path escape", "open",
                                       f"Tool `{tool.name}` takes `{param}` and never "
                                       "resolves it and checks it stays under a root, so "
                                       "`..`, an absolute path, or a symlink leaves the "
                                       "boundary.", [tool.where],
                                       f"Call `{tool.name}` with `{param}` = "
                                       "`../../etc/hosts`, then with an absolute path."))
    if not params_open:
        out.append(Finding("tools", "parameter reopens the boundary", "closed",
                           f"No tool takes a free cwd/repo/host/url/account-style parameter "
                           f"({len(h.tools)} tools read)."))
    if not paths_open:
        out.append(Finding("tools", "path escape", "closed",
                           "Every tool taking a path resolves it and checks containment."
                           if h.tools else "The harness defines no tools of its own."))
    if h.tools:
        out.append(Finding("tools", "tool modules read", "not_verified",
                           "A pattern scan cannot prove a tool is bound to one project, "
                           "account, or host. Read each tool module and say for each what "
                           "it is bound to.",
                           sorted({t.module.rel for t in h.tools})))


def check_outward(h: Harness, facts: dict, out: list[Finding]) -> None:
    gated = h.gated_names()
    held_any: set[str] = set()
    for f in facts.values():
        held_any |= f["tool_names"]
    mention = h.harness_kw("mention_tools")
    mention_names = h.tool_idents(mention[0], mention[1]) if mention else set()
    outward = [t for t in h.tools if OUTWARD.search(h.tool_source(t))]
    for tool in outward:
        is_gated = bool({tool.func, tool.factory} & gated)
        holders = [name for name, f in facts.items() if tool in f["held"]]
        in_mention = h.held_by(tool, mention_names)
        if is_gated:
            out.append(Finding("outward", "outward action gated", "closed",
                               f"Tool `{tool.name}` acts outside and is wrapped in "
                               "make_approval_gated (or given to a persona's "
                               "mention_tools, which the kit wraps).", [tool.where]))
        elif holders or in_mention:
            who = ", ".join(f"`{n}`" for n in holders) or "the Slack mention responder"
            out.append(Finding("outward", "outward action gated", "open",
                               f"Tool `{tool.name}` acts outside the machine and is not "
                               f"behind the approval gate; {who} can fire it with no human "
                               "decision.", [tool.where],
                               f"Ask {who} to perform the action `{tool.name}` does; it "
                               "runs before anyone clicks approve."))
        else:
            out.append(Finding("outward", "outward action gated", "not_verified",
                               f"Tool `{tool.name}` acts outside and is not gated, but the "
                               "scanner could not find who holds it. Trace where it is "
                               "registered.", [tool.where]))
    if not outward:
        out.append(Finding("outward", "outward action gated", "closed",
                           "No tool in the harness's own code matched an outward-action "
                           "pattern (HTTP writes, Slack posts, git push, PR/issue writes, "
                           "mail)."))

    pre = h.harness_kw("pre_approved_tools")
    pre_value = lit(pre[1]) if pre else None
    if pre_value:
        out.append(Finding("outward", "pre_approved_tools", "open",
                           f"`pre_approved_tools` lifts the approval doctrine for "
                           f"{list(pre_value) if pre_value is not UNKNOWN else 'a computed list'}"
                           ". Confirm none of them acts outside.",
                           [f"{pre[0].rel}:{pre[1].lineno}"],
                           "Ask the mention responder to use one of them; it does not "
                           "request approval first."))
    else:
        out.append(Finding("outward", "pre_approved_tools", "closed",
                           "No tool is exempted from the approval doctrine."))

    approver_key = h.slack_env_name("approver_env", "APPROVER_SLACK_ID")
    bot_key = h.slack_env_name("bot_token_env", "SLACK_BOT_TOKEN")
    slack_on = bool(h.env.get(bot_key or ""))
    if not slack_on:
        out.append(Finding("outward", "approver verification", "not_verified",
                           f"`{bot_key}` is not set in env/harness.env, so Slack is not "
                           "connected and no approval has been exercised. Re-run the audit "
                           "after connecting."))
    elif not h.env.get(approver_key or ""):
        # Older kits skipped the click check when no approver was set. A harness
        # still carrying that line lets anyone approve; a current one refuses everyone.
        gate = h.root / "world/slack/approvals.py"
        old_gate = gate.is_file() and OPEN_APPROVER_GATE in gate.read_text(encoding="utf-8", errors="replace")
        if old_gate:
            out.append(Finding("outward", "approver verification", "open",
                               f"Slack is connected but `{approver_key}` is empty, and this "
                               "harness's approval gate only checks the clicker when an "
                               f"approver is configured (world/slack/approvals.py "
                               f"`{OPEN_APPROVER_GATE}`), so anyone in the channel can "
                               "approve. Newer kits refuse every click instead.",
                               ["env/harness.env", "world/slack/approvals.py"],
                               "Have a channel member who is not the operator click ✅ on "
                               "any approval request; it is accepted."))
        else:
            out.append(Finding("outward", "approver verification", "closed",
                               f"Slack is connected but `{approver_key}` is empty, so every "
                               "approval click is refused and nothing outward can be "
                               f"approved until it is set in env/harness.env.",
                               ["env/harness.env"]))
    else:
        out.append(Finding("outward", "approver verification", "closed",
                           f"`{approver_key}` is set; clicks from anyone else are refused."))
    co = h.slack_env_name("co_approver_env", None)
    if co:
        out.append(Finding("outward", "co-approver", "open",
                           f"`co_approver_env={co!r}` honors a second identity's decision. "
                           "If it is a bot, it decides by posting text, and the kit cannot "
                           "verify a human stood behind it — a trust decision the operator "
                           "makes, which this report should name.",
                           [f"{m.rel}:{c.lineno}" for m, c in h.slack_calls],
                           f"Post an approval as the identity in `{co}`; it counts."))

    for agent in h.agents:
        node = agent.kw.get("buttons")
        if node is None:
            continue
        text = segment(agent.module, node)
        if "danger" in text:
            out.append(Finding("outward", "buttons", "not_verified",
                               f"`{agent.name}` declares a danger button; confirm what it "
                               "triggers still goes through approval.", [agent.where]))
    with_buttons = [a.name for a in h.agents if a.kw.get("buttons") is not None]
    out.append(Finding("outward", "buttons", "closed",
                       "Buttons are declared per worker and the kit adds none by default"
                       + (f"; declared on {', '.join(with_buttons)}." if with_buttons
                          else "; no worker declares any.")))


def check_content(h: Harness, facts: dict, out: list[Finding]) -> None:
    outside_agents = []
    for name, f in facts.items():
        agent = f["agent"]
        trigger = agent.kw.get("trigger")
        source = agent.module.source
        if trigger is not None:
            for module, node in h.resolve(agent.module, trigger):
                for n in ast.walk(node):
                    if isinstance(n, ast.Name):
                        target = h._assignment(module, n.id)
                        if target is not None:
                            source += target[0].source
        for n in ast.walk(agent.module.tree):
            if isinstance(n, ast.ImportFrom) and n.module in h.by_dotted:
                source += h.by_dotted[n.module].source
        via_mention = trigger is not None and callee(trigger) == "MentionTrigger"
        fetches = OUTSIDE_INPUT.search(source)
        if via_mention or fetches:
            outside_agents.append((name, "Slack mentions" if via_mention else
                                   f"what its check fetches (`{fetches.group(0)}`)"))
    privileged = {name for name, f in facts.items()
                  if f.get("allow_bash") or f.get("task_allowed") or f.get("allow_plugins")
                  or f.get("mutates") or f["outward_ungated"] or f["self_config"]
                  or f.get("scope_unusual")}
    for name, source in outside_agents:
        agent = facts[name]["agent"]
        if name in privileged:
            out.append(Finding("content", "outside input to a privileged bot", "open",
                               f"`{name}` reads {source} and holds more than its task "
                               "lane (Bash, sub-agents, plugins, a mutating or outward "
                               "tool, or a wider scope). Text planted there can steer that "
                               "authority; only a human approval step can catch it.",
                               [agent.where],
                               f"Plant an instruction in the input `{name}` reads "
                               "(an issue body, a message) asking for the privileged "
                               "action."))
        else:
            out.append(Finding("content", "outside input to a privileged bot", "closed",
                               f"`{name}` reads {source} but holds nothing beyond its task "
                               "lane, so planted text has no authority to steer. The "
                               "content itself is still untrusted.", [agent.where]))
    if not outside_agents:
        if privileged:
            out.append(Finding("content", "outside input to a privileged bot", "not_verified",
                               "The scanner found no Slack trigger or outside fetch feeding "
                               f"{', '.join(f'`{n}`' for n in sorted(privileged))}, which "
                               "hold more than their task lane. Read their checks for input "
                               "it does not recognise.",
                               [facts[n]["agent"].where for n in sorted(privileged)]))
        else:
            out.append(Finding("content", "outside input to a privileged bot", "closed",
                               "No agent holds anything beyond its task lane, so outside "
                               "text has no authority to steer."))
    others = privileged - {n for n, _ in outside_agents}
    if outside_agents and others:
        out.append(Finding("content", "findings fed to other bots", "open",
                           "Every spawn's prompt carries recent findings from all agents "
                           "(world/process/scheduler.py, recent_context_snippet). Outside "
                           f"text read by {', '.join(f'`{n}`' for n, _ in outside_agents)} "
                           "can travel through a finding summary into the prompt of "
                           f"privileged {', '.join(f'`{n}`' for n in sorted(others))}.",
                           [facts[n]["agent"].where for n in sorted(others)],
                           "Have an outside-input bot record a finding whose summary "
                           "carries an instruction; read the next privileged spawn's "
                           "prompt."))

    mention = h.harness_kw("mention_tools")
    if mention:
        names = h.tool_idents(mention[0], mention[1])
        risky = [t for t in h.tools if h.held_by(t, names) and OUTWARD.search(h.tool_source(t))
                 and not ({t.func, t.factory} & h.gated_names())]
        if risky:
            out.append(Finding("content", "mention responder tools", "open",
                               "The Slack mention responder holds ungated outward tools "
                               f"({', '.join(t.name for t in risky)}); anyone who can post "
                               "in the channel can talk it into using them.",
                               [f"{mention[0].rel}:{mention[1].lineno}"],
                               "Mention the main persona asking for that action."))
    relay = h.slack_env_name("relay_channels_env", None)
    if relay:
        out.append(Finding("content", "relay channels", "open",
                           f"`relay_channels_env={relay!r}` lets mentions from other "
                           "channels reach the main persona; their authors are outside "
                           "this harness's channel membership.",
                           [f"{m.rel}:{c.lineno}" for m, c in h.slack_calls],
                           "Mention the main persona from a relay channel."))

    ledger = h.root / "ledger.jsonl"
    approver = h.env.get(h.slack_env_name("approver_env", "APPROVER_SLACK_ID") or "", "")
    co_key = h.slack_env_name("co_approver_env", None)
    co = h.env.get(co_key, "") if co_key else ""
    strangers = []
    denied = 0
    if ledger.is_file():
        for number, line in enumerate(ledger.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                strangers.append(f"ledger.jsonl:{number} (not JSON)")
                continue
            denied += entry.get("decision") == "denied"
            by = entry.get("by")
            if approver and by and by not in (approver, co):
                strangers.append(f"ledger.jsonl:{number}")
    if strangers:
        out.append(Finding("content", "ledger fed into prompts", "open",
                           "Denied ledger lines are read back into every spawn's prompt "
                           "(world/process/ledger.py, recent_rejections_snippet), and these "
                           "lines were not written by the configured approver — or are not "
                           "ledger entries at all.", strangers[:10],
                           "Read those lines; then read the next spawn's prompt."))
    else:
        out.append(Finding("content", "ledger fed into prompts", "closed",
                           f"{denied} denied line(s) reach every spawn's prompt; every "
                           "attributed line was decided by the configured approver."
                           if ledger.is_file() else "No ledger.jsonl yet."))


def check_self_config(h: Harness, facts: dict, out: list[Finding]) -> None:
    for name, f in facts.items():
        for entry in f["self_config"]:
            out.append(Finding("self_config", "writes its own configuration", "open",
                               f"`{name}`'s write_scope includes `{entry}`, which configures "
                               "or records the harness itself (hooks, settings, the scope "
                               "table, its code, credentials, or the ledger).",
                               [f["agent"].where],
                               "After the harness starts, `.the-world/agent-scope.json` "
                               f"lists `{entry}` for `{name}`, and world-scope-guard allows "
                               "its writes there."))
    writes = re.compile(r"write_text|write_bytes|open\([^)]*['\"][wa]|shutil\.|\.unlink\(|"
                        r"\.rename\(|\.replace\(")
    config = re.compile(r"\.claude|\.codex|settings(?:\.local)?\.json|agent-scope|hooks\.json"
                        r"|harness\.py|harness\.env")
    for tool in h.tools:
        source = h.tool_source(tool)
        if writes.search(source) and config.search(source):
            out.append(Finding("self_config", "writes its own configuration", "open",
                               f"Tool `{tool.name}` writes files and names harness "
                               f"configuration (`{config.search(source).group(0)}`).",
                               [tool.where],
                               f"Read `{tool.name}`; call it with that path."))
    if not any(f.category == "self_config" and f.check == "writes its own configuration"
               for f in out):
        out.append(Finding("self_config", "writes its own configuration", "closed",
                           "No agent's scope or tool reaches hooks, settings, the scope "
                           "table, harness code, credentials, or the ledger."))

    disabled = []
    for path in (h.root / ".claude/settings.json", h.root / ".claude/settings.local.json",
                 Path.home() / ".claude/settings.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("disableAllHooks") is True:
                disabled.append(str(path).replace(str(Path.home()), "~"))
        except (OSError, ValueError, AttributeError):
            continue
    provider = h.provider
    if provider in ("claude",) or (h.root / ".claude/hooks").is_dir():
        settings = h.root / ".claude/settings.json"
        try:
            registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        except (OSError, ValueError):
            registered = None
        if registered is None:
            out.append(Finding("self_config", "guard hooks registered", "open",
                               ".claude/settings.json is missing or unreadable, so none of "
                               "the four guard hooks runs.", [".claude/settings.json"],
                               "`cat .claude/settings.json`"))
        else:
            for hook, wants in CLAUDE_HOOKS.items():
                matchers = [m.get("matcher", "") for m in registered.get("PreToolUse", [])
                            if any(hook in x.get("command", "") for x in m.get("hooks", []))]
                covered = set("|".join(matchers).split("|"))
                missing = [t for t in wants.split("|") if t not in covered]
                exists = (h.root / ".claude/hooks" / hook).is_file()
                if not matchers or missing or not exists:
                    why = ("is not registered" if not matchers else
                           f"does not see {', '.join(missing)}" if missing else
                           "is registered but the file is missing")
                    out.append(Finding("self_config", "guard hooks registered", "open",
                                       f"`{hook}` {why}.", [".claude/settings.json"],
                                       f"`grep -n {hook} .claude/settings.json`"))
            if not any(f.check == "guard hooks registered" and f.status == "open" for f in out):
                out.append(Finding("self_config", "guard hooks registered", "closed",
                                   "All four guard hooks are registered for the tools they "
                                   "guard and their files exist."))
    if provider == "codex" or (h.root / ".codex").is_dir():
        hooks = h.root / ".codex/hooks.json"
        guard = h.root / ".codex/hooks/world-guard.py"
        try:
            ok = "world-guard.py" in hooks.read_text(encoding="utf-8") and guard.is_file()
        except OSError:
            ok = False
        out.append(Finding("self_config", "Codex guard hook registered",
                           "closed" if ok else "open",
                           "`.codex/hooks.json` registers world-guard.py and the file exists."
                           if ok else "The Codex guard hook is not registered or missing.",
                           [] if ok else [".codex/hooks.json"],
                           "" if ok else "`cat .codex/hooks.json`"))
    if disabled:
        out.append(Finding("self_config", "hooks disabled", "open",
                           "`disableAllHooks: true` is set; spawned Claude sessions load "
                           "user, project, and local settings, so every guard is off.",
                           disabled, "`grep -n disableAllHooks` in the files listed."))
    else:
        out.append(Finding("self_config", "hooks disabled", "closed",
                           "No settings file the spawns load sets disableAllHooks."))

    kit_integrity(h, out)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kit_integrity(h: Harness, out: list[Finding]) -> None:
    if h.kit is None or not (h.kit / "world").is_dir():
        out.append(Finding("self_config", "kit files unchanged", "not_verified",
                           "The bundled kit was not found next to this skill, so world/ and "
                           "the hooks could not be compared with what the kit ships."))
        return
    pairs = [p.relative_to(h.kit) for p in (h.kit / "world").rglob("*")
             if p.is_file() and "__pycache__" not in p.parts]
    pairs += [Path(".claude/hooks") / name for name in CLAUDE_HOOKS
              if (h.root / ".claude/hooks").is_dir()]
    if (h.root / ".codex").is_dir():
        pairs.append(Path(".codex/hooks/world-guard.py"))
    changed = [rel.as_posix() for rel in pairs
               if (h.root / rel).is_file() and _digest(h.root / rel) != _digest(h.kit / rel)]
    if changed:
        out.append(Finding("self_config", "kit files unchanged", "not_verified",
                           f"{len(changed)} kit-owned file(s) differ from the kit this audit "
                           "ships with — an older kit, or an edit in place. The kit's "
                           "guarantees are only as good as these files; the invariants "
                           "below are checked line by line either way.", changed[:12],
                           "`diff -r <the-world-start>/assets/kit/world world`"))
    else:
        out.append(Finding("self_config", "kit files unchanged", "closed",
                           "world/ and the guard hooks match the bundled kit byte for byte."))
    for category, rel, needle, meaning in KIT_INVARIANTS:
        path = h.root / rel
        if not path.is_file():
            continue
        present = needle in path.read_text(encoding="utf-8", errors="replace")
        out.append(Finding(category, "kit invariant", "closed" if present else "open",
                           f"{meaning} (`{needle}` in {rel})." if present else
                           f"`{needle}` is gone from {rel}: no longer true that {meaning}.",
                           [] if present else [rel],
                           "" if present else f"`grep -n '{needle}' {rel}`"))


def check_secrets(h: Harness, facts: dict, out: list[Finding]) -> None:
    listed = h.git("ls-files")
    tracked = set(listed.stdout.split("\n")) if listed and listed.returncode == 0 else None
    hits = []
    for path in sorted(h.root.rglob("*")):
        rel = path.relative_to(h.root)
        if not path.is_file() or any(p in SECRET_SKIP_DIRS for p in rel.parts):
            continue
        if rel.as_posix() == "env/harness.env":
            continue                # where tokens belong; checked for tracking below
        try:
            if path.stat().st_size > MAX_SECRET_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    where = f"{rel.as_posix()}:{number}"
                    state = ("committed to git" if tracked is not None and rel.as_posix()
                             in tracked else "on disk")
                    hits.append(Finding("secrets", "token outside env/harness.env", "open",
                                        f"A {kind} ({match.group(0)[:4]}…) is {state} in "
                                        f"{rel.as_posix()}.", [where],
                                        f"`grep -c '{match.group(0)[:4]}' {rel.as_posix()}`"
                                        " — rotate it before anything else."))
                    break
    out.extend(hits[:20])
    if len(hits) > 20:
        out.append(Finding("secrets", "token outside env/harness.env", "open",
                           f"{len(hits) - 20} more hits not listed."))
    if not hits:
        out.append(Finding("secrets", "token outside env/harness.env", "closed",
                           "No token pattern found in code, prompts, logs, tasks, or the "
                           "ledger."))

    env_rel = "env/harness.env"
    if not (h.root / env_rel).is_file():
        out.append(Finding("secrets", "harness.env kept out of git", "not_verified",
                           "env/harness.env does not exist yet."))
    elif tracked is None:
        out.append(Finding("secrets", "harness.env kept out of git", "not_verified",
                           "Not a git repository, so tracking could not be checked."))
    elif env_rel in tracked:
        out.append(Finding("secrets", "harness.env kept out of git", "open",
                           "env/harness.env is committed.", [env_rel],
                           "`git ls-files env/harness.env`"))
    else:
        ignored = h.git("check-ignore", "-q", env_rel)
        ok = ignored is not None and ignored.returncode == 0
        out.append(Finding("secrets", "harness.env kept out of git", "closed" if ok else "open",
                           "env/harness.env is gitignored." if ok else
                           "env/harness.env is not ignored; one `git add -A` commits it.",
                           [] if ok else [".gitignore"],
                           "" if ok else "`git check-ignore -v env/harness.env` prints nothing"))

    tokens = sorted(k for k, v in h.env.items() if v and TOKENISH_KEY.search(k))
    bash = [n for n, f in facts.items() if f.get("allow_bash")]
    if bash and tokens:
        out.append(Finding("secrets", "credentials a bot can reach", "open",
                           f"{', '.join(f'`{n}`' for n in bash)} "
                           f"{'holds' if len(bash) == 1 else 'hold'} Bash, and a spawned "
                           "agent inherits the harness process's environment (Codex passes "
                           "it whole: world/runners/codex.py `os.environ.copy()`). Every "
                           "credential "
                           f"in env/harness.env is readable: {', '.join(tokens)}.",
                           [facts[n]["agent"].where for n in bash],
                           "Ask it to print whether `$" + tokens[0] + "` is non-empty "
                           "(never the value)."))
    else:
        out.append(Finding("secrets", "credentials a bot can reach", "closed",
                           f"No agent has Bash, so the {len(tokens)} credential(s) in "
                           "env/harness.env reach agents only through tools."))
    env_reads: dict[str, set[str]] = {}
    for tool in h.tools:
        for node in ast.walk(tool.module.tree):
            if isinstance(node, ast.Subscript) and segment(tool.module, node.value) == "os.environ":
                key = lit(node.slice)
            elif isinstance(node, ast.Call) and segment(tool.module, node.func) in (
                    "os.environ.get", "os.getenv") and node.args:
                key = lit(node.args[0])
            else:
                continue
            if isinstance(key, str):
                env_reads.setdefault(tool.module.rel, set()).add(key)
    if env_reads:
        out.append(Finding("secrets", "credentials a tool holds", "not_verified",
                           "Tools read these credentials; confirm each is the narrowest one "
                           "that does the job (read-only token, one project): " +
                           "; ".join(f"{m}: {', '.join(sorted(k))}"
                                     for m, k in sorted(env_reads.items())),
                           sorted(env_reads)))


def check_silent(h: Harness, facts: dict, out: list[Finding]) -> None:
    for name, f in facts.items():
        agent = f["agent"]
        trigger = agent.kw.get("trigger")
        if trigger is not None and callee(trigger) == "MentionTrigger":
            continue
        writes = h.value(agent, "writes_finding")
        task_type = h.value(agent, "task_type")
        if writes is True:
            continue
        if task_type:
            out.append(Finding("silent", "exit 0 counted as done", "not_verified",
                               f"`{name}` has no writes_finding but a task_type "
                               f"`{task_type}`; read that type's completion predicate.",
                               [agent.where]))
        else:
            out.append(Finding("silent", "exit 0 counted as done", "open",
                               f"`{name}` runs unattended with writes_finding=False and no "
                               "task_type. Only a permission denial marks its run blocked, "
                               "so a run that gives up in prose and exits 0 is recorded as "
                               "done and its on_done bookkeeping fires.",
                               [agent.where],
                               f"Starve `{name}`'s input (an empty or unreachable source); "
                               "the run ends 'ok' with nothing produced."))
    if not any(f.check == "exit 0 counted as done" for f in out):
        out.append(Finding("silent", "exit 0 counted as done", "closed",
                           "Every unattended agent is verified by an artifact."))

    early = []
    for agent in h.agents:
        trigger = agent.kw.get("trigger")
        if trigger is None:
            continue
        check = next((k.value for k in getattr(trigger, "keywords", []) if k.arg == "check"),
                     None)
        if not isinstance(check, ast.Name):
            continue
        for module in h.modules:
            for node in module.tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == check.id:
                    for stmt in _outer_statements(node):
                        targets = []
                        if isinstance(stmt, ast.Assign):
                            targets = stmt.targets
                        elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
                            targets = [stmt.target]
                        mutating_call = isinstance(stmt, ast.Expr) and isinstance(
                            stmt.value, ast.Call) and callee(stmt.value) in (
                            "update", "add", "append", "extend", "pop", "remove", "clear")
                        if any(isinstance(t, ast.Subscript) for t in targets) or mutating_call:
                            early.append(f"{module.rel}:{stmt.lineno}")
    for where in early:
        out.append(Finding("silent", "input marked seen before completion", "open",
                           "A poll check changes state before returning its Signal, so the "
                           "input counts as handled even when the run is blocked. That "
                           "bookkeeping belongs in on_done.", [where],
                           "Make the next run fail; the input is not retried."))
    if not early:
        out.append(Finding("silent", "input marked seen before completion", "closed",
                           "No poll check writes state outside on_done."))

    swallowed = []
    for module in h.modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ExceptHandler) and all(
                    isinstance(s, (ast.Pass, ast.Continue)) or (
                        isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                    for s in node.body):
                swallowed.append(f"{module.rel}:{node.lineno}")
    if swallowed:
        out.append(Finding("silent", "errors swallowed", "open",
                           "An exception handler discards the error with no log or "
                           "result, so the failure looks like nothing happened.",
                           swallowed[:12], "Make the guarded call raise; nothing reports it."))
    else:
        out.append(Finding("silent", "errors swallowed", "closed",
                           "No bare `except: pass` in the harness's own code."))

    concurrency = h.harness_kw("max_concurrent_runs")
    out.append(Finding("silent", "concurrency drops work", "closed",
                       "Runs past max_concurrent_runs are queued by the kit's orchestrator, "
                       "not dropped"
                       + (f" (limit set at {concurrency[0].rel}:{concurrency[1].lineno})."
                          if concurrency else " (default limit 2).")
                       + " Holds only while world/ matches the kit — see self_config."))


def _outer_statements(func: ast.AST):
    """Statements in a function body, not descending into nested defs or lambdas."""
    stack = list(getattr(func, "body", []))
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield stmt
        for name in ("body", "orelse", "finalbody", "handlers"):
            stack.extend(getattr(stmt, name, []) or [])


def audit(root: Path, kit: Path | None) -> tuple[Harness, list[Finding]]:
    h = Harness(root, kit)
    out: list[Finding] = []
    for error in h.parse_errors:
        out.append(Finding("authority", "parse", "not_verified",
                           f"Could not parse {error}; nothing in it was checked."))
    facts = check_authority(h, out)
    check_tools(h, out)
    check_outward(h, facts, out)
    check_content(h, facts, out)
    check_self_config(h, facts, out)
    check_secrets(h, facts, out)
    check_silent(h, facts, out)
    order = {key: i for i, (key, _) in enumerate(CATEGORIES)}
    rank = {"open": 0, "not_verified": 1, "closed": 2}
    out.sort(key=lambda f: (order[f.category], rank[f.status]))
    return h, out


def render(h: Harness, findings: list[Finding]) -> str:
    mark = {"open": "OPEN", "not_verified": "NOT VERIFIED", "closed": "closed"}
    lines = [f"Harness: {h.root}", f"Provider: {h.provider}",
             f"Agents: {', '.join(a.name for a in h.agents) or 'none found'}",
             f"Tools defined in the harness: {len(h.tools)}", ""]
    for key, title in CATEGORIES:
        mine = [f for f in findings if f.category == key]
        counts = {s: sum(f.status == s for f in mine) for s in mark}
        lines.append(f"## {title} — {counts['open']} open, {counts['not_verified']} not "
                     f"verified, {counts['closed']} closed")
        for f in mine:
            lines.append(f"- [{mark[f.status]}] {f.check}: {f.detail}")
            if f.where:
                lines.append(f"    at: {', '.join(f.where)}")
            if f.repro and f.status == "open":
                lines.append(f"    repro: {f.repro}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("harness", type=Path, help="the harness directory (holds harness.py)")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument("--kit", type=Path, default=None,
                        help="the kit to compare world/ against (default: the one bundled "
                             "with the-world-start next to this skill)")
    args = parser.parse_args()
    root = args.harness.expanduser().resolve()
    if not (root / "harness.py").is_file():
        parser.exit(2, f"{root} has no harness.py — point this at a World harness directory\n")
    kit = args.kit or Path(__file__).resolve().parent.parent / "the-world-start/assets/kit"
    h, findings = audit(root, kit.resolve() if kit.is_dir() else None)
    if args.json:
        print(json.dumps({"harness": str(root), "provider": h.provider,
                          "agents": [a.name for a in h.agents],
                          "categories": dict(CATEGORIES),
                          "findings": [asdict(f) for f in findings]},
                         ensure_ascii=False, indent=2))
    else:
        print(render(h, findings))


if __name__ == "__main__":
    main()
