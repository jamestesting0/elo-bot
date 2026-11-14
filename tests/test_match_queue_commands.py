from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch, ANY
import interactions as ipy
from elo_bot.features.queue.policy import (
    QUEUE_TIER_AGED, QUEUE_TIER_MIXED, QUEUE_TIER_PRIORITY, QUEUE_TIER_NORMAL, QUEUE_TIER1_AGED_AFTER_SECONDS,
)
from elo_bot.db import Player, Queue, ServerState
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from tests._patch_sites import patch_sites
import elo_bot.extensions.match.queue as match_queue_module
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
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
                actual = match_queue_module.get_district_options(wizard_available, peak_available)

                # Then: it returns the exact announced groups allowed by the snapshot
                self.assertEqual(actual, expected)

    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
    @patch('elo_bot.features.queue.matching.is_player_occupied')
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

    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
    @patch('elo_bot.features.queue.matching.is_player_occupied')
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

    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
    @patch('elo_bot.features.queue.matching.is_player_occupied')
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

    @patch('elo_bot.features.queue.matching.get_last_opponent')
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
    @patch('elo_bot.features.queue.matching.is_player_occupied')
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

    @patch('elo_bot.features.queue.matching.initiate_match')
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
    @patch('elo_bot.features.queue.matching.is_player_occupied')
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

    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
    @patch('elo_bot.features.queue.matching.is_player_occupied')
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

    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
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

    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.extensions.match.queue.Queue', 'elo_bot.features.queue.state.Queue', 'elo_bot.features.queue.matching.Queue'))
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
    @patch('elo_bot.features.queue.matching.match_players')
    @patch_sites(('elo_bot.features.queue.state.get_match_queue', 'elo_bot.features.queue.matching.get_match_queue'))
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
    @patch('elo_bot.features.queue.matching.match_players')
    @patch_sites(('elo_bot.features.queue.state.get_match_queue', 'elo_bot.features.queue.matching.get_match_queue'))
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
