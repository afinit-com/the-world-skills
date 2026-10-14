"""docs/verify.md's two pieces of code: the fake listener and the per-harness plan.

Issue #2: past the dry run there was no fixed way to check a harness, so every harness
hand-built the same fake listener to test its thread-bound tools, and a sub-persona
posting under the main app's name (no `chat:write.customize`) was found only by reading a
real thread. These pin the helper's contract and what the plan has to name.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from world import Agent, Harness, MentionTrigger, Persona, PollTrigger, SlackConfig, ToolSpec
from world.toolkit import result
from world.tool_check import FakeListener, bind, call, text
from world.verify_plan import verify_plan

KIT = Path(__file__).resolve().parents[1]
ALLOWED_HOST = "github.com"


def comment_factory(writes: list):
    """The shape issue #2 describes: a thread-bound write tool whose own code refuses
    another host. `writes` stands in for the outward call."""

    async def factory(listener, thread_ts):
        async def handler(args):
            if not args["url"].startswith(f"https://{ALLOWED_HOST}/"):
                return result(1, "refused: only github.com")
            writes.append((thread_ts, args["url"]))
            await listener.post(f"commented on {args['url']}", thread_ts=thread_ts)
            listener.state.setdefault("comments", []).append(args["url"])
            listener.save_state()
            return result(0, "commented")

        return [ToolSpec("pr_comment", "Comment on a PR", {"url": str}, handler)]

    return factory


class FakeListenerTests(unittest.TestCase):
    def test_factory_is_bound_to_the_thread_and_refusal_writes_nothing(self) -> None:
        writes: list = []
        listener = FakeListener()
        tools = asyncio.run(bind(comment_factory(writes), listener, "1700000000.000100"))

        refused = asyncio.run(call(tools, "pr_comment", {"url": "https://evil.example/pull/1"}))
        self.assertTrue(refused["is_error"])
        self.assertIn("only github.com", text(refused))
        self.assertEqual(([], [], 0), (writes, listener.posted, listener.saves))

        done = asyncio.run(call(tools, "pr_comment", {"url": "https://github.com/a/b/pull/1"}))
        self.assertFalse(done["is_error"])
        self.assertEqual([("1700000000.000100", "https://github.com/a/b/pull/1")], writes)
        self.assertEqual("1700000000.000100", listener.posted[0]["thread_ts"])
        self.assertEqual(1, listener.saves)

    def test_a_list_is_returned_as_is_and_a_missing_tool_is_an_error(self) -> None:
        async def handler(_args):
            return {"content": [{"type": "text", "text": "x"}], "is_error": False}

        spec = ToolSpec("read", "Read", {}, handler)
        tools = asyncio.run(bind([spec], FakeListener(), "1.1"))
        self.assertEqual([spec], tools)
        self.assertEqual("x", text(asyncio.run(call(tools, "read", {}))))
        with self.assertRaises(LookupError) as caught:
            asyncio.run(call(tools, "write", {}))
        self.assertIn("read", str(caught.exception))

    def test_a_failing_factory_fails_the_check_instead_of_yielding_no_tools(self) -> None:
        # The listener turns a raising factory into "no tools this turn". A check that did
        # the same would report every boundary as holding, having tested none.
        async def broken(_listener, _thread_ts):
            raise RuntimeError("no worktree")

        with self.assertRaises(RuntimeError):
            asyncio.run(bind(broken, FakeListener(), "1.1"))

    def test_thread_worktree_reads_the_reverse_index_like_the_real_listener(self) -> None:
        listener = FakeListener(state={"worktrees": {"/tmp/wt-a": {"thread_ts": "1.1"}}})
        self.assertEqual(Path("/tmp/wt-a"), listener.thread_worktree("1.1"))
        self.assertIsNone(listener.thread_worktree("2.2"))

    def test_importing_it_does_not_need_the_slack_sdk(self) -> None:
        code = ("import sys, world.tool_check, world.verify_plan; "
                "sys.exit('slack_sdk' in sys.modules)")
        done = subprocess.run([sys.executable, "-c", code], cwd=KIT, capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr)


class VerifyPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-secret-value"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("SLACK_CHANNEL", "APPROVER_SLACK_ID"):
            os.environ.pop(name, None)

        def must_not_run(*_args):
            raise AssertionError("the plan must not call a tool factory or a check")

        self.main = Persona("guide", "Guide")
        self.reviewer = Persona("reviewer", "Reviewer", mention_tools=must_not_run)
        self.harness = Harness("Crew", home=Path(tmp.name), provider="codex",
                               main_persona=self.main, slack=SlackConfig())
        self.harness.register(Agent(
            "reviewer", MentionTrigger(), persona=self.reviewer, writes_report=True,
            write_scope=("tasks/{task}/work",),
        ))
        self.harness.register(Agent(
            "watch", PollTrigger(must_not_run), write_scope=("tasks/{task}/work",),
        ))

    def test_plan_names_this_harness_cast_and_what_cannot_run_yet(self) -> None:
        plan = verify_plan(self.harness)
        for step in range(1, 7):
            self.assertIn(f"## {step}. ", plan)
        # The live miss: a sub-persona on the main app without chat:write.customize.
        self.assertIn("Reviewer (`reviewer`) posts under its own name", plan)
        self.assertIn("chat:write.customize", plan)
        self.assertIn("built per thread by a factory", plan)
        self.assertIn("Reviewer: the proposal it wrote matches the approval card", plan)
        self.assertIn("`reviewer`: a report file under `tasks/<task>/work/`", plan)
        self.assertNotIn("`watch`: a report file", plan)
        self.assertIn("`SLACK_CHANNEL` is not set", plan)
        self.assertIn("cannot run yet", plan)
        self.assertIn("never click approve or deny yourself", plan)
        self.assertIn("not confirmed", plan)
        self.assertNotIn("xoxb-secret-value", plan)

    def test_a_set_channel_is_shown_and_an_own_app_needs_no_customize_scope(self) -> None:
        self.reviewer.bot_token_env = "REVIEWER_BOT_TOKEN"
        with mock.patch.dict(os.environ, {"SLACK_CHANNEL": "C0123", "APPROVER_SLACK_ID": "U9"}):
            plan = verify_plan(self.harness)
        self.assertIn("`C0123` (`SLACK_CHANNEL`)", plan)
        self.assertIn("`U9` (`APPROVER_SLACK_ID`)", plan)
        self.assertNotIn("cannot run yet", plan)
        self.assertIn("its own app (`REVIEWER_BOT_TOKEN`)", plan)
        self.assertNotIn("chat:write.customize", plan)

    def test_a_channel_less_harness_says_the_thread_step_does_not_apply(self) -> None:
        with mock.patch("world.verify_plan.CHANNEL", "none"):
            plan = verify_plan(self.harness)
        self.assertIn("step 4 does not apply", plan)
        self.assertNotIn("approval card", plan)

    def test_harness_main_prints_the_plan_and_starts_nothing(self) -> None:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["harness.py", "--verify-plan"]), \
                mock.patch.object(self.harness, "_main_async") as started, \
                contextlib.redirect_stdout(out):
            self.harness.main()
        started.assert_not_called()
        self.assertIn("# Verification plan: Crew", out.getvalue())
        self.assertFalse(self.harness.state_path.exists())


class VerifyDocTests(unittest.TestCase):
    def test_doc_and_legacy_wrapper_carry_the_browser_rules(self) -> None:
        doc = " ".join((KIT / "docs/verify.md").read_text().split())
        for line in ("Never press an approve or deny button",
                     "Send a new message only when the person you are working with allowed it",
                     "only a request that leaves nothing outside Slack",
                     "uv run python harness.py --verify-plan", "world.tool_check"):
            self.assertIn(line, doc)
        legacy = " ".join((KIT / ".claude/the-world/verify.md").read_text().split())
        self.assertIn("docs/verify.md", legacy)
        self.assertIn("never press an approve or deny button", legacy)
        self.assertNotIn("no longer ships", legacy)


if __name__ == "__main__":
    unittest.main()
