"""dig-my-sessions against a fake machine holding all five agents' history formats.

What it must hold: only what a person typed is mined; sessions a program started never
reach the chore list; range and project flags narrow every agent alike; `find` searches
tool output but not injected context; and `--jev` sends only redacted openers with the
vocabulary their errands share, nothing without a key, and leaves the counting to code.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/dig-my-sessions"
DIG = SKILL / "dig.py"
sys.path.insert(0, str(SKILL))
import errands  # noqa: E402
import jev  # noqa: E402
import sources  # noqa: E402

NOW = datetime.now(timezone.utc)
# Built from parts so no token-shaped literal sits in the repository.
FAKE_KEY = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0"
SKILL_LISTING = "- secret-skill: zebrafinch does a thing"


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ms(days_ago: float) -> int:
    return int((NOW - timedelta(days=days_ago)).timestamp() * 1000)


def write_jsonl(path: Path, rows: list[dict], days_ago: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    t = (NOW - timedelta(days=days_ago)).timestamp()
    os.utime(path, (t, t))


def claude_session(root: Path, sid: str, cwd: str, prompt: str, days_ago: float,
                   entrypoint: str = "cli", tool_output: str = "") -> None:
    rows = [
        {"type": "system", "content": SKILL_LISTING, "timestamp": iso(days_ago)},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": SKILL_LISTING},
         "timestamp": iso(days_ago)},
        {"type": "user", "sessionId": sid, "cwd": cwd, "entrypoint": entrypoint,
         "promptSource": "sdk" if entrypoint.startswith("sdk") else "typed",
         "timestamp": iso(days_ago), "message": {"role": "user", "content": prompt}},
        {"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": iso(days_ago),
         "message": {"role": "user", "content": [
             {"type": "text", "text": "Base directory for this skill: expanded skill body"}]}},
        {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": iso(days_ago - 0.01),
         "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": iso(days_ago - 0.01),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": tool_output or "ok"}]}},
    ]
    write_jsonl(root / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl", rows, days_ago)


def codex_session(root: Path, sid: str, cwd: str, prompt: str, days_ago: float,
                  source: str = "cli") -> None:
    rows = [
        {"timestamp": iso(days_ago), "type": "session_meta",
         "payload": {"id": sid, "cwd": cwd, "source": source,
                     "originator": "codex_exec" if source == "exec" else "codex-tui",
                     "base_instructions": SKILL_LISTING}},
        {"timestamp": iso(days_ago), "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": SKILL_LISTING}]}},
        {"timestamp": iso(days_ago), "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"timestamp": iso(days_ago), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": prompt}]}},
        {"timestamp": iso(days_ago), "type": "event_msg",
         "payload": {"type": "user_message", "message": prompt}},
        {"timestamp": iso(days_ago), "type": "compacted",
         "payload": {"replacement_history": [SKILL_LISTING]}},
        {"timestamp": iso(days_ago - 0.01), "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}"}},
    ]
    day = NOW - timedelta(days=days_ago)
    write_jsonl(root / "sessions" / day.strftime("%Y/%m/%d") / f"rollout-{sid}.jsonl",
                rows, days_ago)


def opencode_db(data: Path, sessions: list[tuple[str, str, str, float]]) -> None:
    db = data / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("""
        create table session (id text primary key, project_id text, parent_id text,
            directory text, time_created integer, time_updated integer);
        create table message (id text primary key, session_id text, time_created integer,
            data text);
        create table part (id text primary key, message_id text, session_id text,
            time_created integer, data text);
    """)
    for sid, cwd, prompt, days_ago in sessions:
        t = ms(days_ago)
        con.execute("insert into session values (?,?,?,?,?,?)", (sid, "p", None, cwd, t, t))
        con.execute("insert into message values (?,?,?,?)",
                    (f"{sid}u", sid, t, json.dumps({"role": "user"})))
        con.execute("insert into message values (?,?,?,?)",
                    (f"{sid}a", sid, t + 1, json.dumps({"role": "assistant"})))
        con.execute("insert into part values (?,?,?,?,?)",
                    (f"{sid}p1", f"{sid}u", sid, t, json.dumps({"type": "text", "text": prompt})))
        con.execute("insert into part values (?,?,?,?,?)",
                    (f"{sid}p2", f"{sid}u", sid, t, json.dumps(
                        {"type": "text", "text": "injected reminder", "synthetic": True})))
        con.execute("insert into part values (?,?,?,?,?)",
                    (f"{sid}p3", f"{sid}a", sid, t + 1, json.dumps({"type": "tool", "tool": "bash"})))
    # a subagent's session is a sidechain, not an errand
    con.execute("insert into session values (?,?,?,?,?,?)",
                ("child", "p", sessions[0][0], sessions[0][1], ms(1), ms(1)))
    con.commit()
    con.close()


def cursor_db(user_dir: Path, cid: str, cwd: str, prompt: str, days_ago: float) -> None:
    db = user_dir / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("create table cursorDiskKV (key text primary key, value blob)")
    con.execute("insert into cursorDiskKV values (?,?)", (f"composerData:{cid}", json.dumps({
        "composerId": cid, "createdAt": ms(days_ago), "lastUpdatedAt": ms(days_ago),
        "workspaceIdentifier": {"uri": {"fsPath": cwd}},
        "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1},
                                        {"bubbleId": "b2", "type": 2}]})))
    con.execute("insert into cursorDiskKV values (?,?)", (f"bubbleId:{cid}:b1", json.dumps(
        {"type": 1, "text": prompt, "createdAt": iso(days_ago)})))
    con.execute("insert into cursorDiskKV values (?,?)", (f"bubbleId:{cid}:b2", json.dumps(
        {"type": 2, "text": "", "toolFormerData": {"name": "read_file_v2"}})))
    con.commit()
    con.close()


def gemini_session(home: Path, cwd: str, prompt: str, days_ago: float) -> None:
    (home / "projects.json").parent.mkdir(parents=True, exist_ok=True)
    (home / "projects.json").write_text(json.dumps({"projects": {cwd: "gproj"}}))
    rows = [
        {"sessionId": "g1", "startTime": iso(days_ago), "lastUpdated": iso(days_ago)},
        {"$set": {"messages": [{"id": "m0", "timestamp": iso(days_ago), "type": "user",
                                "content": [{"text": "<session_context>dirs</session_context>"}]}]}},
        {"id": "m1", "timestamp": iso(days_ago), "type": "user", "content": [{"text": prompt}]},
    ]
    write_jsonl(home / "tmp" / "gproj" / "chats" / "session-g1.jsonl", rows, days_ago)


def pb(*fields: tuple[int, bytes | str]) -> bytes:
    """A protobuf message of length-delimited fields, the only wire type agy's text uses."""
    out = b""
    for num, value in fields:
        raw = value.encode() if isinstance(value, str) else value
        out += bytes([num << 3 | 2]) + bytes([len(raw)]) + raw  # every value here < 128 bytes
    return out


