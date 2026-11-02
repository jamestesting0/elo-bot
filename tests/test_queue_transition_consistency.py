import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField, LinkInfo, LinkTypes
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

from cogs import match_commands
from database_models import Match, Player, Queue, ServerState


class TestQueueTransitionConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model in (Queue, Player, Match):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
                state_management_replace_objects=getattr(model.Settings, "state_management_replace_objects", False),
            )))
        for model, field in ((Queue, "tier"), (Player, "user_id")):
            self.enterContext(patch.object(model, field, ExpressionField(field), create=True))
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=SimpleNamespace(name="players")))
        self.enterContext(patch.object(Queue, "_link_fields", {
            "players": LinkInfo(field_name="players", lookup_field_name="players", document_class=Player,
                                link_type=LinkTypes.LIST),
        }))
        self.players = {
            user_id: Player.model_construct(
                id=user_id, user_id=user_id, tag="#TAG", elo=elo, highest_elo=elo,
            ) for user_id, elo in (("111", 1000), ("222", 2000), ("333", 1000), ("444", 1000))
        }
        for player in self.players.values():
            player._save_state()
        self.queues = {
            tier: Queue.model_construct(id=PydanticObjectId(), tier=tier, players=[], entered_at={})
            for tier in match_commands.QUEUE_TIERS
        }
        self.failed_tier: int | None = None
        self.pause_dequeue = False
        self.write_started = anyio.Event()
        self.release_write = anyio.Event()
        self.events: list[str] = []
        self.inserted: list[Match] = []
        self.bot = MagicMock(user=MagicMock(id=match_commands.BOT_ID), fetch_user=AsyncMock())
        self.bot.fetch_user.side_effect = lambda user_id: MagicMock(id=int(user_id), send=AsyncMock())
        self.cog = match_commands.MatchCommands(self.bot)
        self.enterContext(patch.object(match_commands, "QUEUE_OPERATION_LOCK", anyio.Lock()))

        async def find_queue(query, **_kwargs) -> Queue:
            return self.snapshot(query["tier"])

        async def persist_queue(document: Queue, updates, **_kwargs) -> Queue:
            if document.tier == self.failed_tier:
                self.failed_tier = None
                raise OperationFailure("injected queue save failure")
            if self.pause_dequeue and document.tier == 3 and not document.players:
                self.pause_dequeue = False
                self.write_started.set()
                await self.release_write.wait()
            target = self.queues[document.tier]
            for field, value in updates.items():
                if field == "players":
                    target.players = [self.players[reference.id].model_copy(deep=True) for reference in value]
                else:
                    setattr(target, field, deepcopy(value))
            target._save_state()
            self.events.append(f"save-{document.tier}")
            return document

        self.enterContext(patch.object(Queue, "find_one", new=AsyncMock(side_effect=find_queue)))
        self.enterContext(patch.object(Queue, "set", new=persist_queue))

        async def persist_player(document: Player, updates, **_kwargs) -> Player:
            for field, value in updates.items():
                setattr(self.players[document.user_id], field, deepcopy(value))
            self.players[document.user_id]._save_state()
            return document

        self.enterContext(patch.object(Player, "set", new=persist_player))
        self.enterContext(patch.object(Player, "find_one", new=AsyncMock(
            side_effect=lambda query, **_kwargs: self.players[query["user_id"]].model_copy(deep=True),
        )))
        self.enterContext(patch.object(ServerState, "current", new=AsyncMock(return_value=SimpleNamespace(queue_disabled=False))))

        async def insert_match(document: Match, **_kwargs) -> Match:
            document.id = PydanticObjectId()
            self.inserted.append(document.model_copy(deep=True))
            self.events.append("insert-match")
            return document

        def find_matches(query):
            return MagicMock(to_list=AsyncMock(return_value=[
                match.model_copy(deep=True) for match in self.inserted
                if any(p.user_id == query["participants.user_id"] for p in match.participants)
            ]))

        self.enterContext(patch.object(Match, "insert", new=insert_match))
        self.enterContext(patch.object(Match, "find", side_effect=find_matches))

    def seed(self, tier: int, *user_ids: str, timestamp: float = 100.0) -> None:
        queue = self.queues[tier]
        queue.players = [self.players[user_id].model_copy(deep=True) for user_id in user_ids]
        queue.entered_at = dict.fromkeys(user_ids, timestamp)
        queue._save_state()

    def snapshot(self, tier: int) -> Queue:
        queue = self.queues[tier].model_copy(deep=True)
        queue._save_state()
        return queue

    def memberships(self, user_id: str) -> list[int]:
        return [tier for tier, queue in self.queues.items() if any(p.user_id == user_id for p in queue.players)]

    async def test_dequeue_cannot_erase_a_concurrent_successful_join(self) -> None:
        # Given: dequeue is paused at its database write while another player joins.
        self.seed(3, "111")
        self.pause_dequeue = True
        ctx = MagicMock(author=MagicMock(id=111), send=AsyncMock())
        author = MagicMock(spec=ipy.Member, id=222)
        author.has_role.return_value = False

        async def dequeue() -> None:
            await self.cog.match_dequeue.callback.func(self.cog, ctx)

        async def join() -> None:
            await match_commands.queue_player(self.bot, author)

        # When: both operations complete under a deterministic interleaving.
        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(dequeue)
                await self.write_started.wait()
                tasks.start_soon(join)
                await anyio.wait_all_tasks_blocked()
                self.release_write.set()
        # Then: leaving cannot erase the successful new membership.
        self.assertEqual(self.memberships("111"), [])
        self.assertEqual(self.memberships("222"), [3])

    async def test_promotion_destination_failure_preserves_source_membership(self) -> None:
        # Given: an aged player and a failed destination write.
        self.seed(1, "111")
        self.failed_tier = 0
        # When
        with self.assertRaises(OperationFailure):
            await match_commands.promote_tier1_aged_players(self.snapshot(0), self.snapshot(1), 100_000.0)
        # Then: a failed transition cannot remove the only persisted membership.
        self.assertEqual(self.memberships("111"), [1])
        self.assertEqual(self.queues[1].entered_at, {"111": 100.0})

    async def test_failed_priority_queue_write_preserves_the_priority_award(self) -> None:
        # Given: a stored priority award and a failing tier-2 membership write.
        self.players["111"].priority = True
        self.players["111"]._save_state()
        self.failed_tier = 2
        author = MagicMock(spec=ipy.Member, id=111)
        author.has_role.return_value = False
        # When
        with self.assertRaises(OperationFailure):
            await match_commands.queue_player(self.bot, author)
        # Then: an unqueued player retains the award for retry.
        self.assertEqual(self.memberships("111"), [])
        self.assertTrue(self.players["111"].priority)

    async def test_tier_move_destination_failure_preserves_source_membership(self) -> None:
        # Given
        self.seed(3, "111")
        self.failed_tier = 2
        # When
        with self.assertRaises(OperationFailure):
            await match_commands.move_queue_tier(3, 2, 1000.0)
        # Then
        self.assertEqual(self.memberships("111"), [3])
        self.assertEqual(self.queues[3].entered_at, {"111": 100.0})

    async def test_interrupted_source_removal_retries_without_resetting_destination_age(self) -> None:
        # Given: destination membership was saved before source removal failed.
        self.seed(3, "111")
        self.failed_tier = 3
        with self.assertRaises(OperationFailure):
            await match_commands.move_queue_tier(3, 2, 1000.0)
        self.assertTrue(self.memberships("111"))
        # When
        await match_commands.move_queue_tier(3, 2, 2000.0)
        # Then: the original promotion age and exactly one membership survive retry.
        self.assertEqual(self.memberships("111"), [2])
        self.assertEqual(self.queues[2].entered_at, {"111": 1000.0})

    async def test_dequeue_clears_every_copy_of_an_interrupted_transfer(self) -> None:
        # Given
        self.seed(1, "111")
        self.seed(2, "111")
        ctx = MagicMock(author=MagicMock(id=111), send=AsyncMock())
        # When
        await self.cog.match_dequeue.callback.func(self.cog, ctx)
        # Then
        self.assertEqual(self.memberships("111"), [])

    async def test_preinsert_match_failure_preserves_queued_opponent(self) -> None:
        # Given: Discord fails before initiate_match can insert a Match document.
        self.seed(3, "333")
        self.bot.fetch_user.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        inserted = AsyncMock()
        # When
        with patch.object(Match, "insert", new=inserted), self.assertRaises(ipy.errors.HTTPException):
            await match_commands.match_with_best_queue(self.bot, self.players["111"], None, [(self.snapshot(3), 200)])
        # Then: the unmatched opponent remains queued with its original age.
        inserted.assert_not_awaited()
        self.assertEqual(self.memberships("333"), [3])
        self.assertEqual(self.queues[3].entered_at, {"333": 100.0})

    async def test_duplicate_tier_memberships_are_removed_before_the_next_matching_cycle(self) -> None:
        # Given: an interrupted destination-first transfer left the same player in two tiers.
        self.seed(1, "111", "333", timestamp=3_000_000_000.0)
        self.seed(2, "111", "444", timestamp=3_000_000_000_000.0)
        # When
        await match_commands.run_queue_matching_trigger(self.bot)
        # Then: that player participates in exactly one match and is no longer queued.
        appearances = sum(any(p.user_id == "111" for p in match.participants) for match in self.inserted)
        self.assertEqual(appearances, 1)
        self.assertEqual(self.memberships("111"), [])

    async def test_postinsert_queue_failure_does_not_allow_a_second_match_on_retry(self) -> None:
        # Given: Match.insert succeeds but removing the queued opponent fails.
        self.seed(3, "333")
        self.failed_tier = 3
        with self.assertRaises(OperationFailure):
            await match_commands.match_with_best_queue(self.bot, self.players["111"], None, [(self.snapshot(3), 200)])
        self.assertEqual(len(self.inserted), 1)
        self.assertEqual(self.memberships("333"), [3])
        # When: another player attempts to match against the interrupted queue entry.
        matched = await match_commands.match_with_best_queue(
            self.bot, self.players["444"], None, [(self.snapshot(3), 200)],
        )
        # Then: the existing match is retained and its opponent cannot be matched twice.
        self.assertFalse(matched)
        self.assertEqual(len(self.inserted), 1)
        self.assertEqual(self.memberships("333"), [])

    async def test_notification_failure_keeps_the_inserted_match_out_of_queue(self) -> None:
        # Given: the first DM fails after Match insertion.
        self.seed(3, "333")
        failed_send = AsyncMock(side_effect=ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable")))
        self.bot.fetch_user.side_effect = lambda user_id: MagicMock(id=int(user_id), send=failed_send)
        # When
        with self.assertRaises(ipy.errors.HTTPException):
            await match_commands.match_with_best_queue(self.bot, self.players["111"], None, [(self.snapshot(3), 200)])
        # Then: the durable match owns both participants despite the notification error.
        self.assertEqual(len(self.inserted), 1)
        self.assertEqual(self.memberships("333"), [])

    async def test_later_batch_failure_keeps_the_completed_pair_out_of_the_queue(self) -> None:
        # Given: the second match fails before insertion, after the first pair was inserted.
        self.players["222"].elo = 1000
        self.seed(3, "111", "333", "222", "444")
        users = {user_id: MagicMock(id=int(user_id), send=AsyncMock()) for user_id in self.players}

        async def fetch_user(user_id: str):
            if user_id == "222":
                raise ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
            return users[user_id]

        self.bot.fetch_user.side_effect = fetch_user
        # When
        with self.assertRaises(ipy.errors.HTTPException):
            await match_commands.match_players(self.bot, 3, 200)
        # Then: only the uncreated pair remains queued.
        self.assertEqual(len(self.inserted), 1)
        self.assertEqual(self.memberships("111"), [])
        self.assertEqual(self.memberships("333"), [])
        self.assertEqual(self.memberships("222"), [3])
        self.assertEqual(self.memberships("444"), [3])
