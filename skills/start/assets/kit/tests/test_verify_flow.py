"""docs/verify.md's two pieces of code: the fake listener and the per-harness plan.

Issue #2: past the dry run there was no fixed way to check a harness, so every harness
hand-built the same fake listener to test its thread-bound tools, and a sub-persona
posting under the main app's name (no `chat:write.customize`) was found only by reading a
real thread. These pin the helper's contract and what the plan has to name.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from world import Agent, Harness, MentionTrigger, Persona, PollTrigger, SlackConfig, ToolSpec
from world.toolkit import result
from world.tool_check import FakeListener, bind, call, refuses, text
from world.verify_plan import verify_plan

KIT = Path(__file__).resolve().parents[1]
ALLOWED_HOST = "github.com"
WRITES: list = []  # what gh_write really did; a boundary check must leave it empty


def gh_write(url: str) -> None:
    """The outward call a comment tool writes through. Never stubbed means really written."""
    WRITES.append(url)


def comment_factory(check_host: bool = True):
    """The shape issue #2 describes: a thread-bound write tool whose own code refuses
    another host. `check_host=False` is the same tool with that boundary broken."""

    async def factory(listener, thread_ts):
        async def handler(args):
            if check_host and not args["url"].startswith(f"https://{ALLOWED_HOST}/"):
                return result(1, "refused: only github.com")
            gh_write(args["url"])
            await listener.post(f"commented on {args['url']}", thread_ts=thread_ts)
            listener.save_state()
            return result(0, "commented")

        return [ToolSpec("pr_comment", "Comment on a PR", {"url": str}, handler)]

    return factory


FROM_IMPORT_TOOL = f"""
from {__name__} import ToolSpec, gh_write, result


async def factory(listener, thread_ts):
    async def handler(args):
        gh_write(args["url"])  # the boundary check comes after the write: open
        return result(1, "refused")

    return [ToolSpec("pr_comment", "Comment on a PR", {{"url": str}}, handler)]
"""


def from_import_module(test: unittest.TestCase) -> types.ModuleType:
    """A tool module that took the write function with `from x import gh_write`, the usual
    way, so its handler looks up its own name for it rather than this module's."""
    module = types.ModuleType("verify_flow_from_import_tool")
    exec(FROM_IMPORT_TOOL, module.__dict__)
    sys.modules[module.__name__] = module
    test.addCleanup(sys.modules.pop, module.__name__, None)
    return module


def posting_mixin_post_params() -> list[tuple]:
    """PostingMixin.post's parameters, read from source so this runs without slack_sdk."""
    tree = ast.parse((KIT / "world/slack/post.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PostingMixin")
    fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "post")
    params = [(a.arg, "positional", None) for a in fn.args.args]
    params += [(a.arg, "keyword", ast.literal_eval(d))
               for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults)]
    return params


