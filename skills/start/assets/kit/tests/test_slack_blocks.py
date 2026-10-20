"""world/slack/blocks.py and the Block Kit every post now carries.

A harness posted a line like "좋아, 승인 받았다. reports/r12.csv 반영, 모델 … v1
push. 창이 열리면 바로 배포한다." — four facts in one run-on line, because a post was one
mrkdwn string. These pin the builders' structure and Slack's limits (3000 characters a
section, 2000 a field, 10 fields, 150 a header, 50 blocks a message), and that post()
renders plain text into sections without losing what it used to keep.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from world.slack import blocks as bk
from world.types import Button, Persona


def kinds(blocks):
    return [b["type"] for b in blocks]


class CardTests(unittest.TestCase):
    def test_a_card_is_headline_facts_body_then_context(self):
        text, blocks = bk.card(
            "✅ 승인 받음 — r12 배포 대기",
            fields={"결과 파일": "`reports/r12.csv`", "모델": "`model-r12` v1"},
            bullets=["모델 push 완료", "창이 열리면 바로 배포"],
            body="세부 사항은 스레드에서.",
            context="monitor · 다음 확인 10분 뒤",
            buttons=[bk.link_button("결과 열기", "https://example.com")],
        )
        self.assertEqual(["header", "section", "section", "divider", "section", "actions",
                          "divider", "context"], kinds(blocks))
        self.assertEqual("✅ 승인 받음 — r12 배포 대기", blocks[0]["text"]["text"])
        self.assertEqual("*결과 파일*\n`reports/r12.csv`", blocks[1]["fields"][0]["text"])
        self.assertEqual("• 모델 push 완료\n• 창이 열리면 바로 배포", blocks[2]["text"]["text"])
        # The fallback says the same facts, one per line, for notifications.
        self.assertEqual(
            "✅ 승인 받음 — r12 배포 대기\n결과 파일: `reports/r12.csv`\n"
            "모델: `model-r12` v1\n• 모델 push 완료\n• 창이 열리면 바로 배포\n"
            "세부 사항은 스레드에서.\nmonitor · 다음 확인 10분 뒤", text)

    def test_a_bold_headline_keeps_mrkdwn(self):
        _, blocks = bk.card("<@U1> `r12` 승인 필요", headline="bold")
        self.assertEqual([{"type": "section", "text": {"type": "mrkdwn",
                                                       "text": "<@U1> `r12` 승인 필요"}}], blocks)
        _, blocks = bk.card("배포 완료", headline="bold")
        self.assertEqual("*배포 완료*", blocks[0]["text"]["text"])

    def test_limits_are_kept(self):
        _, blocks = bk.card("x" * 400, fields=[(f"k{i}", "v" * 2500) for i in range(12)],
                            context=[f"c{i}" for i in range(14)])
        self.assertLessEqual(len(blocks[0]["text"]["text"]), bk.HEADER_CHARS)
        sections = [b for b in blocks if "fields" in b]
        self.assertEqual([10, 2], [len(b["fields"]) for b in sections])
        self.assertTrue(all(len(f["text"]) <= bk.FIELD_CHARS for b in sections for f in b["fields"]))
        self.assertEqual(10, len(blocks[-1]["elements"]))
        self.assertLessEqual(len(bk.section("y" * 5000)["text"]["text"]), bk.SECTION_CHARS)

    def test_empty_facts_are_left_out(self):
        _, blocks = bk.card("t", fields={"a": "", "b": None, "c": 0})
        self.assertEqual(["*c*\n0"], [f["text"] for f in blocks[1]["fields"]])

    def test_button_rules(self):
        link = bk.link_button("열기", "https://example.com")
        with self.assertRaisesRegex(ValueError, "at most 5"):
            bk.actions([link] * 6)
        with self.assertRaisesRegex(ValueError, "one primary"):
            bk.actions([bk.link_button("a", "https://a.io", style="primary"),
                        bk.link_button("b", "https://b.io", style="primary")])
        with self.assertRaisesRegex(ValueError, "only link buttons"):
            bk.actions([Button.ask("원인 더 파봐", "dig")])
        with self.assertRaisesRegex(ValueError, "label"):
            bk.actions([{"type": "button", "text": {"type": "plain_text", "text": "x" * 76}}])
        rendered = bk.actions([link, Button.link("b", "https://b.io")])
        self.assertEqual(["world_link_0", "world_link_1"],
                         [e["action_id"] for e in rendered["elements"]])
        with self.assertRaises(ValueError):
            bk.link_button("열기", "ftp://example.com")


class TextBlocksTests(unittest.TestCase):
    def test_a_reply_becomes_a_headline_and_one_section_per_group(self):
        blocks = bk.text_blocks("승인 받았다\n\n• `r12` 반영\n• 모델 v1 push\n\n"
                                "창이 열리면 바로 배포")
        self.assertEqual(["*승인 받았다*", "• `r12` 반영\n• 모델 v1 push",
                          "창이 열리면 바로 배포"], [b["text"]["text"] for b in blocks])

    def test_a_headline_with_markup_or_a_lone_line_is_left_as_written(self):
        self.assertEqual("*완료* 4건", bk.text_blocks("*완료* 4건\n자세히")[0]["text"]["text"])
        self.assertEqual([bk.section("한 줄뿐")], bk.text_blocks("한 줄뿐"))
        self.assertEqual("- a", bk.text_blocks("- a\n- b")[0]["text"]["text"][:3])
        self.assertEqual([], bk.text_blocks("  \n\n"))

    def test_a_heading_opens_a_new_group_with_a_divider(self):
        blocks = bk.text_blocks("결과\n\n요약 한 줄\n\n*다음 단계*\n배포")
        self.assertEqual(["section", "section", "divider", "section"], kinds(blocks))

    def test_a_code_block_stays_whole_and_a_long_one_stays_fenced(self):
        blocks = bk.text_blocks("로그\n\n```\na\n\nb\n```", headline=False)
        self.assertEqual("```\na\n\nb\n```", blocks[1]["text"]["text"])
        long = "```\n" + "\n".join(f"line {i} " * 20 for i in range(300)) + "\n```"
        pieces = [b["text"]["text"] for b in bk.text_blocks(long)]
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), bk.SECTION_CHARS)
            self.assertEqual(0, piece.count("```") % 2, piece[:40])

    def test_a_line_longer_than_a_section_is_cut_not_dropped(self):
        pieces = [b["text"]["text"] for b in bk.text_blocks("z" * 7000, headline=False)]
        self.assertEqual(7000, sum(len(p) for p in pieces))

    def test_split_messages_keeps_fifty_and_drops_leading_dividers(self):
        blocks = [bk.section(str(i)) for i in range(50)] + [bk.divider(), bk.section("50")]
        first, second = bk.split_messages(blocks)
        self.assertEqual(50, len(first))
        self.assertEqual([bk.section("50")], second)

    def test_plain_text_reads_sections_fields_and_context(self):
        _, blocks = bk.card("제목", fields={"상태": "ok"}, context="footer")
        self.assertEqual("제목\n*상태*: ok\nfooter", bk.plain_text(blocks))


class PostTests(unittest.IsolatedAsyncioTestCase):
    def poster(self):
        from world.slack.post import PostingMixin

        poster = PostingMixin()
        main = Persona(key="main", display="Main")
        poster.harness = SimpleNamespace(main_persona=main, personas={"main": main},
                                         log=lambda _t: None, slack=SimpleNamespace(channel="C1"))
        poster.identities = {}
        poster.web = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ts": "1.1"}))
        return poster

    def calls(self, poster):
        return [c.kwargs for c in poster.web.chat_postMessage.await_args_list]

    async def test_plain_text_is_posted_as_sections_with_the_text_as_fallback(self):
        poster = self.poster()
        reply = "승인 받았다\n\n• `r12` 반영\n• push 완료"
        footer = bk.context("⏱ 3s")
        await poster.post(reply, context=footer)
        [call] = self.calls(poster)
        self.assertEqual(reply, call["text"])
        self.assertEqual(["section", "section", "context"], kinds(call["blocks"]))
        self.assertEqual("*승인 받았다*", call["blocks"][0]["text"]["text"])

    async def test_a_card_is_posted_as_built(self):
        poster = self.poster()
        text, blocks = bk.card("배포 대기", fields={"파일": "`a.csv`"})
        await poster.post(text, blocks=blocks)
        [call] = self.calls(poster)
        self.assertEqual(blocks, call["blocks"])
        self.assertEqual(text, call["text"])

    async def test_buttons_alone_get_the_text_above_them(self):
        poster = self.poster()
        buttons = bk.actions([bk.link_button("열기", "https://example.com")])
        await poster.post("결과\n\n자세히는 링크", blocks=[buttons])
        [call] = self.calls(poster)
        self.assertEqual(["section", "section", "actions"], kinds(call["blocks"]))

    async def test_past_fifty_blocks_the_rest_follows_in_the_thread_with_the_footer_last(self):
        poster = self.poster()
        await poster.post("\n\n".join(f"p{i}" for i in range(70)), context=bk.context("f"),
                          thread_ts="9.9")
        first, second = self.calls(poster)
        self.assertEqual(50, len(first["blocks"]))
        self.assertNotIn("context", kinds(first["blocks"]))
        self.assertEqual("context", second["blocks"][-1]["type"])
        self.assertEqual("9.9", second["thread_ts"])
        self.assertTrue(second["text"].startswith("p50"))

    async def test_a_full_last_message_moves_one_block_on_for_the_footer(self):
        poster = self.poster()
        await poster.post("\n\n".join(f"p{i}" for i in range(50)), context=bk.context("f"))
        first, second = self.calls(poster)
        self.assertEqual(49, len(first["blocks"]))
        self.assertEqual(["section", "context"], kinds(second["blocks"]))

    async def test_plain_posts_text_alone_in_chunks(self):
        poster = self.poster()
        await poster.post("<@UFACE> " + "x" * 5000, plain=True)
        calls = self.calls(poster)
        self.assertEqual(2, len(calls))
        self.assertTrue(all("blocks" not in c for c in calls))
        self.assertTrue(calls[0]["text"].startswith("<@UFACE> "))


class TaskCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_task_card_is_header_fields_excerpt_evidence_and_buttons(self):
        from world.process import task as tasks
        from world.slack.mentions import MentionMixin
        from world.types import Agent, MentionTrigger, RunResult

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            record = tasks.create(home, type="agent-run", title="📋 아침 보고", agent="watch")
            tasks.append_event(home, record.id, "verify", verdict="done",
                               evidence=["watch 실행 완료"])
            mixin = MentionMixin()
            agent = Agent("watch", MentionTrigger(),
                          buttons=(Button.ask("원인 더 파봐", "dig"),))
            mixin.harness = SimpleNamespace(home=home, agents=[agent], run_footer=False,
                                            knowledge_proposal_agent=None)
            mixin.state = {"threads": {}}
            mixin.save_state = lambda: None
            mixin.post = AsyncMock(return_value="1.2")
            await mixin.announce_task(record, RunResult(status="ok", result_text="완료 4건\n실패 5건"),
                                      SimpleNamespace(key="main"))
            blocks = mixin.post.await_args.kwargs["blocks"]
            self.assertEqual(["header", "section", "divider", "section", "context", "actions"],
                             kinds(blocks))
            self.assertEqual("📋 아침 보고", blocks[0]["text"]["text"])
            self.assertEqual(["*작업*\n`" + record.id + "`", f"*상태*\n{record.status}",
                              "*담당*\nwatch"], [f["text"] for f in blocks[1]["fields"]])
            self.assertEqual("> 완료 4건\n> 실패 5건", blocks[3]["text"]["text"])
            self.assertEqual("근거 · watch 실행 완료", blocks[4]["elements"][0]["text"])


if __name__ == "__main__":
    unittest.main()
