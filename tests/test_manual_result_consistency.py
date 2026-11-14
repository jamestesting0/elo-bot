import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
import anyio
from beanie.odm.fields import ExpressionField
from pymongo.errors import OperationFailure
from pymongo.results import UpdateResult

from elo_bot.features.players.data_boundary import PLAYER_DATA_BOUNDARY
from elo_bot.extensions import data as data_commands
from elo_bot.db import Match, RecordedMatch
from tests import _result_retry_store as fixture
from tests._result_retry_store import apply_operations, matches


class TestManualResultConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        self.state = fixture.ResultRetryState(self)
        self.state.setUp()
        self.enterContext(patch.object(Match, "find_one", new=AsyncMock(return_value=None)))
        self.ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        self.ctx.author.id = 999
        self.users = [MagicMock(id=111), MagicMock(id=222)]
        self.before_record_update = lambda _query, _updates: None

        async def find_record(query, **_kwargs):
            return next((record.model_copy(deep=True) for record in self.state.state.records.values()
                         if matches(record.model_dump(by_alias=True), query)), None)

        async def update_record(query, updates, **_kwargs):
            self.before_record_update(query, updates)
            record = await find_record(query)
            if record is None:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            document = deepcopy(record.model_dump(by_alias=True))
            apply_operations(document, updates)
            self.state.state.records[record.id] = RecordedMatch.model_validate(document)
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        collection = MagicMock(update_one=AsyncMock(side_effect=update_record))
        collection.name = "recorded_matches"
        self.enterContext(patch.object(RecordedMatch, "find_one", new=AsyncMock(side_effect=find_record)))
        self.enterContext(patch.object(RecordedMatch, "get_pymongo_collection", return_value=collection))

    async def invoke(self) -> None:
        self.state.state.player_collection.update_one.reset_mock()
        await self.state.cog.match_submit.callback.func(
            self.state.cog, self.ctx, *self.users, fixture.RESULTS["player1"],
            fixture.RESULTS["player2"], *fixture.DISTRICTS,
        )

    def assert_one_completed_record(self) -> None:
        self.assertEqual(set(self.state.state.records), {"10", "11"})
        self.assertEqual([player["elo"] for player in self.state.state.players.values()], [1033, 972])
        self.assertTrue(self.state.state.records["11"].manual_submission_completed)

    async def test_invitation_waits_for_the_manual_completion_marker(self) -> None:
        async def invite(bot, user_ids):
            self.assertIs(bot, self.state.state.bot)
            self.assertEqual(user_ids, ("111", "222"))
            self.assert_one_completed_record()
            self.assertTrue(all(
                self.state.state.records["11"].to_ref() in player["matches"]
                for player in self.state.state.players.values()
            ))

        self.state.state.invite.side_effect = invite
        await self.invoke()
        self.state.state.invite.assert_awaited_once()

    async def test_failed_manual_completion_marker_does_not_invite_until_retry(self) -> None:
        def fail_completion(_query, _updates):
            raise OperationFailure("injected manual completion checkpoint failure")

        self.before_record_update = fail_completion
        await self.invoke()
        self.assertFalse(self.state.state.records["11"].manual_submission_completed)
        self.state.state.invite.assert_not_awaited()
        self.before_record_update = lambda _query, _updates: None
        await self.invoke()
        self.assert_one_completed_record()
        self.state.state.invite.assert_awaited_once()

    async def test_retry_after_second_player_failure_resumes_original_record(self) -> None:
        # Given: the first player commits before the second player's write fails.
        def fail_second_player(query, updates):
            if query.get("user_id") == "222" and "$inc" in updates:
                self.state.state.before_player_update = lambda _query, _updates: None
                raise OperationFailure("synthetic second player failure")
        self.state.state.before_player_update = fail_second_player
        # When
        await self.invoke()
        # Then: failure is explicit and repeating the same command resumes its record.
        self.assertNotIn("success", self.ctx.send.await_args.args[0])
        self.assertIn("Retry", self.ctx.send.await_args.args[0])
        await self.invoke()
        self.assert_one_completed_record()

    async def test_retry_after_insert_acknowledgement_loss_reuses_saved_record(self) -> None:
        # Given: the record was inserted but the acknowledgement was lost.
        original_insert = RecordedMatch.insert

        async def lose_ack(document, **kwargs):
            await original_insert(document, **kwargs)
            raise OperationFailure("synthetic insert acknowledgement failure")
        # When
        with patch.object(RecordedMatch, "insert", new=lose_ack):
            await self.invoke()
        await self.invoke()
        # Then
        self.assert_one_completed_record()

    async def test_retry_after_discord_failure_does_not_duplicate_ratings(self) -> None:
        # Given: persistence completes before a player notification fails.
        self.state.state.users[0].send.side_effect = [
            ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable")), None,
        ]
        # When
        await self.invoke()
        await self.invoke()
        # Then
        self.assert_one_completed_record()

    async def test_completed_identical_manual_submission_can_be_intentional_new_match(self) -> None:
        # Given
        await self.invoke()
        # When: an operator intentionally records another identical match.
        await self.invoke()
        # Then: only unfinished submissions are deduplicated.
        self.assertEqual(set(self.state.state.records), {"10", "11", "12"})
        self.assertTrue(all(record.manual_submission_completed for key, record in self.state.state.records.items()
                            if key != "10"))

    async def test_completion_acknowledgement_loss_verifies_saved_success(self) -> None:
        def lose_completion_ack(_query, _updates):
            self.state.state.records["11"].manual_submission_completed = True
            raise OperationFailure("synthetic completion acknowledgement failure")
        self.before_record_update = lose_completion_ack
        await self.invoke()
        self.assert_one_completed_record()
        self.assertIn("success", self.ctx.send.await_args.args[0])

    async def test_unverifiable_completion_does_not_recommend_blind_retry(self) -> None:
        original_find = RecordedMatch.find_one

        async def unavailable_completion_read(query, **kwargs):
            if query.get("_id") == "11":
                raise OperationFailure("synthetic read failure")
            return await original_find(query, **kwargs)

        def lose_completion_ack(_query, _updates):
            self.state.state.records["11"].manual_submission_completed = True
            raise OperationFailure("synthetic completion acknowledgement failure")
        self.before_record_update = lose_completion_ack
        with patch.object(RecordedMatch, "find_one", new=unavailable_completion_read):
            await self.invoke()
        response = self.ctx.send.await_args.args[0]
        self.assertIn("ID `11`", response)
        self.assertIn("Check that record", response)
        self.assertNotIn("continue it safely", response)
        self.assert_one_completed_record()

    async def test_request_waiting_before_reset_cannot_create_a_new_record(self) -> None:
        async def reset_while_deferring(**_kwargs):
            async with PLAYER_DATA_BOUNDARY.reset():
                pass
        self.ctx.defer.side_effect = reset_while_deferring
        await self.invoke()
        self.assertEqual(set(self.state.state.records), {"10"})
        self.assertEqual([player["elo"] for player in self.state.state.players.values()], [1000, 1000])
        self.assertIn("reset", self.ctx.send.await_args.args[0])

    async def test_stale_delete_preserves_identical_completed_manual_replacement(self) -> None:
        self.enterContext(patch.object(RecordedMatch, "id", ExpressionField("_id"), create=True))
        await self.invoke()
        original = self.state.state.records["11"]
        data = data_commands.DataCommands(self.state.state.bot)
        stale_read, release_read = anyio.Event(), anyio.Event()
        original_find = RecordedMatch.find_one
        stale_task = None
        delayed = False

        async def delay_initial_read(query, **kwargs):
            nonlocal delayed
            snapshot = await original_find(query, **kwargs)
            if anyio.get_current_task().id == stale_task and not delayed:
                delayed = True
                stale_read.set()
                await release_read.wait()
            return snapshot

        self.enterContext(patch.object(RecordedMatch, "find_one", new=AsyncMock(side_effect=delay_initial_read)))
        stale_ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())

        async def stale_delete():
            nonlocal stale_task
            stale_task = anyio.get_current_task().id
            await data.data_delete_match.callback.func(data, stale_ctx, "11")

        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(stale_delete)
                await stale_read.wait()
                await data.data_delete_match.callback.func(
                    data, MagicMock(defer=AsyncMock(), send=AsyncMock()), "11",
                )
                await self.invoke()
                replacement = self.state.state.records["11"]
                self.assertEqual(replacement.manual_submission_key, original.manual_submission_key)
                self.assertNotEqual(replacement.operation_key, original.operation_key)
                release_read.set()
        self.assert_one_completed_record()
        self.assertIn("already deleted or replaced", stale_ctx.send.await_args.args[0])
