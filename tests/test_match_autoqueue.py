import asyncio
import unittest
from datetime import datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import interactions as ipy

import elobot.extensions.match as match_commands
from elobot.extensions.match import (
    BOT_ID,
    MatchCommands,
    Player,
    Queue,
    QUEUE_TIER_AGED,
    QUEUE_TIER_MIXED,
    QUEUE_TIER_NORMAL,
    QUEUE_TIER_PRIORITY,
    ServerState,
)


class TestQueueCore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.user.id = BOT_ID
        self.bot.fetch_guild = AsyncMock()
        self.command_owner = MatchCommands(self.bot)
        self.queues = {}
        self.enterContext(patch("elobot.extensions.match.find_attempt_match", new=AsyncMock(return_value=None)))
        self.enterContext(patch("elobot.extensions.match.find_attempt_queue", new=AsyncMock(
            side_effect=lambda user_id, attempt_id, _tiers: next(
                (queue for queue in self.queues.values()
                 if user_id in queue.district_selections
                 and queue.district_selections[user_id].attempt_id == attempt_id), None),
        )))

    @staticmethod
    def unbound_callback(callback):
        return getattr(callback, "func", callback)

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

    @staticmethod
    def make_queue(tier: int, players=None, entered_at=None):
        queue = MagicMock(spec=Queue)
        queue.tier = tier
        queue.players = list(players or [])
        queue.entered_at = dict(entered_at or {})
        queue.district_selections = {}
        queue.save_changes = AsyncMock()
        return queue

    @staticmethod
    def make_member(user_id: int = 123):
        member = MagicMock(spec=ipy.Member)
        member.id = user_id
        member.has_role.return_value = False
        return member

    def queue_fixture(self, player, *, tier3_players=None, tier3_entered_at=None):
        queues = {
            QUEUE_TIER_AGED: self.make_queue(QUEUE_TIER_AGED),
            QUEUE_TIER_MIXED: self.make_queue(QUEUE_TIER_MIXED),
            QUEUE_TIER_PRIORITY: self.make_queue(QUEUE_TIER_PRIORITY),
            QUEUE_TIER_NORMAL: self.make_queue(QUEUE_TIER_NORMAL, tier3_players, tier3_entered_at),
        }
        self.queues = queues
        return queues

    async def test_duplicate_does_not_reset_timestamp(self):
        # Given
        player = self.make_player("123", 1000)
        member = self.make_member()
        entered_at = {player.user_id: 1_700_000_000.0}
        queues = self.queue_fixture(player)
        queues[QUEUE_TIER_AGED].players = [player]
        queues[QUEUE_TIER_AGED].entered_at = entered_at
        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: queues[tier]):
            player_model.find_one = AsyncMock(return_value=player)
            # When
            outcome = await match_commands.queue_player(self.bot, member, priority=False)

        # Then
        self.assertEqual(outcome.status, match_commands.QueueOutcomeStatus.ALREADY_QUEUED)
        self.assertEqual(queues[QUEUE_TIER_AGED].entered_at, entered_at)

    async def test_occupied_does_not_mutate_any_queue(self):
        # Given
        player = self.make_player("123", 1000)
        member = self.make_member()
        queues = self.queue_fixture(player)
        occupied = match_commands.PlayerOccupied("unfinished", MagicMock(spec=ipy.Button))
        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: queues[tier]), \
                patch("elobot.extensions.match.is_player_occupied", new=AsyncMock(return_value=occupied)):
            player_model.find_one = AsyncMock(return_value=player)
            # When
            outcome = await match_commands.queue_player(self.bot, member, priority=False)

        # Then
        self.assertEqual(outcome.status, match_commands.QueueOutcomeStatus.OCCUPIED)
        self.assertIs(outcome.occupied, occupied)
        self.assertTrue(all(not queue.players for queue in queues.values()))

    async def test_blacklist_and_queue_disabled_return_distinct_statuses(self):
        # Given
        blacklisted_member = self.make_member()
        blacklisted_member.has_role.return_value = True
        disabled_member = self.make_member(456)

        # When
        blacklist_outcome = await match_commands.queue_player(self.bot, blacklisted_member, priority=False)
        with patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=True))):
            disabled_outcome = await match_commands.queue_player(self.bot, disabled_member, priority=False)

        # Then
        self.assertEqual(blacklist_outcome.status, match_commands.QueueOutcomeStatus.BLACKLISTED)
        self.assertEqual(disabled_outcome.status, match_commands.QueueOutcomeStatus.DISABLED)

    async def test_unregistered_player_returns_structured_status(self):
        member = self.make_member()
        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))):
            player_model.find_one = AsyncMock(return_value=None)
            outcome = await match_commands.queue_player(self.bot, member, priority=False)

        self.assertEqual(outcome.status, match_commands.QueueOutcomeStatus.UNREGISTERED)

    async def test_priority_immediate_match_and_beta_queue_have_distinct_outcomes(self):
        # Given
        player = self.make_player("123", 1000)
        opponent = self.make_player("456", 1130)
        member = self.make_member()
        queues = self.queue_fixture(player, tier3_players=[opponent], tier3_entered_at={opponent.user_id: datetime.now().timestamp()})

        async def insert_match(_bot, _player, _opponent, *, on_inserted):
            await on_inserted()

        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: queues[tier]), \
                patch("elobot.extensions.match.is_player_occupied", new=AsyncMock(return_value=None)), \
                patch("elobot.extensions.match.initiate_match", new=AsyncMock(side_effect=insert_match)) as initiate_match:
            player_model.find_one = AsyncMock(return_value=player)
            player_model.find_all.return_value.to_list = AsyncMock(return_value=[player, opponent])

            # When
            matched_outcome = await match_commands.queue_player(self.bot, member, priority=True)

        # Then
        self.assertEqual(matched_outcome.status, match_commands.QueueOutcomeStatus.MATCHED)
        initiate_match.assert_awaited_once_with(self.bot, player, opponent, on_inserted=ANY)

        # Given
        beta_bot = MagicMock()
        beta_bot.user.id = BOT_ID + 1
        beta_member = self.make_member(789)
        beta_player = self.make_player("789", 1000)
        beta_queues = self.queue_fixture(beta_player)
        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: beta_queues[tier]), \
                patch("elobot.extensions.match.is_player_occupied", new=AsyncMock(return_value=None)):
            player_model.find_one = AsyncMock(return_value=beta_player)
            # When
            beta_outcome = await match_commands.queue_player(beta_bot, beta_member, priority=False)

        # Then
        self.assertEqual(beta_outcome.status, match_commands.QueueOutcomeStatus.BETA_QUEUED)
        self.assertIn(beta_player, beta_queues[QUEUE_TIER_MIXED].players)

    async def test_slash_adapter_and_core_share_normal_queue_persistence(self):
        # Given
        player = self.make_player("123", 1000)
        member = self.make_member()
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = member
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        queues = self.queue_fixture(player)
        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: queues[tier]), \
                patch("elobot.extensions.match.is_player_occupied", new=AsyncMock(return_value=None)):
            player_model.find_one = AsyncMock(return_value=player)
            # When
            await self.unbound_callback(MatchCommands.match_queue.callback)(self.command_owner, ctx)

        # Then
        self.assertIn(player, queues[QUEUE_TIER_NORMAL].players)
        self.assertIn(player.user_id, queues[QUEUE_TIER_NORMAL].entered_at)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        ctx.send.assert_awaited_once()

    async def test_slash_adapter_preserves_occupied_message_and_component(self):
        member = self.make_member()
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = member
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        component = MagicMock(spec=ipy.Button)
        occupied = match_commands.PlayerOccupied("unfinished", component)
        with patch("elobot.extensions.match.queue_player", new=AsyncMock(return_value=match_commands.QueueOutcome(
                match_commands.QueueOutcomeStatus.OCCUPIED,
                occupied,
        ))):
            await self.unbound_callback(MatchCommands.match_queue.callback)(self.command_owner, ctx)

        ctx.send.assert_awaited_once_with(
            f"{match_commands.ERROR_EMOJI} unfinished",
            components=component,
            ephemeral=True,
        )

    async def test_four_player_trigger_returns_matched_after_refetch(self):
        player = self.make_player("123", 1000)
        member = self.make_member()
        waiting_players = [self.make_player(str(user_id), 1500) for user_id in (456, 789, 987)]
        queues = self.queue_fixture(
            player,
            tier3_players=waiting_players,
            tier3_entered_at={waiting.user_id: datetime.now().timestamp() for waiting in waiting_players},
        )

        async def remove_queued_player(_bot):
            queues[QUEUE_TIER_NORMAL].players.remove(player)
            queues[QUEUE_TIER_NORMAL].entered_at.pop(player.user_id)

        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: queues[tier]), \
                patch("elobot.extensions.match.is_player_occupied", new=AsyncMock(return_value=None)), \
                patch("elobot.extensions.match.run_queue_matching_trigger", new=AsyncMock(side_effect=remove_queued_player)) as trigger:
            player_model.find_one = AsyncMock(return_value=player)
            outcome = await match_commands.queue_player(self.bot, member, priority=False)

        self.assertEqual(outcome.status, match_commands.QueueOutcomeStatus.MATCHED)
        trigger.assert_awaited_once()

    async def test_queue_operation_lock_serializes_two_local_callers(self):
        # Given
        player = self.make_player("123", 1000)
        member = self.make_member()
        queues = self.queue_fixture(player)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_find_one(*_args, **_kwargs):
            started.set()
            async with asyncio.timeout(5):
                await release.wait()
            return player

        with patch("elobot.extensions.match.Player") as player_model, \
                patch.object(ServerState, "current", new=AsyncMock(return_value=MagicMock(queue_disabled=False))), \
                patch("elobot.extensions.match.get_match_queue", side_effect=lambda tier: queues[tier]), \
                patch("elobot.extensions.match.is_player_occupied", new=AsyncMock(return_value=None)):
            player_model.find_one = AsyncMock(side_effect=blocked_find_one)
            # When
            async with asyncio.timeout(5):
                first = asyncio.create_task(match_commands.queue_player(self.bot, member, priority=False))
                await started.wait()
                second = asyncio.create_task(match_commands.queue_player(self.bot, member, priority=False))
                release.set()
                first_outcome, second_outcome = await asyncio.gather(first, second)

        # Then
        self.assertEqual({first_outcome.status, second_outcome.status}, {
            match_commands.QueueOutcomeStatus.QUEUED,
            match_commands.QueueOutcomeStatus.ALREADY_QUEUED,
        })
        self.assertEqual([queued.user_id for queued in queues[QUEUE_TIER_NORMAL].players], [player.user_id])

    async def test_scheduled_matching_uses_queue_operation_lock(self):
        lock = asyncio.Lock()
        with patch("elobot.extensions.match.QUEUE_OPERATION_LOCK", lock), \
                patch("elobot.extensions.match.run_queue_matching_trigger", new=AsyncMock()) as trigger, \
                patch.object(ServerState, "update_fields", new=AsyncMock()):
            async with lock:
                callback = self.unbound_callback(MatchCommands.create_matches.callback)
                task = asyncio.create_task(callback(self.command_owner))
                await asyncio.sleep(0)
                trigger.assert_not_awaited()
            async with asyncio.timeout(5):
                await task

        trigger.assert_awaited_once()
