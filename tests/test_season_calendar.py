import unittest
from datetime import UTC, datetime

from elo_bot.core.season_calendar import season_calendar

OCTOBER_RESET = datetime(2026, 10, 4, 10, 34, tzinfo=UTC)
DECEMBER = datetime(2026, 12, 7, 7, tzinfo=UTC)
FEBRUARY = datetime(2027, 2, 1, 7, tzinfo=UTC)


class TestSeasonCalendar(unittest.TestCase):
    def test_dates_follow_the_first_monday_after_the_last_reset(self):
        calendar = season_calendar(datetime(2026, 11, 19, tzinfo=UTC), OCTOBER_RESET, None)
        self.assertEqual(calendar.season_ends, DECEMBER)
        self.assertEqual(calendar.season_ends.weekday(), 0)
        self.assertEqual((calendar.queue_closes.weekday(), calendar.final_selection.weekday()), (4, 4))
        self.assertEqual((DECEMBER - calendar.queue_closes).days, 3)
        self.assertEqual((DECEMBER - calendar.final_selection).days, 10)

    def test_a_reset_a_day_early_or_late_opens_the_same_season(self):
        for reset in (datetime(2026, 10, 4, 10, tzinfo=UTC), datetime(2026, 10, 6, 10, tzinfo=UTC)):
            with self.subTest(reset=reset):
                self.assertEqual(season_calendar(datetime(2026, 11, 1, tzinfo=UTC), reset, None).season_ends, DECEMBER)

    def test_season_runs_on_until_its_final_starts(self):
        for now, final_started, ends in (
            (datetime(2026, 11, 30, 6, 59, tzinfo=UTC), None, DECEMBER),
            (datetime(2026, 11, 30, 7, 1, tzinfo=UTC), None, FEBRUARY),
            (datetime(2026, 12, 20, tzinfo=UTC), datetime(2026, 11, 27, 8, tzinfo=UTC), DECEMBER),
            (datetime(2026, 12, 20, tzinfo=UTC), datetime(2026, 12, 15, tzinfo=UTC), FEBRUARY),
        ):
            with self.subTest(now=now, final_started=final_started):
                self.assertEqual(season_calendar(now, OCTOBER_RESET, final_started).season_ends, ends)
