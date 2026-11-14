from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock, call
import interactions as ipy
from elo_bot.extensions.challenge import ChallengeCommands
from elo_bot.core.match_results import is_match_finished
from elo_bot.features.matches.occupancy import is_player_occupied
from elo_bot.features.matches.lifecycle import initiate_match
from elo_bot.extensions.preset import PresetCommands
from elo_bot.db import Match, OverrideCode, Participant, ServerState
from elo_bot.config.ids import BOT_ID
from tests._patch_sites import patch_sites
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
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

        with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model:
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

                with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), return_value=match) as mock_match, \
                        patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")), \
                        patch_sites(('elo_bot.extensions.match.lifecycle.confirm_match', 'elo_bot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock), \
                        patch_sites(('elo_bot.features.matches.lifecycle.random.choice', 'elo_bot.features.matches.attack_bases.random.choice')) as mock_choice:
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
        with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), return_value=match) as match_type, \
                patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")), \
                patch_sites(('elo_bot.extensions.match.datetime', 'elo_bot.extensions.match.queue.datetime', 'elo_bot.extensions.match.lifecycle.datetime', 'elo_bot.extensions.match.bases.datetime', 'elo_bot.extensions.match.hosting.datetime', 'elo_bot.features.queue.state.datetime', 'elo_bot.features.queue.matching.datetime', 'elo_bot.features.matches.resources.datetime', 'elo_bot.features.matches.lifecycle.datetime', 'elo_bot.features.matches.attack_bases.datetime', 'elo_bot.features.results.recording.datetime')) as clock:
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

        with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), return_value=match) as mock_match, \
                patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")), \
                patch_sites(('elo_bot.extensions.match.lifecycle.confirm_match', 'elo_bot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock), \
                patch_sites(('elo_bot.features.matches.lifecycle.random.choice', 'elo_bot.features.matches.attack_bases.random.choice'),
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

        with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), return_value=match) as mock_match, \
                patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")), \
                patch_sites(('elo_bot.extensions.match.lifecycle.confirm_match', 'elo_bot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock), \
                patch_sites(('elo_bot.features.matches.lifecycle.random.choice', 'elo_bot.features.matches.attack_bases.random.choice'),
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

                with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), return_value=match), \
                        patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")), \
                        patch_sites(('elo_bot.extensions.match.lifecycle.confirm_match', 'elo_bot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock), \
                        patch_sites(('elo_bot.features.matches.lifecycle.random.choice', 'elo_bot.features.matches.attack_bases.random.choice'),
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

                with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), return_value=match) as mock_match, \
                        patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")), \
                        patch_sites(('elo_bot.extensions.match.lifecycle.confirm_match', 'elo_bot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock), \
                        patch_sites(('elo_bot.features.matches.lifecycle.random.choice', 'elo_bot.features.matches.attack_bases.random.choice')) as mock_choice:
                    # When: regular matching resolves the primary presets
                    await initiate_match(bot, *players)

                # Then: the primary selections remain unchanged and no fallback is consumed
                participant = mock_match.call_args.kwargs["participants"][0]
                self.assertEqual(participant.districts, primary)
                mock_choice.assert_not_called()

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch("elo_bot.extensions.preset.Player")
    @patch("elo_bot.extensions.preset.BaseLayout")
    async def test_base_presets_accept_new_district_codes(self, mock_base, mock_player):
        # Given: valid private links for Wizard Valley and Capital Peak
        cog = PresetCommands(self.bot)
        ctx = MagicMock(author=MagicMock(id=111), defer=AsyncMock(), send=AsyncMock())
        attachment = MagicMock(url="https://example.test/base.webp")
        mock_base.find_one = AsyncMock(return_value=None)
        mock_player.find_one = AsyncMock(return_value=None)

        for district_code in ("3A2", "3A0"):
            with self.subTest(district_code=district_code), \
                    patch("elo_bot.extensions.preset.validators.url", return_value=True):
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

        with patch("elo_bot.extensions.challenge.Player") as mock_player, \
                patch("elo_bot.extensions.challenge.Match") as mock_match, \
                patch("elo_bot.extensions.challenge.datetime") as mock_datetime, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)), \
                patch("elo_bot.extensions.challenge.is_player_occupied", new=AsyncMock(return_value=None)):
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

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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
        with patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client_prop:
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

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'), new_callable=AsyncMock)
    async def test_on_start_reconciles_base_store_slots_before_other_tasks(self, mock_sync_slots):
        with patch.object(
                self.cog,
                "inactive_threads_delete",
                new=AsyncMock(side_effect=RuntimeError("stop after reconciliation"))
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after reconciliation"):
                await self.cog.on_start.callback(self.cog)

        mock_sync_slots.assert_awaited_once_with(bot=self.bot)

    @patch_sites(('elo_bot.extensions.match.ServerState.current', 'elo_bot.extensions.match.queue.ServerState.current', 'elo_bot.extensions.match.dashboards.ServerState.current', 'elo_bot.features.queue.matching.ServerState.current', 'elo_bot.features.matches.tips.ServerState.current'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'), new_callable=AsyncMock)
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

    @patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch_sites(('elo_bot.extensions.match.lifecycle.free_match_clans', 'elo_bot.extensions.match.results.free_match_clans'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
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
