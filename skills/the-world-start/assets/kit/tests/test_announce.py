"""Announcement modes through the Slack card boundary; no channel calls."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

from world.process import task as tasks
from world.slack.mentions import MentionMixin
from world.types import Agent, MentionTrigger, RunResult


class AnnounceModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.record = tasks.create(self.home, type="agent-run", title="Report", agent="watch")
        tasks.append_event(self.home, self.record.id, "verify", verdict="done",
                           evidence=["checked report"])

    async def card(self, mode="result", artifacts=(), result="Saved report"):
        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(home=self.home, agents=[
            Agent("watch", MentionTrigger(), announce=mode)], run_footer=False,
            knowledge_proposal_agent=None)
        mixin.state = {"threads": {}}
        mixin.save_state = lambda: None
        posted = []

        async def post(body, **kwargs):
            posted.append(kwargs["blocks"][0]["text"]["text"])
            return "1.2"

        mixin.post = post
        await mixin.announce_task(self.record, RunResult(status="ok", result_text=result,
                                  artifacts=tuple(artifacts)), SimpleNamespace(key="main"))
        return posted[0]

    async def test_evidence_mode_uses_completion_instead_of_final_message(self):
        body = await self.card("evidence")
        self.assertEqual(["> checked report"], body.splitlines()[2:])
        self.assertNotIn("Saved report", body)

    async def test_evidence_without_a_completion_check_keeps_the_old_result_fallback(self):
        self.record = tasks.create(self.home, type="agent-run", title="No check", agent="watch")
        self.assertEqual(await self.card(), await self.card("evidence"))

    def test_invalid_modes_fail_at_construction(self):
        for mode in ("files", "", None, [], 1):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "announce"):
                Agent("watch", MentionTrigger(), announce=mode)

    def report(self, name, content):
        path = tasks.task_work_dir(self.home, self.record.id) / name
        path.write_text(content, encoding="utf-8")
        return str(path.relative_to(self.home))

    async def test_file_mode_shows_intro_before_first_section(self):
        path = self.report("report.md", "# Briefing\n**Four changes**\n## Details\nHidden")
        body = await self.card("file", [path])
        self.assertEqual(["> *Briefing*", "> *Four changes*"], body.splitlines()[2:])
        self.assertNotIn("Saved report", body)

    async def test_file_starting_with_section_keeps_the_first_section(self):
        path = self.report("report.md", "## Summary\nFour changes\n## Details\nHidden")
        body = await self.card("file", [path])
        self.assertEqual(["> *Summary*", "> Four changes"], body.splitlines()[2:])

    async def test_result_mode_is_the_existing_card_byte_for_byte(self):
        path = self.report("report.md", "File content")
        body = await self.card("result", [path], "**Final message**")
        self.assertEqual(f"*Report*\n`{self.record.id}` · queued · watch · checked report\n"
                         "> *Final message*", body)

    async def test_file_and_result_use_identical_rendering_and_limits(self):
        for text in ("**Summary**\n<!channel> <!here> @channel",
                     "ghp_" + "a" * 36 + " xoxb-" + "1" * 40,
                     "```python\n" + "x=1\n" * 200 + "```",
                     "가" * 400):
            with self.subTest(text=text[:25]):
                path = self.report("report.md", text)
                body = await self.card("file", [path])
                self.assertEqual(await self.card("result", result=text), body)
                self.assertNotIn("<!channel>", body)
                self.assertNotIn("ghp_aaaa", body)
                self.assertEqual(0, body.count("```") % 2)

    async def test_unusable_files_fall_back_to_result(self):
        path = self.report("report.md", "")
        absolute = self.home / path
        for data in (b"", b" \n\t", b"\x89PNG\r\n\x1a\n", b"binary\x00data", b"\xff\xfe"):
            with self.subTest(data=data):
                absolute.write_bytes(data)
                self.assertEqual(await self.card(), await self.card("file", [path]))
        absolute.unlink()
        self.assertEqual(await self.card(), await self.card("file", [path]))
        self.assertEqual(await self.card(), await self.card("file"))
        absolute.mkdir()
        self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_unreadable_file_falls_back(self):
        path = self.report("report.md", "Report")
        expected = await self.card()
        with patch("world.channels.base.os.open", side_effect=PermissionError):
            self.assertEqual(expected, await self.card("file", [path]))

    async def test_only_current_run_artifacts_inside_this_tasks_work_are_candidates(self):
        self.report("a-stale.md", "Stale report must not be scanned")
        first = self.report("b-report.md", "First report")
        second = self.report("c-report.md", "Second report")
        outside = self.home / "outside.md"
        outside.write_text("Outside secret")
        other = tasks.create(self.home, type="agent-run", title="Other", agent="watch")
        other_path = tasks.task_work_dir(self.home, other.id) / "report.md"
        other_path.write_text("Other task secret")
        body = await self.card("file", [str(outside), str(other_path), first, second])
        self.assertEqual("> First report", body.splitlines()[-1])
        self.assertEqual(await self.card(), await self.card("file", [str(outside), str(other_path)]))

    async def test_symlinks_outside_work_and_symlinked_work_are_rejected(self):
        outside = self.home / "secret.md"
        outside.write_text("Outside secret")
        work = tasks.task_work_dir(self.home, self.record.id)
        link = work / "report.md"
        link.symlink_to(outside)
        self.assertEqual(await self.card(), await self.card("file", [str(link)]))
        link.unlink()
        link.symlink_to(link)
        self.assertEqual(await self.card(), await self.card("file", [str(link)]))
        link.unlink()
        work.rmdir()
        work.symlink_to(self.home, target_is_directory=True)
        self.assertEqual(await self.card(), await self.card("file", [str(work / "secret.md")]))

    async def test_large_file_reads_a_bounded_prefix_even_across_a_utf8_character(self):
        path = self.report("large.md", "A" * 65535 + "가" + "B" * 1_000_000)
        import os
        real_fdopen = os.fdopen
        reads = []

        def observed(fd, mode):
            source = real_fdopen(fd, mode)
            read = source.read

            def bounded(size=-1):
                reads.append(size)
                return read(size)

            source.read = bounded
            return source

        with patch("world.channels.base.os.fdopen", side_effect=observed):
            body = await self.card("file", [path])
        self.assertEqual([65536], reads)
        self.assertEqual("> " + "A" * 299 + "…", body.splitlines()[-1])

    async def test_section_markers_inside_code_are_not_headings(self):
        text = "Intro\n```md\n## code\n```\n## Details\nHidden"
        path = self.report("report.md", text)
        self.assertEqual(await self.card(result="Intro\n```md\n## code\n```"),
                         await self.card("file", [path]))

    async def test_platform_without_posix_open_flags_can_read_a_report(self):
        path = self.report("report.md", "Summary")
        with patch("world.channels.base.os.O_NONBLOCK", 0, create=True), \
                patch("world.channels.base.os.O_NOFOLLOW", 0, create=True):
            self.assertEqual("> Summary", (await self.card("file", [path])).splitlines()[-1])

    async def test_unusable_primary_does_not_silently_choose_another_report(self):
        first = self.report("a-report.md", "")
        second = self.report("b-report.md", "Another report")
        self.assertEqual(await self.card(), await self.card("file", [first, second]))

    async def test_no_file_and_no_result_keeps_the_existing_evidence_fallback(self):
        self.assertEqual(await self.card(result=""), await self.card("file", result=""))