def agy_cli(root: Path, cwd: str, days_ago: float) -> None:
    (root / "conversations").mkdir(parents=True)
    con = sqlite3.connect(root / "conversation_summaries.db")
    con.execute("create table conversation_summaries (conversation_id text, title text,"
                " workspace_uris text, nesting_depth integer, last_user_input_time datetime,"
                " last_modified_time datetime)")
    for cid, depth, ago in (("a1", 0, days_ago), ("a2", 0, days_ago - 0.5), ("sub", 1, days_ago)):
        when = (NOW - timedelta(days=ago)).isoformat(sep=" ")
        con.execute("insert into conversation_summaries values (?,?,?,?,?,?)",
                    (cid, "release notes", json.dumps([f"file://{cwd}"]), depth, when, when))
    con.commit()
    con.close()
    steps = sqlite3.connect(root / "conversations" / "a1.db")
    steps.execute("create table steps (idx integer primary key, step_payload blob)")
    steps.execute("insert into steps values (0, ?)", (pb((1, "a1"), (2, pb((1, "릴리즈 노트 써줘")))),))
    steps.execute("insert into steps values (1, ?)", (pb(
        (1, "call_42"), (2, "run_command"), (3, pb((1, '{"CommandLine":"git tag v9.9.9-rc"}')))),))
    steps.commit()
    steps.close()
    t = int((NOW - timedelta(days=days_ago)).timestamp() * 1000)
    t2 = int((NOW - timedelta(days=days_ago - 0.5)).timestamp() * 1000) - 5000
    write_jsonl(root / "history.jsonl", [
        {"display": "/model", "timestamp": t, "workspace": cwd, "type": "slash_command"},
        {"display": "지난 릴리즈 이후 변경 모아서 릴리즈 노트 초안 써줘", "timestamp": t,
         "workspace": cwd, "conversationId": "a1"},
        # an older entry: no conversation id, matched by workspace and time
        {"display": "이 레포 CI가 왜 윈도우에서만 깨지는지 봐줘 로그 포함", "timestamp": t2,
         "workspace": cwd},
    ], days_ago)


