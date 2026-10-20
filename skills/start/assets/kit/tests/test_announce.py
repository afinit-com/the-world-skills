"""Announcement modes through the Slack card boundary; no channel calls."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import shutil
import tempfile
import time
import unittest

from world.channels import base
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

    async def card(self, mode="result", artifacts=(), result="Saved report", announce_file=""):
        mixin = MentionMixin()
        mixin.harness = SimpleNamespace(home=self.home, agents=[
            Agent("watch", MentionTrigger(), announce=mode,
                  writes_report=mode == "file", announce_file=announce_file)], run_footer=False,
            knowledge_proposal_agent=None)
        mixin.state = {"threads": {}}
        mixin.save_state = lambda: None
        posted = []

        async def post(body, **kwargs):
            posted.append(body)  # the card's text; its blocks are cut from the same lines
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

    def test_file_mode_needs_an_agent_that_writes_a_report(self):
        # Only a writes_report run has artifacts; without one, file always fell back.
        with self.assertRaisesRegex(ValueError, "writes_report"):
            Agent("watch", MentionTrigger(), announce="file")
        Agent("watch", MentionTrigger(), announce="file", writes_report=True)
        Agent("watch", MentionTrigger(), announce="file", writes_finding=True)

    def report(self, name, content):
        path = tasks.task_work_dir(self.home, self.record.id) / name
        path.write_text(content, encoding="utf-8")
        return str(path.relative_to(self.home))

    def require_safe_open(self):
        # Checked per test: Windows lacks it, and must still run the fallback tests.
        if not base.FILE_PREVIEW_SUPPORTED:
            self.skipTest("file preview needs safe descriptor-relative open")

    async def test_file_mode_shows_intro_before_first_section(self):
        self.require_safe_open()
        # The H1 title repeats the card's headline, so the body starts after it.
        path = self.report("report.md", "# Briefing\n**Four changes**\n## Details\nHidden")
        body = await self.card("file", [path])
        self.assertEqual(["> *Four changes*"], body.splitlines()[2:])
        self.assertNotIn("Saved report", body)

    async def test_title_then_section_shows_the_first_section(self):
        self.require_safe_open()
        # The common briefing shape: '# title', then '## 요약'. It used to show the title only.
        path = self.report("report.md", "# 아침 브리핑 2026-10-16\n\n## 요약\n- 회의 3건\n\n"
                                        "## 상세\nHidden")
        body = await self.card("file", [path])
        self.assertEqual(["> *요약*", "> - 회의 3건"], body.splitlines()[2:])

    async def test_front_matter_is_not_the_opening(self):
        self.require_safe_open()
        path = self.report("report.md", "---\ntitle: Briefing\ndate: 2026-10-16\n---\n"
                                        "# 브리핑\n요약 본문\n## 상세\nHidden")
        self.assertEqual(["> 요약 본문"], (await self.card("file", [path])).splitlines()[2:])
        # A rule with no closing one is a horizontal rule, not front matter.
        path = self.report("rule.md", "Intro\n---\nMore")
        self.assertEqual(["> Intro", "> ---", "> More"],
                         (await self.card("file", [path])).splitlines()[2:])
        path = self.report("open.md", "---\nonly a rule")
        self.assertEqual(["> ---", "> only a rule"],
                         (await self.card("file", [path])).splitlines()[2:])

    async def test_a_title_alone_falls_back_to_result(self):
        path = self.report("report.md", "# Briefing\n")
        self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_empty_h1_and_blank_lines_fall_back(self):
        for text in ("# \n\n", "#\n\n", "\n# \t\n\n"):
            path = self.report("report.md", text)
            self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_file_starting_with_section_keeps_the_first_section(self):
        self.require_safe_open()
        path = self.report("report.md", "## Summary\nFour changes\n## Details\nHidden")
        body = await self.card("file", [path])
        self.assertEqual(["> *Summary*", "> Four changes"], body.splitlines()[2:])

    async def test_result_mode_is_the_existing_card_byte_for_byte(self):
        path = self.report("report.md", "File content")
        body = await self.card("result", [path], "**Final message**")
        self.assertEqual(f"*Report*\n`{self.record.id}` · queued · watch · checked report\n"
                         "> *Final message*", body)

    async def test_file_and_result_use_identical_rendering_and_limits(self):
        self.require_safe_open()
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

    async def test_expanded_redaction_only_applies_to_file_excerpt(self):
        self.require_safe_open()
        text = "password=hunter2, retry=3"
        path = self.report("report.md", text)
        self.assertIn(text, await self.card(result=text))
        self.assertNotIn("hunter2", await self.card("file", [path], result=text))
        self.assertEqual(await self.card(result=text), await self.card("file", result=text))

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
        self.require_safe_open()
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
        self.require_safe_open()
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

    async def test_a_hard_link_to_a_file_outside_work_is_rejected(self):
        self.require_safe_open()
        outside = self.home / "secret.txt"
        outside.write_text("OUTSIDE SECRET")
        link = tasks.task_work_dir(self.home, self.record.id) / "briefing.md"
        os.link(outside, link)
        path = str(link.relative_to(self.home))
        self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_a_file_swapped_after_the_path_check_is_rejected(self):
        self.require_safe_open()
        # The path was checked inside work/, but what got opened is another file.
        outside = self.home / "secret.txt"
        outside.write_text("OUTSIDE SECRET")
        path = self.report("briefing.md", "Inside")
        real_open = os.open
        def swapped(path, flags, **kwargs):
            return real_open(outside, flags) if path == "briefing.md" else real_open(path, flags, **kwargs)

        with patch("world.channels.base.os.open", side_effect=swapped):
            self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_parent_swapped_before_stat_cannot_escape_work(self):
        self.require_safe_open()
        from world.channels import base
        work = tasks.task_work_dir(self.home, self.record.id)
        reports = work / "reports"
        reports.mkdir()
        path = self.report("reports/briefing.md", "Inside")
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "briefing.md").write_text("OUTSIDE SECRET")
        read = base._read_opening

        def swapped(*args, **kwargs):
            reports.rename(work / "original")
            reports.symlink_to(outside, target_is_directory=True)
            return read(*args, **kwargs)

        with patch.object(base, "_read_opening", side_effect=swapped):
            self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_internal_parent_symlink_is_also_rejected(self):
        self.require_safe_open()
        work = tasks.task_work_dir(self.home, self.record.id)
        (work / "real").mkdir()
        self.report("real/briefing.md", "Inside")
        (work / "alias").symlink_to(work / "real", target_is_directory=True)
        self.assertEqual(await self.card(), await self.card(
            "file", [str(work / "alias" / "briefing.md")]))

    async def test_large_file_reads_a_bounded_prefix_even_across_a_utf8_character(self):
        self.require_safe_open()
        path = self.report("large.md", "A" * 65535 + "가" + "B" * 1_000_000)
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
        self.require_safe_open()
        text = "Intro\n```md\n## code\n```\n## Details\nHidden"
        path = self.report("report.md", text)
        self.assertEqual(await self.card(result="Intro\n```md\n## code\n```"),
                         await self.card("file", [path]))

    async def test_tilde_fences_hide_headings_too(self):
        self.require_safe_open()
        for text, opening in (
                ("Intro\n~~~\n## in code\n~~~\n## Details\nHidden", "Intro\n~~~\n## in code\n~~~"),
                # A backtick line inside a tilde block neither opens nor closes it.
                ("Intro\n~~~\n```\n## in code\n~~~\n## Details", "Intro\n~~~\n```\n## in code\n~~~"),
                ("Intro\n````\n```\n## in code\n````\n## Details", "Intro\n````\n```\n## in code\n````")):
            with self.subTest(text=text):
                path = self.report("report.md", text)
                self.assertEqual(await self.card(result=opening), await self.card("file", [path]))

    async def test_an_unclosed_fence_does_not_swallow_the_sections_after_it(self):
        self.require_safe_open()
        path = self.report("report.md", "Intro\n```\nunclosed\n## Details\nHidden")
        self.assertEqual(await self.card(result="Intro\n```\nunclosed"),
                         await self.card("file", [path]))
        # A closed block before the unclosed fence still hides its headings.
        path = self.report("report.md", "Intro\n~~~\n## code\n~~~\n```\nopen\n## Details\nHidden")
        self.assertEqual(await self.card(result="Intro\n~~~\n## code\n~~~\n```\nopen"),
                         await self.card("file", [path]))

    async def test_platform_without_safe_open_falls_back_to_result(self):
        path = self.report("report.md", "Summary")
        with patch("world.channels.base.FILE_PREVIEW_SUPPORTED", False):
            self.assertEqual(await self.card(), await self.card("file", [path]))

    async def test_an_unusable_report_gives_way_to_the_next(self):
        self.require_safe_open()
        first = self.report("a-report.md", "")
        second = self.report("b-report.md", "Another report")
        self.assertEqual("> Another report", (await self.card("file", [first, second])).splitlines()[-1])

    async def test_markdown_reports_come_before_data_logs_and_hidden_files(self):
        self.require_safe_open()
        work = tasks.task_work_dir(self.home, self.record.id)
        for others, report in (
                ({"articles.json": '{"items":[{"title":"x"}]}'}, "briefing.md"),
                ({"fetch.log": "GET https://api.example.com?token=abc 200"}, "summary.md"),
                ({"2026-10-16-log.txt": "step1 ok"}, "2026-10-16-summary.md"),
                ({"NOTES.txt": "scratch"}, "briefing.md"),
                ({".progress": "50%"}, "briefing.md"),
                ({"chart.svg": "<svg></svg>"}, "z.md"),
                ({"assets/a.md": "nested"}, "briefing.md")):
            with self.subTest(report=report, others=list(others)):
                shutil.rmtree(work)
                work.mkdir()
                for name, content in {**others, report: "The briefing"}.items():
                    (work / name).parent.mkdir(exist_ok=True)
                    (work / name).write_text(content)
                # Artifacts as completion lists them: this run's new top-level entries.
                paths = sorted(str(child.relative_to(self.home)) for child in work.iterdir())
                body = await self.card("file", paths)
                self.assertEqual("> The briefing", body.splitlines()[-1])

    async def test_a_plain_text_report_is_used_when_there_is_no_markdown(self):
        self.require_safe_open()
        path = self.report("report.txt", "Plain summary")
        data = self.report("data.json", "{}")
        self.assertEqual("> Plain summary", (await self.card("file", [data, path])).splitlines()[-1])
        self.assertEqual(await self.card(), await self.card("file", [data]))

    async def test_candidate_cap_applies_after_filtering_and_ranking(self):
        self.require_safe_open()
        data = [self.report(f"{i:04}.json", "{}") for i in range(501)]
        report = self.report("briefing.md", "Chosen report")
        for pattern in ("", "briefing.md", "*.md"):
            self.assertEqual("> Chosen report", (await self.card(
                "file", [*data, report], announce_file=pattern)).splitlines()[-1])
        # Default priority must also beat 500 otherwise eligible text files.
        texts = [self.report(f"{i:04}.txt", "Scratch") for i in range(501)]
        self.assertEqual("> Chosen report", (await self.card(
            "file", [*texts, report])).splitlines()[-1])

    def test_many_matching_files_are_ranked_without_resorting_each_one(self):
        # A knowledge-base agent's folder of 10,000 pages must not stall the event loop:
        # candidates are ranked once and trimmed in batches, not re-sorted per file.
        from world.channels import base
        work = tasks.task_work_dir(self.home, self.record.id)
        (work / "pages").mkdir()
        for i in range(10_000):
            (work / "pages" / f"{i:05}.md").write_text("")
        summary = self.report("summary.md", "Summary")
        result = RunResult(status="ok", result_text="Final", artifacts=(
            str((work / "pages").relative_to(self.home)), summary))
        key = base._report_key
        ranked = []

        def counted(rel, pattern):
            ranked.append(rel)
            return key(rel, pattern)

        with patch.object(base, "_report_key", side_effect=counted):
            started = time.perf_counter()
            found = base._report_candidates(self.home, self.record, result, work.resolve())
            elapsed = time.perf_counter() - started
        self.assertEqual(base.REPORT_CANDIDATES, len(found))
        self.assertEqual(work.resolve() / "summary.md", base._report_order(found, "")[0])
        # Each file is ranked a bounded number of times, never once per trim.
        self.assertLessEqual(len(ranked), 3 * 10_001)
        self.assertLess(elapsed, 3.0)  # was ~12s; tens of ms here, generous for slow CI

    async def test_outside_artifact_directory_is_not_walked(self):
        from world.channels import base
        outside = self.home / "outside"
        outside.mkdir()
        with patch.object(base, "_walk", side_effect=AssertionError("outside traversal")):
            self.assertEqual(await self.card(), await self.card("file", [str(outside)]))

    async def test_announce_file_names_the_report(self):
        self.require_safe_open()
        summary = self.report("a-summary.md", "Not this one")
        briefing = self.report("briefing.md", "The briefing")
        body = await self.card("file", [summary, briefing], announce_file="briefing.md")
        self.assertEqual("> The briefing", body.splitlines()[-1])
        # A named report that is not there falls back to result, not to another report.
        self.assertEqual(await self.card(),
                         await self.card("file", [summary], announce_file="briefing.md"))
        # Named explicitly, a .json is used.
        data = self.report("out.json", '{"ok": true}')
        body = await self.card("file", [data], announce_file="*.json")
        self.assertIn("ok", body.splitlines()[-1])

    async def test_announce_file_globs_inside_a_new_folder(self):
        self.require_safe_open()
        work = tasks.task_work_dir(self.home, self.record.id)
        (work / "reports").mkdir()
        (work / "reports" / "2026-10-16.md").write_text("Nested briefing")
        folder = str((work / "reports").relative_to(self.home))
        body = await self.card("file", [folder], announce_file="reports/*.md")
        self.assertEqual("> Nested briefing", body.splitlines()[-1])

    async def test_a_report_rewritten_by_this_attempt_counts_as_its_own(self):
        self.require_safe_open()
        # A retry reuses work/: it rewrites briefing.md (not new, so not an artifact) and
        # adds run2.log. The card shows the rewritten briefing, never an older file.
        old = self.report("old.md", "Stale")
        briefing = self.report("briefing.md", "First attempt")
        stale = time.time() - 3600
        for path in (old, briefing):
            os.utime(self.home / path, (stale, stale))
        tasks.transition(self.home, self.record, "running", reason="claimed")
        self.report("briefing.md", "Second attempt")
        log = self.report("run2.log", "GET /")
        self.assertEqual("> Second attempt", (await self.card("file", [log])).splitlines()[-1])
        self.assertEqual("> Second attempt", (await self.card(
            "file", [log], announce_file="briefing.md")).splitlines()[-1])
        (self.home / briefing).unlink()
        self.assertEqual(await self.card(), await self.card("file", [log]))

    async def test_same_second_previous_attempt_is_excluded(self):
        self.require_safe_open()
        second = int(time.time()) - 10
        stale = self.report("a-stale.md", "Previous attempt")
        current = self.report("z-current.md", "Current attempt")
        tasks.claim(self.home, self.record, tasks.TaskType("agent-run"), now=second + .5)
        for path, fraction in ((stale, .1), (current, .8)):
            stamp = int((second + fraction) * 1_000_000_000)
            os.utime(self.home / path, ns=(stamp, stamp))
        claim = tasks.read_events(self.home, self.record.id)[-1]
        self.assertEqual(int((second + .5) * 1_000_000_000), claim.get("started_ns"))
        self.assertEqual("> Current attempt", (await self.card("file")).splitlines()[-1])

    def test_announce_file_is_checked_at_construction(self):
        Agent("watch", MentionTrigger(), announce="file", writes_report=True,
              announce_file="reports/*.md")
        for pattern, mode in (("briefing.md", "result"), ("../secret.md", "file"),
                              ("/etc/passwd", "file"), ("a/../../b.md", "file")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, "announce_file"):
                Agent("watch", MentionTrigger(), announce=mode, writes_report=True,
                      announce_file=pattern)

    async def test_no_file_and_no_result_keeps_the_existing_evidence_fallback(self):
        self.assertEqual(await self.card(result=""), await self.card("file", result=""))

    def test_startup_warns_when_file_mode_cannot_read_reports_here(self):
        from world.harness import Harness

        harness = Harness("t", home=self.home, git_root=self.home)
        logged = []
        harness.log = logged.append
        harness.register(Agent("watch", MentionTrigger(), announce="file", writes_report=True))
        harness.register(Agent("other", MentionTrigger()))
        with patch("world.channels.base.FILE_PREVIEW_SUPPORTED", True):
            self.assertEqual([], harness.preflight_announce())
        with patch("world.channels.base.FILE_PREVIEW_SUPPORTED", False):
            warned = harness.preflight_announce()
        self.assertEqual(1, len(warned))
        self.assertIn("watch:", warned[0])
        self.assertNotIn("other", warned[0])
        self.assertEqual(warned, logged)
