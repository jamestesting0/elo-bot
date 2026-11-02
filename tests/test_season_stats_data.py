import unittest
from datetime import date
from types import SimpleNamespace

from common.matches.season_stats import (
    SeasonStatsError,
    SeasonWindow,
    StatsAccumulator,
    season_windows,
)
from tests._season_stats_fixture import row, timestamp


class TestSeasonStatsCalculations(unittest.TestCase):
    def setUp(self):
        self.start = timestamp("2026-01-01")
        self.end = timestamp("2026-01-08")
        self.window = SeasonWindow("3", self.start, self.end)

    def calculate(self, *records):
        accumulator = StatsAccumulator()
        for record in records:
            accumulator.add(record, self.window)
        return accumulator.finish(self.window)

    def test_current_first_with_archive_number_gaps(self):
        # Given: out-of-order archives with a missing season number.
        archives = [SimpleNamespace(season_number=3, start=30, end=40),
                    SimpleNamespace(season_number=1, start=10, end=20)]
        # When: page scopes are built at a fixed read time.
        windows = season_windows(archives, 50)
        # Then: current follows the latest archived end and every archive is reachable.
        self.assertEqual([(w.key, w.start, w.end) for w in windows],
                         [("current", 40, 50), ("3", 30, 40), ("1", 10, 20)])

    def test_first_current_season_has_unknown_start(self):
        # Given / When: no reset has recorded a season boundary.
        window = season_windows([], self.end)[0]
        # Then: an unknown official start stays unknown.
        self.assertIsNone(window.start)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", self.start), window)
        self.assertIsNone(accumulator.finish(window).daily_average)

    def test_invalid_archived_period_is_a_load_failure(self):
        # Given / When / Then: corrupt archived dates are rejected rather than replaced.
        with self.assertRaises(SeasonStatsError):
            season_windows([SimpleNamespace(season_number=1, start=20, end=10)], 50)

    def test_strict_boundaries_and_match_type_split(self):
        # Given: records exactly on both boundaries and two eligible records.
        records = [row("0", self.start), row("1", self.start + 1),
                   row("2", self.end - 1, is_challenge=True), row("3", self.end)]
        # When: the season is summarized.
        stats = self.calculate(*records)
        # Then: each eligible match contributes once and types partition the total.
        self.assertEqual((stats.matches, stats.challenges, stats.active_players), (2, 1, 2))

    def test_districts_are_counted_once_per_match_not_per_player(self):
        # Given: the same three districts occur in both players' result maps.
        records = [row("1", self.start + 1, ("Capital Peak", "Wizard Valley", "Dragon Cliffs")),
                   row("2", self.start + 2, ("Wizard Valley",), is_challenge=True)]
        # When: district usage is summarized.
        stats = self.calculate(*records)
        # Then: each district contributes once per match, sorted most played first.
        self.assertEqual(stats.districts[:3], (("Wizard Valley", 2), ("Capital Peak", 1), ("Dragon Cliffs", 1)))
        self.assertEqual(dict(stats.districts)["Goblin Mines"], 0)

    def test_missing_one_district_map_uses_the_persisted_other_side(self):
        # Given: only one participant has persisted district details.
        record = row("1", self.start + 1, players={"100": {}, "200": {
            "districts": {"Capital Peak": {"stars": 3, "destruction": 100}}}})
        # When: the match is counted.
        stats = self.calculate(record)
        # Then: known details count once without guessing the missing side.
        self.assertEqual((dict(stats.districts)["Capital Peak"], stats.missing_districts), (1, 0))

    def test_unknown_district_details_are_reported(self):
        # Given / When: an old recorded match has no district details.
        stats = self.calculate(row("1", self.start + 1))
        # Then: the match remains counted and missing coverage is explicit.
        self.assertEqual((stats.matches, stats.missing_districts), (1, 1))
        self.assertTrue(all(count == 0 for _, count in stats.districts))

    def test_historical_extra_district_is_retained(self):
        # Given / When: a historical district is absent from current choices.
        stats = self.calculate(row("1", self.start + 1, ("Old District",)))
        # Then: stored usage is not silently discarded.
        self.assertEqual(stats.districts[0], ("Old District", 1))

    def test_calendar_weeks_and_peak_ties_across_new_year(self):
        # Given: two equally busy UTC weeks crossing a year and Sunday/Monday boundary.
        window = SeasonWindow("1", timestamp("2025-12-28"), timestamp("2026-01-06"))
        accumulator = StatsAccumulator()
        for index, day in enumerate(("2025-12-29", "2025-12-29", "2026-01-05", "2026-01-05")):
            accumulator.add(row(str(index), timestamp(day) + 1), window)
        # When: peaks are chosen.
        stats = accumulator.finish(window)
        # Then: earliest tied days/weeks are shown with their tie count.
        self.assertEqual((stats.day.start, stats.day.count, stats.day.ties), (date(2025, 12, 29), 2, 2))
        self.assertEqual((stats.week.start, stats.week.count, stats.week.ties), (date(2025, 12, 29), 2, 2))

    def test_daily_average_includes_inactive_days(self):
        # Given / When: two matches in a seven-calendar-day period.
        stats = self.calculate(row("1", self.start + 1), row("2", self.end - 1))
        # Then: inactive days remain in the denominator.
        self.assertEqual(stats.active_days, 2)
        self.assertAlmostEqual(stats.daily_average, 2 / 7)

    def test_empty_and_future_current_periods_do_not_divide_by_zero(self):
        # Given: a current start later than the captured read time.
        accumulator = StatsAccumulator()
        # When: an empty future window is summarized.
        stats = accumulator.finish(SeasonWindow("current", self.end, self.start))
        # Then: counts and pace are zero; no peak/player is invented.
        self.assertEqual((stats.matches, stats.daily_average), (0, 0))
        self.assertIsNone(stats.day)
        self.assertIsNone(stats.leader)

    def test_player_peak_ties_are_deterministic(self):
        # Given / When: both participants have one match.
        stats = self.calculate(row("1", self.start + 1))
        # Then: the stable player ID and number of tied players are shown.
        self.assertEqual((stats.leader.user_id, stats.leader.count, stats.leader.ties), ("100", 1, 2))

    def test_sunday_and_monday_are_in_different_calendar_weeks(self):
        window = SeasonWindow("1", timestamp("2026-01-01"), timestamp("2026-01-06"))
        accumulator = StatsAccumulator()
        accumulator.add(row("1", timestamp("2026-01-04") + 1), window)
        accumulator.add(row("2", timestamp("2026-01-05") + 1), window)
        stats = accumulator.finish(window)
        self.assertEqual((stats.week.start, stats.week.count, stats.week.ties), (date(2025, 12, 29), 1, 2))

    def test_subday_period_has_one_calendar_day_of_activity(self):
        window = SeasonWindow("1", self.start + 3600, self.start + 7200)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", self.start + 4000), window)
        self.assertEqual(accumulator.finish(window).daily_average, 1)
