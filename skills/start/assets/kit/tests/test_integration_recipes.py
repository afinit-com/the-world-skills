"""docs/integrations.md's recipes, run as written.

Each recipe is a ```python block that starts with `# recipe: <name>`. The test executes
that block from the doc itself, so the doc cannot drift from code that works. Every
network and subprocess call is faked; nothing here reaches GitHub, Linear, or Jira.
"""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from world.toolkit import make_approval_gated
from world.tools.spec import ToolSpec
from world.types import Signal

DOC = Path(__file__).resolve().parents[1] / "docs" / "integrations.md"


def load_recipe(name: str) -> dict:
    blocks = re.findall(r"```python\n(.*?)```", DOC.read_text(), re.S)
    found = [b for b in blocks if b.startswith(f"# recipe: {name}\n")]
    assert len(found) == 1, f"expected one '# recipe: {name}' block, found {len(found)}"
    namespace: dict = {"__name__": f"recipe_{name}"}
    exec(compile(found[0], f"{DOC}#{name}", "exec"), namespace)  # noqa: S102 — the doc's own code
    return namespace


class Grant:
    """One approval in one thread: is_approved/consume as the harness binds them."""

    def __init__(self, granted: bool) -> None:
        self.granted = granted
        self.consumed = 0

    def consume(self) -> None:
        self.consumed += 1
        self.granted = False

    def gate(self, spec: ToolSpec) -> ToolSpec:
        return make_approval_gated(spec, is_approved=lambda: self.granted, consume=self.consume)


def tools_in(namespace: dict) -> list[str]:
    return [v.name for v in namespace.values() if isinstance(v, ToolSpec)]


class RecipeShapeTests(unittest.TestCase):
    def test_each_recipe_has_exactly_one_tool_and_no_dangerous_sibling(self) -> None:
        for name, expected in (("linear", "linear_comment"), ("github", "github_pr_comment"),
                               ("jira", "jira_transition")):
            with self.subTest(name):
                namespace = load_recipe(name)
                self.assertEqual([expected], tools_in(namespace))
                code = re.search(rf"# recipe: {name}\n(.*?)```", DOC.read_text(), re.S).group(1)
                for verb in ("merge", "delete", "shell=True", "os.system"):
                    self.assertNotIn(verb, code)


class LinearRecipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.r = load_recipe("linear")
        self.calls: list[tuple[str, dict]] = []
        self.responses: list = []

        def fake(query: str, variables: dict) -> dict:
            self.calls.append((query, variables))
            reply = self.responses.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        self.r["_linear"] = fake

    def issues(self, *identifiers: str) -> dict:
        return {"issues": {"nodes": [
            {"id": f"uuid-{i}", "identifier": i, "title": f"title {i}", "url": f"https://l/{i}"}
            for i in identifiers
        ]}}

    async def test_check_signals_new_issue_then_waits_and_skips_handled(self) -> None:
        state: dict = {"agents": {}}
        self.responses = [self.issues("ENG-1", "ENG-2")]
        signal = await self.r["linear_check"](state, 1000.0)
        self.assertIsInstance(signal, Signal)
        self.assertIn("ENG-1", signal.prompt)
        self.assertEqual({"team": "ENG", "label": "harness"}, self.calls[0][1])

        self.assertIsNone(await self.r["linear_check"](state, 1001.0))  # cooldown, no call
        self.assertEqual(1, len(self.calls))

        signal.on_done(None)
        signal.on_done(None)  # idempotent
        self.responses = [self.issues("ENG-1", "ENG-2")]
        nxt = await self.r["linear_check"](state, 1000.0 + self.r["LINEAR_POLL_EVERY"])
        self.assertIn("ENG-2", nxt.prompt)
        self.assertEqual(["ENG-1"], state["agents"]["linear-intake"]["seen"])

    async def test_check_failure_is_recorded_not_raised(self) -> None:
        state: dict = {"agents": {}}
        self.responses = [OSError("connection refused")]
        self.assertIsNone(await self.r["linear_check"](state, 1.0))
        self.assertIn("connection refused", state["agents"]["linear-intake"]["last_error"])

    async def test_comment_refuses_without_approval_and_runs_once_with_it(self) -> None:
        grant = Grant(False)
        gated = grant.gate(self.r["linear_comment"])
        blocked = await gated.handler({"issue": "ENG-1", "body": "hi"})
        self.assertTrue(blocked["is_error"])
        self.assertIn("BLOCKED", blocked["content"][0]["text"])
        self.assertEqual([], self.calls)

        grant.granted = True
        self.responses = [{"issue": {"id": "uuid-1", "team": {"key": "ENG"}}},
                          {"commentCreate": {"success": True}}]
        done = await gated.handler({"issue": "ENG-1", "body": "hi"})
        self.assertFalse(done["is_error"])
        self.assertEqual({"id": "uuid-1", "body": "hi"}, self.calls[1][1])
        self.assertEqual(1, grant.consumed)

        again = await gated.handler({"issue": "ENG-1", "body": "hi again"})
        self.assertTrue(again["is_error"])
        self.assertEqual(2, len(self.calls))

    async def test_comment_refuses_another_team(self) -> None:
        self.responses = [{"issue": {"id": "uuid-9", "team": {"key": "SEC"}}}]
        response = await self.r["linear_comment"].handler({"issue": "SEC-9", "body": "hi"})
        self.assertTrue(response["is_error"])
        self.assertEqual(1, len(self.calls))  # looked up, never written


class GithubRecipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.r = load_recipe("github")
        self.calls: list[tuple[str, str]] = []
        self.responses: list = []

        def fake(path: str, etag: str):
            self.calls.append((path, etag))
            return self.responses.pop(0)

        self.r["_github_get"] = fake

    @staticmethod
    def pr(number: int, sha: str, reviewers: list[str]) -> dict:
        return {"number": number, "title": f"pr {number}", "head": {"sha": sha},
                "html_url": f"https://github.com/acme/api/pull/{number}",
                "requested_reviewers": [{"login": r} for r in reviewers]}

    async def test_check_picks_requested_reviews_and_reuses_list_on_304(self) -> None:
        state: dict = {"agents": {}}
        every = self.r["GITHUB_POLL_EVERY"]
        self.responses = [(200, '"e1"', [self.pr(7, "a" * 40, ["someone"]),
                                         self.pr(8, "b" * 40, ["acme-review-bot"])])]
        signal = await self.r["github_check"](state, 0.0)
        self.assertIn("acme/api#8", signal.prompt)
        self.assertEqual(("/repos/acme/api/pulls?state=open&per_page=50", ""), self.calls[0])

        self.responses = [(304, '"e1"', None)]
        again = await self.r["github_check"](state, every)
        self.assertIn("#8", again.prompt)       # unchanged list, item not yet handled
        self.assertEqual('"e1"', self.calls[1][1])

        again.on_done(None)
        self.responses = [(304, '"e1"', None)]
        self.assertIsNone(await self.r["github_check"](state, 2 * every))

        self.responses = [(200, '"e2"', [self.pr(8, "c" * 40, ["acme-review-bot"])])]
        pushed = await self.r["github_check"](state, 3 * every)
        self.assertIn("cccccccccccc", pushed.prompt)  # a new push asks for a new review

    async def test_comment_refuses_without_approval_and_runs_once_with_it(self) -> None:
        grant = Grant(False)
        gated = grant.gate(self.r["github_pr_comment"])
        spawned: list = []

        class FakeProcess:
            returncode = 0

            async def communicate(self, data: bytes):
                spawned[-1]["stdin"] = data
                return b"https://github.com/acme/api/pull/8#issuecomment-1\n", None

        async def fake_exec(*argv, **kwargs):
            spawned.append({"argv": argv, "env": kwargs["env"]})
            return FakeProcess()

        env = {"MYHARNESS_GITHUB_TOKEN": "ghp_fake", "PATH": "/bin", "GH_TOKEN": "user-wide"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
            blocked = await gated.handler({"number": 8, "body": "LGTM; $(rm -rf ~)"})
            self.assertTrue(blocked["is_error"])
            self.assertEqual([], spawned)

            grant.granted = True
            done = await gated.handler({"number": 8, "body": "LGTM; $(rm -rf ~)"})
            self.assertFalse(done["is_error"])
            again = await gated.handler({"number": 8, "body": "twice"})
            self.assertTrue(again["is_error"])

        self.assertEqual(1, len(spawned))
        self.assertEqual(("gh", "pr", "comment", "8", "--repo", "acme/api", "--body-file", "-"),
                         spawned[0]["argv"])
        self.assertEqual(b"LGTM; $(rm -rf ~)", spawned[0]["stdin"])  # stdin, not argv
        self.assertEqual("ghp_fake", spawned[0]["env"]["GH_TOKEN"])
        self.assertEqual(1, grant.consumed)

    async def test_comment_rejects_a_non_number(self) -> None:
        for bad in ("8 --repo other/repo", True, -1):
            response = await self.r["github_pr_comment"].handler({"number": bad, "body": "x"})
            self.assertTrue(response["is_error"])


class JiraRecipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.r = load_recipe("jira")
        self.calls: list[tuple] = []
        self.responses: list = []

        def fake(method: str, path: str, body: dict | None = None):
            self.calls.append((method, path, body))
            return self.responses.pop(0)

        self.r["_jira"] = fake

    async def test_check_runs_bound_jql_and_signals_new_issue(self) -> None:
        state: dict = {"agents": {}}
        self.responses = [{"issues": [{"key": "OPS-4", "fields": {"summary": "disk full"}}]}]
        signal = await self.r["jira_check"](state, 0.0)
        self.assertIn("OPS-4", signal.prompt)
        method, path, body = self.calls[0]
        self.assertEqual(("POST", "/rest/api/3/search/jql"), (method, path))
        self.assertTrue(body["jql"].startswith("project = OPS AND"))
        signal.on_done(None)
        self.responses = [{"issues": [{"key": "OPS-4", "fields": {"summary": "disk full"}}]}]
        self.assertIsNone(await self.r["jira_check"](state, self.r["JIRA_POLL_EVERY"]))

    async def test_transition_refuses_without_approval_and_runs_once_with_it(self) -> None:
        grant = Grant(False)
        gated = grant.gate(self.r["jira_transition"])
        args = {"issue": "OPS-4", "status": "In Review"}
        self.assertTrue((await gated.handler(args))["is_error"])
        self.assertEqual([], self.calls)

        grant.granted = True
        self.responses = [
            {"key": "OPS-4", "fields": {"project": {"key": "OPS"}}},
            {"transitions": [{"id": "31", "to": {"name": "Done"}},
                             {"id": "21", "to": {"name": "In Review"}}]},
            None,
        ]
        done = await gated.handler(args)
        self.assertFalse(done["is_error"])
        self.assertEqual(("POST", "/rest/api/3/issue/OPS-4/transitions",
                          {"transition": {"id": "21"}}), self.calls[-1])
        self.assertEqual(1, grant.consumed)
        self.assertTrue((await gated.handler(args))["is_error"])
        self.assertEqual(3, len(self.calls))

    async def test_transition_refuses_status_off_the_allowlist_and_other_projects(self) -> None:
        done = await self.r["jira_transition"].handler({"issue": "OPS-4", "status": "Done"})
        self.assertTrue(done["is_error"])
        self.assertEqual([], self.calls)

        self.responses = [{"key": "SEC-2", "fields": {"project": {"key": "SEC"}}}]
        moved = await self.r["jira_transition"].handler({"issue": "OPS-9", "status": "In Review"})
        self.assertTrue(moved["is_error"])
        self.assertEqual(1, len(self.calls))  # looked up, never transitioned

    async def test_transition_key_cannot_escape_the_issue_path(self) -> None:
        self.responses = [{"key": "OPS-1", "fields": {"project": {"key": "SEC"}}}]
        await self.r["jira_transition"].handler({"issue": "../../project/X", "status": "In Review"})
        self.assertNotIn("/../", self.calls[0][1])


if __name__ == "__main__":
    unittest.main()
