import sys
import os
sys.path.append(os.getcwd())

import asyncio
import random
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock, ANY, call
from aiohttp import web
import anyio
import interactions as ipy
from beanie import PydanticObjectId

import cogs.match_commands as match_commands
from cogs.challenge_commands import ChallengeCommands
from cogs.match_commands import MatchCommands, QUEUE_TIER_AGED, QUEUE_TIER_MIXED, QUEUE_TIER_PRIORITY, \
    QUEUE_TIER_NORMAL, QUEUE_TIER1_AGED_AFTER_SECONDS, collect_occupied_base_store_slots, \
    clear_participant_base_store, clear_match_base_stores, construct_start_slot_instruction, \
    get_start_base_store_slot_availability, is_match_finished, is_player_occupied, choose_bases, \
    initiate_match
from cogs.preset_commands import PresetCommands
from elobot.db import BaseStore, Match, OverrideCode, Participant, Player, Queue, ServerState
from elobot.config.game import BASE_STORE_SLOTS
from elobot.config.ids import BOT_ID
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.features.base_store.slots import (
    clear_all_base_store_slots, collect_base_store_slot_owners, format_base_store_slot_status,
)


class TestMatchCommands(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def enable_base_store_clear(match: MagicMock) -> None:
        def clear(index: int, reset_stored_flag: bool = False) -> bool:
            match.participants[index].base_store = None
            if reset_stored_flag:
                match.participants[index].opponent_bases_stored = False
            return True

        match.clear_base_store = AsyncMock(side_effect=clear)

    @staticmethod
    def enable_base_store_message_binding(match: MagicMock) -> None:
        match.result_operation = None
        match.autowin_winner_id = None

        def attach(index: int, message_id: int, *, ready: bool = True) -> bool:
            store = match.participants[index].base_store
            if store is None:
                return False
            store.message_id = message_id
            store.ready = ready
            return True

        match.attach_base_store_message = AsyncMock(side_effect=attach)
        match.get_pymongo_collection.return_value.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1),
        )

    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.owner = AsyncMock()
        self.bot.owner.id = 123456789
        self.bot.owner.send = AsyncMock()
        self.bot.user.id = BOT_ID
        self.bot.fetch_guild = AsyncMock()
        mock_user = MagicMock()
        mock_user.send = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=mock_user)
        self.bot.fetch_channel = AsyncMock()
        self.bot.ext = {}
        self.bot.coc_client = MagicMock()
        self.bot.coc_client.get_clan = AsyncMock()
        for command in vars(MatchCommands).values():
            if hasattr(command, "callback"):
                command.callback = getattr(command.callback, "func", command.callback)
        self.cog = MatchCommands(self.bot)
        self.enterContext(patch.dict(match_commands.RESOURCE_MATCH_SIDE_LOCKS, clear=True))
        self.enterContext(patch.dict(match_commands.RESOURCE_CLAN_LOCKS, clear=True))
        self.enterContext(patch("cogs.base_store.preparation.BASE_STORE_RESERVATION_LOCK", new=asyncio.Lock()))
        self.enterContext(patch.object(match_commands, "invite_eligible_players", new=AsyncMock()))
        self.queue_docs = {}
        self.enterContext(patch("cogs.match_commands.find_attempt_match", new=AsyncMock(return_value=None)))
        self.enterContext(patch("cogs.match_commands.find_attempt_queue", new=AsyncMock(
            side_effect=lambda user_id, attempt_id, _tiers: next(
                (queue for queue in self.queue_docs.values()
                 if user_id in queue.district_selections
                 and queue.district_selections[user_id].attempt_id == attempt_id), None),
        )))

    async def test_inactive_threads_delete_removes_only_finished_threads_inactive_for_one_day(self):
        old_thread = MagicMock()
        old_thread.name = "🔴 Old match"
        old_thread.fetch_messages = AsyncMock(return_value=[
            MagicMock(created_at=datetime.now(timezone.utc) - timedelta(days=3))
        ])
        old_thread.delete = AsyncMock()

        recent_thread = MagicMock()
        recent_thread.name = "🔴 Recent match"
        recent_thread.fetch_messages = AsyncMock(return_value=[
            MagicMock(created_at=datetime.now(timezone.utc) - timedelta(hours=23))
        ])
        recent_thread.delete = AsyncMock()

        empty_thread = MagicMock()
        empty_thread.name = "🔴 Empty match"
        empty_thread.fetch_messages = AsyncMock(return_value=[])
        empty_thread.delete = AsyncMock()

        unfinished_thread = MagicMock()
        unfinished_thread.name = "🟢 Active match"
        unfinished_thread.fetch_messages = AsyncMock()
        unfinished_thread.delete = AsyncMock()

        channel = MagicMock()
        channel.fetch_all_threads = AsyncMock(return_value=MagicMock(threads=[
            old_thread,
            recent_thread,
            empty_thread,
            unfinished_thread,
        ]))
        self.bot.fetch_channel.return_value = channel

        with patch.object(match_commands, "Match") as match_model:
            match_model.find_one = AsyncMock(return_value=None)
            await self.cog.inactive_threads_delete.callback.func(self.cog)

        old_thread.delete.assert_awaited_once()
        recent_thread.delete.assert_not_awaited()
        empty_thread.delete.assert_not_awaited()
        unfinished_thread.delete.assert_not_awaited()
        self.assertEqual(old_thread.fetch_messages.await_args_list, [call(limit=1), call(limit=1)])
        recent_thread.fetch_messages.assert_awaited_once_with(limit=1)
        empty_thread.fetch_messages.assert_awaited_once_with(limit=1)
        unfinished_thread.fetch_messages.assert_not_awaited()

    @staticmethod
    def make_player(user_id: str, elo: int, priority: bool = False):
        player = MagicMock(spec=Player)
        player.user_id = user_id
        player.elo = elo
        player.priority = priority
        player.matches = []
        player.district_presets = [None, None, None]
        player.capital_peak_enabled = True
        player.save_changes = AsyncMock()
        return player

    def make_queue(self, tier: int, players=None, entered_at=None):
        queue = MagicMock(spec=Queue)
        queue.tier = tier
        queue.players = list(players or [])
        queue.entered_at = dict(entered_at or {})
        queue.district_selections = {}
        queue.save_changes = AsyncMock()
        self.queue_docs[tier] = queue
        return queue

    @staticmethod
    def make_layout(link: str, records=None):
        layout = MagicMock()
        layout.link = link
        layout.image_url = f"https://example.test/{link}.png"
        layout.quality = "Good"
        layout.records = list(records or [])
        return layout

    async def run_choose_bases_with_layouts(
            self, layouts, district_quality=("Capital Peak", ("Primary", "Secondary"))):
        district, qualities = district_quality
        player = MagicMock(spec=ipy.User)
        player.id = 123
        opponent = MagicMock(spec=ipy.User)
        opponent.id = 456

        player_participant = MagicMock(user_id="123", bases=[None])
        opponent_participant = MagicMock(user_id="456", bases=[None], secondary_base_index=2)
        match = MagicMock(
            participants=[player_participant, opponent_participant],
            final_districts=[district],
            token="TOKEN",
        )
        match.save_changes = AsyncMock()

        layouts_query = MagicMock()
        layouts_query.to_list = AsyncMock(return_value=layouts)
        matches_query = MagicMock()
        matches_query.to_list = AsyncMock(return_value=[])

        with patch("cogs.match_commands.Player") as mock_player, \
                patch("cogs.match_commands.Match") as mock_match, \
                patch("cogs.match_commands.BaseLayout") as mock_base_layout, \
                patch("cogs.match_commands.NotIn", side_effect=lambda *args: args), \
                patch("cogs.match_commands.get_base_quality", return_value=qualities), \
                patch("cogs.match_commands.random.randint", return_value=2), \
                patch("cogs.match_commands.random.choices", wraps=random.Random(1).choices) as mock_choices:
            mock_player.find_one = AsyncMock(side_effect=[MagicMock(), MagicMock()])
            mock_match.find_one = AsyncMock(return_value=match)
            mock_match.find.return_value = matches_query
            mock_base_layout.expiration_date.__gt__.return_value = "unexpired"
            mock_base_layout.quality.__eq__.side_effect = lambda value: ("quality", value)
            mock_base_layout.find.return_value = layouts_query

            result = await choose_bases("TOKEN", player, opponent)

        match.save_changes.assert_awaited_once()
        self.mock_base_layout = mock_base_layout
        return result, opponent_participant, mock_choices

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

    def test_district_options_follow_announced_groups_and_snapshotted_availability(self):
        # Given: every combination of snapshotted conditional-district availability
        first = ["Barbarian Camp", "Skeleton Park"]
        second = ["Balloon Lagoon", "Builder's Workshop", "Golem Quarry"]
        third = ["Dragon Cliffs", "Goblin Mines"]
        cases = [
            (False, False, [first, second, third]),
            (True, False, [first + ["Wizard Valley"], second, third]),
            (False, True, [first, second, third + ["Capital Peak"]]),
            (True, True, [first + ["Wizard Valley"], second, third + ["Capital Peak"]]),
        ]

        for wizard_available, peak_available, expected in cases:
            with self.subTest(wizard=wizard_available, peak=peak_available):
                # When: the shared selection helper builds the three groups
                actual = match_commands.get_district_options(wizard_available, peak_available)

                # Then: it returns the exact announced groups allowed by the snapshot
                self.assertEqual(actual, expected)

    async def test_initiate_match_snapshots_rules_and_sanitizes_presets(self):
        # Given: both players below the Wizard cutoff, either-player overflow, and a Peak opt-out
        cases = [
            (1199, 1199, True, True, (True, True),
             ["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
             ["Wizard Valley", "Balloon Lagoon", "Capital Peak"]),
            (1201, 1199, True, False, (False, False),
             ["Capital Peak", "Unknown District", "Wizard Valley"],
             [None, None, None]),
            (1199, 1199, True, False, (True, False),
             ["Capital Peak", "Balloon Lagoon", None],
             [None, "Balloon Lagoon", None]),
        ]

        for elo1, elo2, peak1, peak2, availability, presets, expected_presets in cases:
            with self.subTest(elo1=elo1, elo2=elo2, peak2=peak2):
                users = [MagicMock(id=111), MagicMock(id=222)]
                for user in users:
                    user.send = AsyncMock()
                bot = MagicMock()
                bot.fetch_user = AsyncMock(side_effect=users)
                players = [
                    MagicMock(user_id="111", elo=elo1, capital_peak_enabled=peak1,
                              district_presets=list(presets)),
                    MagicMock(user_id="222", elo=elo2, capital_peak_enabled=peak2,
                              district_presets=[None, None, None]),
                ]
                match = MagicMock(token="TOKEN", base_submission=False, insert=AsyncMock())

                with patch("cogs.match_commands.Match", return_value=match) as mock_match, \
                        patch("cogs.match_commands.get_base_quality", return_value=("Good", "Good")), \
                        patch("cogs.match_commands.confirm_match", new_callable=AsyncMock), \
                        patch("cogs.match_commands.random.choice") as mock_choice:
                    # When: a regular Match is created
                    await initiate_match(bot, *players)

                # Then: mutual rules are snapshotted and invalid presets cannot auto-confirm
                data = mock_match.call_args.kwargs
                self.assertEqual(
                    (data["wizard_valley_available"], data["capital_peak_available"]), availability
                )
                self.assertEqual(data["participants"][0].districts, expected_presets)
                mock_choice.assert_not_called()

    async def test_ready_participant_is_recorded_at_match_creation(self):
        users = [MagicMock(id=111, send=AsyncMock()), MagicMock(id=222, send=AsyncMock())]
        bot = MagicMock(fetch_user=AsyncMock(side_effect=users))
        players = [
            MagicMock(user_id="111", elo=1200, capital_peak_enabled=True,
                      district_presets=["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]),
            MagicMock(user_id="222", elo=1200, capital_peak_enabled=True,
                      district_presets=[None, None, None]),
        ]
        match = MagicMock(token="TOKEN", base_submission=False, insert=AsyncMock())
        with patch("cogs.match_commands.Match", return_value=match) as match_type, \
                patch("cogs.match_commands.get_base_quality", return_value=("Good", "Good")), \
                patch("cogs.match_commands.datetime") as clock:
            clock.now.return_value.timestamp.return_value = 1000.0
            await initiate_match(bot, *players)

        created = match_type.call_args.kwargs
        ready, waiting = created["participants"]
        self.assertEqual(created["matched_time"], 1000.0)
        self.assertTrue(created["confirmation_tracking"])
        self.assertEqual((ready.confirmed_at, ready.confirmation_method), (1000.0, "ready"))
        self.assertEqual((waiting.confirmed_at, waiting.confirmation_method), (None, None))

    async def test_initiate_match_randomizes_unavailable_conditional_presets_for_both_players(self):
        # Given: both players stored unavailable conditional presets in their canonical slots
        users = [MagicMock(id=111), MagicMock(id=222)]
        for user in users:
            user.send = AsyncMock()
        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=users)
        players = [
            MagicMock(user_id="111", elo=1200, capital_peak_enabled=False,
                      district_presets=["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
                      secondary_district_presets=[None, None, None]),
            MagicMock(user_id="222", elo=1199, capital_peak_enabled=False,
                      district_presets=["Wizard Valley", None, "Capital Peak"],
                      secondary_district_presets=[None, None, None]),
        ]
        match = MagicMock(token="TOKEN", base_submission=False, insert=AsyncMock())

        with patch("cogs.match_commands.Match", return_value=match) as mock_match, \
                patch("cogs.match_commands.get_base_quality", return_value=("Good", "Good")), \
                patch("cogs.match_commands.confirm_match", new_callable=AsyncMock), \
                patch("cogs.match_commands.random.choice",
                      side_effect=["Skeleton Park", "Goblin Mines", "Barbarian Camp", "Dragon Cliffs"]) as mock_choice:
            # When: a regular Match is created with unavailable conditional presets
            await initiate_match(bot, *players)

        # Then: each participant receives deterministic in-group fallbacks in slot order
        participants = mock_match.call_args.kwargs["participants"]
        self.assertEqual(participants[0].districts, ["Skeleton Park", "Balloon Lagoon", "Goblin Mines"])
        self.assertEqual(participants[1].districts, ["Barbarian Camp", None, "Dragon Cliffs"])
        self.assertTrue(participants[0].confirmed)
        self.assertFalse(participants[1].confirmed)
        self.assertEqual(
            mock_choice.call_args_list,
            [
                call(["Barbarian Camp", "Skeleton Park"]),
                call(["Dragon Cliffs", "Goblin Mines"]),
                call(["Barbarian Camp", "Skeleton Park"]),
                call(["Dragon Cliffs", "Goblin Mines"]),
            ],
        )

    async def test_initiate_match_prefers_secondary_presets_before_random_fallbacks(self):
        # Given: unavailable conditional presets, with fallbacks configured for only one player
        users = [MagicMock(id=111), MagicMock(id=222)]
        for user in users:
            user.send = AsyncMock()
        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=users)
        players = [
            MagicMock(
                user_id="111",
                elo=1200,
                capital_peak_enabled=False,
                district_presets=["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
                secondary_district_presets=["Skeleton Park", None, "Goblin Mines"],
            ),
            MagicMock(
                user_id="222",
                elo=1199,
                capital_peak_enabled=False,
                district_presets=["Wizard Valley", None, "Capital Peak"],
                secondary_district_presets=[None, None, None],
            ),
        ]
        match = MagicMock(token="TOKEN", base_submission=False, insert=AsyncMock())

        with patch("cogs.match_commands.Match", return_value=match) as mock_match, \
                patch("cogs.match_commands.get_base_quality", return_value=("Good", "Good")), \
                patch("cogs.match_commands.confirm_match", new_callable=AsyncMock), \
                patch("cogs.match_commands.random.choice",
                      side_effect=["Barbarian Camp", "Dragon Cliffs"]) as mock_choice:
            # When: a regular match resolves the unavailable presets
            await initiate_match(bot, *players)

        # Then: configured secondary presets win, and missing ones use in-slot randomness
        participants = mock_match.call_args.kwargs["participants"]
        self.assertEqual(
            participants[0].districts,
            ["Skeleton Park", "Balloon Lagoon", "Goblin Mines"],
        )
        self.assertEqual(
            participants[1].districts,
            ["Barbarian Camp", None, "Dragon Cliffs"],
        )
        self.assertEqual(
            mock_choice.call_args_list,
            [
                call(["Barbarian Camp", "Skeleton Park"]),
                call(["Dragon Cliffs", "Goblin Mines"]),
            ],
        )

    async def test_initiate_match_randomizes_invalid_secondary_presets_in_same_slot(self):
        # Given: unavailable primaries paired with unknown, wrong-slot, or conditional secondaries
        cases = [
            (
                ["Wizard Valley", "Balloon Lagoon", None],
                ["Unknown District", None, None],
                ["Skeleton Park"],
                [["Barbarian Camp", "Skeleton Park"]],
            ),
            (
                ["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
                ["Capital Peak", None, "Wizard Valley"],
                ["Barbarian Camp", "Goblin Mines"],
                [
                    ["Barbarian Camp", "Skeleton Park"],
                    ["Dragon Cliffs", "Goblin Mines"],
                ],
            ),
            (
                ["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
                ["Wizard Valley", None, "Capital Peak"],
                ["Skeleton Park", "Dragon Cliffs"],
                [
                    ["Barbarian Camp", "Skeleton Park"],
                    ["Dragon Cliffs", "Goblin Mines"],
                ],
            ),
        ]

        for primary, secondary, random_values, expected_groups in cases:
            with self.subTest(secondary=secondary):
                users = [MagicMock(id=111), MagicMock(id=222)]
                for user in users:
                    user.send = AsyncMock()
                bot = MagicMock()
                bot.fetch_user = AsyncMock(side_effect=users)
                players = [
                    MagicMock(
                        user_id="111",
                        elo=1200,
                        capital_peak_enabled=False,
                        district_presets=primary,
                        secondary_district_presets=secondary,
                    ),
                    MagicMock(
                        user_id="222",
                        elo=1199,
                        capital_peak_enabled=False,
                        district_presets=[None, None, None],
                        secondary_district_presets=[None, None, None],
                    ),
                ]
                match = MagicMock(token="TOKEN", base_submission=False, insert=AsyncMock())

                with patch("cogs.match_commands.Match", return_value=match), \
                        patch("cogs.match_commands.get_base_quality", return_value=("Good", "Good")), \
                        patch("cogs.match_commands.confirm_match", new_callable=AsyncMock), \
                        patch("cogs.match_commands.random.choice",
                              side_effect=random_values) as mock_choice:
                    # When: regular matching resolves each invalid secondary
                    await initiate_match(bot, *players)

                # Then: every fallback remains random within its canonical slot
                self.assertEqual(
                    mock_choice.call_args_list,
                    [call(group) for group in expected_groups],
                )

    async def test_initiate_match_ignores_secondaries_unless_conditional_primary_is_unavailable(self):
        # Given: valid secondary values with available or unconditional primary presets
        cases = [
            (
                1199,
                True,
                ["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
            ),
            (
                1200,
                False,
                ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"],
            ),
        ]

        for elo, capital_peak_enabled, primary in cases:
            with self.subTest(primary=primary):
                users = [MagicMock(id=111), MagicMock(id=222)]
                for user in users:
                    user.send = AsyncMock()
                bot = MagicMock()
                bot.fetch_user = AsyncMock(side_effect=users)
                players = [
                    MagicMock(
                        user_id="111",
                        elo=elo,
                        capital_peak_enabled=capital_peak_enabled,
                        district_presets=primary,
                        secondary_district_presets=["Skeleton Park", None, "Goblin Mines"],
                    ),
                    MagicMock(
                        user_id="222",
                        elo=elo,
                        capital_peak_enabled=capital_peak_enabled,
                        district_presets=[None, None, None],
                        secondary_district_presets=[None, None, None],
                    ),
                ]
                match = MagicMock(token="TOKEN", base_submission=False, insert=AsyncMock())

                with patch("cogs.match_commands.Match", return_value=match) as mock_match, \
                        patch("cogs.match_commands.get_base_quality", return_value=("Good", "Good")), \
                        patch("cogs.match_commands.confirm_match", new_callable=AsyncMock), \
                        patch("cogs.match_commands.random.choice") as mock_choice:
                    # When: regular matching resolves the primary presets
                    await initiate_match(bot, *players)

                # Then: the primary selections remain unchanged and no fallback is consumed
                participant = mock_match.call_args.kwargs["participants"][0]
                self.assertEqual(participant.districts, primary)
                mock_choice.assert_not_called()

    @patch("cogs.match_commands.Match")
    async def test_district_select_rejects_stale_or_forged_values(self, mock_match):
        # Given: unavailable conditional choices and a district forged into the wrong slot
        cases = [
            (0, "Wizard Valley", False, True),
            (2, "Capital Peak", True, False),
            (0, "Capital Peak", True, True),
        ]

        for index, district, wizard_available, peak_available in cases:
            with self.subTest(index=index, district=district):
                participant = Participant(user_id="111")
                match = MagicMock(
                    thread=None,
                    participants=[participant, Participant(user_id="222")],
                    wizard_valley_available=wizard_available,
                    capital_peak_available=peak_available,
                    save_changes=AsyncMock(),
                )
                mock_match.find_one = AsyncMock(return_value=match)
                ctx = MagicMock(channel=None, custom_id=f"district_select|TOKEN|{index}", values=[district])
                ctx.author.id = 111
                ctx.send = AsyncMock()

                # When: a stale or forged component value reaches the callback
                await self.cog.district_select.callback(ctx)

                # Then: it is rejected without mutating or saving the participant
                self.assertEqual(participant.districts, [None, None, None])
                match.save_changes.assert_not_awaited()

    @patch("cogs.preset_commands.Player")
    @patch("cogs.preset_commands.BaseLayout")
    async def test_base_presets_accept_new_district_codes(self, mock_base, mock_player):
        # Given: valid private links for Wizard Valley and Capital Peak
        cog = PresetCommands(self.bot)
        ctx = MagicMock(author=MagicMock(id=111), defer=AsyncMock(), send=AsyncMock())
        attachment = MagicMock(url="https://example.test/base.webp")
        mock_base.find_one = AsyncMock(return_value=None)
        mock_player.find_one = AsyncMock(return_value=None)

        for district_code in ("3A2", "3A0"):
            with self.subTest(district_code=district_code), \
                    patch("cogs.preset_commands.validators.url", return_value=True):
                mock_base.find_one.reset_mock()
                link = f"https://link.clashofclans.com/en?action=OpenLayout&id=TH16%3ACC%{district_code}"

                # When: the preset callback receives either announced district code
                await cog.preset_base_add.callback(ctx, base_link=link, base_ss=attachment)

                # Then: it passes district validation and reaches the normal base lookup
                mock_base.find_one.assert_awaited_once()

    async def test_challenge_creation_snapshots_mutual_district_availability(self):
        # Given: two players at the Wizard cutoff where one has opted out of Capital Peak
        cog = ChallengeCommands(self.bot)
        ctx = MagicMock(author=MagicMock(id=111, mention="<@111>"), defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.author.send = AsyncMock()
        opponent = MagicMock(id=222, mention="<@222>")
        opponent.has_role.return_value = False
        opponent.send = AsyncMock()
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        recent_normal_match = MagicMock(id="1", time=int(now.timestamp()), is_challenge=False)
        players = [
            MagicMock(elo=1200, challenge=True, matches=[recent_normal_match], last_challenged=["", ""],
                      capital_peak_enabled=True),
            MagicMock(elo=1200, challenge=True, matches=[recent_normal_match], blocked_users=[],
                      capital_peak_enabled=False),
        ]
        match = MagicMock(insert=AsyncMock())

        with patch("cogs.challenge_commands.Player") as mock_player, \
                patch("cogs.challenge_commands.Match") as mock_match, \
                patch("cogs.challenge_commands.datetime") as mock_datetime, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)), \
                patch("cogs.challenge_commands.is_player_occupied", new=AsyncMock(return_value=None)):
            mock_player.find_one = AsyncMock(side_effect=players)
            mock_match.find.return_value.to_list = AsyncMock(return_value=[])
            mock_match.find_one = AsyncMock(return_value=None)
            mock_match.return_value = match
            mock_datetime.now.return_value = now

            # When: the challenge Match is created
            await cog.challenge_send.callback(
                ctx, opponent, "Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"
            )

        # Then: Wizard is unavailable at the strict boundary while mutual Peak is disabled
        data = mock_match.call_args.kwargs
        self.assertEqual((data["wizard_valley_available"], data["capital_peak_available"]), (False, False))

    def test_collect_occupied_base_store_slots(self):
        match = MagicMock()
        match.thread = "987"
        match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
            ),
            MagicMock(base_store=None),
        ]

        occupied = collect_occupied_base_store_slots([match])

        self.assertEqual(occupied, {"#CLAN": {"Slot 1"}})
        orphaned_match = MagicMock(
            thread=None,
            participants=[MagicMock(
                user_id="456",
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 2", stored_at=10),
            )],
        )
        self.assertEqual(
            collect_occupied_base_store_slots([match, orphaned_match]),
            {"#CLAN": {"Slot 1"}},
        )

        slot_owners = collect_base_store_slot_owners([match])
        self.assertEqual(slot_owners, {"#CLAN": {"Slot 1": ("987", "123")}})
        self.assertEqual(
            format_base_store_slot_status("#CLAN", {}),
            "",
        )
        self.assertEqual(
            format_base_store_slot_status("#CLAN", slot_owners),
            "Slot 1: <#987> <@123>",
        )

    @patch("cogs.base_store.repository.Match")
    async def test_start_slot_availability_excludes_current_thread_and_tracks_foreign_slots(self, mock_match):
        current_match = MagicMock(thread="100")
        current_match.participants = [
            MagicMock(
                user_id="1",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]
        foreign_slot_1 = MagicMock(thread="200")
        foreign_slot_1.participants = [
            MagicMock(
                user_id="2",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]
        foreign_slot_2 = MagicMock(thread="300")
        foreign_slot_2.participants = [
            MagicMock(
                user_id="3",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 2", stored_at=3000000000
                ),
            )
        ]
        mock_match.find.return_value.to_list = AsyncMock(side_effect=[
            [current_match],
            [current_match, foreign_slot_1],
            [current_match, foreign_slot_1, foreign_slot_2],
        ])

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")
        self.assertEqual(available, list(BASE_STORE_SLOTS))
        self.assertEqual(owners, {})
        self.assertEqual(construct_start_slot_instruction(available, "Test Clan"), "")

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")
        self.assertEqual(available, ["Slot 2"])
        self.assertEqual(owners, {"Slot 1": ("200", "2")})
        self.assertEqual(
            construct_start_slot_instruction(available, "Test Clan"),
            "\n\nPlease use the non-occupied base slot `Slot 2` in **Test Clan**.",
        )

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")
        self.assertEqual(available, [])
        self.assertEqual(owners, {
            "Slot 1": ("200", "2"),
            "Slot 2": ("300", "3"),
        })

    @patch("cogs.base_store.repository.Match")
    async def test_start_slot_availability_ignores_unrelated_reservations(self, mock_match):
        cases = [
            ("unmapped legacy", MagicMock(
                thread="200",
                participants=[MagicMock(user_id="2", base_store=None, opponent_bases_stored=True)],
                times=[None],
            )),
            ("no thread owner", MagicMock(
                thread=None,
                participants=[
                    MagicMock(
                        user_id=str(index),
                        base_store=BaseStore(clan_tag="#CLAN", slot=slot, stored_at=3000000000),
                    )
                    for index, slot in enumerate(BASE_STORE_SLOTS, start=2)
                ],
            )),
            ("other clan", MagicMock(
                thread="200",
                participants=[MagicMock(
                    user_id="2",
                    base_store=BaseStore(clan_tag="#OTHER", slot="Slot 1", stored_at=3000000000),
                )],
            )),
        ]
        for reason, match in cases:
            with self.subTest(reason=reason):
                mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

                available, owners = await get_start_base_store_slot_availability("#CLAN", "100")

                self.assertEqual(available, list(BASE_STORE_SLOTS))
                self.assertEqual(owners, {})

    @patch("cogs.base_store.repository.latest_completed_raid_weekend_end_timestamp", return_value=200)
    @patch("cogs.base_store.repository.Match")
    async def test_start_slot_availability_clears_expired_reservations(
            self, mock_match, _mock_latest_raid_end):
        participant = MagicMock(
            user_id="2",
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=100),
            opponent_bases_stored=True,
        )
        expired_match = MagicMock(thread="200", participants=[participant])
        expired_match.save_changes = AsyncMock()
        self.enable_base_store_clear(expired_match)
        mock_match.find.return_value.to_list = AsyncMock(return_value=[expired_match])

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")

        self.assertEqual(available, list(BASE_STORE_SLOTS))
        self.assertEqual(owners, {})
        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        expired_match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        expired_match.save_changes.assert_not_awaited()

    @patch("cogs.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    async def test_clear_participant_base_store_releases_slot_and_stored_flag(self, mock_sync_slots):
        participant = MagicMock(
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
            opponent_bases_stored=True
        )
        match = MagicMock(participants=[participant])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        await clear_participant_base_store(match, 0, reset_stored_flag=True, reason="undone")

        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        mock_sync_slots.assert_awaited_once_with("#CLAN", bot=None)

    @patch("cogs.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    async def test_clear_participant_base_store_resynchronizes_unmapped_legacy_reservation(self, mock_sync_slots):
        participant = MagicMock(base_store=None, opponent_bases_stored=True)
        match = MagicMock(participants=[participant])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        await clear_participant_base_store(match, 0, reset_stored_flag=True, reason="undone")

        self.assertFalse(participant.opponent_bases_stored)
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        mock_sync_slots.assert_awaited_once_with(None, bot=None)

    @patch("cogs.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    async def test_clear_match_base_stores_releases_all_slots_and_flags(self, mock_sync_slots):
        participants = [
            MagicMock(
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
                opponent_bases_stored=True
            ),
            MagicMock(
                base_store=BaseStore(clan_tag="#CLAN2", slot="Slot 2", stored_at=20),
                opponent_bases_stored=True
            ),
        ]
        match = MagicMock(participants=participants)
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        await clear_match_base_stores(match, reset_stored_flags=True, reason="undone")

        self.assertTrue(all(participant.base_store is None for participant in participants))
        self.assertTrue(all(not participant.opponent_bases_stored for participant in participants))
        self.assertEqual(match.clear_base_store.await_args_list, [
            call(0, reset_stored_flag=True), call(1, reset_stored_flag=True),
        ])
        match.save_changes.assert_not_awaited()
        self.assertEqual(mock_sync_slots.await_args_list, [
            call("#CLAN", bot=None), call("#CLAN2", bot=None),
        ])

    @patch("elobot.features.base_store.slots.Clan")
    @patch("elobot.features.base_store.slots.Match")
    async def test_clear_all_base_store_slots_frees_match_metadata_and_clan_slots(self, mock_match, mock_clan):
        stored_participant = MagicMock(
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
            opponent_bases_stored=True
        )
        started_participant = MagicMock(base_store=None, opponent_bases_stored=True)
        match = MagicMock(participants=[stored_participant, started_participant], times=[None, 100])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        legacy_participant = MagicMock(base_store=None, opponent_bases_stored=True)
        legacy_match = MagicMock(participants=[legacy_participant], times=[None])
        legacy_match.save_changes = AsyncMock()
        self.enable_base_store_clear(legacy_match)
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match, legacy_match])

        clan = MagicMock(available_slots=["Slot 1"])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        await clear_all_base_store_slots()

        self.assertIsNone(stored_participant.base_store)
        self.assertFalse(stored_participant.opponent_bases_stored)
        self.assertTrue(started_participant.opponent_bases_stored)
        self.assertFalse(legacy_participant.opponent_bases_stored)
        self.assertEqual(clan.available_slots, list(BASE_STORE_SLOTS))
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        legacy_match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        legacy_match.save_changes.assert_not_awaited()
        clan.save_changes.assert_awaited_once()

    @patch("cogs.match_commands.Match")
    async def test_match_list_success(self, mock_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.guild.fetch_member = AsyncMock(side_effect=lambda user_id: MagicMock(
            id=int(user_id), display_name=f"Player {user_id}",
        ))
        
        # Mock matches
        mock_match1 = MagicMock(spec=Match)
        mock_match1.matched_time = 3000000000
        mock_match1.participants = [
            MagicMock(user_id="111"),
            MagicMock(user_id="222")
        ]
        mock_match1.host_clans = ["#CLAN1", "#CLAN2"]
        
        mock_match2 = MagicMock(spec=Match)
        mock_match2.matched_time = 3000000100
        mock_match2.participants = [
            MagicMock(user_id="333"),
            MagicMock(user_id="444")
        ]
        mock_match2.host_clans = [None]

        mock_match.find_all.return_value.to_list = AsyncMock(return_value=[mock_match1, mock_match2])
        
        # Mock clan fetch
        mock_clan = MagicMock()
        mock_clan.name = "Test Clan"
        mock_clan.tag = "#CLAN1"
        mock_clan.share_link = "http://link"
        
        # Execute
        with patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client_prop:
             mock_coc_client_prop.return_value.get_clan = AsyncMock(return_value=mock_clan)
             await self.cog.match_list.callback(ctx, hidden=False)

        # Verify
        ctx.defer.assert_called_once_with(ephemeral=False)
        mock_match.find_all.assert_called_once()
        ctx.send.assert_called_once()
        
        # Check embed content
        _, kwargs = ctx.send.call_args
        embed = kwargs.get('embed')
        self.assertIsNotNone(embed)
        self.assertIn("Upcoming Elo Matches", embed.title)
        self.assertIn("**[Player 111](<https://discordapp.com/users/111>)** vs. "
                      "**[Player 222](<https://discordapp.com/users/222>)**", embed.description)
        self.assertIn("**[Player 333](<https://discordapp.com/users/333>)** vs. "
                      "**[Player 444](<https://discordapp.com/users/444>)**", embed.description)
        self.assertIn("[Test Clan (#CLAN1)]", embed.description)

    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    @patch("cogs.match_commands.is_player_occupied")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_match_queue_success_add_to_tier3(
            self, mock_server_state_current, mock_is_occupied, mock_queue, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 123
        ctx.author.has_role.return_value = False
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        mock_server_state_current.return_value = MagicMock(queue_disabled=False)
        
        # Mock player
        mock_player_doc = MagicMock(spec=Player)
        mock_player_doc.user_id = "123"
        mock_player_doc.priority = False
        mock_player_doc.elo = 1000
        mock_player_doc.matches = []
        mock_player_doc.district_presets = [None, None, None]
        mock_player_doc.capital_peak_enabled = True
        mock_player.find_one = AsyncMock(return_value=mock_player_doc)
        
        # Mock queues (tier 1, 2, 3)
        mock_queue_doc = MagicMock(spec=Queue)
        mock_queue_doc.tier = QUEUE_TIER_NORMAL
        mock_queue_doc.players = []
        mock_queue_doc.entered_at = {}
        mock_queue_doc.district_selections = {}
        self.queue_docs[QUEUE_TIER_NORMAL] = mock_queue_doc
        mock_queue_doc.save_changes = AsyncMock()
        
        # Explicitly make find_one an AsyncMock that returns mock_queue_doc
        mock_queue.find_one = AsyncMock(return_value=mock_queue_doc)
        
        # Mock is_player_occupied
        mock_is_occupied.return_value = None
        
        # Execute
        await self.cog.match_queue.callback(ctx)
        
        # Verify
        ctx.defer.assert_called_once_with(ephemeral=True)
        # Should check if player added to tier 3 queue (normal)
        # Since we mocked find_one to return the same queue object, we check if append was called
        self.assertIn(mock_player_doc, mock_queue_doc.players)
        self.assertIn(mock_player_doc.user_id, mock_queue_doc.entered_at)
        mock_queue_doc.save_changes.assert_called()
        ctx.send.assert_called()
        self.assertIn("You are now added to the match-making queue", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    @patch("cogs.match_commands.is_player_occupied")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_match_queue_priority_adds_to_tier2_when_closest_over_100_without_eligible_match(
            self, mock_server_state_current, mock_is_occupied, mock_queue, mock_player):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 123
        ctx.author.has_role.return_value = False
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        player = self.make_player("123", 1000)
        far_player = self.make_player("456", 1201)
        tier0 = self.make_queue(QUEUE_TIER_AGED)
        tier1 = self.make_queue(QUEUE_TIER_MIXED)
        tier2 = self.make_queue(QUEUE_TIER_PRIORITY)
        tier3 = self.make_queue(QUEUE_TIER_NORMAL, [far_player])

        mock_server_state_current.return_value = MagicMock(queue_disabled=False)
        mock_player.find_one = AsyncMock(return_value=player)
        mock_player.find_all.return_value.to_list = AsyncMock(return_value=[player, far_player])
        mock_queue.find_one = AsyncMock(side_effect=[tier0, tier1, tier2, tier3])
        mock_is_occupied.return_value = None

        await self.cog.match_queue.callback(ctx, priority=True)

        self.assertIn(player, tier2.players)
        self.assertIn(player.user_id, tier2.entered_at)
        self.assertNotIn(player, tier3.players)
        tier2.save_changes.assert_called()
        ctx.send.assert_called()
        self.assertIn("You are now added to the match-making queue", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    @patch("cogs.match_commands.is_player_occupied")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_match_queue_priority_with_empty_queue_adds_to_tier3(
            self, mock_server_state_current, mock_is_occupied, mock_queue, mock_player):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 123
        ctx.author.has_role.return_value = False
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        player = self.make_player("123", 1000)
        tier0 = self.make_queue(QUEUE_TIER_AGED)
        tier1 = self.make_queue(QUEUE_TIER_MIXED)
        tier2 = self.make_queue(QUEUE_TIER_PRIORITY)
        tier3 = self.make_queue(QUEUE_TIER_NORMAL)

        mock_server_state_current.return_value = MagicMock(queue_disabled=False)
        mock_player.find_one = AsyncMock(return_value=player)
        mock_player.find_all.return_value.to_list = AsyncMock(return_value=[player])
        mock_queue.find_one = AsyncMock(side_effect=[tier0, tier1, tier2, tier3])
        mock_is_occupied.return_value = None

        await self.cog.match_queue.callback(ctx, priority=True)

        self.assertIn(player, tier3.players)
        self.assertIn(player.user_id, tier3.entered_at)
        self.assertNotIn(player, tier2.players)
        tier3.save_changes.assert_called()
        ctx.send.assert_called()
        self.assertIn("You are now added to the match-making queue", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.get_last_opponent")
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    @patch("cogs.match_commands.is_player_occupied")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_match_queue_priority_closest_within_100_uses_normal_queue(
            self, mock_server_state_current, mock_is_occupied, mock_queue, mock_player, mock_get_last_opponent):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 123
        ctx.author.has_role.return_value = False
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        player = self.make_player("123", 1000)
        recent_opponent = self.make_player("456", 1050)
        tier0 = self.make_queue(QUEUE_TIER_AGED)
        tier1 = self.make_queue(QUEUE_TIER_MIXED)
        tier2 = self.make_queue(QUEUE_TIER_PRIORITY)
        tier3 = self.make_queue(QUEUE_TIER_NORMAL, [recent_opponent])

        mock_server_state_current.return_value = MagicMock(queue_disabled=False)
        mock_player.find_one = AsyncMock(return_value=player)
        mock_player.find_all.return_value.to_list = AsyncMock(return_value=[player, recent_opponent])
        mock_queue.find_one = AsyncMock(side_effect=[tier0, tier1, tier2, tier3])
        mock_is_occupied.return_value = None
        mock_get_last_opponent.side_effect = [recent_opponent.user_id, None]

        await self.cog.match_queue.callback(ctx, priority=True)

        self.assertIn(player, tier3.players)
        self.assertIn(player.user_id, tier3.entered_at)
        self.assertNotIn(player, tier2.players)
        tier3.save_changes.assert_called()
        ctx.send.assert_called()
        self.assertIn("You are now added to the match-making queue", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.initiate_match")
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    @patch("cogs.match_commands.is_player_occupied")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_match_queue_priority_matches_immediately_within_150(
            self, mock_server_state_current, mock_is_occupied, mock_queue, mock_player, mock_initiate_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 123
        ctx.author.has_role.return_value = False
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        player = self.make_player("123", 1000)
        opponent = self.make_player("456", 1130)
        tier0 = self.make_queue(QUEUE_TIER_AGED)
        tier1 = self.make_queue(QUEUE_TIER_MIXED)
        tier2 = self.make_queue(QUEUE_TIER_PRIORITY)
        tier3 = self.make_queue(QUEUE_TIER_NORMAL, [opponent], {opponent.user_id: datetime.now().timestamp()})

        mock_server_state_current.return_value = MagicMock(queue_disabled=False)
        mock_player.find_one = AsyncMock(return_value=player)
        mock_player.find_all.return_value.to_list = AsyncMock(return_value=[player, opponent])
        mock_queue.find_one = AsyncMock(side_effect=[tier0, tier1, tier2, tier3])
        mock_is_occupied.return_value = None

        async def insert_match(_bot, _player, _opponent, *, on_inserted):
            await on_inserted()

        mock_initiate_match.side_effect = insert_match

        await self.cog.match_queue.callback(ctx, priority=True)

        mock_initiate_match.assert_awaited_once_with(ANY, player, opponent, on_inserted=ANY)
        self.assertNotIn(opponent, tier3.players)
        self.assertNotIn(opponent.user_id, tier3.entered_at)
        tier3.save_changes.assert_called()
        self.assertIn("A match has been found", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    @patch("cogs.match_commands.is_player_occupied")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_match_queue_already_in_queue(
            self, mock_server_state_current, mock_is_occupied, mock_queue, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 123
        ctx.author.has_role.return_value = False
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        mock_server_state_current.return_value = MagicMock(queue_disabled=False)
        
        mock_player_doc = MagicMock(spec=Player)
        mock_player_doc.user_id = "123"
        mock_player_doc.priority = False
        mock_player_doc.elo = 1000
        mock_player_doc.matches = []
        mock_player.find_one = AsyncMock(return_value=mock_player_doc)
        
        # Mock queue to contain player
        queues = [self.make_queue(tier) for tier in (
            QUEUE_TIER_AGED, QUEUE_TIER_MIXED, QUEUE_TIER_PRIORITY, QUEUE_TIER_NORMAL,
        )]
        queues[-1].players = [mock_player_doc]
        queues[-1].entered_at = {mock_player_doc.user_id: datetime.now().timestamp()}
        mock_queue.find_one = AsyncMock(side_effect=queues)
        
        # Execute
        await self.cog.match_queue.callback.func(self.cog, ctx)
        
        # Verify
        ctx.send.assert_called_with(f"{ERROR_EMOJI} You are already in the match-making queue.", ephemeral=True)


    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    async def test_match_dequeue_success(self, mock_queue, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.User)
        ctx.author.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # Mock player
        mock_player_doc = MagicMock(spec=Player)
        mock_player_doc.user_id = "123"
        mock_player.find_one = AsyncMock(return_value=mock_player_doc)

        # Mock queue with player
        mock_queue_doc = MagicMock(spec=Queue)
        mock_queue_doc.players = [mock_player_doc]
        mock_queue_doc.district_selections = {}
        mock_queue_doc.save_changes = AsyncMock()
        
        # Mock find_one to return queue for tier 1, then None for others
        mock_queue.find_one = AsyncMock(side_effect=[mock_queue_doc, None, None, None])

        # Execute
        await self.cog.match_dequeue(ctx)

        # Verify
        self.assertNotIn(mock_player_doc, mock_queue_doc.players)
        mock_queue_doc.save_changes.assert_called_once()
        ctx.send.assert_called_with(f"{SUCCESS_EMOJI} You have been removed from the match-making queue.", ephemeral=True)

    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Queue")
    async def test_match_dequeue_not_in_queue(self, mock_queue, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.User)
        ctx.author.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # Mock player
        mock_player_doc = MagicMock(spec=Player)
        mock_player_doc.user_id = "123"
        mock_player.find_one = AsyncMock(return_value=mock_player_doc)

        # Mock empty queues
        mock_queue_doc = MagicMock(spec=Queue)
        mock_queue_doc.players = []
        mock_queue_doc.district_selections = {}
        mock_queue.find_one = AsyncMock(return_value=mock_queue_doc)

        # Execute
        await self.cog.match_dequeue.callback(ctx)

        # Verify
        ctx.send.assert_called_with(f"{ERROR_EMOJI} You are not currently in the match-making queue.", ephemeral=True)

    @patch.object(ServerState, "update_fields", new_callable=AsyncMock)
    @patch("cogs.match_commands.match_players")
    @patch("cogs.match_commands.get_match_queue")
    async def test_create_matches_promotes_15_hour_tier1_players_to_tier0(
            self, mock_get_match_queue, mock_match_players, mock_update_fields):
        now = datetime.now().timestamp()
        old_player = self.make_player("123", 1000)
        young_player = self.make_player("456", 1010)

        tier0 = self.make_queue(QUEUE_TIER_AGED)
        tier1 = self.make_queue(
            QUEUE_TIER_MIXED,
            [old_player, young_player],
            {
                old_player.user_id: now - QUEUE_TIER1_AGED_AFTER_SECONDS - 1,
                young_player.user_id: now,
            },
        )
        tier2 = self.make_queue(QUEUE_TIER_PRIORITY)
        tier3 = self.make_queue(QUEUE_TIER_NORMAL)
        queues = {
            QUEUE_TIER_AGED: tier0,
            QUEUE_TIER_MIXED: tier1,
            QUEUE_TIER_PRIORITY: tier2,
            QUEUE_TIER_NORMAL: tier3,
        }

        mock_get_match_queue.side_effect = lambda tier: queues[tier]
        mock_match_players.return_value = None
        await self.cog.create_matches.callback()

        self.assertIn(old_player, tier0.players)
        self.assertNotIn(old_player, tier1.players)
        self.assertIn(old_player.user_id, tier0.entered_at)
        mock_match_players.assert_any_await(ANY, QUEUE_TIER_AGED, 250, queue=tier0)
        mock_update_fields.assert_awaited_once_with(last_matching=ANY)

    @patch.object(ServerState, "update_fields", new_callable=AsyncMock)
    @patch("cogs.match_commands.match_players")
    @patch("cogs.match_commands.get_match_queue")
    async def test_create_matches_moves_each_queue_down_one_tier(
            self, mock_get_match_queue, mock_match_players, mock_update_fields):
        now = datetime.now().timestamp()
        tier2_player = self.make_player("222", 1000)
        tier3_player = self.make_player("333", 1000)

        tier0 = self.make_queue(QUEUE_TIER_AGED)
        tier1 = self.make_queue(QUEUE_TIER_MIXED)
        tier2 = self.make_queue(QUEUE_TIER_PRIORITY, [tier2_player], {tier2_player.user_id: now})
        tier3 = self.make_queue(QUEUE_TIER_NORMAL, [tier3_player], {tier3_player.user_id: now})
        queues = {
            QUEUE_TIER_AGED: tier0,
            QUEUE_TIER_MIXED: tier1,
            QUEUE_TIER_PRIORITY: tier2,
            QUEUE_TIER_NORMAL: tier3,
        }

        mock_get_match_queue.side_effect = lambda tier: queues[tier]
        mock_match_players.return_value = None
        await self.cog.create_matches.callback()

        self.assertIn(tier2_player, tier1.players)
        self.assertIn(tier3_player, tier2.players)
        self.assertFalse(tier3.players)
        mock_update_fields.assert_awaited_once_with(last_matching=ANY)

    @patch("cogs.match_commands.record_match")
    @patch("cogs.match_commands.Player")
    async def test_match_submit_success(self, mock_player, mock_record_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        player1 = MagicMock(spec=ipy.User)
        player1.id = 111
        player2 = MagicMock(spec=ipy.User)
        player2.id = 222
        
        # Mock players found
        mock_player.find_one = AsyncMock(side_effect=[
            MagicMock(elo=1300, capital_peak_enabled=False),
            MagicMock(elo=1300, capital_peak_enabled=False),
        ])

        # Execute
        await self.cog.match_submit.callback(
            ctx, 
            player1=player1, 
            player2=player2, 
            player1_result="3.100\n3.100\n3.100", 
            player2_result="2.90\n2.80\n1.50",
            district1="Barbarian Camp",
            district2="Balloon Lagoon",
            district3="Dragon Cliffs"
        )

        # Verify
        mock_record_match.assert_called_once()
        ctx.send.assert_called()
        self.assertIn("is submitted", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.record_match")
    @patch("cogs.match_commands.Player")
    async def test_match_submit_rejects_unavailable_conditional_districts(
            self, mock_player, mock_record_match):
        # Given: Wizard is at its Elo limit or either player has opted out of Peak
        cases = [
            (
                [MagicMock(elo=1200, capital_peak_enabled=True),
                 MagicMock(elo=1199, capital_peak_enabled=True)],
                ("Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"),
            ),
            (
                [MagicMock(elo=1200, capital_peak_enabled=True),
                 MagicMock(elo=1200, capital_peak_enabled=False)],
                ("Barbarian Camp", "Balloon Lagoon", "Capital Peak"),
            ),
        ]
        users = [MagicMock(id=111), MagicMock(id=222)]

        for player_docs, districts in cases:
            with self.subTest(districts=districts):
                ctx = MagicMock(spec=ipy.SlashContext, guild=MagicMock())
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                mock_player.find_one = AsyncMock(side_effect=player_docs)
                mock_record_match.reset_mock()

                # When: a forged manual submission requests the unavailable district
                await self.cog.match_submit.callback(
                    ctx,
                    player1=users[0],
                    player2=users[1],
                    player1_result="3.100\n3.100\n3.100",
                    player2_result="2.90\n2.80\n1.50",
                    district1=districts[0],
                    district2=districts[1],
                    district3=districts[2],
                )

                # Then: the forged result never reaches recording
                mock_record_match.assert_not_awaited()
                self.assertIn("not available", ctx.send.await_args.args[0].lower())

    @patch("cogs.match_commands.Match")
    async def test_match_resubmit_success(self, mock_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 123
        ctx.channel.name = "🔴 Match Channel"
        ctx.send_modal = AsyncMock()
        ctx.send = AsyncMock()
        
        # Mock match found
        mock_match_doc = MagicMock()
        mock_match_doc.is_autowin = False
        # Configure __contains__ to return False (author not in match)
        mock_match_doc.__contains__.return_value = False
        
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)

        # Execute
        await self.cog.match_resubmit.callback(ctx)

        # Verify
        ctx.send_modal.assert_called_once()
        modal = ctx.send_modal.call_args[0][0]
        self.assertIsInstance(modal, ipy.Modal)
        self.assertEqual(modal.title, "Result Submission")

    async def test_match_autowin_success(self):
        from elobot.db import Helper
        from tests._autowin_fixture import AutowinState

        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.rows[Match][0].update(is_war=False, hosts=["900", None], times=[1000, None])
        ctx = fixture.context()

        await fixture.invoke(ctx)

        self.assertTrue(fixture.rows[Match][0]["is_autowin"])
        self.assertTrue(fixture.rows[Match][0]["autowin_completed"])
        self.assertEqual(fixture.elos(), [1005, 965])
        self.assertEqual(fixture.rows[Helper][0]["points"], 3)
        self.assertIn("An auto-win has been given", ctx.send.call_args[0][0])
        action_rows = ctx.channel.send.await_args.kwargs["components"]
        self.assertEqual(
            [(button.label, button.custom_id) for button in action_rows[0].components],
            [("Delete Thread", "delete_thread|AUTOWIN")],
        )

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.match_commands.BaseLayout")
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_success(
            self, mock_choose_bases, mock_base_layout, mock_preparation_match, mock_match, mock_sync_slots):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.channel.id = 12345
        ctx.channel.send = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        clan_obj.share_link = "http://clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 1", "Slot 2"]}
        
        # Mock match found
        mock_match_doc = MagicMock()
        mock_match_doc.token = "TOKEN"
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match_doc.final_districts = ["D1", "D2", "D3"]
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_message_binding(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one
        
        # Mock existing base check (none found)
        mock_base_layout.find_one = AsyncMock(return_value=None)
        
        # Execute
        with (
            patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
            patch("cogs.base_store.publication.Match.find_one", new=AsyncMock(return_value=mock_match_doc)),
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        # Verify
        mock_choose_bases.assert_called()
        mock_match_doc.get_pymongo_collection.return_value.update_one.assert_awaited_once()
        self.assertEqual(mock_match_doc.participants[0].base_store.clan_tag, "#CLAN")
        self.assertEqual(mock_match_doc.participants[0].base_store.slot, "Slot 1")
        self.assertEqual(ctx.channel.send.await_args.kwargs["embed"].title, "**Base Storage**")
        self.assertEqual(ctx.channel.send.await_args.kwargs["components"], [])
        published = ctx.channel.send.return_value
        self.assertEqual(published.edit.await_args.kwargs["embed"]["title"], "**Base Slot Reserved**")
        self.assertTrue(published.edit.await_args.kwargs["components"])
        self.assertEqual(mock_match_doc.participants[0].base_store.message_id, int(published.id))
        self.assertTrue(mock_match_doc.participants[0].base_store.ready)
        ctx.send.assert_called()
        self.assertIn("Please store the bases", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_chooses_bases_before_reserving_slot(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.channel.id = 12345
        ctx.channel.send = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 1", "Slot 2"]}

        mock_match_doc = MagicMock()
        mock_match_doc.token = "TOKEN"
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match_doc.final_districts = ["D1", "D2", "D3"]
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_message_binding(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one

        async def choose_bases_side_effect(*_args, **_kwargs):
            self.assertFalse(mock_match_doc.participants[0].opponent_bases_stored)
            self.assertIsNone(mock_match_doc.participants[0].base_store)

        mock_choose_bases.side_effect = choose_bases_side_effect

        with (
            patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
            patch("cogs.base_store.publication.Match.find_one", new=AsyncMock(return_value=mock_match_doc)),
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        mock_choose_bases.assert_awaited_once()
        self.assertTrue(mock_match_doc.participants[0].opponent_bases_stored)
        self.assertEqual(mock_match_doc.participants[0].base_store.slot, "Slot 1")
        ctx.send.assert_called()
        self.assertIn("Please store the bases", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_serializes_reservations_for_the_same_participant(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        state = {"stored": False, "base_store": None}
        claim_entered = asyncio.Event()
        release_claim = asyncio.Event()
        second_preparation_entered = asyncio.Event()
        prepare = self.cog._prepare_base_store
        preparation_calls = 0

        async def prepare_store(request):
            nonlocal preparation_calls
            preparation_calls += 1
            if preparation_calls == 2:
                second_preparation_entered.set()
            return await prepare(request)

        def make_match():
            participant = MagicMock(
                user_id="123",
                opponent_bases_stored=state["stored"],
                base_store=state["base_store"],
                result=[None, None, None],
            )
            opponent = MagicMock(
                user_id="456",
                bases=[],
                result=[None, None, None],
            )
            match = MagicMock(
                token="TOKEN",
                is_war=False,
                is_autowin=False,
                participants=[participant, opponent],
                final_districts=["D1", "D2", "D3"],
            )

            async def save_changes():
                state["stored"] = participant.opponent_bases_stored
                state["base_store"] = participant.base_store

            match.save_changes = AsyncMock(side_effect=save_changes)
            self.enable_base_store_message_binding(match)
            async def claim(_query, update):
                claim_entered.set()
                with anyio.fail_after(5):
                    await release_claim.wait()
                if state["stored"]:
                    return MagicMock(matched_count=0)
                state["stored"] = True
                state["base_store"] = BaseStore.model_validate(update["$set"]["participants.0.base_store"])
                return MagicMock(matched_count=1)
            match.get_pymongo_collection.return_value.update_one.side_effect = claim
            return match

        mock_match.find_one = AsyncMock(side_effect=lambda *_args, **_kwargs: make_match())
        mock_preparation_match.find_one = mock_match.find_one
        mock_sync_slots.return_value = {"#CLAN": list(BASE_STORE_SLOTS)}

        clan_obj = MagicMock(tag="#CLAN", name="Test Clan", share_link="http://clan")

        def make_ctx():
            ctx = MagicMock(spec=ipy.SlashContext)
            ctx.channel.guild.fetch_member = AsyncMock(
                side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
            )
            ctx.channel.id = 12345
            ctx.channel.send = AsyncMock()
            ctx.defer = AsyncMock()
            ctx.send = AsyncMock()
            ctx.author.id = 999
            return ctx

        contexts = [make_ctx(), make_ctx()]
        with (
            patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
            patch("cogs.base_store.publication.Match.find_one", new=AsyncMock(side_effect=lambda *_args: make_match())),
            patch.object(self.cog, "_prepare_base_store", new=AsyncMock(side_effect=prepare_store)),
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            with anyio.fail_after(5):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(
                        self.cog.match_base_store.callback(contexts[0], player=0, clan="#CLAN", slot="Slot 1"),
                    )
                    await claim_entered.wait()
                    tasks.create_task(
                        self.cog.match_base_store.callback(contexts[1], player=0, clan="#CLAN", slot="Slot 2"),
                    )
                    await second_preparation_entered.wait()
                    self.assertEqual(mock_choose_bases.await_count, 1)
                    self.assertFalse(state["stored"])
                    release_claim.set()

        self.assertEqual(
            mock_choose_bases.await_count,
            1,
            [ctx.send.await_args_list for ctx in contexts],
        )
        self.assertTrue(state["stored"])
        self.assertEqual(state["base_store"].slot, "Slot 1")
        self.assertTrue(state["base_store"].ready)
        contexts[0].channel.send.assert_awaited_once()
        contexts[1].channel.send.assert_not_awaited()
        contexts[1].send.assert_awaited_once()

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_does_not_claim_slot_when_choose_bases_fails(
            self, mock_choose_bases, _mock_storage_sync, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 1", "Slot 2"]}

        mock_match_doc = MagicMock(result_operation=None, autowin_winner_id=None)
        mock_match_doc.token = "TOKEN"
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match_doc.final_districts = ["D1", "D2", "D3"]
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_clear(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one
        mock_choose_bases.side_effect = RuntimeError("base selection failed")

        with patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            with self.assertRaises(RuntimeError):
                await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        self.assertIsNone(mock_match_doc.participants[0].base_store)
        self.assertFalse(mock_match_doc.participants[0].opponent_bases_stored)
        mock_match_doc.save_changes.assert_not_awaited()
        mock_match_doc.clear_base_store.assert_not_awaited()

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_does_not_claim_slot_when_preclaim_resync_fails(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")
        mock_sync_slots.side_effect = [
            {"#CLAN": ["Slot 1", "Slot 2"]},
            RuntimeError("slot synchronization failed"),
        ]

        participant = MagicMock(user_id="123", opponent_bases_stored=False, base_store=None)
        mock_match_doc = MagicMock(
            token="TOKEN", result_operation=None, autowin_winner_id=None,
            participants=[participant, MagicMock(user_id="456", opponent_bases_stored=False, base_store=None)],
        )
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_clear(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one

        with patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            with self.assertRaisesRegex(RuntimeError, "slot synchronization failed"):
                await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        mock_match_doc.save_changes.assert_not_awaited()
        mock_match_doc.clear_base_store.assert_not_awaited()
        mock_choose_bases.assert_awaited_once()

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_blocks_unavailable_slot(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 2"]}

        mock_match_doc = MagicMock(result_operation=None, autowin_winner_id=None)
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one

        with patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        mock_choose_bases.assert_not_called()
        ctx.send.assert_called()
        self.assertIn("already being used", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.sync_base_store_slots")
    @patch("cogs.match_commands.Match")
    @patch("cogs.base_store.preparation.Match")
    @patch("cogs.match_commands.choose_bases")
    async def test_match_base_store_blocks_when_clan_slots_cannot_be_verified(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")
        mock_sync_slots.return_value = {}

        participant = MagicMock(user_id="123", opponent_bases_stored=False, base_store=None)
        mock_match.find_one = AsyncMock(return_value=MagicMock(
            participants=[participant, MagicMock(user_id="456")], result_operation=None, autowin_winner_id=None,
        ))
        mock_preparation_match.find_one = mock_match.find_one

        with patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        mock_choose_bases.assert_not_awaited()
        self.assertFalse(participant.opponent_bases_stored)
        self.assertIn("Unable to verify", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.is_raid_weekend", return_value=False)
    @patch("cogs.base_store.repository.Clan")
    @patch("cogs.base_store.repository.Match")
    async def test_sync_base_store_slots_tracks_occupancy_outside_raid_weekend(
            self, mock_match, mock_clan, mock_is_raid_weekend):
        from cogs.match_commands import sync_base_store_slots

        match = MagicMock(participants=[
            MagicMock(base_store=BaseStore(
                clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
            ))
        ])
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        clan = MagicMock()
        clan.tag = "#CLAN"
        clan.available_slots = []
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN": ["Slot 2"]})
        self.assertEqual(clan.available_slots, ["Slot 2"])
        clan.save_changes.assert_awaited_once()
        mock_is_raid_weekend.assert_not_called()

    @patch("cogs.base_store.repository.Clan")
    @patch("cogs.base_store.repository.Match")
    async def test_sync_base_store_slots_blocks_all_clans_for_pending_legacy_reservation(
            self, mock_match, mock_clan):
        from cogs.match_commands import sync_base_store_slots

        legacy_match = MagicMock(
            thread="987",
            participants=[MagicMock(base_store=None, opponent_bases_stored=True)],
            times=[None],
        )
        mock_match.find.return_value.to_list = AsyncMock(return_value=[legacy_match])

        clans = [
            MagicMock(tag="#CLAN1", available_slots=list(BASE_STORE_SLOTS)),
            MagicMock(tag="#CLAN2", available_slots=list(BASE_STORE_SLOTS)),
        ]
        for clan in clans:
            clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=clans)

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN1": [], "#CLAN2": []})
        for clan in clans:
            self.assertEqual(clan.available_slots, [])
            clan.save_changes.assert_awaited_once()

    @patch("cogs.base_store.repository.Clan")
    @patch("cogs.base_store.repository.Match")
    async def test_sync_base_store_slots_ignores_orphaned_reservations(self, mock_match, mock_clan):
        from cogs.match_commands import sync_base_store_slots

        orphaned_match = MagicMock(
            thread=None,
            participants=[
                MagicMock(
                    user_id="1",
                    base_store=BaseStore(
                        clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                    ),
                ),
                MagicMock(
                    user_id="2",
                    base_store=None,
                    opponent_bases_stored=True,
                ),
            ],
            times=[None, None],
        )
        mock_match.find.return_value.to_list = AsyncMock(return_value=[orphaned_match])

        clan = MagicMock(tag="#CLAN", available_slots=[])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN": list(BASE_STORE_SLOTS)})
        self.assertEqual(clan.available_slots, list(BASE_STORE_SLOTS))
        clan.save_changes.assert_awaited_once()

    @patch("cogs.base_store.repository.Clan")
    @patch("cogs.base_store.repository.Match")
    async def test_sync_base_store_slots_does_not_block_for_started_legacy_match(
            self, mock_match, mock_clan):
        from cogs.match_commands import sync_base_store_slots

        started_match = MagicMock(
            participants=[MagicMock(base_store=None, opponent_bases_stored=True)],
            times=[100],
        )
        mock_match.find.return_value.to_list = AsyncMock(return_value=[started_match])

        clan = MagicMock(tag="#CLAN", available_slots=[])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN": list(BASE_STORE_SLOTS)})
        self.assertEqual(clan.available_slots, list(BASE_STORE_SLOTS))
        clan.save_changes.assert_awaited_once()

    @patch("cogs.base_store.repository.latest_completed_raid_weekend_end_timestamp", return_value=200)
    @patch("cogs.base_store.repository.Clan")
    @patch("cogs.base_store.repository.Match")
    async def test_sync_base_store_slots_expires_reservations_from_completed_weekend(
            self, mock_match, mock_clan, _mock_latest_raid_end):
        from cogs.match_commands import sync_base_store_slots

        participant = MagicMock(
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=100),
            opponent_bases_stored=True,
        )
        match = MagicMock(participants=[participant], times=[None])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        clan = MagicMock(tag="#CLAN", available_slots=[])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        self.assertEqual(available, {"#CLAN": list(BASE_STORE_SLOTS)})

    @patch("cogs.match_commands.sync_base_store_slots", new_callable=AsyncMock)
    async def test_on_start_reconciles_base_store_slots_before_other_tasks(self, mock_sync_slots):
        with patch.object(
                self.cog,
                "inactive_threads_delete",
                new=AsyncMock(side_effect=RuntimeError("stop after reconciliation"))
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after reconciliation"):
                await self.cog.on_start.callback(self.cog)

        mock_sync_slots.assert_awaited_once_with(bot=self.bot)

    @patch("cogs.match_commands.ServerState.current", new_callable=AsyncMock)
    @patch("cogs.match_commands.sync_base_store_slots", new_callable=AsyncMock)
    async def test_on_start_runs_inactive_thread_cleanup_only_on_main_bot(self, _mock_sync_slots, mock_state):
        mock_state.return_value = MagicMock(last_matching=None)
        task_names = (
            "inactive_threads_delete", "auto_match_confirm", "auto_match_cancel", "match_confirmation_remind",
            "base_notify", "host_notify", "match_data_clean_up", "repair_match_publications",
            "raid_weekend_start_notify", "raid_weekend_auto_store", "update_clan_availability_embed",
            "update_hosting_queue_embed", "create_matches",
        )
        for bot_id, cleanup_runs in ((BOT_ID, True), (BOT_ID + 1, False)):
            with self.subTest(bot_id=bot_id):
                self.bot.user.id = bot_id
                tasks = {name: AsyncMock() for name in task_names}
                for task in tasks.values():
                    task.start = MagicMock()
                with patch.multiple(self.cog, **tasks):
                    await self.cog.on_start.callback(self.cog)

                cleanup = tasks["inactive_threads_delete"]
                self.assertEqual(cleanup.await_count, int(cleanup_runs))
                self.assertEqual(cleanup.start.call_count, int(cleanup_runs))
                tasks["auto_match_confirm"].assert_awaited_once()
                tasks["auto_match_confirm"].start.assert_called_once()
                tasks["create_matches"].start.assert_called_once()

    @patch("cogs.match_commands.clear_participant_base_store", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_undo_base_storage_button_releases_stored_slot(self, mock_match, mock_clear_base_store):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "undo_base_storage|TOKEN|0"
        ctx.message.delete = AsyncMock()
        ctx.message.edit = AsyncMock()
        ctx.message.components = []
        ctx.message.id = 777
        ctx.message.embeds = [ipy.Embed(title="**Base Slot Reserved**")]
        ctx.send = AsyncMock()

        match = MagicMock(is_war=True, participants=[Participant(
            user_id="123", opponent_bases_stored=True,
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", message_id=777),
        )])
        mock_match.find_one = AsyncMock(return_value=match)
        mock_clear_base_store.return_value = True

        await self.cog.undo_base_storage_button.callback(ctx)

        mock_clear_base_store.assert_awaited_once_with(
            match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
        )
        ctx.message.delete.assert_not_awaited()
        self.assertEqual(ctx.message.edit.await_args.kwargs["components"], [])
        ctx.send.assert_called()
        self.assertIn("undone", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.Match")
    async def test_failed_manual_store_recovery_does_not_claim_undo_was_restored(self, mock_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.author.id = 999
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(participants=[
            Participant(
                user_id="123",
                opponent_bases_stored=True,
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="HELD"),
            ),
            Participant(user_id="456"),
        ])
        mock_match.find_one = AsyncMock(return_value=match)

        async def fail_recovery(interaction_ctx, _presentation) -> bool:
            await interaction_ctx.send("Stored-slot controls could not be confirmed.", ephemeral=True)
            return False

        with (
                patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as coc_client,
                patch(
                    "cogs.match_commands.base_store_publication.present_retained_base_store",
                    new=AsyncMock(side_effect=fail_recovery),
                ),
        ):
            coc_client.return_value.get_clan = AsyncMock(return_value=MagicMock())
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        messages = [call.args[0] for call in ctx.send.await_args_list]
        self.assertEqual(messages, ["Stored-slot controls could not be confirmed."])
        self.assertTrue(all("has been restored" not in message for message in messages))

    @patch("cogs.match_commands.remove_helper_task", new_callable=AsyncMock)
    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.base_store.storage.clear_participant_base_store", new_callable=AsyncMock)
    @patch("cogs.match_commands.send_bases", new_callable=AsyncMock)
    @patch("cogs.match_commands.is_clan_available", new_callable=AsyncMock)
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Match")
    async def test_match_war_start_from_stored_button_retains_slot_after_sending_bases(
            self, mock_match, mock_player, mock_is_clan_available, mock_send_bases,
            mock_clear_base_store, _mock_free_clan, _mock_occupy_clan, mock_clan_find,
            _mock_remove_helper_task):
        events = []
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "start_match|0|#CLAN|Slot 1"
        ctx.message.id = 888
        ctx.message.embeds = [ipy.Embed(title="**Bases Stored**")]
        ctx.message.components = [ipy.ActionRow(ipy.Button(
            style=ipy.ButtonStyle.DANGER, label="Undo", custom_id="undo_base_storage|TOKEN|0",
        ))]
        ctx.message.edit = AsyncMock(side_effect=lambda **_kwargs: events.append("slot_reserved"))
        self.bot.fetch_channel = AsyncMock(return_value=ctx.channel)
        self.bot.cache.fetch_message = AsyncMock(return_value=ctx.message)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 12345
        ctx.channel.name = "🟡 Match Thread"
        ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[MagicMock(embeds=[MagicMock(fields=[])])])
        start_message = MagicMock(id=777)
        start_message.pin = AsyncMock()
        start_message.edit = AsyncMock(side_effect=lambda **_kwargs: events.append("activate"))

        async def record_pending(*_args, **kwargs):
            self.assertEqual(kwargs["components"], [])
            self.assertNotEqual(kwargs["embed"].title, "**Match Started**")
            events.append("pending")
            return start_message

        ctx.channel.send = AsyncMock(side_effect=record_pending)

        host = MagicMock(spec=ipy.User)
        host.id = 999
        host.mention = "<@999>"

        opponent_bases = [MagicMock(link="http://base1"), MagicMock(link="http://base2"), MagicMock(link="http://base3")]
        match = MagicMock()
        match.token = "TOKEN"
        match.thread = "12345"
        match.is_war = False
        match.times = [None, None]
        match.hosts = [None, None]
        match.host_clans = [None, None]
        match.persistent_host_clans = [None, None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [None, None]
        match.final_districts = ["D1", "D2", "D3"]
        match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=None)
            ),
            MagicMock(user_id="456", bases=opponent_bases, base_store=None),
        ]
        match.save_changes = AsyncMock()
        match.set = AsyncMock()
        match.get_saved_state.return_value = None
        self.enable_base_store_message_binding(match)
        collection = match.get_pymongo_collection.return_value

        async def record_binding(query, update):
            if "times.0" in update["$set"]:
                self.assertIsNone(query["times.0"])
                self.assertEqual(update["$set"]["hosts.0"], "999")
                self.assertEqual(update["$set"]["host_clans.0"], "#CLAN")
                return MagicMock(matched_count=1)
            if "message_publications" in update["$set"]:
                self.assertEqual(query["message_publications"], {"$exists": False})
                self.assertEqual(update["$set"]["message_publications"], [None, None])
                return MagicMock(matched_count=1)
            self.assertEqual(query["hosts.0"], "999")
            if set(update["$set"]) == {"message_publications.0"}:
                self.assertTrue(query["message_publications.0"]["pending"])
                self.assertFalse(update["$set"]["message_publications.0"].get("pending", False))
                self.assertEqual(query["old_msgs.0"], 777)
                return MagicMock(matched_count=1)
            self.assertEqual(query["host_clans.0"], "#CLAN")
            self.assertEqual(set(update["$set"]), {"old_msgs.0", "message_publications.0"})
            self.assertEqual(update["$set"]["old_msgs.0"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["message_id"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["embeds"][0]["title"], "**Match Started**")
            events.append("bind")
            return MagicMock(matched_count=1)

        collection.update_one = AsyncMock(side_effect=record_binding)
        collection.find_one = AsyncMock(return_value={"_id": match.id})
        _mock_occupy_clan.side_effect = lambda *_args: events.append("occupy")
        mock_send_bases.side_effect = lambda *_args, **_kwargs: events.append("send_bases")

        mock_match.find_one = AsyncMock(return_value=match)

        mock_is_clan_available.return_value = True
        mock_clan_find.return_value = MagicMock(availability_str="Available")
        mock_player.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
        clan_obj = MagicMock()
        clan_obj.name = "Test Clan"
        clan_obj.tag = "#CLAN"
        clan_obj.share_link = "http://clan"
        player_user = MagicMock(mention="<@123>")
        player_acc = MagicMock(tag="#PLAYER", share_link="http://player")

        with (
                patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch(
                    "cogs.match_commands.get_start_base_store_slot_availability",
                    new_callable=AsyncMock,
                    return_value=(["Slot 1"], {"Slot 2": ("987", "789")}),
                ) as mock_start_slots,
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            mock_coc_client.return_value.get_player = AsyncMock(return_value=player_acc)
            self.bot.fetch_user = AsyncMock(return_value=player_user)

            await self.cog.start_match_button.callback.func(self.cog, ctx)

        self.assertEqual(events, ["occupy", "pending", "bind", "activate", "send_bases", "slot_reserved"])
        self.assertEqual(start_message.edit.await_args.kwargs["embed"]["title"], "**Match Started**")
        self.assertTrue(start_message.edit.await_args.kwargs["components"])
        completion_writes = [item for item in collection.update_one.await_args_list
                             if set(item.args[1]["$set"]) == {"message_publications.0"}]
        self.assertEqual(len(completion_writes), 1)
        self.assertEqual(completion_writes[0].args[0]["old_msgs.0"], 777)
        self.assertEqual(match.old_msgs[0], 777)
        mock_send_bases.assert_awaited_once_with(ANY, "TOKEN", 0, 1, scout=False)
        self.assertEqual(match.attach_base_store_message.await_args_list,
                         [call(0, 888, ready=True), call(0, 888, ready=False)])
        self.assertEqual(match.participants[0].base_store.message_id, 888)
        self.assertFalse(match.participants[0].base_store.ready)
        self.assertEqual(ctx.message.edit.await_args.kwargs["embeds"][0]["title"], "**Bases Stored**")
        mock_clear_base_store.assert_not_awaited()
        self.assertEqual(match.host_clans[0], "#CLAN")
        self.assertEqual(match.persistent_host_clans[0], "#CLAN")
        ctx.send.assert_called()
        self.assertIn("stored in **slot 1** in **Test Clan**", ctx.send.call_args[0][0])
        self.assertNotIn("non-occupied base slot", ctx.send.call_args[0][0])
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)

    @patch(
        "cogs.match_commands.get_start_base_store_slot_availability",
        new_callable=AsyncMock,
        return_value=([], {"Slot 1": ("200", "2"), "Slot 2": ("300", "3")}),
    )
    @patch("cogs.match_commands.is_raid_weekend", return_value=False)
    @patch("cogs.match_commands.is_clan_available", new_callable=AsyncMock, return_value=True)
    @patch("cogs.match_commands.Match")
    async def test_match_war_start_blocks_when_both_foreign_slots_are_occupied(
            self, mock_match, _mock_is_clan_available, _mock_is_raid_weekend, mock_start_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 12345
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )

        match = MagicMock(
            thread="12345",
            is_war=True,
            times=[None, None],
            participants=[MagicMock(user_id="123", base_store=None, opponent_bases_stored=False),
                          MagicMock(user_id="456", base_store=None, opponent_bases_stored=False)],
        )
        mock_match.find_one = AsyncMock(return_value=match)
        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")

        with (
                patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch("cogs.match_commands.send_bases", new_callable=AsyncMock) as mock_send_bases,
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_war_start.callback(ctx, player=0, clan="#CLAN", scout=False)

        mock_send_bases.assert_not_awaited()
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)
        response = ctx.send.call_args[0][0]
        self.assertIn("both base slots", response.lower())
        self.assertIn("<#200> <@2>", response)
        self.assertIn("<#300> <@3>", response)

    async def test_start_war_uses_the_callers_preheld_reservations(self) -> None:
        from cogs.war_hosting.war_start import SelfHostedWarStartReply, WarStart, start_war

        match = MagicMock(
            token="TOKEN",
            thread="101",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            model_fields_set={"persistent_host_clans"},
            old_msgs=[None, None],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        match.set = AsyncMock()
        match.get_pymongo_collection.return_value.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        clan_reservation = MagicMock(clan_tag="#AVAILABLE")
        side_reservation = MagicMock(key=("TOKEN", 0))
        message = MagicMock(id=777)
        start = WarStart(
            match=match,
            player=0,
            host=MagicMock(id=100),
            clan=MagicMock(tag="#AVAILABLE"),
            coc_client=MagicMock(),
            reply=SelfHostedWarStartReply(),
            clan_reservation=clan_reservation,
            side_reservation=side_reservation,
        )

        with (
                patch(
                    "cogs.war_hosting.war_start.match_commands.current_resource_clan_reservation",
                    return_value=None,
                ),
                patch(
                    "cogs.war_hosting.war_start.match_commands.try_reserve_resource_clan",
                    new_callable=AsyncMock,
                ) as reserve_clan,
                patch(
                    "cogs.war_hosting.war_start.match_commands.current_resource_match_side_reservation",
                    return_value=None,
                ),
                patch(
                    "cogs.war_hosting.war_start.match_commands.try_reserve_resource_match_side",
                    new_callable=AsyncMock,
                ) as reserve_side,
                patch(
                    "cogs.war_hosting.war_start.match_commands.Match.find_one",
                    new=AsyncMock(return_value=match),
                ),
                patch(
                    "cogs.war_hosting.war_start.match_commands.Clan.find_one",
                    new=AsyncMock(return_value=MagicMock(availability_str="Available")),
                ),
                patch(
                    "cogs.war_hosting.war_start.match_commands.occupy_clan",
                    new_callable=AsyncMock,
                ) as occupy,
                patch(
                    "cogs.war_hosting.war_start.match_commands.free_clan",
                    new_callable=AsyncMock,
                ),
                patch(
                    "cogs.war_hosting.war_start.complete_war_start",
                    new=AsyncMock(return_value=message),
                ),
        ):
            result = await start_war(MagicMock(), MagicMock(spec=ipy.SlashContext), start)

        self.assertIs(result, message)
        reserve_clan.assert_not_awaited()
        reserve_side.assert_not_awaited()
        clan_reservation.release.assert_called_once_with()
        side_reservation.release.assert_called_once_with()
        occupy.assert_awaited_once_with(match, 0)
        update = match.get_pymongo_collection.return_value.update_one.await_args.args[1]["$set"]
        self.assertEqual(update["hosts.0"], "100")
        self.assertEqual(update["host_clans.0"], "#AVAILABLE")
        self.assertEqual(update["persistent_host_clans.0"], "#AVAILABLE")
        self.assertEqual(match.persistent_host_clans[0], "#AVAILABLE")

    async def test_claim_resource_clan_initializes_persistent_array_for_legacy_match(self) -> None:
        legacy_match = Match.model_construct(
            token="TOKEN",
            matched_time=1,
            thread="101",
            host_clans=["#LEGACY", None],
            hosts=[None, None],
            participants=[MagicMock(user_id="100"), MagicMock(user_id="200")],
        )
        legacy_match.id = PydanticObjectId()
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        reservation = MagicMock(clan_tag="#AVAILABLE")
        side_reservation = MagicMock(key=("TOKEN", 1))

        with patch.object(Match, "find_one", new=AsyncMock(return_value=legacy_match)), \
                patch.object(Match, "get_pymongo_collection", return_value=collection), \
                patch.object(Match, "set", new_callable=AsyncMock), \
                patch("cogs.match_commands.Clan.find_one", new=AsyncMock(
                    return_value=MagicMock(availability_str="Available"))), \
                patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock):
            claimed = await match_commands.claim_resource_clan(
                reservation,
                legacy_match,
                1,
                side_reservation=side_reservation,
            )

        self.assertTrue(claimed)
        collection.update_one.assert_awaited_once_with(
            {"_id": legacy_match.id, "persistent_host_clans": {"$exists": False}},
            {"$set": {"persistent_host_clans": ["#LEGACY", None]}},
        )
        self.assertIn("persistent_host_clans", legacy_match.model_fields_set)
        self.assertEqual(legacy_match.persistent_host_clans, ["#LEGACY", "#AVAILABLE"])

    async def test_abort_started_war_side_preserves_persistent_host_clan(self) -> None:
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        match = Match.model_construct(
            token="TOKEN",
            matched_time=1,
            is_war=True,
            times=[1000, None],
            hosts=["100", None],
            host_clans=["#CURRENT", None],
            old_msgs=[777, None],
            participants=[],
        )
        match.id = PydanticObjectId()
        settings = MagicMock(use_state_management=True, state_management_save_previous=False, keep_nulls=True)

        with patch.object(Match, "get_pymongo_collection", return_value=collection), \
                patch.object(Match, "get_settings", return_value=settings):
            aborted = await match.abort_started_war_side(0, 1000, "100", "#CURRENT", 777)

        self.assertTrue(aborted)
        initialize, abort = collection.update_one.await_args_list
        self.assertEqual(
            initialize.args[1],
            {"$set": {"persistent_host_clans": ["#CURRENT", None]}},
        )
        update = abort.args[1]["$set"]
        self.assertNotIn("persistent_host_clans.0", update)
        self.assertEqual(match.persistent_host_clans, ["#CURRENT", None])

    async def test_abort_started_live_match_requires_exact_started_state(self) -> None:
        from cogs.live_hosting import live_start
        from elobot.db.match_models import ExpectedLiveStartState

        expected = ExpectedLiveStartState(
            times=(1000, None),
            hosts=("100", None),
            host_clans=("#CURRENT", None),
            old_messages=(777, None),
        )
        settings = MagicMock(use_state_management=True, state_management_save_previous=False, keep_nulls=True)

        for field, actual in (
                (None, None),
                ("times", (999, None)),
                ("hosts", ("999", None)),
                ("host_clans", ("#OTHER", None)),
                ("old_msgs", (999, None)),
                ("is_war", True),
        ):
            with self.subTest(field=field):
                match = Match.model_construct(
                    token="TOKEN",
                    matched_time=1,
                    is_war=False,
                    times=[1000, None],
                    hosts=["100", None],
                    host_clans=["#CURRENT", None],
                    persistent_host_clans=["#PREFERRED", None],
                    old_msgs=[777, None],
                    participants=[],
                )
                match.id = PydanticObjectId()
                if field is None:
                    match.model_fields_set.discard("persistent_host_clans")
                stored = {
                    "_id": match.id,
                    "is_war": False,
                    "times": [1000, None],
                    "hosts": ["100", None],
                    "host_clans": ["#CURRENT", None],
                    "old_msgs": [777, None],
                }
                if field:
                    stored[field] = list(actual) if isinstance(actual, tuple) else actual
                collection = MagicMock()

                async def update_one(query, _update):
                    matched = all(stored.get(key) == value for key, value in query.items())
                    return MagicMock(matched_count=int(matched))

                collection.update_one = AsyncMock(side_effect=update_one)
                before = (list(match.times), list(match.hosts), list(match.host_clans), list(match.old_msgs))
                with patch.object(Match, "get_pymongo_collection", return_value=collection), \
                        patch.object(Match, "get_settings", return_value=settings):
                    aborted = await match.abort_started_live_match(expected)

                self.assertEqual(aborted, field is None)
                query, update = collection.update_one.await_args.args
                self.assertEqual(query, {
                    "_id": match.id,
                    "is_war": False,
                    "times": [1000, None],
                    "hosts": ["100", None],
                    "host_clans": ["#CURRENT", None],
                    "old_msgs": [777, None],
                })
                self.assertEqual(update, {"$set": {
                    "times": [None, None], "hosts": [None, None],
                    "host_clans": [None, None],
                }})
                self.assertEqual(match.old_msgs, [777, None])
                if field is None:
                    self.assertEqual(match.persistent_host_clans, ["#CURRENT", None])
                else:
                    self.assertEqual(
                        (match.times, match.hosts, match.host_clans, match.old_msgs),
                        before,
                    )

        stale_match = MagicMock(
            times=[1000, None], hosts=["100", None], host_clans=["#CURRENT", None],
            old_msgs=[777, None], thread="12345", msg=None,
        )
        stale_match.abort_started_live_match = AsyncMock(return_value=False)
        message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with (
                patch("cogs.live_hosting.live_start.match_commands.clear_match_base_stores",
                      new_callable=AsyncMock) as clear_bases,
                patch("cogs.live_hosting.live_start.match_commands.free_clan",
                      new_callable=AsyncMock) as free_clan,
                patch("cogs.live_hosting.live_start.schedule_channel_name_edit") as rename,
        ):
            self.assertFalse(await live_start.abort_live_start(MagicMock(), live_start.LiveAbort(stale_match, message)))
        clear_bases.assert_not_awaited()
        message.delete.assert_not_awaited()
        message.edit.assert_not_awaited()
        free_clan.assert_not_awaited()
        rename.assert_not_called()

        missing_message_id = MagicMock(
            times=[1000, None], hosts=["100", None], host_clans=["#CURRENT", None],
            old_msgs=[None, None], thread="12345", msg=None,
        )
        missing_message_id.abort_started_live_match = AsyncMock(return_value=True)
        supplied_message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with (
                patch("cogs.live_hosting.live_start.match_commands.clear_match_base_stores",
                      new_callable=AsyncMock) as clear_bases,
                patch("cogs.live_hosting.live_start.match_commands.free_clan",
                      new_callable=AsyncMock) as free_clan,
                patch("cogs.live_hosting.live_start.schedule_channel_name_edit") as rename,
        ):
            self.assertFalse(await live_start.abort_live_start(
                MagicMock(), live_start.LiveAbort(missing_message_id, supplied_message),
            ))
        missing_message_id.abort_started_live_match.assert_not_awaited()
        clear_bases.assert_not_awaited()
        supplied_message.delete.assert_not_awaited()
        supplied_message.edit.assert_not_awaited()
        free_clan.assert_not_awaited()
        rename.assert_not_called()

        unassigned = MagicMock(
            times=[1000, None], hosts=[None, None], host_clans=[None, None],
            old_msgs=[None, None], thread="12345", msg=None,
        )
        unassigned.abort_started_live_match = AsyncMock(return_value=True)
        with (
                patch("cogs.live_hosting.live_start.match_commands.clear_match_base_stores",
                      new_callable=AsyncMock) as clear_bases,
                patch("cogs.live_hosting.live_start.match_commands.free_clan",
                      new_callable=AsyncMock) as free_clan,
                patch("cogs.live_hosting.live_start.schedule_channel_name_edit") as rename,
        ):
            self.assertTrue(await live_start.abort_live_start(
                MagicMock(bot=self.bot), live_start.LiveAbort(unassigned, None),
            ))
        clear_bases.assert_not_awaited()
        free_clan.assert_not_awaited()
        rename.assert_called_once()

        two_clans = MagicMock(
            times=[1000, None], hosts=["100", None], host_clans=["#ONE", "#TWO"],
            old_msgs=[777, None], thread="12345", msg=None,
        )
        two_clans.abort_started_live_match = AsyncMock(return_value=True)
        message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with (
                patch("cogs.live_hosting.live_start.match_commands.clear_match_base_stores",
                      new_callable=AsyncMock),
                patch("cogs.live_hosting.live_start.match_commands.free_clan",
                      new_callable=AsyncMock) as free_clan,
                patch("cogs.live_hosting.live_start.schedule_channel_name_edit"),
        ):
            self.assertTrue(await live_start.abort_live_start(MagicMock(), live_start.LiveAbort(two_clans, message)))
        self.assertCountEqual(
            free_clan.await_args_list,
            [
                call("#ONE", expected_thread="12345"),
                call("#TWO", expected_thread="12345"),
            ],
        )

    async def test_match_abort_preserves_persistent_host_clans(self) -> None:
        from elobot.db.match_models import ExpectedLiveStartState

        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        old_message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        ctx.channel.fetch_message = AsyncMock(return_value=old_message)
        ctx.author.id = 999
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(
            thread="12345",
            times=[1000, None],
            hosts=["999", None],
            host_clans=["#CURRENT", None],
            persistent_host_clans=["#PREFERRED", None],
            old_msgs=[777, None],
            msg=None,
            participants=[MagicMock(result=[None] * 3, base_store=None, opponent_bases_stored=False),
                          MagicMock(result=[None] * 3, base_store=None, opponent_bases_stored=False)],
            model_fields_set={"persistent_host_clans"},
        )
        match.set = AsyncMock()
        match.abort_started_live_match = AsyncMock(return_value=True)

        with patch("cogs.match_commands.Match") as match_model, \
                patch("cogs.match_commands.is_match_finished", return_value=False), \
                patch("cogs.match_commands.clear_match_base_stores", new_callable=AsyncMock), \
                patch("cogs.match_commands.free_clan", new_callable=AsyncMock), \
                patch("cogs.live_hosting.live_start.schedule_channel_name_edit"):
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_abort.callback.func(self.cog, ctx)

        match.abort_started_live_match.assert_awaited_once_with(ExpectedLiveStartState(
            times=(1000, None),
            hosts=("999", None),
            host_clans=("#CURRENT", None),
            old_messages=(777, None),
        ))
        match.set.assert_not_awaited()
        self.assertEqual(match.persistent_host_clans, ["#PREFERRED", None])
        old_message.delete.assert_not_awaited()
        self.assertEqual(old_message.edit.await_args.kwargs["embeds"][0].title, "**Match Aborted**")
        self.assertEqual(old_message.edit.await_args.kwargs["components"], [])

    async def test_match_war_autostart_passes_preheld_reservations_to_helper(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel = MagicMock(
            id=12345,
            name="Match Thread",
            created_at=datetime.now(timezone.utc),
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            participants=[
                MagicMock(user_id="100", result=[None] * 3, base_store=None, opponent_bases_stored=False),
                MagicMock(user_id="200", result=[None] * 3, base_store=None, opponent_bases_stored=False),
            ],
        )
        clan = MagicMock(tag="#CLAN", name="Helper Clan")
        clan_reservation = MagicMock(clan_tag="#CLAN")
        side_reservation = MagicMock(key=("TOKEN", 1))
        self.bot.coc_client.get_clan = AsyncMock(return_value=clan)

        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.is_raid_weekend", return_value=False),
                patch("cogs.match_commands.is_clan_available", new=AsyncMock(return_value=True)),
                patch(
                    "cogs.match_commands.confirm_pending_opponent_bases",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "cogs.match_commands.try_reserve_resource_match_side",
                    new=AsyncMock(return_value=side_reservation),
                ),
                patch(
                    "cogs.match_commands.try_reserve_resource_clan",
                    new=AsyncMock(return_value=clan_reservation),
                ),
                patch(
                    "cogs.match_commands.get_start_base_store_slot_availability",
                    new_callable=AsyncMock,
                ) as available_slots,
                patch(
                    "cogs.match_commands.helper_host.host_war_automatically",
                    new_callable=AsyncMock,
                ) as automatic_host,
        ):
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_war_autostart.callback(
                ctx,
                player=1,
                clan="#CLAN",
                scout=True,
            )

        available_slots.assert_not_awaited()
        automatic_host.assert_awaited_once()
        runtime, host_ctx, request = automatic_host.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(host_ctx, ctx)
        self.assertIs(request.match, match)
        self.assertEqual(request.player, 1)
        self.assertIs(request.clan, clan)
        self.assertIs(request.host, ctx.author)
        self.assertTrue(request.scout)
        self.assertIs(request.clan_reservation, clan_reservation)
        self.assertIs(request.side_reservation, side_reservation)
        clan_reservation.release.assert_called_once_with()
        side_reservation.release.assert_called_once_with()

    def test_match_autostart_metadata_and_options(self) -> None:
        command = self.cog.match_autostart

        self.assertEqual(command.resolved_name, "match autostart")
        self.assertEqual([str(option.name) for option in command.options], ["clan", "scout", "district"])
        self.assertTrue(command.options[0].required)
        self.assertFalse(command.options[1].required)
        self.assertFalse(command.options[2].required)
        self.assertEqual([str(option.name) for option in self.cog.match_start.options], ["clan", "scout"])
        self.assertEqual(len(command.checks), 1)
        self.assertIs(command.max_concurrency.bucket, match_commands.CHANNEL_USER_CONCURRENCY_BUCKET)
        self.assertEqual(command.max_concurrency.concurrent, 1)

    async def test_match_autostart_passes_both_reservations_and_human_requester(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel = MagicMock(id=12345)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            old_msgs=[None, None],
            final_districts=["D1"],
            participants=[MagicMock(user_id="100"), MagicMock(user_id="200")],
        )
        match.participants[0].bases = [MagicMock(link="https://p1-d1")]
        match.participants[1].bases = [MagicMock(link="https://p2-d1")]
        clan = MagicMock(tag="#CLAN", name="Helper Clan")
        p1_doc = MagicMock(tag="#P1", elo=1200)
        p2_doc = MagicMock(tag="#P2", elo=1100)
        player1 = MagicMock(tag="#P1", town_hall=3)
        player2 = MagicMock(tag="#P2", town_hall=3)
        side0 = MagicMock()
        side1 = MagicMock()
        clan_reservation = MagicMock()
        started_message = MagicMock(id=777)
        events: list[str] = []

        async def confirm_eta(*_args, **_kwargs) -> bool:
            events.append("eta")
            return True

        async def reserve_match_side(_token: str, side: int):
            events.append(f"reserve-{side}")
            return (side0, side1)[side]

        async def reserve_clan(_clan_tag: str):
            events.append("reserve-clan")
            return clan_reservation

        async def choose_side(*_args, **_kwargs):
            events.append("choose")
            return match

        async def claim_clan(*_args, **_kwargs) -> bool:
            events.append("local-start")
            return True

        async def run_job(*_args, **_kwargs) -> None:
            events.append("runner")

        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.Player") as player_model,
                patch("cogs.match_commands.is_raid_weekend", return_value=False),
                patch("cogs.match_commands.is_clan_available", new=AsyncMock(return_value=True)),
                patch("cogs.match_commands.is_resource_clan_locked", return_value=False),
                patch("cogs.match_commands.get_start_base_store_slot_availability", new_callable=AsyncMock) as slots,
                patch("cogs.live_hosting.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch(
                    "cogs.live_hosting.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ) as live_slots,
                patch("cogs.live_hosting.helper_live.match_commands.choose_bases",
                      new=AsyncMock(side_effect=choose_side)),
                patch("cogs.live_hosting.helper_live.confirm_helper_availability",
                      new=AsyncMock(side_effect=confirm_eta)),
                patch("cogs.live_hosting.helper_live.run_helper_host_job",
                      new=AsyncMock(side_effect=run_job)) as runner,
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_match_side",
                      new=AsyncMock(side_effect=reserve_match_side)) as reserve_side,
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_clan",
                      new=AsyncMock(side_effect=reserve_clan)),
                patch("cogs.live_hosting.live_start.match_commands.claim_resource_clan",
                      new=AsyncMock(side_effect=claim_clan)),
                patch("cogs.live_hosting.live_start.complete_live_start",
                      new=AsyncMock(return_value=started_message)),
        ):
            match_model.find_one = AsyncMock(side_effect=[match, match, match])
            player_model.find_one = AsyncMock(side_effect=[p1_doc, p2_doc])
            self.cog.coc_client.get_player = AsyncMock(side_effect=[player1, player2])
            self.cog.coc_client.get_clan = AsyncMock(return_value=clan)

            await self.cog.match_autostart.callback(ctx, clan="#CLAN", scout=True)

        slots.assert_not_awaited()
        live_slots.assert_awaited_once_with("#CLAN", bot=self.bot)
        self.assertEqual(
            reserve_side.await_args_list,
            [call("TOKEN", 0), call("TOKEN", 1)],
        )
        side0.release.assert_called_once_with()
        side1.release.assert_called_once_with()
        self.assertEqual(events, [
            "reserve-0", "reserve-1", "reserve-clan", "choose", "choose",
            "eta", "local-start", "runner",
        ])
        runner.assert_awaited_once()
        runtime, host_ctx, request = runner.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(host_ctx, ctx)
        self.assertEqual(request.requester_id, "999")
        self.assertEqual(request.create_request.job_type, "host_live_match")
        self.assertEqual(request.create_request.requested_by, "999")
        self.assertIs(request.started_message, started_message)
        self.assertIsNone(request.on_success)
        self.assertFalse(request.additional_cancel_user_ids)

    async def test_concurrent_automatic_live_loser_has_zero_side_effects(self) -> None:
        from cogs.live_hosting import helper_live, live_reservations, live_start

        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            old_msgs=[None, None],
            final_districts=["D1"],
            participants=[
                MagicMock(user_id="100", result=[None], bases=[MagicMock(link="https://p1-d1")]),
                MagicMock(user_id="200", result=[None], bases=[MagicMock(link="https://p2-d1")]),
            ],
        )
        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=lambda user_id: MagicMock(id=int(user_id)))
        runtime = MagicMock(bot=bot)
        winner_ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        loser_ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        request = helper_live.AutomaticLiveHost(
            match=match,
            clan=MagicMock(tag="#CLAN", name="Helper Clan"),
            host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"),
            player2=MagicMock(tag="#P2"),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            requested_by="999",
        )
        choose_entered = anyio.Event()
        release_choose = anyio.Event()
        choose_calls = 0

        async def choose_side(*_args, **_kwargs):
            nonlocal choose_calls
            choose_calls += 1
            if choose_calls == 1:
                choose_entered.set()
                with anyio.fail_after(5):
                    await release_choose.wait()
            return match

        eta = AsyncMock(return_value=True)
        start = AsyncMock(return_value=live_start.LiveStartSucceeded(message=MagicMock(id=777)))
        runner = AsyncMock()
        abort = AsyncMock()
        with (
                patch("cogs.live_hosting.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch(
                    "cogs.live_hosting.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ),
                patch("cogs.live_hosting.helper_live.match_commands.choose_bases",
                      new=AsyncMock(side_effect=choose_side)) as choose,
                patch("cogs.live_hosting.helper_live.match_commands.available_resource_clans_text",
                      new=AsyncMock(return_value="available clans")),
                patch("cogs.live_hosting.helper_live.confirm_helper_availability", new=eta),
                patch("cogs.live_hosting.helper_live.live_start.start_live_match", new=start),
                patch("cogs.live_hosting.helper_live.run_helper_host_job", new=runner),
                patch("cogs.live_hosting.helper_live.live_start.abort_live_start", new=abort),
        ):
            with anyio.fail_after(5):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(helper_live.host_live_automatically(runtime, winner_ctx, request))
                    await choose_entered.wait()
                    await helper_live.host_live_automatically(runtime, loser_ctx, request)
                    loser_choose_count = choose.await_count
                    release_choose.set()

        self.assertEqual(loser_choose_count, 1)
        self.assertEqual(choose.await_count, 2)
        eta.assert_awaited_once()
        start.assert_awaited_once()
        self.assertIsInstance(start.await_args.args[2].reservations, live_reservations.LiveStartReservations)
        runner.assert_awaited_once()
        abort.assert_not_awaited()
        self.assertEqual(bot.fetch_user.await_count, 2)
        loser_ctx.send.assert_awaited_once()
        self.assertFalse(match_commands.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 0)].locked())
        self.assertFalse(match_commands.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 1)].locked())
        self.assertFalse(match_commands.RESOURCE_CLAN_LOCKS["#CLAN"].locked())

    async def test_match_autostart_rejects_when_either_side_started_before_helper(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999)
        ctx.channel = MagicMock(id=12345)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        for field, index in (("times", 0), ("times", 1), ("hosts", 0), ("hosts", 1)):
            with self.subTest(field=field, index=index):
                times = [None, None]
                hosts = [None, None]
                (times if field == "times" else hosts)[index] = 123 if field == "times" else "888"
                match = MagicMock(
                    token="TOKEN", thread="12345", is_war=False, times=times, hosts=hosts,
                    participants=[MagicMock(user_id="100"), MagicMock(user_id="200")],
                )
                with (
                        patch("cogs.match_commands.Match") as match_model,
                        patch("cogs.match_commands.Player") as player_model,
                        patch("cogs.match_commands.choose_bases", new_callable=AsyncMock) as choose,
                        patch("cogs.match_commands.try_reserve_resource_match_side", new_callable=AsyncMock) as reserve,
                        patch("cogs.match_commands.try_reserve_resource_clan",
                              new_callable=AsyncMock) as reserve_clan,
                        patch("cogs.match_commands.live_start.start_live_match", new_callable=AsyncMock) as start,
                        patch("cogs.match_commands.helper_live.confirm_helper_availability",
                              new_callable=AsyncMock) as eta,
                        patch("cogs.match_commands.helper_live.run_helper_host_job", new_callable=AsyncMock) as post_poll,
                        patch("cogs.match_commands.live_start.abort_live_start", new_callable=AsyncMock) as abort,
                        patch("cogs.match_commands.available_resource_clans_text",
                              new=AsyncMock(return_value="available clans")),
                ):
                    match_model.find_one = AsyncMock(return_value=match)
                    await self.cog.match_autostart.callback(ctx, clan="#CLAN")

                player_model.find_one.assert_not_called()
                choose.assert_not_awaited()
                eta.assert_not_awaited()
                reserve.assert_not_awaited()
                reserve_clan.assert_not_awaited()
                start.assert_not_awaited()
                post_poll.assert_not_awaited()
                abort.assert_not_awaited()

    async def test_host_live_automatically_builds_paired_payload_and_polls_success(self) -> None:
        from cogs.live_hosting import helper_live, live_start

        job_id = "0123456789abcdef0123456789abcdef"
        events: list[str] = []
        request_payload = None
        request_api_key = None
        polls = iter(("running", "succeeded"))

        def job(status: str) -> dict[str, object]:
            return {
                "id": job_id, "type": "host_live_match", "status": status,
                "created_at": 100.0, "started_at": 101.0,
                "finished_at": 102.0 if status == "succeeded" else None,
                "events": [], "result": None, "error": None,
            }

        async def eta_handler(request: web.Request) -> web.Response:
            events.append("eta")
            self.assertEqual(request.query["type"], "host_live_match")
            self.assertEqual(request.headers["X-Api-Key"], "test-key")
            return web.json_response({"estimate_start_at": 0.0, "estimate_end_at": 180.0})

        async def post_handler(request: web.Request) -> web.Response:
            nonlocal request_payload, request_api_key
            events.append("post")
            request_payload = await request.json()
            request_api_key = request.headers.get("X-Api-Key")
            return web.json_response(job("queued"), status=201)

        async def poll_handler(request: web.Request) -> web.Response:
            status = next(polls)
            events.append(f"poll-{status}")
            self.assertEqual(request.headers["X-Api-Key"], "test-key")
            return web.json_response(job(status))

        app = web.Application()
        app.router.add_get("/v1/eta", eta_handler)
        app.router.add_post("/v1/jobs", post_handler)
        app.router.add_get(f"/v1/jobs/{job_id}", poll_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]

        async def wait_forever(*_args, **_kwargs) -> None:
            with anyio.fail_after(5):
                await asyncio.Event().wait()

        bot = MagicMock()
        bot.wait_for_component = AsyncMock(side_effect=wait_forever)
        player1_user = MagicMock(id=100)
        player2_user = MagicMock(id=200)
        bot.fetch_user = AsyncMock(side_effect=[player1_user, player2_user])
        runtime = MagicMock(
            bot=bot,
            helper_api_base_url=f"http://127.0.0.1:{port}",
            helper_api_key="test-key",
        )
        progress = MagicMock(id=888)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=555)
        ctx.send = AsyncMock(return_value=progress)
        ctx.edit = AsyncMock()
        match = MagicMock(
            token="TOKEN", thread="12345", final_districts=["D1", "D2"],
            participants=[
                MagicMock(user_id="100", bases=[MagicMock(link="https://p1-d1"), MagicMock(link="https://p1-d2")]),
                MagicMock(user_id="200", bases=[MagicMock(link="https://p2-d1"), MagicMock(link="https://p2-d2")]),
            ],
            times=[None, None], hosts=[None, None], host_clans=[None, None], old_msgs=[None, None],
        )
        request = helper_live.AutomaticLiveHost(
            match=match, clan=MagicMock(tag="#CLAN", name="Helper Clan"), host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"), player2=MagicMock(tag="#P2"),
            player1_elo=1200, player2_elo=1100, scout=False, requested_by="999",
        )

        async def choose_side(_token, _player, opponent, **_kwargs):
            events.append("choose-p2" if opponent is player2_user else "choose-p1")
            return match

        async def start_side(*_args, **_kwargs):
            events.append("live-start")
            match.times[0] = 100
            match.hosts[0] = "888"
            match.host_clans[0] = "#CLAN"
            match.old_msgs[0] = 777
            return live_start.LiveStartSucceeded(message=MagicMock(id=777))

        try:
            with (
                    patch(
                        "cogs.live_hosting.helper_live.live_eligibility.clan_has_all_base_store_slots",
                        new=AsyncMock(return_value=True),
                    ),
                    patch("cogs.live_hosting.helper_live.match_commands.choose_bases",
                          new=AsyncMock(side_effect=choose_side)),
                    patch("cogs.live_hosting.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                    patch("cogs.live_hosting.helper_live.live_start.start_live_match",
                          new=AsyncMock(side_effect=start_side)),
                    patch("elobot.integrations.helper_api.anyio.sleep", new=AsyncMock()),
            ):
                await helper_live.host_live_automatically(runtime, ctx, request)
        finally:
            await runner.cleanup()

        self.assertEqual(events, [
            "choose-p2", "choose-p1", "eta", "live-start", "post", "poll-running", "poll-succeeded",
        ])
        self.assertEqual(request_api_key, "test-key")
        self.assertEqual(request_payload, {
            "type": "host_live_match",
            "requested_by": "999",
            "payload": {
                "host_clan_tag": "#CLAN",
                "player_1_tag": "#P1",
                "player_2_tag": "#P2",
                "base_links_for_player_1": ["https://p2-d1", "https://p2-d2"],
                "base_links_for_player_2": ["https://p1-d1", "https://p1-d2"],
            },
        })

    async def test_host_live_automatically_rejects_invalid_pairs_before_eta(self) -> None:
        from cogs.live_hosting import helper_live

        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=[MagicMock(id=100), MagicMock(id=200)])
        runtime = MagicMock(bot=bot)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            final_districts=["D1", "D2"],
            participants=[
                MagicMock(user_id="100", bases=[MagicMock(link="duplicate"), MagicMock(link="duplicate")]),
                MagicMock(user_id="200", bases=[MagicMock(link="other-1"), MagicMock(link="other-2")]),
            ],
        )
        request = helper_live.AutomaticLiveHost(
            match=match,
            clan=MagicMock(tag="#CLAN"),
            host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"),
            player2=MagicMock(tag="#P2"),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            requested_by="999",
        )

        with (
                patch(
                    "cogs.live_hosting.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ),
                patch("cogs.live_hosting.helper_live.match_commands.choose_bases",
                      new=AsyncMock(return_value=match)),
                patch("cogs.live_hosting.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch("cogs.live_hosting.helper_live.confirm_helper_availability",
                      new_callable=AsyncMock) as eta,
                patch("cogs.live_hosting.helper_live.live_start.start_live_match",
                      new_callable=AsyncMock) as start,
                patch("cogs.live_hosting.helper_live.run_helper_host_job",
                      new_callable=AsyncMock) as post_poll,
        ):
            await helper_live.host_live_automatically(runtime, ctx, request)

        eta.assert_not_awaited()
        start.assert_not_awaited()
        post_poll.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertFalse(match_commands.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 0)].locked())
        self.assertFalse(match_commands.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 1)].locked())
        self.assertFalse(match_commands.RESOURCE_CLAN_LOCKS["#CLAN"].locked())

    async def test_host_live_automatically_rejects_clan_with_one_occupied_slot(self) -> None:
        from cogs.live_hosting import helper_live, live_reservations, live_start

        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=[MagicMock(id=100), MagicMock(id=200)])
        runtime = MagicMock(bot=bot)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            final_districts=["D1"],
            participants=[
                MagicMock(user_id="100", result=[None], bases=[MagicMock(link="https://p1-d1")]),
                MagicMock(user_id="200", result=[None], bases=[MagicMock(link="https://p2-d1")]),
            ],
        )
        request = helper_live.AutomaticLiveHost(
            match=match,
            clan=MagicMock(tag="#CLAN", name="Helper Clan"),
            host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"),
            player2=MagicMock(tag="#P2"),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            requested_by="999",
        )
        side0 = MagicMock()
        side1 = MagicMock()
        clan = MagicMock()
        reservations = live_reservations.LiveStartReservations(side0=side0, side1=side1, clan=clan)

        with (
                patch("cogs.live_hosting.helper_live.live_start.reserve_live_start",
                      new=AsyncMock(return_value=reservations)),
                patch("cogs.base_store.storage.sync_base_store_slots",
                      new=AsyncMock(return_value={"#CLAN": ["Slot 2"]})) as sync_slots,
                patch("cogs.live_hosting.helper_live.match_commands.choose_bases",
                      new=AsyncMock(return_value=match)) as choose_bases,
                patch("cogs.live_hosting.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch("cogs.live_hosting.helper_live.confirm_helper_availability",
                      new=AsyncMock(return_value=True)) as confirm_eta,
                patch("cogs.live_hosting.helper_live.live_start.start_live_match",
                      new=AsyncMock(return_value=live_start.LiveStartSucceeded(message=MagicMock()))) as start,
                patch("cogs.live_hosting.helper_live.run_helper_host_job", new_callable=AsyncMock) as run_job,
        ):
            await helper_live.host_live_automatically(runtime, ctx, request)

        sync_slots.assert_awaited_once_with("#CLAN", bot=runtime.bot)
        choose_bases.assert_not_awaited()
        confirm_eta.assert_not_awaited()
        start.assert_not_awaited()
        run_job.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertIn("both base slots", ctx.send.await_args.args[0].lower())
        clan.release.assert_called_once_with()
        side1.release.assert_called_once_with()
        side0.release.assert_called_once_with()

    @patch("cogs.match_commands.HelperTask")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_update_hosting_queue_embed_shows_only_helper_tasks(
            self, mock_server_state_current, mock_helper_task):
        mock_server_state_current.return_value = MagicMock(
            hosting_queue_embed=MagicMock(channel=123, message=456)
        )
        embed = ipy.Embed(title="**Hosting Queue**", description="old")
        message = MagicMock(embeds=[embed])
        message.edit = AsyncMock()
        queue_channel = MagicMock()
        queue_channel.fetch_message = AsyncMock(return_value=message)
        queue_channel.guild.fetch_member = AsyncMock(return_value=MagicMock(id=111, display_name="Player"))
        self.bot.fetch_channel = AsyncMock(side_effect=[queue_channel, MagicMock()])

        task = MagicMock(thread=987, player_id="111", timestamp=3000)
        mock_helper_task.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[task])

        await self.cog.update_hosting_queue_embed.callback.func(self.cog)

        timestamp = ipy.Timestamp.fromtimestamp(3000).format(ipy.TimestampStyles.RelativeTime)
        message.edit.assert_awaited_once()
        self.assertEqual(message.edit.await_args.kwargs["embed"]["description"],
                         f"<#987> **[Player](<https://discordapp.com/users/111>)** {timestamp}\n")

    @patch("cogs.match_commands.HelperTask")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_update_hosting_queue_embed_avoids_concurrent_message_fetches(
            self, mock_server_state_current, mock_helper_task):
        # Given an update that is still waiting for the queue embed message
        mock_server_state_current.return_value = MagicMock(
            hosting_queue_embed=MagicMock(channel=123, message=456)
        )
        message = MagicMock(embeds=[MagicMock(description="")])
        message.edit = AsyncMock()
        first_fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        overlap_detected = asyncio.Event()
        active_fetches = 0

        async def fetch_message(_message_id: int) -> MagicMock:
            nonlocal active_fetches
            active_fetches += 1
            if active_fetches > 1:
                overlap_detected.set()
                release_fetch.set()
            first_fetch_started.set()
            with anyio.fail_after(5):
                await release_fetch.wait()
            active_fetches -= 1
            return message

        queue_channel = MagicMock()
        queue_channel.fetch_message = AsyncMock(side_effect=fetch_message)
        self.bot.fetch_channel = AsyncMock(return_value=queue_channel)
        mock_helper_task.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[])

        # When the scheduler starts another update before the first one finishes
        async def run_scheduled_update() -> None:
            await self.cog.update_hosting_queue_embed.callback.func(self.cog)
            release_fetch.set()

        scheduled_update = ipy.Task(run_scheduled_update, ipy.IntervalTrigger(seconds=15))
        with anyio.fail_after(5):
            first_update = scheduled_update._fire(datetime.now())
            await first_fetch_started.wait()
            second_update = scheduled_update._fire(datetime.now())
            await asyncio.gather(first_update, second_update)

        # Then only the active update may fetch the Discord message
        self.assertFalse(overlap_detected.is_set())
        queue_channel.fetch_message.assert_awaited_once_with(456)

    @patch("cogs.match_commands.sync_base_store_slots", new_callable=AsyncMock)
    @patch("cogs.match_commands.is_raid_weekend", return_value=False)
    @patch("cogs.match_commands.Clan")
    @patch("cogs.match_commands.Match")
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_update_clan_availability_embed_shows_slots_outside_raid_weekend(
            self, mock_server_state_current, mock_match, mock_clan, mock_is_raid_weekend, mock_sync_slots):
        mock_server_state_current.return_value = MagicMock(
            clan_availability_embed=MagicMock(channel=123, message=456)
        )

        field = ipy.EmbedField(name="**Test Clan (#CLAN)**", value="old")
        embed = ipy.Embed(title="**Clan Availability**", fields=[field])
        message = MagicMock(embeds=[embed])
        message.edit = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)
        channel.guild.fetch_member = AsyncMock(return_value=MagicMock(id=123, display_name="Player"))
        self.bot.fetch_channel = AsyncMock(return_value=channel)

        clan = MagicMock()
        clan.tag = "#CLAN"
        clan.availability_str = "Available"
        clan.available_slots = ["Slot 2"]
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        stored_match = MagicMock(thread="987")
        stored_match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]
        mock_match.find.return_value.to_list = AsyncMock(return_value=[stored_match])

        await self.cog.update_clan_availability_embed.callback.func(self.cog)

        mock_sync_slots.assert_awaited_once_with(bot=self.bot)
        self.assertEqual(
            message.edit.await_args.kwargs["embed"]["fields"][0]["value"],
            "Available\nSlot 1: <#987> **[Player](<https://discordapp.com/users/123>)**",
        )
        message.edit.assert_awaited_once()
        mock_is_raid_weekend.assert_not_called()

    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_host_clan_select_updates_occupancy_before_discord_side_effects(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a started war side whose replacement clan can be persisted.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "host_clan_select|TOKEN|0"
        ctx.values = ["#NEW"]
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Started**", fields=[MagicMock(), MagicMock(), MagicMock()])]
        events = []

        match = MagicMock()
        match.id = "MATCH"
        match.is_war = True
        match.old_msgs = [123, None]
        match.times = [100, None]
        match.hosts = ["999", None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.participants = [MagicMock(result=[]), MagicMock(result=[])]

        async def record_match_set(_query, _update):
            events.append(("persist", match.host_clans[0]))
            return MagicMock(matched_count=1)

        async def record_free(_old_clan, **_kwargs):
            events.append(("free", match.host_clans[0]))

        async def record_occupy(_match, _player):
            events.append(("occupy", match.host_clans[0]))

        async def record_discord_edit(*_args, **_kwargs):
            events.append(("discord", match.host_clans[0]))

        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(side_effect=record_match_set)
        mock_match.find_one = AsyncMock(return_value=match)
        mock_free_clan.side_effect = record_free
        mock_occupy_clan.side_effect = record_occupy
        ctx.message.edit = AsyncMock(side_effect=record_discord_edit)
        self.bot.coc_client.get_clan = AsyncMock(
            return_value=MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        )
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        # When the side's host clan is changed.
        async def publish_existing(_ctx, _match, _expected, *, embed, existing_message):
            await existing_message.edit(embed=embed)

        with patch("cogs.match_commands.publish_match_message", new=AsyncMock(side_effect=publish_existing)) as publisher:
            await self.cog.host_clan_select.callback.func(self.cog, ctx)
        publisher.assert_awaited_once()
        self.assertIs(publisher.await_args.kwargs["existing_message"], ctx.message)

        # Then persistence and in-memory sync precede occupancy, which precedes Discord I/O.
        self.assertEqual(
            events,
            [
                ("persist", "#OLD"),
                ("occupy", "#NEW"),
                ("free", "#NEW"),
                ("discord", "#NEW"),
            ],
        )
        collection.update_one.assert_awaited_once()
        self.assertEqual(collection.update_one.await_args.args[0]["old_msgs.0"], 123)
        self.assertEqual(collection.update_one.await_args.args[1], {"$set": {
            "host_clans.0": "#NEW",
            "persistent_host_clans.0": "#NEW",
        }})
        self.assertEqual(match.persistent_host_clans[0], "#NEW")
        mock_free_clan.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        mock_occupy_clan.assert_awaited_once_with(match, 0)

    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_host_clan_select_persistence_failure_skips_occupancy_and_discord(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a valid war-side clan selection whose durable write fails.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "host_clan_select|TOKEN|0"
        ctx.values = ["#NEW"]
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Started**", fields=[MagicMock(), MagicMock(), MagicMock()])]
        ctx.message.edit = AsyncMock()

        match = MagicMock()
        match.id = "MATCH"
        match.is_war = True
        match.old_msgs = [123, None]
        match.times = [100, None]
        match.hosts = ["999", None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.participants = [MagicMock(result=[]), MagicMock(result=[])]
        match.get_pymongo_collection.return_value.update_one = AsyncMock(
            side_effect=RuntimeError("match write failed"),
        )
        mock_match.find_one = AsyncMock(return_value=match)
        self.bot.coc_client.get_clan = AsyncMock(
            return_value=MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        )
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        # When the durable match update fails, then no occupancy or Discord mutation follows.
        with self.assertRaisesRegex(RuntimeError, "match write failed"):
            await self.cog.host_clan_select.callback.func(self.cog, ctx)

        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        mock_free_clan.assert_not_awaited()
        mock_occupy_clan.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_match_set_clan_updates_occupancy_before_discord_side_effects(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a scheduled match whose selected host clan can be persisted.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 12345

        old_message = MagicMock()
        old_message.edit = AsyncMock()
        events = []

        match = MagicMock()
        match.thread = "12345"
        match.is_autowin = False
        match.is_war = False
        match.times = [1234567890, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [777, None]
        match.msg = None
        match.participants = [
            MagicMock(user_id="123", result=[]),
            MagicMock(user_id="456", result=[]),
        ]

        async def record_match_set(_update):
            events.append(("persist", match.host_clans[0]))

        async def record_free(_old_clan, **_kwargs):
            events.append(("free", match.host_clans[0]))

        async def record_occupy(_match, _player1_index, _player2_index):
            events.append(("occupy", match.host_clans[0]))

        async def record_discord_fetch(_message_id):
            events.append(("discord", match.host_clans[0]))
            return old_message

        match.set = AsyncMock(side_effect=record_match_set)
        mock_match.find_one = AsyncMock(return_value=match)
        mock_free_clan.side_effect = record_free
        mock_occupy_clan.side_effect = record_occupy
        ctx.channel.fetch_message = AsyncMock(side_effect=record_discord_fetch)

        clan = MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        self.bot.coc_client.get_clan = AsyncMock(return_value=clan)
        mock_clan_find.return_value = MagicMock(availability_str="Available")
        self.bot.fetch_channel = AsyncMock(return_value=MagicMock())

        # When the host clan is changed.
        async def publish_existing(_ctx, _match, _expected, *, embed, existing_message):
            await existing_message.edit(embed=embed)

        with patch("cogs.match_commands.publish_match_message", new=AsyncMock(side_effect=publish_existing)) as publisher:
            await self.cog.match_set_clan.callback.func(self.cog, ctx, clan="#NEW")
        publisher.assert_awaited_once()
        self.assertIs(publisher.await_args.kwargs["existing_message"], old_message)

        # Then persistence and in-memory sync precede occupancy, which precedes Discord I/O.
        self.assertEqual(
            events,
            [
                ("persist", "#OLD"),
                ("occupy", "#NEW"),
                ("free", "#NEW"),
                ("discord", "#NEW"),
            ],
        )
        match.set.assert_awaited_once_with({
            "host_clans.0": "#NEW",
            "persistent_host_clans.0": "#NEW",
        })
        self.assertEqual(match.persistent_host_clans[0], "#NEW")
        mock_free_clan.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        mock_occupy_clan.assert_awaited_once_with(match, 0, 1)

    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_match_set_clan_persistence_failure_skips_occupancy_and_discord(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a valid scheduled match whose host-clan write fails.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 12345
        ctx.channel.fetch_message = AsyncMock()

        match = MagicMock()
        match.thread = "12345"
        match.is_autowin = False
        match.is_war = False
        match.times = [1234567890, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [777, None]
        match.msg = None
        match.participants = [
            MagicMock(user_id="123", result=[]),
            MagicMock(user_id="456", result=[]),
        ]
        match.set = AsyncMock(side_effect=RuntimeError("match write failed"))
        mock_match.find_one = AsyncMock(return_value=match)

        clan = MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        self.bot.coc_client.get_clan = AsyncMock(return_value=clan)
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        # When the durable match update fails, then no occupancy or Discord mutation follows.
        with self.assertRaisesRegex(RuntimeError, "match write failed"):
            await self.cog.match_set_clan.callback.func(self.cog, ctx, clan="#NEW")

        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        mock_free_clan.assert_not_awaited()
        mock_occupy_clan.assert_not_awaited()
        ctx.channel.fetch_message.assert_not_awaited()
        ctx.send.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()

    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.remove_helper_task", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.clear_match_base_stores", new_callable=AsyncMock)
    @patch("cogs.match_commands.send_bases", new_callable=AsyncMock)
    @patch("cogs.match_commands.choose_tip", new_callable=AsyncMock)
    @patch("cogs.match_commands.is_clan_available", new_callable=AsyncMock)
    @patch("cogs.match_commands.is_raid_weekend", return_value=False)
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Match")
    async def test_match_start_updates_occupancy_before_discord_side_effects(
            self, mock_match, mock_player, _mock_is_raid_weekend, mock_is_clan_available, mock_choose_tip,
            mock_send_bases, mock_clear_base_stores, mock_free_clan, mock_occupy_clan,
            _mock_remove_helper_task, mock_clan_find):
        # Given a valid unstarted match and an available replacement clan.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 12345
        ctx.channel.name = "🟡 Match Thread"
        start_message = MagicMock(id=777)
        start_message.pin = AsyncMock()
        ctx.channel.edit = AsyncMock()
        events = []

        match = MagicMock()
        match.token = "TOKEN"
        match.thread = "12345"
        match.is_war = False
        match.times = [None, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [None, None]
        match.msg = None
        match.final_districts = ["D1", "D2", "D3"]
        match.participants = [
            MagicMock(
                user_id="123",
                bases=[MagicMock(link="http://p1-1"), MagicMock(link="http://p1-2"), MagicMock(link="http://p1-3")],
                base_store=None, opponent_bases_stored=False,
            ),
            MagicMock(
                user_id="456",
                bases=[MagicMock(link="http://p2-1"), MagicMock(link="http://p2-2"), MagicMock(link="http://p2-3")],
                base_store=None, opponent_bases_stored=False,
            ),
        ]
        async def record_match_set(update):
            if "is_war" in update:
                events.append(("persist", match.host_clans[0]))

        async def record_free(_old_clan, **_kwargs):
            events.append(("free", match.host_clans[0]))

        async def record_occupy(_match, _player1_index, _player2_index):
            events.append(("occupy", match.host_clans[0]))

        async def record_discord_send(*_args, **kwargs):
            self.assertEqual(kwargs["components"], [])
            self.assertNotEqual(kwargs["embed"].title, "**Match Started**")
            events.append(("pending", match.host_clans[0]))
            return start_message

        async def record_binding(query, update):
            if "message_publications" in update["$set"]:
                self.assertEqual(query["message_publications"], {"$exists": False})
                self.assertEqual(update["$set"]["message_publications"], [None, None])
                return MagicMock(matched_count=1)
            if set(update["$set"]) == {"message_publications.0"}:
                self.assertTrue(query["message_publications.0"]["pending"])
                self.assertFalse(update["$set"]["message_publications.0"].get("pending", False))
                self.assertEqual(query["old_msgs.0"], 777)
                return MagicMock(matched_count=1)
            if "public_message_publication" in update["$set"]:
                self.assertEqual(update["$set"]["public_message_publication"]["message_id"], 888)
                if "msg" in update["$set"]:
                    self.assertIsNone(query["msg"])
                    self.assertEqual(update["$set"]["msg"], "888")
                    self.assertTrue(update["$set"]["public_message_publication"]["pending"])
                else:
                    self.assertEqual(query["msg"], "888")
                    self.assertTrue(query["public_message_publication"]["pending"])
                    self.assertFalse(update["$set"]["public_message_publication"].get("pending", False))
                return MagicMock(matched_count=1)
            self.assertEqual(query["host_clans.0"], "#CLAN")
            self.assertIsNone(query["host_clans.1"])
            self.assertEqual(set(update["$set"]), {"old_msgs.0", "message_publications.0"})
            self.assertEqual(update["$set"]["old_msgs.0"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["message_id"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["embeds"][0]["title"], "**Match Started**")
            events.append(("bind", match.host_clans[0]))
            return MagicMock(matched_count=1)

        async def record_activate(**kwargs):
            self.assertEqual(match.old_msgs[0], 777)
            self.assertEqual(kwargs["embed"]["title"], "**Match Started**")
            events.append(("activate", match.host_clans[0]))

        match.set = AsyncMock(side_effect=record_match_set)
        match.get_saved_state.return_value = None
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(side_effect=record_binding)
        collection.find_one = AsyncMock(return_value={"_id": match.id})
        start_message.edit = AsyncMock(side_effect=record_activate)
        mock_match.find_one = AsyncMock(side_effect=[match, match, match, match, match])
        mock_free_clan.side_effect = record_free
        mock_occupy_clan.side_effect = record_occupy
        ctx.channel.send = AsyncMock(side_effect=record_discord_send)

        p1_doc = MagicMock(tag="#P1", elo=1200)
        p2_doc = MagicMock(tag="#P2", elo=1100)
        mock_player.find_one = AsyncMock(side_effect=[p1_doc, p2_doc])

        player1_acc = MagicMock(town_hall=3, tag="#P1", share_link="http://p1")
        player2_acc = MagicMock(town_hall=3, tag="#P2", share_link="http://p2")
        clan_obj = MagicMock()
        clan_obj.name = "Test Clan"
        clan_obj.tag = "#CLAN"
        clan_obj.share_link = "http://clan"
        public_message = MagicMock(id=888, edit=AsyncMock())
        public_channel = MagicMock()
        public_channel.send = AsyncMock(return_value=public_message)

        mock_is_clan_available.return_value = True
        mock_clan_find.return_value = MagicMock(availability_str="Available")
        mock_choose_tip.return_value = "tip"

        with (
                patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch(
                    "cogs.match_commands.get_start_base_store_slot_availability",
                    new_callable=AsyncMock,
                    return_value=(["Slot 2"], {"Slot 1": ("987", "789")}),
                ) as mock_start_slots,
        ):
            mock_coc_client.return_value.get_player = AsyncMock(side_effect=[player1_acc, player2_acc])
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            self.bot.fetch_channel = AsyncMock(return_value=public_channel)

            # When the match is started.
            await self.cog.match_start.callback.func(self.cog, ctx, clan="#CLAN")

        # Then persistence and in-memory sync precede occupancy, which precedes Discord I/O.
        self.assertEqual(
            events,
            [
                ("persist", "#OLD"),
                ("occupy", "#CLAN"),
                ("free", "#CLAN"),
                ("pending", "#CLAN"),
                ("bind", "#CLAN"),
                ("activate", "#CLAN"),
            ],
        )
        mock_free_clan.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        mock_occupy_clan.assert_awaited_once_with(match, 0, 1)
        self.assertEqual(collection.update_one.await_args.args[0]["old_msgs.0"], 777)
        self.assertEqual(mock_send_bases.await_args_list[0].args[:4], (ANY, "TOKEN", 1, 0))
        self.assertEqual(mock_send_bases.await_args_list[1].args[:4], (ANY, "TOKEN", 0, 1))
        self.assertEqual(match.persistent_host_clans[0], "#CLAN")
        mock_clear_base_stores.assert_not_awaited()
        self.assertEqual(public_channel.send.await_args.args[0], "tip")
        self.assertEqual(public_channel.send.await_args.kwargs["components"], [])
        ctx.send.assert_called()
        self.assertIn("Successfully started the match", ctx.send.call_args[0][0])
        self.assertIn("non-occupied base slot `Slot 2`", ctx.send.call_args[0][0])
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)

    async def test_match_start_uses_shared_live_start_after_atomic_claim(self) -> None:
        from cogs.live_hosting import live_start

        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel.id = 12345
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            participants=[MagicMock(user_id="123"), MagicMock(user_id="456")],
        )
        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")
        shared_start = AsyncMock(return_value=live_start.LiveStartSucceeded(message=MagicMock()))

        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.Player") as player_model,
                patch("cogs.match_commands.is_raid_weekend", return_value=False),
                patch("cogs.match_commands.is_clan_available", new=AsyncMock(return_value=True)),
                patch("cogs.match_commands.is_resource_clan_locked", return_value=False),
                patch("cogs.match_commands.get_start_base_store_slot_availability", new=AsyncMock(
                    return_value=(["Slot 2"], {"Slot 1": ("987", "789")}),
                )),
                patch("cogs.match_commands.live_start.start_live_match", new=shared_start),
                patch("cogs.match_commands.claim_resource_clan", new_callable=AsyncMock) as direct_claim,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            player_model.find_one = AsyncMock(side_effect=[
                MagicMock(tag="#P1", elo=1200), MagicMock(tag="#P2", elo=1100),
            ])
            self.cog.coc_client.get_player = AsyncMock(side_effect=[
                MagicMock(town_hall=3), MagicMock(town_hall=3),
            ])
            self.cog.coc_client.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_start.callback.func(self.cog, ctx, clan="#CLAN")

        shared_start.assert_awaited_once()
        direct_claim.assert_not_awaited()

    async def test_live_start_holds_both_side_reservations_until_claim(self) -> None:
        from cogs.live_hosting import live_start

        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match = MagicMock(
            token="TOKEN", times=[None, None], hosts=[None, None], host_clans=[None, None],
            persistent_host_clans=[None, None], old_msgs=[None, None], is_war=False,
        )
        request = live_start.LiveStart(
            match=match, host=MagicMock(id=999), clan=MagicMock(tag="#CLAN"),
            player1=MagicMock(), player2=MagicMock(), player1_elo=1200, player2_elo=1100,
            scout=False, reply=live_start.AutomaticLiveStartReply(),
        )
        claim_entered = asyncio.Event()
        allow_claim = asyncio.Event()

        async def pending_claim(*_args, **_kwargs):
            claim_entered.set()
            with anyio.fail_after(5):
                await allow_claim.wait()
            return False

        with (
                patch("cogs.live_hosting.live_start.match_commands.Match.find_one",
                      new=AsyncMock(return_value=match)),
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_clan",
                      new=AsyncMock(return_value=MagicMock())),
                patch("cogs.live_hosting.live_start.match_commands.claim_resource_clan",
                      new=AsyncMock(side_effect=pending_claim)),
        ):
            with anyio.fail_after(5):
                async with asyncio.TaskGroup() as tasks:
                    task = tasks.create_task(live_start.start_live_match(MagicMock(), MagicMock(), request))
                    await claim_entered.wait()
                    self.assertIsNone(await match_commands.try_reserve_resource_match_side("TOKEN", 1))
                    allow_claim.set()
                self.assertIsInstance(task.result(), live_start.LiveStartClanUnavailable)

        for player in (0, 1):
            reservation = await match_commands.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

        with (
                patch("cogs.live_hosting.live_start.match_commands.Match.find_one",
                      new=AsyncMock(return_value=match)),
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_clan",
                      new=AsyncMock(return_value=MagicMock())),
                patch("cogs.live_hosting.live_start.match_commands.claim_resource_clan",
                      new=AsyncMock(return_value=True)),
                patch("cogs.live_hosting.live_start.complete_live_start",
                      new=AsyncMock(return_value=MagicMock())),
        ):
            self.assertIsInstance(
                await live_start.start_live_match(MagicMock(), MagicMock(), request),
                live_start.LiveStartSucceeded,
            )
        for player in (0, 1):
            reservation = await match_commands.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

        busy = MagicMock(times=[None, 1], hosts=[None, None])
        with patch("cogs.live_hosting.live_start.match_commands.Match.find_one",
                   new=AsyncMock(return_value=busy)):
            self.assertIsInstance(
                await live_start.start_live_match(MagicMock(), MagicMock(), request),
                live_start.LiveStartBusy,
            )
        for player in (0, 1):
            reservation = await match_commands.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

        with (
                patch("cogs.live_hosting.live_start.match_commands.Match.find_one",
                      new=AsyncMock(return_value=match)),
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_clan",
                      new=AsyncMock(return_value=MagicMock())),
                patch("cogs.live_hosting.live_start.match_commands.claim_resource_clan",
                      new=AsyncMock(side_effect=RuntimeError("claim failed"))),
        ):
            with self.assertRaisesRegex(RuntimeError, "claim failed"):
                await live_start.start_live_match(MagicMock(), MagicMock(), request)
        for player in (0, 1):
            reservation = await match_commands.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

    async def test_live_start_persistence_failure_has_no_occupancy_or_discord_effects(self) -> None:
        from cogs.live_hosting import live_start

        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            old_msgs=[None, None],
            model_fields_set={"persistent_host_clans"},
        )
        match.set = AsyncMock(side_effect=RuntimeError("match write failed"))
        side0 = MagicMock()
        side1 = MagicMock()
        clan_reservation = MagicMock(clan_tag="#CLAN")
        ctx = MagicMock()
        ctx.channel.send = AsyncMock()
        request = live_start.LiveStart(
            match=match,
            host=MagicMock(id=999),
            clan=MagicMock(tag="#CLAN"),
            player1=MagicMock(),
            player2=MagicMock(),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            reply=live_start.AutomaticLiveStartReply(),
        )

        with (
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_match_side",
                      new=AsyncMock(side_effect=[side0, side1])),
                patch("cogs.live_hosting.live_start.match_commands.try_reserve_resource_clan",
                      new=AsyncMock(return_value=clan_reservation)),
                patch("cogs.match_commands.Match.find_one", new=AsyncMock(return_value=match)),
                patch("cogs.match_commands.Clan.find_one", new=AsyncMock(
                    return_value=MagicMock(availability_str="Available"))),
                patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock) as occupy,
        ):
            with self.assertRaisesRegex(RuntimeError, "match write failed"):
                await live_start.start_live_match(MagicMock(), ctx, request)

        occupy.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()

    @patch("cogs.match_commands.Clan.find_one", new_callable=AsyncMock)
    @patch("cogs.match_commands.remove_helper_task", new_callable=AsyncMock)
    @patch("cogs.match_commands.occupy_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.free_clan", new_callable=AsyncMock)
    @patch("cogs.match_commands.clear_match_base_stores", new_callable=AsyncMock)
    @patch("cogs.match_commands.send_bases", new_callable=AsyncMock)
    @patch("cogs.match_commands.is_clan_available", new_callable=AsyncMock, return_value=True)
    @patch("cogs.match_commands.is_raid_weekend", return_value=False)
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Match")
    async def test_match_start_persistence_failure_skips_occupancy_and_discord(
            self, mock_match, mock_player, _mock_is_raid_weekend, _mock_is_clan_available, mock_send_bases,
            mock_clear_base_stores, mock_free_clan, mock_occupy_clan, mock_remove_helper_task, mock_clan_find):
        # Given a valid unstarted match whose initial state write fails.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 12345
        ctx.channel.send = AsyncMock()

        match = MagicMock()
        match.token = "TOKEN"
        match.thread = "12345"
        match.is_autowin = False
        match.is_war = False
        match.times = [None, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.participants = [
            MagicMock(user_id="123", result=[]),
            MagicMock(user_id="456", result=[]),
        ]
        match.set = AsyncMock(side_effect=RuntimeError("match write failed"))
        mock_match.find_one = AsyncMock(return_value=match)

        mock_player.find_one = AsyncMock(side_effect=[
            MagicMock(tag="#P1", elo=1200),
            MagicMock(tag="#P2", elo=1100),
        ])
        player1_acc = MagicMock(town_hall=3)
        player2_acc = MagicMock(town_hall=3)
        clan_obj = MagicMock(name="Test Clan", tag="#CLAN", share_link="http://clan")
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        with (
                patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch(
                    "cogs.match_commands.get_start_base_store_slot_availability",
                    new_callable=AsyncMock,
                    return_value=(["Slot 2"], {"Slot 1": ("987", "789")}),
                ),
        ):
            mock_coc_client.return_value.get_player = AsyncMock(side_effect=[player1_acc, player2_acc])
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)

            # When the durable match update fails, then no occupancy or Discord mutation follows.
            with self.assertRaisesRegex(RuntimeError, "match write failed"):
                await self.cog.match_start.callback.func(self.cog, ctx, clan="#CLAN")

        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        mock_free_clan.assert_not_awaited()
        mock_occupy_clan.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()
        ctx.send.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()
        mock_send_bases.assert_not_awaited()
        mock_clear_base_stores.assert_not_awaited()
        mock_remove_helper_task.assert_not_awaited()

    @patch(
        "cogs.match_commands.get_start_base_store_slot_availability",
        new_callable=AsyncMock,
        return_value=([], {"Slot 1": ("200", "2"), "Slot 2": ("300", "3")}),
    )
    @patch("cogs.match_commands.is_raid_weekend", return_value=False)
    @patch("cogs.match_commands.is_clan_available", new_callable=AsyncMock, return_value=True)
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Match")
    async def test_match_start_blocks_when_both_foreign_slots_are_occupied(
            self, mock_match, mock_player, _mock_is_clan_available, _mock_is_raid_weekend, mock_start_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 12345

        match = MagicMock(
            thread="12345",
            is_war=False,
            times=[None, None],
            participants=[MagicMock(user_id="123"), MagicMock(user_id="456")],
        )
        match.set = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)
        mock_player.find_one = AsyncMock(side_effect=[
            MagicMock(tag="#P1", elo=1200),
            MagicMock(tag="#P2", elo=1100),
        ])
        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")

        with (
                patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch("cogs.match_commands.send_bases", new_callable=AsyncMock) as mock_send_bases,
        ):
            mock_coc_client.return_value.get_player = AsyncMock(side_effect=[
                MagicMock(town_hall=3),
                MagicMock(town_hall=3),
            ])
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_start.callback(ctx, clan="#CLAN")

        mock_send_bases.assert_not_awaited()
        match.set.assert_not_awaited()
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)
        response = ctx.send.call_args[0][0]
        self.assertIn("both base slots", response.lower())
        self.assertIn("<#200> <@2>", response)
        self.assertIn("<#300> <@3>", response)

    @patch("cogs.match_commands.clear_match_base_stores", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_match_data_clean_up_keeps_match_without_thread(
            self, mock_match, mock_clear_base_stores):
        match = MagicMock(thread=None)
        match.delete = AsyncMock()
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        guild = MagicMock()
        guild.fetch_thread = AsyncMock()
        self.bot.fetch_guild = AsyncMock(return_value=guild)

        await self.cog.match_data_clean_up.callback.func(self.cog)

        guild.fetch_thread.assert_not_awaited()
        mock_clear_base_stores.assert_not_awaited()
        match.delete.assert_not_awaited()

    @patch("cogs.match_commands.free_match_clans", new_callable=AsyncMock)
    @patch("cogs.match_commands.clear_match_base_stores", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_match_data_clean_up_requires_confirmed_deletion_before_release(
            self, mock_match, mock_clear_base_stores, mock_free_match_clans):
        match = MagicMock()
        match.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        match.thread = "987"
        match.delete = AsyncMock()
        collection = match.get_pymongo_collection.return_value
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        guild = MagicMock()
        guild.fetch_member = AsyncMock(side_effect=[MagicMock(), MagicMock()])
        guild.fetch_thread = AsyncMock(return_value=None)
        self.bot.fetch_guild = AsyncMock(return_value=guild)
        self.bot.http.get_channel = AsyncMock(return_value={"id": "987"})

        await self.cog.match_data_clean_up.callback.func(self.cog)

        self.bot.http.get_channel.assert_awaited_once_with(987)
        mock_clear_base_stores.assert_not_awaited()
        mock_free_match_clans.assert_not_awaited()
        match.delete.assert_not_awaited()
        collection.delete_one.assert_not_awaited()

    @patch("cogs.match_commands.clear_match_base_stores")
    @patch("cogs.match_commands.is_raid_weekend", return_value=True)
    @patch("cogs.match_commands.Player")
    @patch("cogs.match_commands.Match")
    async def test_match_start_blocks_without_stored_bases_during_raid_weekend(
            self, mock_match, mock_player, _mock_is_raid_weekend, mock_clear_base_stores):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        match = MagicMock()
        match.is_war = False
        match.times = [None, None]
        match.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match.find_one = AsyncMock(return_value=match)

        p1_doc = MagicMock(tag="#P1")
        p2_doc = MagicMock(tag="#P2")
        mock_player.find_one = AsyncMock(side_effect=[p1_doc, p2_doc])
        player_acc = MagicMock(town_hall=3)
        with patch("cogs.match_commands.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_player = AsyncMock(return_value=player_acc)
            await self.cog.match_start.callback(ctx, clan="#CLAN")

        ctx.send.assert_called()
        self.assertIn("requires both players' bases", ctx.send.call_args[0][0])
        self.assertIn("separate slots in the same clan", ctx.send.call_args[0][0])
        mock_clear_base_stores.assert_not_called()

    @patch("cogs.match_commands.Match")
    async def test_match_base_remove_success(self, mock_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        # Mock match
        mock_match_doc = MagicMock(spec=Match)
        mock_base_obj = MagicMock()
        mock_base_obj.link = "http://link"
        mock_match_doc.participants = [
            MagicMock(user_id="123", bases=[mock_base_obj, None, None]),
            MagicMock(user_id="456", bases=[None, None, None])
        ]
        mock_match_doc.is_war = True
        mock_match_doc.hosts = [None, None]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        
        # Execute
        await self.cog.match_base_remove.callback(ctx, player=1, district="Barbarian Camp")

        # Verify
        mock_match_doc.save_changes.assert_called_once()
        self.assertIsNone(mock_match_doc.participants[0].bases[0])
        ctx.send.assert_called()
        self.assertIn("removed", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.validators")
    @patch("cogs.match_commands.BaseLayout")
    @patch("cogs.match_commands.Match")
    async def test_match_base_set_image_success(self, mock_match, mock_base_layout, mock_validators):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 999
        
        mock_validators.url.return_value = True
        
        # Mock Match
        mock_match_doc = MagicMock()
        mock_base = MagicMock()
        mock_match_doc.participants = [
            MagicMock(user_id="111", bases=[mock_base, None, None]),
            MagicMock(user_id="222")
        ]
        mock_match_doc.hosts = [str(ctx.author.id)]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match_doc.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        
        attachment = MagicMock(spec=ipy.Attachment)
        attachment.url = "http://image.url"
        attachment.content_type = "image/png"
        
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)
        
        # Execute
        # Mock download_file (imported in cogs.match_commands)
        self.bot.owner.send = AsyncMock()
        self.bot.owner.send.return_value.attachments = [MagicMock(url="http://new_image")]
        
        with patch("cogs.match_commands.download_file", new=AsyncMock(return_value=True)):
             with patch("cogs.match_commands.delete_file", new=AsyncMock()):
                 await self.cog.match_base_set_image.callback(ctx, player=0, district="Barbarian Camp", base_ss=attachment)

        # Verify
        mock_match_doc.save_changes.assert_called_once()
        ctx.send.assert_called()
        self.assertIn("set", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.validators")
    @patch("cogs.match_commands.BaseLayout")
    @patch("cogs.match_commands.Match")
    async def test_match_base_set_success(self, mock_match, mock_base_layout, mock_validators):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 999
        
        mock_validators.url.return_value = True
        mock_base_layout.find_one = AsyncMock(return_value=None) # No existing base
        
        # Mock Match
        mock_match_doc = MagicMock()
        mock_match_doc.participants = [MagicMock(user_id="111"), MagicMock(user_id="222")]
        mock_match_doc.hosts = [str(ctx.author.id)]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match_doc.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        
        # Execute
        # Link must be valid per code logic
        # 3A1 is Barbarian Camp, encoded as %3A1 in the ID part (because split('%')[2] gets 3A1)
        await self.cog.match_base_set.callback(ctx, player=123, base_link="https://link.clashofclans.com/?action=OpenLayout&id=TH15%3ACC%3A1%3AAAAAA", base_ss=None)

        # Verify
        mock_match_doc.save_changes.assert_called()
        ctx.send.assert_called()
        self.assertIn("set", ctx.send.call_args[0][0])

    @patch("cogs.match_commands.validators")
    @patch("cogs.match_commands.BaseLayout")
    @patch("cogs.match_commands.Match")
    async def test_match_base_submit_success(self, mock_match, mock_base_layout, mock_validators):
        for scheduled_time in (3000000000, None):
            with self.subTest(scheduled_time=scheduled_time):
                ctx = MagicMock(spec=ipy.SlashContext)
                ctx.author.id = 123
                ctx.author.has_any_role.return_value = True
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                ctx.channel.id = 999
                ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[])
                mock_msg = MagicMock(
                    pin=AsyncMock(), edit=AsyncMock(),
                    embeds=[MagicMock(fields=[MagicMock() for _ in range(4)] + [MagicMock(value="")])],
                )
                mock_msg.author.id = BOT_ID
                mock_msg.components = [ipy.ActionRow(ipy.Button(
                    style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN",
                ))]
                ctx.channel.history.return_value.__aiter__.return_value = [mock_msg]
                mock_validators.url.return_value = True
                mock_base_layout.find_one = AsyncMock(return_value=None)
                mock_match_doc = MagicMock(
                    token="TOKEN", thread_message_id=None,
                    participants=[MagicMock(user_id="123"), MagicMock(user_id="456")],
                    times=[scheduled_time, scheduled_time], is_war=False,
                    matched_time=3000000000, final_districts=["Barbarian Camp"],
                    save_changes=AsyncMock(),
                )
                mock_match.find_one = AsyncMock(return_value=mock_match_doc)

                await self.cog.match_base_submit.callback(
                    ctx,
                    base_link="https://link.clashofclans.com/?action=OpenLayout&id=TH15%3ACC%3A1%3AAAAAA",
                    base_ss=None,
                )

                mock_match_doc.save_changes.assert_awaited()
                self.assertIn("submitted", ctx.send.await_args.args[0])

    @patch("cogs.match_commands.BaseLayout")
    @patch("cogs.match_commands.Match")
    async def test_match_base_view_success(self, mock_match, mock_base_layout):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 999
        
        # Mock Match
        mock_match_doc = MagicMock()
        mock_match_doc.participants = [
            MagicMock(user_id="123", bases=[MagicMock(link="http://link", image="http://image")]),
            MagicMock(user_id="456")
        ]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        
        # Mock BaseLayout
        mock_base_layout.find.return_value.to_list = AsyncMock(return_value=[])
        
        # Execute
        await self.cog.match_base_view.callback(ctx)

        # Verify
        mock_base_layout.find.assert_called()
        ctx.send.assert_called()
        self.assertIn("Here are", ctx.send.call_args[0][0])
        self.assertIsInstance(ctx.send.call_args[1]['embeds'][0], ipy.Embed)

    def test_is_match_finished_distinguishes_regular_and_targeted_war_completion(self):
        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.participants = [
            MagicMock(result=["3.100", "2.90", "2.80"]),
            MagicMock(result=[None, None, None]),
        ]

        self.assertFalse(is_match_finished(match))
        self.assertTrue(is_match_finished(match, player=0))
        self.assertFalse(is_match_finished(match, player=1))

        match.participants[1].result = ["3.100", "3.100", "3.100"]
        self.assertTrue(is_match_finished(match))

        match.is_autowin = True
        match.participants[0].result = [None, None, None]
        self.assertTrue(is_match_finished(match))
        self.assertTrue(is_match_finished(match, player=0))

    @patch("cogs.match_commands.Match")
    async def test_player_occupied_distinguishes_regular_and_war_completion(self, mock_match):
        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = False
        match.thread = "123"
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        self.assertIn("unfinished", (await is_player_occupied("111")).message)

        match.is_war = True
        self.assertIsNone(await is_player_occupied("111"))

    @patch("cogs.match_commands.Match")
    async def test_player_occupied_offers_inline_confirmation_for_unconfirmed_match(self, mock_match):
        match = MagicMock(spec=Match)
        match.token = "abc123"
        match.is_autowin = False
        match.is_challenge = False
        match.is_war = False
        match.thread = None
        match.participants = [
            MagicMock(user_id="111", confirmed=False),
            MagicMock(user_id="222", confirmed=False),
        ]
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        occupied = await is_player_occupied("111")

        self.assertEqual(
            "You have an unconfirmed match, please check your DM or confirm it now.",
            occupied.message,
        )
        self.assertEqual("Confirm Match", occupied.component.label)
        self.assertEqual("confirm_occupied_match|abc123", occupied.component.custom_id)

    @patch("cogs.match_commands.Match")
    async def test_confirm_occupied_match_opens_district_confirmation_modal(self, mock_match):
        match = MagicMock(wizard_valley_available=False, capital_peak_available=False)
        mock_match.find_one = AsyncMock(return_value=match)
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "confirm_occupied_match|abc123"
        ctx.send_modal = AsyncMock()

        await self.cog.confirm_occupied_match_button.callback(ctx)

        modal = ctx.send_modal.call_args.args[0]
        components = modal["data"]["components"]
        self.assertEqual("confirm_occupied_match_modal|abc123", modal["data"]["custom_id"])
        self.assertEqual(3, len(components))
        self.assertTrue(all(component["type"] == 18 for component in components))
        self.assertEqual(
            [
                "inline_district|abc123|0",
                "inline_district|abc123|1",
                "inline_district|abc123|2",
            ],
            [label["component"]["custom_id"] for label in components],
        )

    @patch("cogs.match_commands.Match")
    async def test_inline_confirmation_modal_reuses_match_confirmation(self, mock_match):
        participant = MagicMock(
            user_id="111",
            confirmed=False,
            districts=[None, None, None],
        )
        opponent = MagicMock(user_id="222", confirmed=False)
        match = MagicMock(spec=Match, participants=[participant, opponent])
        match.is_challenge = False
        match.confirmation_tracking = False
        match.wizard_valley_available = False
        match.capital_peak_available = False
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)

        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "confirm_occupied_match_modal|abc123"
        ctx.author.id = 111
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        await self.cog.confirm_occupied_match_modal.callback(
            ctx,
            **{
                "inline_district|abc123|0": ["Barbarian Camp"],
                "inline_district|abc123|1": ["Balloon Lagoon"],
                "inline_district|abc123|2": ["Dragon Cliffs"],
            },
        )

        self.assertTrue(participant.confirmed)
        self.assertEqual(
            ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"],
            participant.districts,
        )
        match.save_changes.assert_awaited_once()

    @patch("cogs.match_commands.Match")
    async def test_original_dm_cannot_confirm_after_inline_modal_confirmation(self, mock_match):
        participant = MagicMock(
            user_id="111",
            confirmed=True,
            districts=["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"],
        )
        opponent = MagicMock(user_id="222", confirmed=False)
        match = MagicMock(spec=Match, participants=[participant, opponent])
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)

        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "match_confirm|abc123"
        ctx.author.id = 111
        ctx.channel = None
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.edit = AsyncMock()

        await self.cog.match_confirm_button.callback(ctx)

        match.save_changes.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        self.assertIn("already confirmed", ctx.send.call_args.args[0])

    @patch("cogs.match_commands.Match")
    async def test_match_start_blocks_finished_regular_match(self, mock_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = False
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=["2.90", "2.80", "1.70"]),
        ]
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.match_start.callback(ctx, clan="#CLAN")

        self.assertIn("finished", ctx.send.call_args[0][0].lower())

    @patch("cogs.match_commands.Match")
    async def test_match_result_store_allows_finished_war_player_correction(self, mock_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.id = PydanticObjectId("000000000000000000000001")
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.is_autowin = False
        match.is_war = True
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.times = [100, 200]
        match.hosts = [None, None]
        match.host_clans = ["#FIRST", "#SECOND"]
        match.old_msgs = [123, 456]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        match.set = AsyncMock()
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.match_result_store.callback(
            ctx, player=0, district="Barbarian Camp", stars=2, destruction=88
        )

        collection.update_one.assert_awaited_once_with(
            {"_id": match.id, "is_war": True, "participants.0.user_id": "111", "times.0": 100,
             "hosts.0": None, "host_clans.0": "#FIRST", "old_msgs.0": 123, "result_operation": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             "participants.0.result.0": "3.100"},
            {"$set": {"participants.0.result.0": "2.88"}},
        )
        self.assertIn("recorded", ctx.send.call_args[0][0].lower())

        ctx.send.reset_mock()
        collection.update_one.reset_mock()
        await self.cog.match_result_store.callback(
            ctx, player=1, district="Barbarian Camp", stars=2, destruction=90
        )

        collection.update_one.assert_awaited_once_with(
            {"_id": match.id, "is_war": True, "participants.1.user_id": "222", "times.1": 200,
             "hosts.1": None, "host_clans.1": "#SECOND", "old_msgs.1": 456, "result_operation": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             "participants.1.result.0": None},
            {"$set": {"participants.1.result.0": "2.90"}},
        )
        match.set.assert_not_awaited()
        self.assertIn("recorded", ctx.send.call_args[0][0].lower())

    @patch("cogs.match_commands.Match")
    async def test_result_store_button_allows_finished_war_player_correction(self, mock_match):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "match_result_store|token|0"
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send_modal = AsyncMock()

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.old_msgs = [123, None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.hosts = [None, None]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.result_store_button.callback(ctx)

        ctx.send_modal.assert_awaited_once()
        self.assertEqual("store_modal|token|0", ctx.send_modal.call_args.args[0].custom_id)

    @patch("cogs.match_commands.Match")
    async def test_result_store_button_modal_title_stays_within_discord_limits(self, mock_match):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "match_result_store|token|0"
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send_modal = AsyncMock()

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.old_msgs = [123, None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.hosts = [None, None]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", None, None]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        mock_match.find_one = AsyncMock(return_value=match)

        mock_user = MagicMock(spec=ipy.User)
        mock_user.__str__ = MagicMock(return_value="VeryLongDisplayNameExceedingLimits12345#1234")
        self.cog.bot.fetch_user = AsyncMock(return_value=mock_user)

        await self.cog.result_store_button.callback(ctx)

        ctx.send_modal.assert_awaited_once()
        modal = ctx.send_modal.call_args.args[0]
        self.assertLessEqual(len(modal.title), 45)

    @patch("cogs.match_commands.download_file", new_callable=AsyncMock)
    @patch("cogs.match_commands.Match")
    async def test_match_base_set_image_checks_finished_opponent(self, mock_match, mock_download):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 123
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = True
        match.hosts = ["999", None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.participants = [
            MagicMock(user_id="111", result=[None, None, None], bases=[MagicMock(), None, None]),
            MagicMock(user_id="222", result=["3.100", "2.90", "2.80"], bases=[None, None, None]),
        ]
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.match_base_set_image.callback(
            ctx,
            player=0,
            district="Barbarian Camp",
            base_ss=MagicMock(spec=ipy.Attachment),
        )

        mock_download.assert_not_awaited()
        match.save_changes.assert_not_awaited()
        self.assertIn("finished", ctx.send.call_args[0][0].lower())

    @patch.object(OverrideCode, "is_valid", new_callable=AsyncMock)
    @patch("cogs.match_commands.BaseLayout")
    @patch("cogs.match_commands.Match")
    async def test_match_base_remove_all_checks_effective_player_opponent(
            self, mock_match, mock_base_layout, mock_is_valid):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 111
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        own_base = MagicMock(link="https://example.com/base")
        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = True
        match.hosts = [None, None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.participants = [
            MagicMock(user_id="111", result=[None, None, None], bases=[own_base, None, None]),
            MagicMock(user_id="222", result=["3.100", "2.90", "2.80"], bases=[None, None, None]),
        ]
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)
        mock_base_layout.find_one = AsyncMock(return_value=None)
        mock_is_valid.return_value = False

        await self.cog.match_base_remove_all.callback(ctx, player=1)

        self.assertIs(match.participants[0].bases[0], own_base)
        match.save_changes.assert_not_awaited()
        self.assertIn("finished", ctx.send.call_args[0][0].lower())

    async def test_match_autowin_allows_finished_war_player_correction(self):
        from tests._autowin_fixture import AutowinState

        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.rows[Match][0]["participants"][0]["result"] = ["3.100", "2.90", "2.80"]
        fixture.rows[Player][0]["elo"] = 1200
        fixture.rows[Player][1].update(elo=1200, highest_elo=1250)
        ctx = fixture.context()

        await fixture.invoke(ctx, winner="222")

        self.assertTrue(fixture.rows[Match][0]["is_autowin"])
        self.assertTrue(fixture.rows[Match][0]["autowin_completed"])
        self.assertEqual(fixture.elos(), [1165, 1205])
        self.assertEqual(fixture.rows[Player][1]["highest_elo"], 1250)
        self.assertIn("auto-win has been given", ctx.send.await_args.args[0].lower())
        fixture.cleanup.assert_has_awaits([call("321", "111"), call("321", "222")])
        fixture.schedule.assert_called_once_with(ctx.channel, "🔴")
        fixture.autoqueue.assert_awaited_once_with(fixture.bot, fixture.match.token, ("111", "222"))
        ctx.channel.edit.assert_not_called()

    @patch("cogs.match_commands.Match")
    async def test_result_store_modal_allows_finished_war_player_correction(self, mock_match):
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "store_modal|token|0"
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Ended**")]

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.id = "MATCH"
        match.is_autowin = False
        match.is_war = True
        match.old_msgs = [123, None]
        match.times = [100, None]
        match.hosts = [None, None]
        match.host_clans = [None, None]
        match.submitters = [None, None]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.result_store_modal.callback.func(
            self.cog,
            ctx,
            result1="2.88",
            result2="2.90",
            result3="2.80",
        )

        collection.update_one.assert_awaited_once()
        self.assertEqual(collection.update_one.await_args.args[0]["old_msgs.0"], 123)
        self.assertEqual(collection.update_one.await_args.args[1], {"$set": {
            "participants.0.result.0": "2.88",
            "participants.0.result.1": "2.90",
            "participants.0.result.2": "2.80",
            "submitters.0": "999",
        }})
        self.assertIn("stored", ctx.send.call_args[0][0].lower())

    @patch("cogs.match_commands.Match")
    async def test_process_match_results_rechecks_finished_channel(self, mock_match):
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "result_modal|token"
        ctx.channel.name = "🔴 Finished match"
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.thread = "123"
        match.participants = [
            MagicMock(user_id="111"),
            MagicMock(user_id="222"),
        ]
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)
        self.bot.fetch_channel = AsyncMock(return_value=ctx.channel)

        await self.cog._process_match_results(
            ctx,
            {
                "player1": "3.100\n2.90\n2.80",
                "player2": "2.90\n2.80\n1.70",
            },
        )

        match.save_changes.assert_not_awaited()
        self.assertIn("already ended", ctx.send.call_args[0][0].lower())

    @patch("cogs.match_commands.Match")
    async def test_raid_weekend_start_notify_skips_ended_matches(self, mock_match):
        cases = [
            ("autowin", True, [None] * 3, "match-thread"),
            ("completed results", False, ["3.100"] * 3, "match-thread"),
            ("red thread", False, [None] * 3, "🔴 match-thread"),
        ]
        for reason, autowin, results, thread_name in cases:
            with self.subTest(reason=reason):
                thread = AsyncMock(spec=ipy.ThreadChannel)
                thread.name = thread_name
                thread.send = AsyncMock()
                self.bot.fetch_channel = AsyncMock(return_value=thread)
                match = MagicMock(spec=Match)
                match.thread = "123"
                match.is_autowin = autowin
                match.participants = [
                    MagicMock(user_id=user_id, opponent_bases_stored=False, result=list(results))
                    for user_id in ("111", "222")
                ]
                mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

                callback = getattr(self.cog.raid_weekend_start_notify, "callback", self.cog.raid_weekend_start_notify)
                await callback()

                thread.send.assert_not_called()

    @patch("cogs.match_commands.Match")
    async def test_raid_weekend_start_notify_sends_to_active_match(self, mock_match):
        thread = AsyncMock(spec=ipy.ThreadChannel)
        thread.name = "match-thread"
        thread.send = AsyncMock()
        self.bot.fetch_channel = AsyncMock(return_value=thread)

        active_match = MagicMock(spec=Match)
        active_match.thread = "123"
        active_match.is_autowin = False
        active_match.participants = [
            MagicMock(user_id="111", opponent_bases_stored=False, result=[None, None, None]),
            MagicMock(user_id="222", opponent_bases_stored=True, result=[None, None, None]),
        ]
        find_result = MagicMock()
        find_result.to_list = AsyncMock(return_value=[active_match])
        mock_match.find.return_value = find_result

        callback = getattr(self.cog.raid_weekend_start_notify, "callback", self.cog.raid_weekend_start_notify)
        await callback()

        thread.send.assert_called_once()
        self.assertIn("<@111>", thread.send.call_args[0][0])
        self.assertIn("raid weekend is starting soon", thread.send.call_args[0][0])

    def test_is_match_finished_detects_completed_indicators(self):
        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.autowin_completed = True
        match.participants = [
            MagicMock(result=[None, None, None]),
            MagicMock(result=[None, None, None]),
        ]
        self.assertTrue(is_match_finished(match))

        match.autowin_completed = False
        match.recorded_match_id = "rec123"
        self.assertTrue(is_match_finished(match))

        match.recorded_match_id = None
        match.result_operation = MagicMock(completed=True)
        self.assertTrue(is_match_finished(match))


if __name__ == "__main__":
    unittest.main()