def claude_work(root: Path, sid: str, cwd: str, turns: list[tuple[float, str, list]]) -> None:
    """A Claude session of several typed turns, each followed by its tool calls."""
    rows = []
    for days_ago, prompt, calls in turns:
        rows.append({"type": "user", "sessionId": sid, "cwd": cwd, "promptSource": "typed",
                     "timestamp": iso(days_ago), "message": {"role": "user", "content": prompt}})
        rows.extend({"type": "assistant", "sessionId": sid, "cwd": cwd,
                     "timestamp": iso(days_ago - 0.001),
                     "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
                    for name, inp in calls)
    write_jsonl(root / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl", rows,
                min(t[0] for t in turns))


SYNC = [("Bash", {"command": "cd ~/code/shop && git fetch origin && gh pr list --state open"}),
        ("Bash", {"command": "tracker sync --team core"}),
        ("Bash", {"command": "tracker sync --mine | head"})]


DEPLOY = "스테이징 배포 상태 확인하고 슬랙에 결과 요약해서 올려줘 deploy check"


def build_machine(base: Path) -> dict[str, str]:
    home = base / "home"
    proj = str(home / "code" / "shop")
    work = str(home / "code" / "acme-work")
    claude = base / "claude"
    claude_session(claude, "c1", proj, DEPLOY + " 오늘도", 3, tool_output="branch feat/PAY-881 pushed")
    claude_session(claude, "c2", proj, DEPLOY + " 이번 주도", 10)
    claude_session(claude, "c3", work, "acme 대시보드 숫자 왜 틀렸는지 디버깅해줘 정말로", 4)
    claude_session(claude, "c4", proj, "Generate a short git branch name for this task please", 2,
                   entrypoint="sdk-py")
    claude_session(claude, "c5", "/tmp/fixture-run", "fixture가 잘 도는지 이 테스트 실행해봐줘", 2)
    claude_session(claude, "c6", proj, "오래된 일: 결제 모듈 리팩터링 계획 세워줘 자세히", 200)
    codex = base / "codex"
    codex_session(codex, "x1", proj, DEPLOY + " codex로", 5)
    codex_session(codex, "x2", proj, "You are a dispatched worker. Task: rename the config key.", 5)
    codex_session(codex, "x3", proj, "scheduled exec run summarise yesterday's logs please", 6,
                  source="exec")
    data = base / "data"
    opencode_db(data, [("o1", proj, "빌드 큐에 작업이 몇 개 남았는지 확인해줘 새로 돌리지는 말고", 7)])
    cursor_db(base / "cursor", "k1", proj, "환경변수 API_BASE 어디서 설정해놨는지 찾아줘 .env인가", 8)
    agy_cli(base / "gemini" / "antigravity-cli", proj, 6)
    gemini_session(base / "gemini", proj, "이 레포 구조 설명해줘 처음 보는 사람 기준으로", 9)
    env = dict(os.environ)
    env.update({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex),
                "XDG_DATA_HOME": str(data), "CURSOR_USER_DIR": str(base / "cursor"),
                "GEMINI_DIR": str(base / "gemini"), "PYTHONDONTWRITEBYTECODE": "1"})
    env.pop("TYPESAFE_API_KEY", None)
    env.pop("TYPESAFE_BASE_URL", None)
    return env


