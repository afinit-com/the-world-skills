"""Schedule.daily(weekdays=...) and AnySchedule."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from world import AnySchedule, Schedule, ScheduleTrigger
from world.process import heartbeat
from world.process.orchestrator import _due_signal
from world.types import Agent
from world.world_map import _runs

WEEKDAYS = range(5)  # Mon-Fri


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


class WeekdayScheduleTests(unittest.TestCase):
    # 2026-07-13 is a Monday.
    def test_a_weekend_looks_back_to_friday(self) -> None:
        schedule = Schedule.daily(9, 0, "UTC", weekdays=WEEKDAYS)
        self.assertEqual("2026-07-17T09:00:00+00:00",   # Friday
                         schedule.previous_occurrence(utc(2026, 7, 19, 12, 0)).isoformat())
        self.assertEqual("2026-07-17T09:00:00+00:00",
                         schedule.previous_occurrence(utc(2026, 7, 18, 9, 0)).isoformat())

    def test_a_weekday_before_its_time_looks_back_one_working_day(self) -> None:
        schedule = Schedule.daily(9, 0, "UTC", weekdays=WEEKDAYS)
        self.assertEqual("2026-07-10T09:00:00+00:00",   # Monday 08:59 -> last Friday
                         schedule.previous_occurrence(utc(2026, 7, 13, 8, 59)).isoformat())
        self.assertEqual("2026-07-13T09:00:00+00:00",
                         schedule.previous_occurrence(utc(2026, 7, 13, 9, 0)).isoformat())

    def test_next_skips_the_weekend_and_is_strictly_after_now(self) -> None:
        schedule = Schedule.daily(9, 0, "UTC", weekdays=WEEKDAYS)
        self.assertEqual("2026-07-20T09:00:00+00:00",   # Friday 09:00 exactly -> Monday
                         schedule.next_occurrence(utc(2026, 7, 17, 9, 0)).isoformat())
        self.assertEqual("2026-07-20T09:00:00+00:00",   # Sunday -> Monday, not Saturday
                         schedule.next_occurrence(utc(2026, 7, 19, 12, 0)).isoformat())
        self.assertEqual("2026-07-14T09:00:00+00:00",
                         schedule.next_occurrence(utc(2026, 7, 13, 10, 0)).isoformat())

    def test_the_weekday_is_the_schedules_zone_not_utc(self) -> None:
        seoul = Schedule.daily(9, 0, "Asia/Seoul", weekdays=WEEKDAYS)
        # Sunday 23:30 UTC is Monday 08:30 in Seoul: Monday's run has not happened.
        self.assertEqual("2026-07-10T09:00:00+09:00",
                         seoul.previous_occurrence(utc(2026, 7, 12, 23, 30)).isoformat())
        # Monday 00:30 UTC is Monday 09:30 in Seoul.
        self.assertEqual("2026-07-13T09:00:00+09:00",
                         seoul.previous_occurrence(utc(2026, 7, 13, 0, 30)).isoformat())
        west = Schedule.daily(18, 0, "America/Los_Angeles", weekdays=WEEKDAYS)
        # Saturday 02:00 UTC is still Friday 19:00 in Los Angeles: Friday's run counts.
        self.assertEqual("2026-07-17T18:00:00-07:00",
                         west.previous_occurrence(utc(2026, 7, 18, 2, 0)).isoformat())
        self.assertEqual("2026-07-20T18:00:00-07:00",
                         west.next_occurrence(utc(2026, 7, 18, 2, 0)).isoformat())

    def test_a_single_day_and_a_split_week(self) -> None:
        split = Schedule.daily(7, 30, "UTC", weekdays=[4, 0, 2, 0])   # Mon, Wed, Fri
        self.assertEqual((0, 2, 4), split.weekdays)
        self.assertEqual("2026-07-15T07:30:00+00:00",   # Thursday -> Wednesday
                         split.previous_occurrence(utc(2026, 7, 16, 12, 0)).isoformat())
        self.assertEqual("2026-07-17T07:30:00+00:00",
                         split.next_occurrence(utc(2026, 7, 16, 12, 0)).isoformat())

    def test_without_weekdays_daily_is_every_day(self) -> None:
        self.assertEqual((), Schedule.daily(9).weekdays)
        self.assertEqual(Schedule("daily", 9), Schedule.daily(9))
        self.assertEqual("2026-07-19T09:00:00+00:00",   # a Sunday
                         Schedule.daily(9).previous_occurrence(utc(2026, 7, 19, 12, 0)).isoformat())

    def test_describe_names_the_days(self) -> None:
        self.assertEqual("daily Mon-Fri 09:00 Asia/Seoul",
                         Schedule.daily(9, 0, "Asia/Seoul", weekdays=WEEKDAYS).describe())
        self.assertEqual("daily Mon,Wed,Fri 07:30 UTC",
                         Schedule.daily(7, 30, weekdays=(0, 2, 4)).describe())
        self.assertEqual("daily Sat-Sun 10:00 UTC",
                         Schedule.daily(10, weekdays=(5, 6)).describe())
        self.assertEqual("daily Tue 10:00 UTC", Schedule.daily(10, weekdays=(1,)).describe())

    def test_bad_weekdays_are_refused_when_declared(self) -> None:
        with self.assertRaises(ValueError):
            Schedule.daily(9, weekdays=())          # would never run
        with self.assertRaises(ValueError):
            Schedule.daily(9, weekdays=(7,))
        with self.assertRaises(ValueError):
            Schedule("weekly", 9, weekday=0, weekdays=(0, 1))

    def test_a_weekday_schedule_does_not_fire_on_a_weekend(self) -> None:
        agent = Agent("reminder", ScheduleTrigger(Schedule.daily(9, 0, "UTC", weekdays=WEEKDAYS)))
        friday_run = {"last_fired": utc(2026, 7, 17, 9, 0, 5).timestamp()}
        self.assertIsNone(_due_signal(agent, friday_run, utc(2026, 7, 18, 9, 1).timestamp()))
        self.assertIsNone(_due_signal(agent, friday_run, utc(2026, 7, 19, 23, 0).timestamp()))
        self.assertIsNotNone(_due_signal(agent, friday_run, utc(2026, 7, 20, 9, 0).timestamp()))


class AnyScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.twice = AnySchedule(Schedule.daily(9, 0, "UTC"), Schedule.daily(18, 0, "UTC"))

    def test_previous_is_the_latest_part_and_next_the_earliest(self) -> None:
        noon = utc(2026, 7, 13, 12, 0)
        self.assertEqual("2026-07-13T09:00:00+00:00",
                         self.twice.previous_occurrence(noon).isoformat())
        self.assertEqual("2026-07-13T18:00:00+00:00", self.twice.next_occurrence(noon).isoformat())
        night = utc(2026, 7, 13, 20, 0)
        self.assertEqual("2026-07-13T18:00:00+00:00",
                         self.twice.previous_occurrence(night).isoformat())
        self.assertEqual("2026-07-14T09:00:00+00:00", self.twice.next_occurrence(night).isoformat())

    def test_parts_in_different_zones_compare_as_instants(self) -> None:
        mixed = AnySchedule(Schedule.daily(9, 0, "Asia/Seoul"), Schedule.daily(9, 0, "UTC"))
        now = utc(2026, 7, 13, 5, 0)    # Seoul's 09:00 was 00:00 UTC; UTC's is still ahead
        self.assertEqual("2026-07-13T09:00:00+09:00", mixed.previous_occurrence(now).isoformat())
        self.assertEqual("2026-07-13T09:00:00+00:00", mixed.next_occurrence(now).isoformat())

    def test_parts_may_mix_kinds_and_weekdays(self) -> None:
        mixed = AnySchedule(
            Schedule.daily(9, 0, "UTC", weekdays=WEEKDAYS), Schedule.weekly(5, 12, 0, "UTC"))
        sunday = utc(2026, 7, 19, 8, 0)
        self.assertEqual("2026-07-18T12:00:00+00:00",   # Saturday's weekly part
                         mixed.previous_occurrence(sunday).isoformat())
        self.assertEqual("2026-07-20T09:00:00+00:00", mixed.next_occurrence(sunday).isoformat())

    def test_describe_lists_every_part(self) -> None:
        self.assertEqual("daily 09:00 UTC + daily 18:00 UTC", self.twice.describe())

    def test_nested_parts_flatten_and_equal_schedules_compare_equal(self) -> None:
        nested = AnySchedule(Schedule.daily(9, 0, "UTC"), AnySchedule(Schedule.daily(18, 0, "UTC")))
        self.assertEqual(self.twice, nested)
        self.assertEqual(hash(self.twice), hash(nested))

    def test_it_needs_at_least_one_schedule(self) -> None:
        with self.assertRaises(ValueError):
            AnySchedule()
        with self.assertRaises(TypeError):
            AnySchedule("daily 09:00")

    def test_each_time_fires_once_and_downtime_catches_up_once(self) -> None:
        agent = Agent("digest", ScheduleTrigger(self.twice))
        state = {"last_fired": utc(2026, 7, 13, 8, 0).timestamp()}

        morning = _due_signal(agent, state, utc(2026, 7, 13, 9, 0, 3).timestamp())
        self.assertIsNotNone(morning)
        morning.on_done(None)
        self.assertIsNone(_due_signal(agent, state, utc(2026, 7, 13, 17, 59).timestamp()))

        evening = _due_signal(agent, state, utc(2026, 7, 13, 18, 0, 3).timestamp())
        self.assertIsNotNone(evening)
        evening.on_done(None)
        self.assertIsNone(_due_signal(agent, state, utc(2026, 7, 13, 23, 0).timestamp()))

        # Down across three occurrences: one run for the latest, not three.
        late = _due_signal(agent, state, utc(2026, 7, 15, 10, 0).timestamp())
        self.assertIsNotNone(late)
        late.on_done(None)
        self.assertIsNone(_due_signal(agent, state, utc(2026, 7, 15, 10, 1).timestamp()))


class ScheduleDisplayTests(unittest.TestCase):
    def test_the_heartbeat_cadence_reads_either_kind(self) -> None:
        now = utc(2026, 7, 18, 12, 0).timestamp()   # a Saturday
        self.assertEqual(
            "daily Mon-Fri 09:00 UTC (next 2026-07-20 09:00)",
            heartbeat._cadence(Schedule.daily(9, 0, "UTC", weekdays=WEEKDAYS), now))
        self.assertEqual(
            "daily 09:00 UTC + daily 18:00 UTC (next 2026-07-18 18:00)",
            heartbeat._cadence(
                AnySchedule(Schedule.daily(9, 0, "UTC"), Schedule.daily(18, 0, "UTC")), now))

    def test_the_world_map_reads_either_kind(self) -> None:
        def runs(schedule) -> str:
            return _runs(ScheduleTrigger(schedule))

        self.assertEqual("매일 09:00 (UTC)", runs(Schedule.daily(9)))
        self.assertEqual("매주 월~금요일 09:00 (UTC)", runs(Schedule.daily(9, weekdays=WEEKDAYS)))
        self.assertEqual("매주 월·수요일 09:00 (UTC)", runs(Schedule.daily(9, weekdays=(0, 2))))
        self.assertEqual("매주 월요일 09:00 (UTC)", runs(Schedule.weekly(0, 9)))
        self.assertEqual(
            "매일 09:00 (UTC) / 매월 1일 08:00 (UTC)",
            runs(AnySchedule(Schedule.daily(9), Schedule.monthly(1, 8))))


class DryRunNextRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_not_due_line_names_the_zone_of_the_next_part(self) -> None:
        from world.process import orchestrator

        logged: list[str] = []
        harness = SimpleNamespace(log=logged.append)
        agent = Agent("digest", ScheduleTrigger(
            AnySchedule(Schedule.daily(9, 0, "Asia/Seoul"), Schedule.daily(9, 0, "UTC"))))
        now = utc(2026, 7, 13, 5, 0).timestamp()
        state = {"agents": {"digest": {"last_fired": now}}}

        made = await orchestrator._enqueue_due_agent(
            harness, state, agent, now, dry_run=True, scheduler=None)

        self.assertFalse(made)
        self.assertEqual(["[dry-run] digest not due — next run 2026-07-13 09:00 UTC"], logged)


if __name__ == "__main__":
    unittest.main()
