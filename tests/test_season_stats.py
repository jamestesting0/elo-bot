import unittest
import re

from interactions.models.discord.message import process_message_payload
from tests._season_stats_fixture import row, timestamp

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.emojis import DISTRICT_EMOJI
from elo_bot.features.seasons.presentation import stats_controls, stats_embed
from elo_bot.features.seasons.stats import SeasonWindow, StatsAccumulator


class TestSeasonStatsRendering(unittest.TestCase):
    def test_current_page_serializes_standard_layout_and_correct_district_counts(self):
        # Given: an ongoing season and a match containing two districts on both sides.
        window = SeasonWindow("current", timestamp("2026-01-01"), timestamp("2026-01-08"))
        accumulator = StatsAccumulator()
        accumulator.add(row("1", window.start + 1, ("Capital Peak", "Wizard Valley")), window)
        accumulator.add(row("2", window.start + 2, ("Capital Peak", "Wizard Valley"), is_challenge=True), window)
        # When: the actual SDK message payload is serialized.
        payload = process_message_payload(embeds=stats_embed(window, accumulator.finish(window)),
                                          components=stats_controls([window], 0))
        # Then: one coherent standard embed contains accurate counts and boundary controls.
        embed = payload["embeds"][0]
        self.assertEqual((embed["title"], embed["color"], embed["footer"]),
                         ("**Current Season Stats**", COLOR, {"text": FOOTER_TEXT, "icon_url": FOOTER_IMAGE}))
        self.assertIn("End: Ongoing", embed["description"])
        self.assertIn(f"{DISTRICT_EMOJI} Capital Peak `2`", embed["fields"][-1]["value"])
        self.assertIn("Goblin Mines `0`", embed["fields"][-1]["value"])
        self.assertEqual([field["name"] for field in embed["fields"]],
                         ["**Activity**", "**Districts**"])
        activity = embed["fields"][0]["value"]
        self.assertEqual(re.findall(r"`(\d+)`", activity), ["2", "1", "1", "2", "2", "2"])
        self.assertEqual(
            re.findall(r"<t:(-?\d+):D>", activity),
            [str(timestamp("2025-12-29")), str(timestamp("2026-01-01"))],
        )
        for field in embed["fields"]:
            self.assertFalse(field["inline"])
            self.assertNotIn("**", field["value"])
        buttons = payload["components"][0]["components"]
        self.assertEqual([(b["style"], b["label"], b["disabled"]) for b in buttons],
                         [(1, "◀️️", True), (2, "1/1", False), (1, "▶️", True)])

    def test_archived_page_shows_actual_end_and_navigable_middle(self):
        # Given: a saved middle season among three pages.
        windows = [SeasonWindow("current", 20, 30), SeasonWindow("3", 10, 20), SeasonWindow("1", 0, 10)]
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 15), windows[1])
        # When: that page is rendered.
        embed = stats_embed(windows[1], accumulator.finish(windows[1]))
        controls = stats_controls(windows, 1)[0].components
        # Then: dates and navigation describe the same archived scope.
        self.assertIn("End: <t:20:F>", embed.description)
        self.assertEqual(re.findall(r"<t:(-?\d+):D>", embed.fields[0].value), ["-259200", "0"])
        self.assertEqual(re.findall(r"`(\d+)`", embed.fields[0].value), ["1", "0", "1", "1", "1", "2"])
        self.assertEqual([c.custom_id for c in controls],
                         ["season_stats_left|3", "season_stats_page|3", "season_stats_right|3"])
        self.assertEqual(controls[1].label, "2/3")
        self.assertFalse(controls[0].disabled or controls[2].disabled)

    def test_empty_first_season_displays_unknown_dates_and_zero_districts(self):
        # Given: no known official start and no recorded matches.
        window = SeasonWindow("current", None, 30)
        # When: the current view is built.
        embed = stats_embed(window, StatsAccumulator().finish(window))
        # Then: missing dates/peaks are explicit and all configured district counts are zero.
        self.assertIn("Not recorded", embed.description)
        self.assertEqual(re.findall(r"`(\d+)`", embed.fields[0].value), ["0"] * 4)
        self.assertNotIn("<t:", embed.fields[0].value)
        self.assertEqual(embed.fields[-1].value.count("`0`"), 9)

    def test_missing_district_data_has_visible_coverage_note(self):
        # Given: an old eligible result has no recorded district details.
        window = SeasonWindow("1", 10, 20)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 15), window)
        # When: district usage is displayed.
        embed = stats_embed(window, accumulator.finish(window))
        # Then: counts never imply complete district coverage.
        self.assertEqual(re.findall(r"`(\d+)`", embed.fields[-1].value), ["0"] * 9 + ["1"])

    def test_confirmation_wait_display_reports_averages(self):
        window = SeasonWindow("1", 10, 30)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 15, matched_time=100, players={
            "100": {"confirmed_at": 110, "confirmation_method": "ready"},
            "200": {"confirmed_at": 130, "confirmation_method": "automatic"},
        }), window)
        accumulator.add(row("2", 16), window)
        value = stats_embed(window, accumulator.finish(window)).fields[1].value
        self.assertIn("`15s`", value)
        self.assertIn("`30s`", value)

    def test_confirmation_wait_displays_human_readable_averages(self):
        window = SeasonWindow("1", 10, 30)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 15, matched_time=100, players={
            "100": {"confirmed_at": 220, "confirmation_method": "ready"},
            "200": {"confirmed_at": 4920, "confirmation_method": "automatic"},
        }), window)
        payload = process_message_payload(embeds=stats_embed(window, accumulator.finish(window)))
        fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
        value = fields["**Confirmation Wait**"]
        self.assertIn("`40m 10s`", value)
        self.assertIn("`1h 20m 20s`", value)

    def test_confirmation_wait_rounds_across_minute_boundary(self):
        window = SeasonWindow("1", 10, 30)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 15, matched_time=100, players={
            "100": {"confirmed_at": 159.6, "confirmation_method": "manual"},
            "200": {},
        }), window)
        value = stats_embed(window, accumulator.finish(window)).fields[1].value
        self.assertEqual(value.count("`1m`"), 2)

    def test_confirmation_wait_display_has_no_invented_empty_averages(self):
        window = SeasonWindow("1", 10, 30)
        for legacy_record in (False, True):
            with self.subTest(legacy_record=legacy_record):
                accumulator = StatsAccumulator()
                if legacy_record:
                    accumulator.add(row("1", 15), window)
                payload = process_message_payload(embeds=stats_embed(window, accumulator.finish(window)))
                names = [field["name"] for field in payload["embeds"][0]["fields"]]
                self.assertNotIn("**Confirmation Wait**", names)

    def test_confirmation_wait_display_keeps_recorded_zero(self):
        window = SeasonWindow("current", 10, 30)
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 15, matched_time=100, players={
            "100": {"confirmed_at": 100, "confirmation_method": "ready"},
            "200": {},
        }), window)
        payload = process_message_payload(embeds=stats_embed(window, accumulator.finish(window)))
        fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
        self.assertIn("`0s`", fields["**Confirmation Wait**"])
        self.assertEqual(len(fields["**Confirmation Wait**"].splitlines()), 1)