class FakeListenerTests(unittest.TestCase):
    def setUp(self) -> None:
        WRITES.clear()
        self.outward = [(sys.modules[__name__], "gh_write")]

    def test_a_refusal_is_checked_with_the_outward_call_stubbed(self) -> None:
        listener = FakeListener()
        tools = asyncio.run(bind(comment_factory(), listener, "1700000000.000100"))
        refused = asyncio.run(refuses(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                                      outward=self.outward))
        self.assertIn("only github.com", text(refused))
        self.assertEqual(([], [], 0), (WRITES, listener.posted, listener.saves))

    def test_an_open_boundary_fails_the_check_and_writes_nothing(self) -> None:
        # Review of #2: the first version ran the real write when a boundary was open, so
        # the check itself could comment or merge. Now it fails with the write stubbed.
        tools = asyncio.run(bind(comment_factory(check_host=False), FakeListener(), "1.1"))
        with self.assertRaises(AssertionError) as caught:
            asyncio.run(refuses(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                                outward=self.outward))
        self.assertIn("boundary is open", str(caught.exception))
        self.assertEqual([], WRITES)

    def test_a_from_imported_write_is_stubbed_where_the_tool_looks_it_up(self) -> None:
        # Review of PR #6: patching the function's home left a `from x import gh_write`
        # name pointing at the real one, so the write ran and `refuses` passed.
        module = from_import_module(self)
        for outward in (self.outward, [f"{__name__}.gh_write"]):
            tools = asyncio.run(bind(module.factory, FakeListener(), "1.1"))
            with self.assertRaises(AssertionError) as caught:
                asyncio.run(refuses(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                                    outward=outward))
            self.assertIn("boundary is open", str(caught.exception))
        self.assertEqual([], WRITES)
        self.assertIs(gh_write, module.gh_write)  # put back after the check

    def test_a_reached_stub_is_named_even_when_the_handler_then_breaks(self) -> None:
        # Review of PR #6: `rc, out = await run_cmd(...)` on a stub raises ValueError, which
        # hid that the write had been reached.
        async def handler(args):
            rc, out = gh_write(args["url"])
            return result(rc, out)

        tools = [ToolSpec("pr_comment", "Comment", {"url": str}, handler)]
        with self.assertRaises(AssertionError) as caught:
            asyncio.run(call(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                             outward=self.outward))
        self.assertIn("boundary is open", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, (TypeError, ValueError))
        self.assertEqual([], WRITES)

    def test_a_write_left_in_a_task_is_still_caught_by_the_stub(self) -> None:
        # Review of PR #6: a write handed to create_task ran after the stubs were removed.
        async def handler(args):
            async def later():
                await asyncio.sleep(0.01)
                gh_write(args["url"])

            asyncio.create_task(later())
            return result(1, "refused")

        tools = [ToolSpec("pr_comment", "Comment", {"url": str}, handler)]

        async def check():
            await refuses(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                          outward=self.outward)
            await asyncio.sleep(0.05)

        with self.assertRaises(AssertionError) as caught:
            asyncio.run(check())
        self.assertIn("boundary is open", str(caught.exception))
        self.assertEqual([], WRITES)

    def test_a_task_outliving_the_check_is_cancelled_and_fails_it(self) -> None:
        async def handler(args):
            async def forever():
                await asyncio.sleep(3600)
                gh_write(args["url"])

            asyncio.create_task(forever(), name="slow-write")
            return result(1, "refused")

        tools = [ToolSpec("pr_comment", "Comment", {"url": str}, handler)]
        with mock.patch("world.tool_check.SETTLE_SECONDS", 0.01), \
                self.assertRaises(AssertionError) as caught:
            asyncio.run(refuses(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                                outward=self.outward))
        self.assertIn("slow-write", str(caught.exception))
        self.assertEqual([], WRITES)

    def test_a_write_check_will_not_run_without_a_stub(self) -> None:
        tools = asyncio.run(bind(comment_factory(), FakeListener(), "1.1"))
        with self.assertRaises(ValueError):
            asyncio.run(refuses(tools, "pr_comment", {"url": "https://evil.example/p/1"},
                                outward=[]))
        with self.assertRaises(TypeError):  # call() has no default for outward either
            asyncio.run(call(tools, "pr_comment", {"url": "https://evil.example/p/1"}))
        self.assertEqual([], WRITES)

    def test_an_accepted_input_fails_refuses(self) -> None:
        async def handler(_args):
            return {"content": [{"type": "text", "text": "fine"}], "is_error": False}

        tools = [ToolSpec("read", "Read", {}, handler)]
        with self.assertRaises(AssertionError) as caught:
            asyncio.run(refuses(tools, "read", {}, outward=self.outward))
        self.assertIn("accepted", str(caught.exception))

    def test_a_list_is_returned_as_is_and_a_missing_tool_is_an_error(self) -> None:
        async def handler(_args):
            return {"content": [{"type": "text", "text": "x"}], "is_error": False}

        spec = ToolSpec("read", "Read", {}, handler)
        tools = asyncio.run(bind([spec], FakeListener(), "1.1"))
        self.assertEqual([spec], tools)
        self.assertEqual("x", text(asyncio.run(call(tools, "read", {}, outward=()))))
        with self.assertRaises(LookupError) as caught:
            asyncio.run(call(tools, "write", {}, outward=()))
        self.assertIn("read", str(caught.exception))

    def test_post_takes_exactly_the_real_listeners_arguments(self) -> None:
        # Review of #2: **kwargs let `thread_id=` through here and a TypeError in Slack.
        fake = [(name, "positional" if p.kind is p.POSITIONAL_OR_KEYWORD else "keyword",
                 None if p.default is p.empty else p.default)
                for name, p in inspect.signature(FakeListener.post).parameters.items()]
        self.assertEqual(posting_mixin_post_params(), fake)
        listener = FakeListener()
        with self.assertRaises(TypeError):
            asyncio.run(listener.post("hi", thread_id="1.1"))
        self.assertEqual([], listener.posted)

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
        with mock.patch.dict(os.environ, {"SLACK_CHANNEL": "C0123", "APPROVER_SLACK_ID": "U9",
                                          "REVIEWER_BOT_TOKEN": "xoxb-reviewer-secret"}):
            plan = verify_plan(self.harness)
        self.assertNotIn("xoxb-reviewer-secret", plan)
        self.assertIn("`C0123` (`SLACK_CHANNEL`)", plan)
        self.assertIn("`U9` (`APPROVER_SLACK_ID`)", plan)
        self.assertNotIn("cannot run yet", plan)
        self.assertIn("its own app (`REVIEWER_BOT_TOKEN`)", plan)
        self.assertNotIn("chat:write.customize", plan)

    def test_a_named_but_unset_token_falls_back_to_the_main_app(self) -> None:
        # Review of #2: the variable's name alone was read as "own app", but the runtime
        # (PostingMixin._face) posts through the main app whenever the token is missing.
        self.reviewer.bot_token_env = "REVIEWER_BOT_TOKEN"
        os.environ.pop("REVIEWER_BOT_TOKEN", None)
        plan = verify_plan(self.harness)
        self.assertNotIn("its own app", plan)
        self.assertIn("chat:write.customize", plan)
        self.assertIn("`REVIEWER_BOT_TOKEN` is not set, so it falls back to the main app", plan)

    def test_the_icon_line_reads_chat_icon_not_the_portrait(self) -> None:
        # Review of PR #6: the plan said "its portrait" for a local portrait file that
        # Slack never shows, and "no portrait" for a persona with only an icon_url.
        self.reviewer.portrait = "portraits/reviewer.png"
        self.assertIn("the main app's icon (no `icon_url` set)", verify_plan(self.harness))
        self.reviewer.portrait, self.reviewer.icon_url = "", "https://img.example/r.png"
        self.assertIn("Reviewer (`reviewer`) posts under its own name, its icon (`icon_url`)",
                      verify_plan(self.harness))
        self.reviewer.icon_url = ""
        self.reviewer.portrait = "https://img.example/r.png"  # before icon_url existed
        self.assertIn("its icon (the http(s) `portrait`)", verify_plan(self.harness))

    def test_an_explicit_gateway_decides_the_channel_like_the_runtime(self) -> None:
        # Review of #2: Harness.channel wins at runtime, but the plan ignored it and asked
        # a poll-only harness for a Slack thread.
        from world.channels import NullGateway

        self.harness.channel = NullGateway()
        plan = verify_plan(self.harness)
        self.assertIn("step 4 does not apply", plan)
        self.assertNotIn("SLACK_CHANNEL", plan)
        self.assertNotIn("approval card", plan)

        class HomeGrownGateway:
            enabled = True

        self.harness.channel = HomeGrownGateway()
        plan = verify_plan(self.harness)
        self.assertIn("`HomeGrownGateway`, which this plan does not know", plan)
        self.assertIn("not confirmed", plan)
        self.assertNotIn("SLACK_CHANNEL", plan)

        class SlackGateway:  # matched by name, so the plan never imports slack_sdk
            pass

        self.harness.channel = SlackGateway()
        self.assertIn("`SLACK_CHANNEL` is not set", verify_plan(self.harness))

    def test_a_non_slack_channel_gets_no_slack_scope_or_app_lines(self) -> None:
        # Review of PR #6: a Discord or home-grown gateway was told to check
        # chat:write.customize and a Slack app token for each sub-persona.
        class DiscordGateway:  # matched by name, like the Slack one
            pass

        class HomeGrownGateway:
            pass

        self.reviewer.bot_token_env = "REVIEWER_BOT_TOKEN"
        for gateway in (DiscordGateway(), HomeGrownGateway()):
            self.harness.channel = gateway
            plan = verify_plan(self.harness)
            self.assertIn("Reviewer (`reviewer`) posts under its own name, no picture of its "
                          "own (no `icon_url` set) and voice", plan)
            for slack_only in ("chat:write.customize", "REVIEWER_BOT_TOKEN", "main app"):
                self.assertNotIn(slack_only, plan)

    def test_a_co_approver_is_named_beside_the_approver(self) -> None:
        self.assertNotIn("co-approver", verify_plan(self.harness))
        self.harness.slack.co_approver_env = "CO_APPROVER_SLACK_ID"
        with mock.patch.dict(os.environ, {"CO_APPROVER_SLACK_ID": "U7"}):
            plan = verify_plan(self.harness)
        self.assertIn("co-approver", plan)
        self.assertIn("`U7` (`CO_APPROVER_SLACK_ID`)", plan)

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
    def test_slack_bot_token_checks_document_scope_and_human_approval(self):
        for rel in ("docs/verify.md", "templates/verify-wrapper.md.tmpl"):
            doc = (KIT / rel).read_text(encoding="utf-8")
            for required in ("Slack only", "bin/say-as-human.py", "SLACK_AS_HUMAN_SECRET",
                             "--prefix", "init", "send", "--thread"):
                self.assertIn(required, doc, rel)
            self.assertIn("approval", doc)
        skill = (KIT.parents[1] / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("bin/say-as-human.py", skill)

    def test_doc_and_legacy_wrapper_carry_the_browser_rules(self) -> None:
        doc = " ".join((KIT / "docs/verify.md").read_text().split())
        for line in ("Never press an approve or deny button",
                     "Send a new message only when the person you are working with allowed it",
                     "only a request that leaves nothing outside Slack",
                     "uv run python harness.py --verify-plan", "world.tool_check",
                     "a boundary check never lets a write tool reach the outside",
                     "outward=["):
            self.assertIn(line, doc)
        self.assertNotIn("where it landed", doc)  # an open boundary must not have written
        # Review of PR #6: the doc offered `subprocess.run` as the thing to stub, which the
        # kit's run_cmd never calls, and gave no way to stub writes apart from reads.
        self.assertNotIn("an HTTP post, `subprocess.run`", doc)
        for line in ("from world.toolkit import run_cmd", "async def gh_write(",
                     "Stub a function that only writes", "Name the function where the tool "
                     "looks it up", "asyncio.create_task"):
            self.assertIn(line, doc)
        tool = " ".join((KIT / "templates/skill-wrapper-tool.md.tmpl").read_text().split())
        self.assertIn("writes through one function that only writes", tool)
        legacy = " ".join((KIT / ".claude/the-world/verify.md").read_text().split())
        self.assertIn("docs/verify.md", legacy)
        self.assertIn("never press an approve or deny button", legacy)
        self.assertNotIn("no longer ships", legacy)


class LegacyWrapperTests(unittest.TestCase):
    """Review of PR #6: the kit-owned compatibility paths still said "`write_scope` never
    empty" and "no step-by-step flow for adding a tool", against the equip template and
    world-scope-guard.py (no scope = writes nothing)."""

    def read(self, rel: str) -> str:
        return " ".join((KIT / rel).read_text(encoding="utf-8").split())

    def test_no_wrapper_says_write_scope_is_never_empty(self) -> None:
        for rel in (".claude/the-world/agent.md", ".claude/the-world/plugins.md",
                    ".claude/the-world/tool.md", "templates/skill-wrapper.md.tmpl",
                    "templates/skill-wrapper-tool.md.tmpl"):
            text = self.read(rel)
            self.assertNotIn("never empty", text, rel)
            self.assertIn("`write_scope`", text, rel)
        self.assertIn("empty, which writes nothing", self.read("templates/skill-wrapper.md.tmpl"))

    def test_the_legacy_tool_path_leads_to_the_equip_flow(self) -> None:
        tool = self.read(".claude/the-world/tool.md")
        self.assertNotIn("no longer ships", tool)
        self.assertIn("templates/skill-wrapper-tool.md.tmpl", tool)
        self.assertIn("Split the request, place each piece, then build", tool)
        self.assertIn("wait for the user's confirmation", tool)
        self.assertIn("bin/scaffold-world-skill.py <word> tool", tool)
        self.assertIn("Split the request, place each piece, then build",
                      self.read("templates/skill-wrapper-tool.md.tmpl"))

    @unittest.skipUnless((KIT.parents[1] / "SKILL.md").exists(), "run from the kit's source")
    def test_the_start_skill_says_how_to_renew_an_old_wrapper(self) -> None:
        skill = " ".join((KIT.parents[1] / "SKILL.md").read_text(encoding="utf-8").split())
        self.assertNotIn("is never empty", skill)
        self.assertIn("re-run the scaffolder with the same flow", skill)
        # Review of PR #6: the usage line said `agent|tool` while the flow takes `verify`.
        self.assertIn("bin/scaffold-world-skill.py <word> agent|tool|verify|ops", skill)

    @unittest.skipUnless((KIT.parents[1] / "SKILL.md").exists(), "run from the kit's source")
    def test_the_start_skill_names_world_words_and_where_they_work(self) -> None:
        # A harness built on a chosen world came out with `recruit` and `equip` after the world was
        # chosen, and the finish never said the words load only in a harness session.
        skill = " ".join((KIT.parents[1] / "SKILL.md").read_text(encoding="utf-8").split())
        self.assertNotIn("Default to `recruit` and `equip` when the user has no preference", skill)
        self.assertIn("Show them in the same message as the cast and their voices", skill)
        self.assertIn("Fall back to `recruit`, `equip`, `verify` and `ops` only when no world "
                      "was chosen", skill)
        self.assertIn("`cd <harness> && claude`", skill)
        self.assertIn("`cd <harness> && codex`", skill)
        self.assertIn("This is the last line", skill)

    @unittest.skipUnless((KIT.parents[1] / "SKILL.md").exists(), "run from the kit's source")
    def test_the_start_skill_lays_out_messages_with_the_card_helper(self) -> None:
        skill = " ".join((KIT.parents[1] / "SKILL.md").read_text(encoding="utf-8").split())
        self.assertIn("**Messages are laid out, not run on.**", skill)
        self.assertIn("`listener.post(text, blocks=blocks)`", skill)
        self.assertIn("world.slack.blocks.card(title,", skill)


if __name__ == "__main__":
    unittest.main()
