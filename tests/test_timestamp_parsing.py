import unittest
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from elo_bot.core.timestamp_dates import TimestampInputError
from elo_bot.core.timestamps import resolve_timestamp
from elo_bot.features.timestamps.ui import submitted_candidates, timestamp_choices


BERLIN = ZoneInfo("Europe/Berlin")
MONDAY = datetime(2026, 11, 16, 14, tzinfo=UTC)


class TestTimestampParsing(unittest.TestCase):
    def test_english_phrases_resolve_against_local_calendar(self):
        examples = {
            "19 in 2 days": datetime(2026, 11, 18, 18, tzinfo=UTC),
            "in two days at 7pm": datetime(2026, 11, 18, 18, tzinfo=UTC),
            "in two hours": datetime(2026, 11, 16, 16, tzinfo=UTC),
            "two hours from now": datetime(2026, 11, 16, 16, tzinfo=UTC),
            "30 minutes from now": datetime(2026, 11, 16, 14, 30, tzinfo=UTC),
            "in 1h 30m": datetime(2026, 11, 16, 15, 30, tzinfo=UTC),
            "2hrs": datetime(2026, 11, 16, 16, tzinfo=UTC),
            "in half an hour": datetime(2026, 11, 16, 14, 30, tzinfo=UTC),
            "next Thursday at 19": datetime(2026, 11, 26, 18, tzinfo=UTC),
            "Thursday next week at 7pm": datetime(2026, 11, 26, 18, tzinfo=UTC),
            "this Thursday at midnight": datetime(2026, 11, 18, 23, tzinfo=UTC),
            "tomorrow at noon": datetime(2026, 11, 17, 11, tzinfo=UTC),
            "tmrw at noon": datetime(2026, 11, 17, 11, tzinfo=UTC),
            "tmr 7pm": datetime(2026, 11, 17, 18, tzinfo=UTC),
            "tmrw @ 7pm": datetime(2026, 11, 17, 18, tzinfo=UTC),
            "7pm tomorrow": datetime(2026, 11, 17, 18, tzinfo=UTC),
            "19 the day after tomorrow": datetime(2026, 11, 18, 18, tzinfo=UTC),
            "2026-11-22T19:30": datetime(2026, 11, 22, 18, 30, tzinfo=UTC),
            "22 November at 19:30": datetime(2026, 11, 22, 18, 30, tzinfo=UTC),
        }
        for phrase, expected in examples.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve_timestamp(phrase, MONDAY, BERLIN), (expected,))

    def test_weekday_and_numeric_date_choices_keep_each_plausible_date(self):
        self.assertEqual(resolve_timestamp("Thursday", MONDAY, BERLIN), (
            date(2026, 11, 19), date(2026, 11, 26), date(2026, 12, 3), date(2026, 12, 10),
            date(2026, 12, 17), date(2026, 12, 24), date(2026, 12, 31),
        ))
        self.assertEqual(resolve_timestamp("03/04/2027", MONDAY, BERLIN), (date(2027, 3, 4), date(2027, 4, 3)))
        self.assertEqual(resolve_timestamp("tomorrow at 7", MONDAY, BERLIN), (
            datetime(2026, 11, 17, 6, tzinfo=UTC), datetime(2026, 11, 17, 18, tzinfo=UTC),
        ))

    def test_calendar_days_preserve_local_time_across_spring_clock_change(self):
        before_change = datetime(2026, 3, 27, 18, tzinfo=UTC)
        self.assertEqual(resolve_timestamp("in 2 days", before_change, BERLIN), (
            datetime(2026, 3, 29, 17, tzinfo=UTC),
        ))
        self.assertEqual(resolve_timestamp("in 48 hours", before_change, BERLIN), (
            datetime(2026, 3, 29, 18, tzinfo=UTC),
        ))
        with self.assertRaises(TimestampInputError):
            resolve_timestamp("2026-03-29 at 02:30", before_change, BERLIN)

    def test_clock_fold_options_have_distinct_labels_and_freeze_selected_moment(self):
        choices = timestamp_choices("2026-10-25 at 02:30", MONDAY, BERLIN)
        self.assertEqual(len(choices), 2)
        self.assertIn("UTC+02:00", str(choices[0].name))
        self.assertIn("UTC+01:00", str(choices[1].name))
        self.assertEqual(submitted_candidates(str(choices[1].value), MONDAY, BERLIN), (
            datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        ))
        with self.assertRaises(TimestampInputError):
            submitted_candidates(str(choices[1].value), MONDAY, ZoneInfo("America/Los_Angeles"))

    def test_partial_clock_and_duration_offer_resolved_calendar_completions(self):
        choices = timestamp_choices("19 in 2", MONDAY, BERLIN)
        days = [submitted_candidates(str(choice.value), MONDAY, BERLIN) for choice in choices]
        self.assertEqual(days, [(datetime(*day, 18, tzinfo=UTC),)
                                for day in ((2026, 11, 18), (2026, 11, 30), (2027, 1, 16))])

    def test_clock_suffixes_keep_am_and_pm_choices_until_disambiguated(self):
        morning = datetime(2026, 11, 17, 1, tzinfo=UTC)
        afternoon = datetime(2026, 11, 17, 13, tzinfo=UTC)
        cases = {
            "tmrw 2c": (morning, afternoon),
            "tmrw 2 o'clock": (morning, afternoon),
            "tmrw 2 o’clock": (morning, afternoon),
            "tmrw 2oclock": (morning, afternoon),
            "2 o clock tomorrow": (morning, afternoon),
            "tmrw 2a": (morning,),
            "tmrw at 2p": (afternoon,),
            "tmrw 2 p.m.": (afternoon,),
            "tmrw 2 o’clock pm": (afternoon,),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                choices = timestamp_choices(phrase, MONDAY, BERLIN)
                self.assertEqual(tuple(submitted_candidates(choice.value, MONDAY, BERLIN)[0] for choice in choices),
                                 expected)
                self.assertEqual(resolve_timestamp(phrase, MONDAY, BERLIN), expected)
        partial = timestamp_choices("tmrw 2 o’cl", MONDAY, BERLIN)
        self.assertEqual(tuple(submitted_candidates(choice.value, MONDAY, BERLIN)[0] for choice in partial),
                         (morning, afternoon))

    def test_corrected_suggestions_retain_the_intended_date_time_and_units(self):
        for phrase, correction, expected in (
            ("day 1", "in 1 day", datetime(2026, 11, 17, 14, tzinfo=UTC)),
            ("tommorow at 19", "tomorrow at 19", datetime(2026, 11, 17, 18, tzinfo=UTC)),
            ("next firday at 7pm", "next friday at 7pm", datetime(2026, 11, 27, 18, tzinfo=UTC)),
            ("in 2 houres", "in 2 hours", datetime(2026, 11, 16, 16, tzinfo=UTC)),
        ):
            with self.subTest(phrase=phrase):
                choices = timestamp_choices(phrase, MONDAY, BERLIN)
                self.assertEqual(len(choices), 1)
                self.assertIn(correction, str(choices[0].name).lower())
                self.assertEqual(submitted_candidates(str(choices[0].value), MONDAY, BERLIN), (expected,))

    def test_unknown_words_and_invalid_numeric_times_are_not_autocorrected(self):
        for phrase in ("xyzzy at 19", "tmrw at 29:30", "2026-11-31 at 19", "firday at 29:30"):
            with self.subTest(phrase=phrase):
                self.assertEqual(timestamp_choices(phrase, MONDAY, BERLIN), [])

    def test_calendar_periods_keep_all_plausible_days_and_accept_explicit_dates_and_times(self):
        next_week = tuple(date(2026, 11, day) for day in range(23, 30))
        cases = {
            "next day": (date(2026, 11, 17),),
            "the next day at 18": (datetime(2026, 11, 17, 17, tzinfo=UTC),),
            "previous day": (date(2026, 11, 15),),
            "next week": next_week,
            "18:00 next week": tuple(datetime(2026, 11, day, 17, tzinfo=UTC) for day in range(23, 30)),
            "next week on friday": (date(2026, 11, 27),),
            "this weekend": (date(2026, 11, 21), date(2026, 11, 22)),
            "next month": tuple(date(2026, 12, day) for day in range(1, 26)),
            "the 28th of next month at 19": (datetime(2026, 12, 28, 18, tzinfo=UTC),),
            "end of next month": (date(2026, 12, 31),),
            "first day of next year": (date(2027, 1, 1),),
            "one day": (datetime(2026, 11, 17, 14, tzinfo=UTC),),
            "now": (MONDAY,),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve_timestamp(phrase, MONDAY, BERLIN), expected)

    def test_period_completions_cover_year_and_dates_before_or_after_clock_changes(self):
        year = timestamp_choices("next year", MONDAY, BERLIN)
        labels = [str(choice.name).lower() for choice in year]
        self.assertEqual(sum("next year" in label for label in labels), 2)
        self.assertTrue(any("1 jan 2027" in label for label in labels))
        self.assertTrue(any("31 dec 2027" in label for label in labels))
        december = datetime(2026, 12, 30, 8, tzinfo=UTC)
        self.assertEqual(resolve_timestamp("next week", december, BERLIN),
                         tuple(date(2027, 1, day) for day in range(4, 11)))
        for phrase in ("next we", "next weak", "next wek"):
            with self.subTest(phrase=phrase):
                options = timestamp_choices(phrase, MONDAY, BERLIN)
                self.assertEqual(tuple(submitted_candidates(option.value, MONDAY, BERLIN)[0] for option in options),
                                 tuple(date(2026, 11, day) for day in range(23, 30)))
