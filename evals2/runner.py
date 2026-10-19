#!/usr/bin/env python3
"""A second eval runner, for the contracts `claude plugin eval` structurally cannot check.

Why this exists, measured against claude plugin eval:

  - `regex target: files` matches file PATHS, not contents, and `file_exists` sees only
    files the run wrote. So "the scope is declared in code" and "this file survived" are
    both inexpressible there, and a deletion is indistinguishable from an untouched file.
  - Its sandbox does not mount the plugin's own assets, so `assets/kit/` is unreachable
    inside a run and nothing can be built against the real API. Runs guessed `Tool(...)`
    when the real name is `ToolSpec`, and every grader passed them.

The consequence there is that the suite grades prose: a design described in the final
message, never a system. This runner grades objects. It hands the model a workspace with a
real importable `world/`, lets it work, and then IMPORTS what it produced with the kit's own
dependencies and asserts against the live `Harness`, `Agent` and `ToolSpec` instances.

It is not a replacement. `claude plugin eval` sandboxes each run and this does not: the
session runs as you, with whatever tools the case allows, in a temp workspace. Use it on
cases you wrote.

Usage:
    evals2/.venv/bin/python evals2/runner.py                     # every case, both arms
    evals2/.venv/bin/python evals2/runner.py --case equip_real --runs 3
    evals2/.venv/bin/python evals2/runner.py --arms with         # skip the ablation arm
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner_api import Check  # noqa: E402 — needs the sys.path line above

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "skills" / "start" / "assets" / "kit"
CASES = Path(__file__).resolve().parent / "cases"
CHECKER = Path(__file__).resolve().parent / ".venv" / "bin" / "python"


@dataclass
class Run:
    """What the agent session left behind, for a case's checks to read."""

    ws: Path
    case: str
    arm: str
    before: dict[str, str]  # path -> sha256, as scaffolded
    after: dict[str, str]
    final_text: str
    cost_usd: float
    seconds: float
    error: str | None = None
    checks: list[Check] = field(default_factory=list)

    # --- the three questions plugin eval cannot answer -----------------------------

    def deleted(self) -> list[str]:
        return sorted(p for p in self.before if p not in self.after)

    def modified(self) -> list[str]:
        return sorted(p for p in self.before if p in self.after and self.after[p] != self.before[p])

    def added(self) -> list[str]:
        return sorted(p for p in self.after if p not in self.before)

    def probe(self, code: str) -> dict:
        """Run `code` against the workspace with the kit's dependencies available, and
        parse the single JSON object it prints. This is the whole point of this runner:
        the workspace's own `world/` is on sys.path, so the code the model wrote gets
        imported for real and the assertions look at live objects.

        A failure comes back as {"error": ...} rather than raising, because a harness that
        cannot even be imported is a result, not a crash.
        """
        env = {**os.environ, "PYTHONPATH": str(self.ws), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("VIRTUAL_ENV", None)
        try:
            out = subprocess.run(
                [str(CHECKER), "-c", code], cwd=self.ws, env=env,
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return {"error": "probe timed out after 120s"}
        text = (out.stdout or "").strip()
        if not text:
            return {"error": f"probe printed nothing; stderr: {(out.stderr or '')[-800:]}"}
        try:
            return json.loads(text.splitlines()[-1])
        except json.JSONDecodeError:
            return {"error": f"probe output was not JSON: {text[-800:]}"}


def snapshot(ws: Path) -> dict[str, str]:
    out = {}
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
            continue
        out[str(p.relative_to(ws))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def load_case(name: str):
    path = CASES / name / "case.py"
    spec = importlib.util.spec_from_file_location(f"case_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_kit(ws: Path) -> None:
    """Put a real, importable `world/` in the workspace, the way an installed harness has
    one. Copying beats `install-kit.py` here only because the case fixtures write their own
    harness.py and settings; the point is that `import world` resolves to files on disk that
    the model can also read."""
    shutil.copytree(KIT / "world", ws / "world")


SRT = shutil.which("srt")


def sandbox_profile(ws: Path) -> Path:
    """Write a Seatbelt/bubblewrap profile for one run and return its path.

    srt (anthropic-experimental/sandbox-runtime) generates the platform profile from this
    JSON, so the runner does not hand-write Seatbelt. Verified on macOS: reading
    ~/.ssh and writing anywhere in $HOME come back "Operation not permitted", the workspace
    reads and writes fine, and a request to a domain outside allowedDomains gets nothing.

    The three home paths that stay readable are what `claude` itself needs to start: its
    config, and the keychain that holds the OAuth credentials. Blanking $HOME or
    CLAUDE_CONFIG_DIR instead breaks login outright, which is why the sandbox draws the line
    here rather than at the home directory boundary. The agent's Bash can still read
    ~/.claude if it goes looking; what it cannot do is reach the rest of the home, write
    outside the workspace, or talk to anything but the API.

    The profile lives OUTSIDE the workspace and is not in allowRead, so the sandboxed
    process cannot read the rules it is running under. srt reads it before confining.
    """
    home = Path.home()
    cfg_dir = Path(tempfile.mkdtemp(prefix="srtcfg-"))
    cfg = cfg_dir / "profile.json"
    cfg.write_text(json.dumps({
        "filesystem": {
            "denyRead": [str(home)],
            "allowRead": [
                str(ws), str(ROOT),                    # the case's room, and the plugin
                str(home / ".claude"), str(home / ".claude.json"),
                str(home / "Library" / "Keychains"),   # OAuth credentials
                str(home / ".nvm"), str(home / ".local"),
            ],
            "allowWrite": [
                str(ws),
                str(home / ".claude"),                 # sessions, history
                "/tmp", "/private/tmp", "/private/var/folders",
            ],
            "denyWrite": [],
        },
        # Empty would mean no network at all, and the session needs the API to exist.
        "network": {"allowedDomains": ["*.anthropic.com"], "deniedDomains": []},
    }, indent=2))
    return cfg


def run_agent(case, ws: Path, arm: str, *, sandbox: bool) -> tuple[str, float, float, str | None]:
    """Start the session the case describes, in a room it cannot recognise as an exam.

    `--setting-sources project` drops the operator's ~/.claude: no user-level CLAUDE.md, and
    none of the hooks in their settings.json. An operator's settings can carry a PreToolUse
    hook that rewrites Bash calls and other global hooks, so a measurement taken with user
    settings loaded is a measurement of that setup, not of the skill. `project` stays because a case's own .claude/settings.json is part of what is
    under test. `--strict-mcp-config` with no --mcp-config leaves the session with no MCP
    servers, where it would otherwise inherit every connected one.

    What this does NOT isolate: $HOME is still the real one, so Bash and the file tools can
    reach outside the workspace. Confine that with ALLOWED_TOOLS, or wrap the whole call in
    a sandbox (anthropic-experimental/sandbox-runtime generates the Seatbelt profile) when a
    case needs Bash.

    The workspace name carries no case or arm, because the session can read its own cwd.

    `sandbox` wraps the whole call in srt; see sandbox_profile.
    """
    cmd = [
        "claude", "-p", case.PROMPT,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--allowed-tools", *case.ALLOWED_TOOLS,
        "--add-dir", str(ws),
        "--setting-sources", "project",
        "--strict-mcp-config",
    ]
    if arm == "with":
        cmd += ["--plugin-dir", str(ROOT)]
    if sandbox:
        # srt warns and stalls 3s when stdin stays open with nothing on it, hence DEVNULL
        # below.
        cmd = [SRT, "--settings", str(sandbox_profile(ws)), "--", *cmd]
    started = time.time()
    try:
        out = subprocess.run(cmd, cwd=ws, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL,
                             timeout=getattr(case, "TIMEOUT_S", 1800))
    except subprocess.TimeoutExpired:
        return "", 0.0, time.time() - started, f"timed out after {getattr(case, 'TIMEOUT_S', 1800)}s"
    elapsed = time.time() - started
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return "", 0.0, elapsed, f"could not parse CLI output: {(out.stdout or out.stderr)[-500:]}"
    return payload.get("result", ""), float(payload.get("total_cost_usd") or 0.0), elapsed, None


def score(checks: list[Check]) -> float:
    total = sum(c.weight for c in checks)
    return (sum(c.weight for c in checks if c.ok) / total) if total else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", help="case dir name; repeatable (default: all)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--arms", default="with,without", help="with,without")
    ap.add_argument("--keep", action="store_true", help="keep workspaces for inspection")
    ap.add_argument("--out", default=None, help="write the full result JSON here")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="skip srt. The session then reaches the whole home and the network")
    args = ap.parse_args()

    sandbox = not args.no_sandbox
    if sandbox and not SRT:
        sys.exit("srt not found. Install it, or pass --no-sandbox and accept an unconfined "
                 "session:\n  npm install -g @anthropic-ai/sandbox-runtime")
    print(f"sandbox: {'srt' if sandbox else 'OFF — session is unconfined'}")

    if not CHECKER.exists():
        sys.exit(f"checker venv missing. Create it:\n"
                 f"  uv venv --python 3.13 evals2/.venv\n"
                 f"  uv pip install --python evals2/.venv/bin/python -e {KIT}")

    names = args.case or sorted(p.name for p in CASES.iterdir() if (p / "case.py").exists())
    arms = [a for a in args.arms.split(",") if a]
    results: list[Run] = []

    for name in names:
        case = load_case(name)
        for arm in arms:
            for i in range(args.runs):
                # No case or arm in the path: the session can read its own cwd.
                ws = Path(tempfile.mkdtemp(prefix="ws-"))
                install_kit(ws)
                case.scaffold(ws)
                before = snapshot(ws)
                text, cost, secs, err = run_agent(case, ws, arm, sandbox=sandbox)
                run = Run(ws=ws, case=name, arm=arm, before=before, after=snapshot(ws),
                          final_text=text, cost_usd=cost, seconds=secs, error=err)
                run.checks = [] if err else case.checks(run)
                results.append(run)
                mark = f"ERROR {err}" if err else f"score {score(run.checks):.2f}"
                print(f"  {name} [{arm}] run {i+1}/{args.runs}: {mark}  ${cost:.2f}  {secs:.0f}s")
                for c in run.checks:
                    print(f"    {'PASS' if c.ok else 'FAIL'} w{c.weight} {c.name}"
                          f"{'' if c.ok else ': ' + c.detail[:300]}")
                if not args.keep:
                    shutil.rmtree(ws, ignore_errors=True)
                else:
                    print(f"    kept {ws}")

    print()
    print(f"{'CASE':<22} {'ARM':<9} {'SCORE':>6} {'RUNS':>5} {'COST':>8}")
    summary = {}
    for name in names:
        for arm in arms:
            rows = [r for r in results if r.case == name and r.arm == arm]
            if not rows:
                continue
            scores = [score(r.checks) for r in rows]
            cost = sum(r.cost_usd for r in rows)
            summary[f"{name}/{arm}"] = {"scores": scores, "mean": statistics.fmean(scores),
                                        "cost_usd": cost}
            print(f"{name:<22} {arm:<9} {statistics.fmean(scores):>6.2f} {len(rows):>5} "
                  f"${cost:>7.2f}   {' '.join(f'{s:.2f}' for s in scores)}")
    if len(arms) == 2:
        for name in names:
            a, b = summary.get(f"{name}/with"), summary.get(f"{name}/without")
            if a and b:
                print(f"\n{name}: with {a['mean']:.2f}  without {b['mean']:.2f}  "
                      f"Δ {a['mean'] - b['mean']:+.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"summary": summary,
             # The three diffs are properties, so asdict() drops them — and they are the
             # reason this runner exists, so they go in the artifact explicitly. before/after
             # stay as the raw hashes behind them.
             "runs": [{**{k: v for k, v in asdict(r).items() if k != "ws"},
                       "ws": str(r.ws),
                       "deleted": r.deleted(), "modified": r.modified(), "added": r.added()}
                      for r in results]},
            ensure_ascii=False, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
