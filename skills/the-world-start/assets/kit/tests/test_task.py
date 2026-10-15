from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from world.process import task as tasks


def _home() -> Path:
    return Path(tempfile.mkdtemp())


def _make(home: Path, **kwargs) -> tasks.Task:
    kwargs.setdefault("type", "migration")
    kwargs.setdefault("title", "orders에 note 컬럼 추가")
    return tasks.create(home, **kwargs)


class RecordTests(unittest.TestCase):
    def test_a_colliding_id_gets_a_fresh_one_instead_of_overwriting(self) -> None:
        """Ids carry 16 random bits. Two tasks drawing the same one used to share a
        directory, and the second task.md replaced the first without a word."""
        home = _home()
        draws = iter(["abcd", "abcd", "beef"])
        with patch.object(tasks.secrets, "token_hex", side_effect=lambda _n: next(draws)):
            first = _make(home, slug="first")
            second = _make(home, slug="second")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual({"first", "second"}, {t.slug for t in tasks.list_tasks(home)})

    def test_a_task_that_cannot_be_written_in_full_leaves_no_directory_behind(self) -> None:
        home = _home()
        with patch.object(tasks, "append_event", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                _make(home, source="원문")
        self.assertEqual([], list(tasks.tasks_dir(home).iterdir()))
        self.assertEqual([], tasks.list_tasks(home))

    def test_an_invalid_task_leaves_no_directory_behind(self) -> None:
        home = _home()
        with self.assertRaises(tasks.TaskError):
            _make(home, origin=tasks.Origin(kind="nobody"))
        self.assertEqual([], list(tasks.tasks_dir(home).iterdir()))

    def test_a_line_break_in_a_value_cannot_write_header_keys(self) -> None:
        """The header is one key per line and a repeated key keeps its last value, so a
        title carrying "\nid: …" or "\n---\n" rewrote the record when it was read back."""
        home = _home()
        made = _make(home, title="Daily\nid: 20261001-dead\u2028type: x\n---\nbody")
        got = tasks.read(home, made.id)
        self.assertEqual(made.id, got.id)
        self.assertEqual("migration", got.type)
        self.assertEqual("Daily id: 20261001-dead type: x --- body", got.title)
        self.assertEqual(made.title, got.title)

    def test_roundtrip_preserves_unicode_lists_and_nested_groups(self) -> None:
        home = _home()
        made = _make(
            home, slug="orders-note",
            origin=tasks.Origin(kind="operator", by="U01", replay=True),
            reads=("db://shop/main",),
            writes=("db://shop/main/orders", "db://shop/staging/x,y"),
            depends_on=("20260915-77c1",),
            policy="migration-v3",
        )
        got = tasks.read(home, made.id)
        self.assertEqual(made.title, got.title)
        self.assertEqual("operator", got.origin.kind)
        self.assertTrue(got.origin.replay)
        self.assertEqual(("db://shop/main",), got.reads)
        # A comma inside a resource URI must not split the field — that is why lists
        # are JSON rather than comma-joined.
        self.assertEqual(("db://shop/main/orders", "db://shop/staging/x,y"), got.writes)
        self.assertEqual(("20260915-77c1",), got.depends_on)
        self.assertEqual("migration-v3", got.policy)

    def test_the_directory_is_the_id_alone(self) -> None:
        home = _home()
        made = _make(home, slug="orders-note")
        self.assertTrue((tasks.tasks_dir(home) / made.id).is_dir())
        self.assertEqual("orders-note", tasks.read(home, made.id).slug)
        self.assertIsNone(tasks.task_dir(home, "not-an-id"))

    def test_source_is_preserved_byte_for_byte_and_dedups(self) -> None:
        home = _home()
        raw = "공지 원본 <b>HTML</b>\n".encode()
        made = _make(home, source=raw, source_name="notice.html")
        directory = tasks.task_dir(home, made.id)
        self.assertEqual(raw, (directory / "source" / "notice.html").read_bytes())
        self.assertTrue(made.origin.source_hash.startswith("sha256:"))
        self.assertEqual(made.id, tasks.find_by_source_hash(home, made.origin.source_hash).id)
        self.assertIsNone(tasks.find_by_source_hash(home, ""))

    def test_created_survives_save_and_updated_moves(self) -> None:
        home = _home()
        made = _make(home)
        made.updated = ""
        tasks.save(home, made)
        got = tasks.read(home, made.id)
        self.assertEqual(made.created, got.created)
        self.assertNotEqual("", got.updated)

    def test_validate_rejects_bad_values(self) -> None:
        home = _home()
        with self.assertRaises(tasks.TaskError):
            _make(home, title="   ")
        with self.assertRaises(tasks.TaskError):
            _make(home, slug="Not A Slug")
        with self.assertRaises(tasks.TaskError):
            tasks.Origin(kind="wherever").validate()
        # The same resource cannot be both shared and exclusive.
        with self.assertRaises(tasks.TaskError):
            _make(home, reads=("db://a",), writes=("db://a",))

    def test_events_are_append_only_and_typed(self) -> None:
        home = _home()
        made = _make(home)
        tasks.record_manual(home, made, by="U01", summary="운영자가 데이터셋 직접 삭제")
        kinds = [event["kind"] for event in tasks.read_events(home, made.id)]
        self.assertEqual(["created", "manual"], kinds)
        self.assertTrue(all(event.get("at") for event in tasks.read_events(home, made.id)))


class CompletionTests(unittest.TestCase):
    def test_done_cannot_be_assigned_by_a_caller(self) -> None:
        home = _home()
        made = _make(home)
        with self.assertRaises(tasks.TaskError) as caught:
            tasks.transition(home, made, "done")
        self.assertIn("verify()", str(caught.exception))
        self.assertEqual("queued", tasks.read(home, made.id).status)

    def test_predicate_decides_done(self) -> None:
        home = _home()
        made = _make(home)
        pending = tasks.TaskType("migration", completion=lambda _t: tasks.Verdict.pending("새 컬럼이 아직 없음"))
        self.assertEqual("pending", tasks.verify(home, made, pending).state)
        self.assertEqual("queued", tasks.read(home, made.id).status)

        finishing = tasks.TaskType("migration", completion=lambda _t: tasks.Verdict.done("스키마 확인", "자원 정리"))
        self.assertEqual("done", tasks.verify(home, made, finishing).state)
        self.assertEqual("done", tasks.read(home, made.id).status)

    def test_verdict_requires_evidence(self) -> None:
        with self.assertRaises(tasks.TaskError):
            tasks.Verdict("done", ())
        with self.assertRaises(tasks.TaskError):
            tasks.Verdict("finished", ("x",))

    def test_fallback_contract_is_the_artifact_check(self) -> None:
        home = _home()
        made = _make(home)
        untyped = tasks.TaskType("generic")
        self.assertEqual("pending", tasks.verify(home, made, untyped).state)
        (tasks.task_dir(home, made.id) / "work" / "run.md").write_text("결과")
        self.assertEqual("done", tasks.verify(home, made, untyped).state)

    def test_milestones_are_recorded_but_never_finish_a_task(self) -> None:
        home = _home()
        made = _make(home)
        migration = tasks.TaskType("migration", milestones=("pr_opened",),
                         completion=lambda _t: tasks.Verdict.pending("아직"))
        tasks.record_milestone(home, made, "pr_opened", migration, ref="https://example/pr/812")
        self.assertEqual("queued", tasks.read(home, made.id).status)
        self.assertIn("milestone", [e["kind"] for e in tasks.read_events(home, made.id)])
        with self.assertRaises(tasks.TaskError):
            tasks.record_milestone(home, made, "pr_merged", migration)

    def test_settled_tasks_do_not_move_again(self) -> None:
        home = _home()
        made = _make(home)
        tasks.verify(home, made, tasks.TaskType("migration", completion=lambda _t: tasks.Verdict.done("ok")))
        with self.assertRaises(tasks.TaskError):
            tasks.transition(home, made, "queued")


class RetryTests(unittest.TestCase):
    def test_budget_exhaustion_blocks_rather_than_looping(self) -> None:
        home = _home()
        made = _make(home, task_type=tasks.TaskType("migration", retry_budget=2))
        tasks.transition(home, made, "running")
        tasks.record_attempt(home, made, outcome="error", summary="timeout")
        self.assertEqual("running", tasks.read(home, made.id).status)
        tasks.record_attempt(home, made, outcome="error", summary="timeout")
        self.assertEqual("blocked", tasks.read(home, made.id).status)

    def test_success_does_not_block_even_at_the_budget(self) -> None:
        home = _home()
        made = _make(home, task_type=tasks.TaskType("migration", retry_budget=1))
        tasks.transition(home, made, "running")
        tasks.record_attempt(home, made, outcome="ok")
        self.assertEqual("running", tasks.read(home, made.id).status)


class ApprovalScopeTests(unittest.TestCase):
    def test_grant_goes_stale_when_the_approved_files_change(self) -> None:
        home = _home()
        (home / "migrations").mkdir()
        changed = home / "migrations" / "001.sql"
        changed.write_text("ALTER TABLE orders ADD COLUMN note TEXT;")
        made = _make(home, writes=("db://shop/main/orders",))

        files, targets = ["migrations/001.sql"], list(made.writes)
        tasks.grant_approval(home, made, by="U01", scope_hash=tasks.compute_scope_hash(home, files, targets))
        self.assertTrue(tasks.check_approval(home, made, files, targets))

        changed.write_text("DROP TABLE orders;")
        self.assertFalse(tasks.check_approval(home, made, files, targets))
        self.assertEqual("stale", tasks.read(home, made.id).approval.state)

    def test_changing_the_target_also_invalidates(self) -> None:
        home = _home()
        (home / "m.sql").write_text("x")
        made = _make(home)
        files = ["m.sql"]
        tasks.grant_approval(home, made, by="U01",
                         scope_hash=tasks.compute_scope_hash(home, files, ["db://shop/a"]))
        self.assertFalse(tasks.check_approval(home, made, files, ["db://shop/b"]))

    def test_deleting_an_approved_file_invalidates(self) -> None:
        home = _home()
        gone = home / "m.sql"
        gone.write_text("x")
        made = _make(home)
        tasks.grant_approval(home, made, by="U01", scope_hash=tasks.compute_scope_hash(home, ["m.sql"], []))
        gone.unlink()
        self.assertFalse(tasks.check_approval(home, made, ["m.sql"], []))

    def test_denied_and_ungranted_never_pass(self) -> None:
        home = _home()
        made = _make(home)
        self.assertFalse(tasks.check_approval(home, made, [], []))
        tasks.deny_approval(home, made, by="U01", reason="범위가 너무 넓음")
        self.assertFalse(tasks.check_approval(home, made, [], []))


class SchedulingTests(unittest.TestCase):
    def test_disjoint_resources_run_in_parallel_and_writers_are_exclusive(self) -> None:
        home = _home()
        mine = _make(home, writes=("db://shop/a",))
        same_write = _make(home, writes=("db://shop/a",))
        reader = _make(home, reads=("db://shop/a",))
        elsewhere = _make(home, writes=("db://shop/b",))

        self.assertEqual([same_write.id], tasks.conflicts(mine, [same_write]))
        self.assertEqual([reader.id], tasks.conflicts(mine, [reader]))
        self.assertEqual([], tasks.conflicts(mine, [elsewhere]))
        # Two readers of the same resource are not a conflict.
        other_reader = _make(home, reads=("db://shop/a",))
        self.assertEqual([], tasks.conflicts(reader, [other_reader]))

    def test_ready_waits_on_dependencies_and_locks(self) -> None:
        home = _home()
        upstream = _make(home, writes=("db://shop/a",))
        downstream = _make(home, depends_on=(upstream.id,))

        allowed, why = tasks.ready(home, downstream, [])
        self.assertFalse(allowed)
        self.assertIn(upstream.id, why)

        tasks.verify(home, upstream, tasks.TaskType("migration", completion=lambda _t: tasks.Verdict.done("ok")))
        allowed, why = tasks.ready(home, downstream, [])
        self.assertTrue(allowed, why)

        missing = _make(home, depends_on=("20260101-ffff",))
        self.assertFalse(tasks.ready(home, missing, [])[0])

    def test_claim_sets_a_lease_and_restart_reclaims_it(self) -> None:
        home = _home()
        made = _make(home)
        now = time.time()
        tasks.claim(home, made, tasks.TaskType("migration", timeout_s=1800), now=now)
        self.assertEqual("running", tasks.read(home, made.id).status)
        self.assertEqual([], tasks.reclaim_expired(home, now))

        self.assertEqual([made.id], [t.id for t in tasks.reclaim_expired(home, now + 1801)])
        self.assertEqual("queued", tasks.read(home, made.id).status)
        self.assertIn("lease_expired", [e["kind"] for e in tasks.read_events(home, made.id)])

    def test_list_tasks_filters_by_status(self) -> None:
        home = _home()
        first = _make(home)
        _make(home)
        tasks.transition(home, first, "waiting", reason="approval")
        self.assertEqual([first.id], [t.id for t in tasks.list_tasks(home, status="waiting")])
        self.assertEqual(2, len(tasks.list_tasks(home)))


if __name__ == "__main__":
    unittest.main()


class TaskScopedWriteTests(unittest.IsolatedAsyncioTestCase):
    """`tasks/{task}/work` — one write root, resolved from the run, never from the agent."""

    async def test_the_token_resolves_to_the_running_task_only(self) -> None:
        from world.context import reset_current_task, set_current_task
        from world.toolkit import make_write_report

        home = _home()
        mine = _make(home, slug="mine")
        other = _make(home, slug="other")
        write = make_write_report(home, ("tasks/{task}/work",))

        token = set_current_task(mine.id)
        try:
            ok = await write.handler({"path": f"tasks/{mine.id}/work/note.md", "content": "x"})
            self.assertFalse(ok["is_error"])
            # The token names ONE task. Naming another task's directory is outside the
            # root, not a different root — the agent cannot reach across.
            escape = await write.handler(
                {"path": f"tasks/{other.id}/work/note.md", "content": "x"}
            )
            self.assertTrue(escape["is_error"])
            self.assertIn("outside your write scope", escape["content"][0]["text"])
        finally:
            reset_current_task(token)

    async def test_the_description_names_the_token_and_the_call_fills_it(self) -> None:
        # The description is built outside any run. It used to resolve the root there
        # and say "(none right now)", so the agent was told it had nowhere to write.
        from world.context import reset_current_task, set_current_task
        from world.toolkit import make_write_report

        home = _home()
        mine = _make(home, slug="mine")
        write = make_write_report(home, ("tasks/{task}/work",))
        self.assertIn("tasks/{task}/work/", write.description)
        self.assertNotIn("none right now", write.description)

        token = set_current_task(mine.id)
        try:
            ok = await write.handler({"path": "tasks/{task}/work/note.md", "content": "x"})
            self.assertFalse(ok["is_error"], ok)
            self.assertTrue((home / "tasks" / mine.id / "work" / "note.md").is_file())
        finally:
            reset_current_task(token)

    async def test_outside_a_run_the_root_grants_nothing(self) -> None:
        from world.toolkit import make_write_report

        home = _home()
        made = _make(home)
        write = make_write_report(home, ("tasks/{task}/work",))
        denied = await write.handler(
            {"path": f"tasks/{made.id}/work/note.md", "content": "x"}
        )
        self.assertTrue(denied["is_error"])
        literal = await write.handler({"path": "tasks/{task}/work/note.md", "content": "x"})
        self.assertTrue(literal["is_error"])


class AskTests(unittest.IsolatedAsyncioTestCase):
    """What backlog/ became: a task with no agent that the scheduler never claims."""

    def _tool(self, home, **kw):
        from world.toolkit import make_write_ask

        return make_write_ask(home, **kw)

    async def test_an_ask_is_parked_not_queued(self) -> None:
        home = _home()
        write = self._tool(home, thread_ts="T1")
        out = await write.handler(
            {"slug": "cron-via-slack", "status": "open", "body": "# cron\n\n요청 원문"}
        )
        self.assertFalse(out["is_error"])
        [record] = tasks.list_tasks(home)
        self.assertEqual(tasks.ASK_TYPE, record.type)
        # waiting, and with no agent — the scheduler only ever claims `queued`.
        self.assertEqual("waiting", record.status)
        self.assertEqual("", record.agent)
        self.assertEqual("T1", record.thread)
        self.assertFalse(tasks.ready(home, record, [])[0])

    async def test_the_same_slug_updates_instead_of_piling_up(self) -> None:
        home = _home()
        write = self._tool(home)
        await write.handler({"slug": "same", "status": "open", "body": "# 처음"})
        await write.handler({"slug": "same", "status": "open", "body": "# 고쳐씀"})
        self.assertEqual(1, len(tasks.list_tasks(home)))
        self.assertIn("고쳐씀", tasks.list_tasks(home)[0].body)

    async def test_done_is_earned_by_a_recorded_resolution(self) -> None:
        home = _home()
        write = self._tool(home, thread_ts="T1")
        await write.handler({"slug": "built", "status": "open", "body": "# 요청"})
        await write.handler({"slug": "built", "status": "done", "body": "# 요청"})
        [record] = tasks.list_tasks(home)
        self.assertEqual("done", record.status)
        kinds = [e["kind"] for e in tasks.read_events(home, record.id)]
        # Not assigned: a resolution was recorded, then the predicate verified it.
        self.assertIn("resolved", kinds)
        self.assertLess(kinds.index("resolved"), kinds.index("verify"))

    async def test_dropped_stays_on_the_record(self) -> None:
        home = _home()
        write = self._tool(home)
        await write.handler({"slug": "nope", "status": "open", "body": "# 요청"})
        await write.handler({"slug": "nope", "status": "dropped", "body": "# 요청"})
        self.assertEqual("dropped", tasks.list_tasks(home)[0].status)
        # A settled slug is not reanimated — asking again opens a new one.
        self.assertIsNone(tasks.find_by_slug(home, "nope", type=tasks.ASK_TYPE))

    async def test_bad_input_is_refused_with_a_usable_message(self) -> None:
        home = _home()
        write = self._tool(home)
        for args in (
            {"slug": "Not A Slug", "status": "open", "body": "x"},
            {"slug": "ok-slug", "status": "무엇", "body": "x"},
            {"slug": "ok-slug", "status": "open", "body": "   "},
        ):
            out = await write.handler(args)
            self.assertTrue(out["is_error"], args)
        self.assertEqual([], tasks.list_tasks(home))
