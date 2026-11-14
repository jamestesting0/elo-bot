import random
from unittest.mock import patch
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
    async def test_choose_bases_prioritizes_unseen_over_attacks_outside_last_15_records(self):
        # Given: a seen base whose player record is buried under other players' attacks
        seen = self.make_layout(
            "seen", [[123, 3, 100, 100]] + [["456", 3, 100, 200]] * 15,
        )
        unseen = self.make_layout("unseen", [["456", 3, 100, 300]] * 30)

        # When: choosing from a more popular unseen base and the seen base
        embeds, participant, choices = await self.run_choose_bases_with_layouts([seen, unseen])

        # Then: only the unseen base can be saved and displayed
        self.assertEqual(choices.call_args.args[0], [unseen])
        self.assertEqual(participant.bases[0].link, "unseen")
        self.assertEqual(embeds[0].image.url, unseen.image_url)

    async def test_choose_bases_uses_maximum_valid_player_timestamp_in_unordered_history(self):
        # Given: an older record appended after the player's latest attack, plus malformed records
        recent = self.make_layout("recent", [
            [123, 3, 100, 900.5],
            ["123", 3, 100, 100],
            ["456", 3, 100, 1200],
            ["123", 3, 100],
            "malformed",
            ("123", 3, 100, False),
            ("123", 3, 100, True),
            ["123", 3, 100, "900"],
            ["123", 3, 100, -1],
            ("123", 3, 100, 0),
            ["123", 3, 100, None],
        ])
        oldest = self.make_layout("oldest", [("123", 3, 100, 500)])

        # When: selecting using each base's latest valid attack timestamp
        _, participant, choices = await self.run_choose_bases_with_layouts([recent, oldest])

        # Then: neither record order nor malformed records make a recent base eligible
        self.assertEqual(choices.call_args.args[0], [oldest])
        self.assertEqual(participant.bases[0].link, "oldest")

    async def test_choose_bases_filters_to_smallest_last_hit_before_total_attack_ranking(self):
        # Given: the oldest base has more total attacks than several newer alternatives
        layouts = [
            self.make_layout(
                f"base-{index}",
                [["123", 3, 100, timestamp]] + [["456", 3, 100, 9999]] * filler_count,
            )
            for index, (timestamp, filler_count) in enumerate([
                (110, 4),
                (10, 8),
                (90, 1),
                (30, 0),
                (120, 0),
                (50, 3),
                (20, 6),
                (80, 2),
                (40, 5),
                (60, 7),
                (70, 9),
                (100, 0),
            ])
        ]

        # When: choosing from more than ten previously attacked bases
        _, participant, choices = await self.run_choose_bases_with_layouts(layouts)

        # Then: total usage cannot admit a more recently attacked base
        self.assertEqual(choices.call_args.args[0], [layouts[1]])
        self.assertEqual(participant.bases[0].link, "base-1")

    async def test_choose_bases_ranks_all_unseen_bases_by_total_attacks(self):
        # Given: the three least-used unseen bases fall beyond the old ten-base shortlist
        layouts = [self.make_layout(f"base-{count}", [["456", 3, 100, 500]] * count)
                   for count in range(12, -1, -1)]

        # When: ranking bases with no recorded attack by this player
        _, participant, choices = await self.run_choose_bases_with_layouts(layouts)

        # Then: the full unseen pool contributes to the three least-used finalists
        self.assertEqual(choices.call_args.args[0], [layouts[12], layouts[11], layouts[10]])
        self.assertEqual(participant.bases[0].link, "base-0")

    async def test_choose_bases_weights_three_oldest_finalists_50_25_25(self):
        # Given: four bases tied for oldest last hit, with different total usage
        layouts = [self.make_layout(f"base-{count}", [["123", 3, 100, 100]]
                                    + [["456", 3, 100, 500]] * count)
                   for count in (3, 2, 1, 0)]
        for draw, expected in ((0, 0), (0.499999, 0), (0.5, 1), (0.749999, 1), (0.75, 2), (0.999999, 2)):
            with self.subTest(draw=draw), patch.object(random.Random, "random", return_value=draw):
                # When: exercising the real weighted sampler at each probability boundary
                embeds, participant, _ = await self.run_choose_bases_with_layouts(layouts)

                # Then: saved selection and displayed image match the 50/25/25 intervals
                self.assertEqual(participant.bases[0].link, f"base-{expected}")
                self.assertEqual(embeds[0].image.url, f"https://example.test/base-{expected}.png")

    async def test_choose_bases_normalizes_weights_for_one_or_two_finalists(self):
        # Given: fewer than three unseen bases, ordered by total usage
        layouts = [self.make_layout("first"), self.make_layout("second", [["456", 3, 100, 500]])]
        for count, draw, expected in ((1, 0.999999, "first"), (2, 0.666666, "first"), (2, 0.666667, "second")):
            with self.subTest(count=count, draw=draw), patch.object(random.Random, "random", return_value=draw):
                # When: selecting near the normalized probability boundaries
                _, participant, _ = await self.run_choose_bases_with_layouts(layouts[:count])

                # Then: one candidate is certain, and two candidates retain their 2:1 weights
                self.assertEqual(participant.bases[0].link, expected)

    async def test_choose_bases_applies_wizard_quality_mapping_only_to_wizard_valley(self):
        # Given: every Wizard tier transition plus one non-Wizard control
        cases = [
            ("Wizard Valley", "Average", "Good"),
            ("Wizard Valley", "Good", "Great"),
            ("Wizard Valley", "Great", "Excellent"),
            ("Wizard Valley", "Excellent", "Excellent"),
            ("Barbarian Camp", "Good", "Good"),
        ]

        for district, normal_quality, expected_quality in cases:
            with self.subTest(district=district, normal_quality=normal_quality):
                # When: helper-base selection builds its public base query
                await self.run_choose_bases_with_layouts(
                    [], (district, (normal_quality, normal_quality))
                )

                # Then: only Wizard Valley advances one tier and Excellent stays capped
                quality_filters = [
                    expression[1] for expression in self.mock_base_layout.find.call_args.args
                    if isinstance(expression, tuple) and expression[0] == "quality"
                ]
                self.assertEqual(quality_filters, [expected_quality])

    async def test_choose_bases_saves_actual_adjusted_layout_quality(self):
        layout = self.make_layout("wizard")
        layout.quality = "Great"
        _, participant, _ = await self.run_choose_bases_with_layouts(
            [layout], ("Wizard Valley", ("Good", "Good"))
        )
        self.assertEqual(participant.bases[0].base_quality, "Great")