def dig(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DIG), *args], env=env, capture_output=True,
                          encoding="utf-8")


def chores(env: dict, *args: str) -> dict:
    with tempfile.TemporaryDirectory() as out:
        target = Path(out) / "out.json"
        r = dig(env, "chores", *args, "--json", str(target))
        if r.returncode:
            raise AssertionError(r.stderr)
        return json.loads(target.read_text())


class ChoresTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env = build_machine(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_every_agent_and_only_what_a_person_typed(self) -> None:
        result = chores(self.env)
        self.assertEqual(result["scanned"]["by_agent"],
                         {"claude": 5, "codex": 3, "opencode": 1, "cursor": 1, "agy": 2,
                          "gemini": 1})
        openers = {r["agent"]: r for r in result["sessions"]}
        self.assertEqual(set(openers), {"claude", "codex", "opencode", "cursor", "agy", "gemini"})
        dumped = json.dumps(result, ensure_ascii=False)
        for injected in ("zebrafinch", "environment_context", "expanded skill body",
                         "injected reminder", "session_context"):
            self.assertNotIn(injected, dumped)
        self.assertEqual(openers["cursor"]["tools"], ["read_file_v2"])
        agy = sorted(r["opener"][:6] for r in result["sessions"] if r["agent"] == "agy")
        self.assertEqual(agy, ["이 레포 C", "지난 릴리즈"])
        self.assertNotIn("/model", dumped)
        self.assertEqual(openers["codex"]["project"], "~/code/shop")

    def test_sessions_a_program_started_never_reach_the_chore_list(self) -> None:
        result = chores(self.env)
        self.assertEqual(result["scanned"]["automated_dropped"], 4)
        dumped = json.dumps(result["sessions"], ensure_ascii=False)
        for automated in ("branch name", "dispatched worker", "scheduled exec", "fixture가"):
            self.assertNotIn(automated, dumped)
        kept = chores(self.env, "--include-automated")
        self.assertIn("dispatched worker", json.dumps(kept["sessions"], ensure_ascii=False))

    def test_range_agents_and_projects_narrow_every_agent_alike(self) -> None:
        self.assertNotIn("결제 모듈", json.dumps(chores(self.env), ensure_ascii=False))
        old = (NOW - timedelta(days=201)).date().isoformat()
        cut = (NOW - timedelta(days=150)).date().isoformat()
        only_old = chores(self.env, "--since", old, "--until", cut)
        self.assertEqual([r["opener"][:9] for r in only_old["sessions"]], ["오래된 일: 결제"])
        codex_only = chores(self.env, "--agent", "codex")
        self.assertEqual(set(codex_only["scanned"]["by_agent"]), {"codex"})
        no_work = chores(self.env, "--exclude-project", "acme")
        self.assertNotIn("acme", json.dumps(no_work["sessions"]))
        only_work = chores(self.env, "--project", "acme", "--agent", "claude,cursor")
        self.assertEqual([r["project"] for r in only_work["sessions"]], ["~/code/acme-work"])
        bad = dig(self.env, "chores", "--agent", "copilot")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("unknown agent copilot", bad.stderr)

    def test_find_searches_tool_output_not_injected_context(self) -> None:
        r = dig(self.env, "find", "PAY-881")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude --resume c1", r.stdout)
        self.assertEqual(dig(self.env, "find", "zebrafinch").returncode, 1)
        r = dig(self.env, "find", "deploy", "check", "--agent", "codex")
        self.assertIn("codex resume x1", r.stdout)
        self.assertNotIn("claude", r.stdout)
        r = dig(self.env, "find", "API_BASE")
        self.assertIn("cursor", r.stdout)
        # agy: the tool call inside a protobuf step blob, with its resume command
        r = dig(self.env, "find", "v9.9.9-rc")
        self.assertIn("agy --conversation a1", r.stdout)
        self.assertNotIn("--conversation sub", dig(self.env, "find", "release notes").stdout)
        r = dig(self.env, "find", "빌드 큐")
        self.assertIn("opencode --session o1", r.stdout)

    def test_jev_sends_nothing_without_a_key_or_on_a_dry_run(self) -> None:
        dry = chores(self.env, "--jev-dry-run")
        self.assertTrue(dry["jev"]["dry_run"])
        self.assertEqual(dry["jev"]["plan"]["step1_requests"], len(dry["sessions"]))
        missing = chores(self.env, "--jev")
        self.assertIn("TYPESAFE_API_KEY is not set", missing["jev"]["error"])


class FakeJev(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeJev.requests.append({"auth": self.headers.get("Authorization"), "body": body})
        state = json.dumps(body["state"], ensure_ascii=False)
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "choice":
                kind = "recurring_chore" if "deploy" in state else "exploration"
                answers[qid] = {"type": "choice", "choice": kind, "confidence": 0.9,
                                "probabilities": {kind: 0.9}}
            else:
                other = json.dumps(q["instructions"], ensure_ascii=False)
                same = any(w in state and w in other for w in ("deploy", "tracker sync"))
                answers[qid] = {"type": "noul", "noul": 0.95 if same else 0.1}
        out = json.dumps({"model": "jev-test", "answers": answers,
                          "usage": {"input_tokens": 100, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_) -> None:
        pass


class JevTests(unittest.TestCase):
    def test_jev_groups_by_code_and_sends_only_redacted_openers(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), FakeJev)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        FakeJev.requests = []
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = build_machine(base)
            claude_session(base / "claude", "c7", str(base / "home/code/shop"),
                           f"{DEPLOY} token={FAKE_KEY} me@example.com https://x.test/a?b=c", 1)
            env.update({"TYPESAFE_API_KEY": "test-key",
                        "TYPESAFE_BASE_URL": f"http://127.0.0.1:{server.server_port}"})
            result = chores(env, "--jev")
        server.shutdown()
        j = result["jev"]
        self.assertNotIn("error", j)
        sent = json.dumps([r["body"] for r in FakeJev.requests], ensure_ascii=False)
        for secret in (FAKE_KEY, "me@example.com", "x.test", "zebrafinch", str(base)):
            self.assertNotIn(secret, sent)
        self.assertTrue(all(r["auth"] == "Bearer test-key" for r in FakeJev.requests))
        self.assertEqual({r["body"]["model"] for r in FakeJev.requests}, {jev.MODEL})
        # the four deploy openers, across two agents, are one chore counted by code
        top = j["shortlist"][0]
        self.assertEqual(top["sessions"], 4)
        self.assertEqual(top["agents"], ["claude", "codex"])
        self.assertEqual(top["days"], 4)
        self.assertEqual(j["usage"]["input_tokens"], 100 * len(FakeJev.requests))

    def test_jev_groups_errands_worded_apart_by_the_work_they_did(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), FakeJev)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        FakeJev.requests = []
        private = [("Bash", {"command": "zz-handle-9 push && curl https://private.example/x"}),
                   ("Write", {"file_path": "/home/u/notes/secret-plan.md", "content": "x"})]
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = build_machine(base)
            proj = str(base / "home/code/shop")
            # the same chore, three wordings that share no word; the middle errand sits in
            # a session left open for days, after an unrelated one
            claude_work(base / "claude", "s1", proj, [(12, "지난번 하던 거 이어서 보자 원격이랑 이슈 목록 기준으로", SYNC)])
            claude_work(base / "claude", "s2", proj, [
                (11, "결제 위젯 색상 바꿔줘 버튼만 파란색으로 하고", private),
                (8, "where are we on the open tickets and branches today", SYNC + private)])
            claude_work(base / "claude", "s3", proj, [(4, "현황 파악 부탁해 오늘 기준으로 한 번에 정리해서 알려줘", SYNC)])
            env.update({"TYPESAFE_API_KEY": "test-key",
                        "TYPESAFE_BASE_URL": f"http://127.0.0.1:{server.server_port}"})
            result = chores(env, "--jev")
        server.shutdown()
        server.server_close()
        sent = json.dumps([r["body"] for r in FakeJev.requests], ensure_ascii=False)
        self.assertIn("tracker sync", sent)
        for leaked in ("zz-handle-9", "private.example", "secret-plan", "notes/", str(base)):
            self.assertNotIn(leaked, sent)
        group = next(g for g in result["jev"]["shortlist"]
                     if any("이어서 보자" in m["opener"] for m in g["members"]))
        self.assertEqual(group["errands"], 3)
        self.assertEqual(group["sessions"], 3)
        self.assertNotIn("결제 위젯", json.dumps(group["members"], ensure_ascii=False))


class ErrandTests(unittest.TestCase):
    def test_a_session_left_open_is_cut_into_errands_where_the_person_was_away(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "claude"
            claude_work(root, "w1", "/home/u/code/shop", [
                (9, "릴리즈 노트 초안 써줘 지난 태그 이후 변경 기준으로", []),
                (9 - 0.05, "좋아 그거 PR 설명에도 넣어줘", []),
                (2, "오늘 들어온 이슈들 트래커랑 원격 브랜치 확인해서 정리해줘", SYNC),
            ])
            os.environ["CLAUDE_CONFIG_DIR"] = str(root)
            try:
                found = list(sources.claude(sources.Window(NOW - timedelta(days=30), NOW)))
            finally:
                os.environ.pop("CLAUDE_CONFIG_DIR")
            rows = errands.build(found, str)
        self.assertEqual([r["turns"] for r in rows], [2, 1])
        self.assertEqual({r["session"] for r in rows}, {"w1"})
        self.assertEqual(rows[0]["local"], {})
        self.assertEqual(rows[1]["local"]["cmd:tracker sync"], 2)
        self.assertEqual(rows[1]["local"]["cmd:git fetch"], 1)

    def test_command_heads_skip_plumbing_heredocs_and_unshareable_names(self) -> None:
        heads = errands.command_heads(
            "cd x && FOO=1 uv run pytest -q | tee log\n"
            "python3 - <<'EOF'\nimport json\nfor x in y: print(x)\nEOF\n"
            "./scripts/Screenshot_2026.png; gh api repos/a/b")
        self.assertEqual(heads, ["pytest", "gh api"])

    def test_only_vocabulary_common_to_three_errands_is_shared(self) -> None:
        rows = [{"local": {"tool:Bash": 3, "cmd:git fetch": 2, "host:private.example": 1,
                           "edits:.md": 1, "cmd:zz-handle-9": 2, "skill:release": 1}}
                for _ in range(2)]
        rows.append({"local": {"cmd:git fetch": 1, "skill:release": 1, "host:private.example": 1,
                               "edits:.md": 1}})
        errands.share(rows)
        self.assertEqual(rows[0]["shared"], {"tools": ["Bash"], "did": ["git fetch", "release"]})
        state = json.dumps([jev.state_of({"opener": "x", **r}) for r in rows])
        for private in ("private.example", ".md", "zz-handle-9"):
            self.assertNotIn(private, state)


class RedactTests(unittest.TestCase):
    def test_redact_strips_credentials_and_contact_details(self) -> None:
        text = (f"curl -H 'Authorization: Bearer {FAKE_KEY}' https://api.example.com/v1\n"
                "Cookie: sid=abc123def456\n"
                "password=hunter2 mail someone@example.org xoxb-" + "1234567890-abcdefghij "
                + "deadbeef" * 5)
        out = jev.redact(text)
        for leaked in (FAKE_KEY, "abc123def456", "hunter2", "someone@example.org", "api.example.com",
                       "xoxb-1234567890", "deadbeef"):
            self.assertNotIn(leaked, out)
        self.assertLessEqual(len(jev.redact("가" * 2000)), jev.MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
