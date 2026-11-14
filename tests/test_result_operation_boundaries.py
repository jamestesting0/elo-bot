import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from pymongo.errors import OperationFailure

from elobot.extensions import data as data_commands
from elobot.features.hosting.war import result_store
from elobot.features.hosting.war.helper_store import HelperScoreStoreOutcome
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.features.results.operations import ResultOperationConflict, begin_result_operation
from elobot.db import Match
from tests import _result_retry_store as retry_fixture


class TestResultOperationBoundaries(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = retry_fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.data = data_commands.DataCommands(self.runtime.state.bot)
        self.runtime.match["old_msgs"] = [99, None]
        self.runtime.state.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@111>"))

    def store_context(self) -> MagicMock:
        return MagicMock(custom_id="store_modal|TOKEN|0", author=MagicMock(id=999), defer=AsyncMock(), send=AsyncMock(),
            message=MagicMock(id=99, embeds=[ipy.Embed(title="**Match Ended**")], edit=AsyncMock()))

    async def delete_record(self, ctx: MagicMock) -> None:
        await self.data.data_delete_match.callback.func(self.data, ctx, "11")

    async def test_pending_record_deleted_by_an_operator_is_not_recreated_on_retry(self) -> None:
        # Given: Player 1 commits, Player 2 fails, then an operator deletes that pending record.
        def fail_second(query, updates) -> None:
            if query.get("user_id") == "222" and "$addToSet" in updates:
                self.runtime.state.before_player_update = lambda _query, _updates: None
                raise OperationFailure("injected pending result failure")
        self.runtime.state.before_player_update = fail_second
        with self.assertRaises(OperationFailure):
            await self.runtime.cog._finalize_match_results("TOKEN", retry_fixture.RESULTS)
        await self.delete_record(MagicMock(defer=AsyncMock(), send=AsyncMock()))
        self.runtime.state.player_collection.update_one.reset_mock()
        ctx = MagicMock(send=AsyncMock(), message=None)
        # When
        await self.runtime.cog._finalize_match_results("TOKEN", retry_fixture.RESULTS, ctx)
        # Then: deletion remains authoritative and the retry cannot report terminal success.
        self.assertEqual(set(self.runtime.state.records), {"10"})
        self.assertEqual([player["elo"] for player in self.runtime.state.players.values()], [1000, 1000])
        self.assertFalse(any(call.args[0].startswith(str(SUCCESS_EMOJI)) for call in ctx.send.await_args_list))
        self.assertIn("deleted and will not be recreated", ctx.send.await_args.args[0])
        self.assertTrue(self.runtime.match["result_operation"]["cancelled"])

    async def test_operator_deletion_waits_for_an_inflight_result_writer(self) -> None:
        # Given: finalization holds the token while its second player write is suspended.
        writer_paused, release_writer, delete_started, delete_finished = (anyio.Event() for _ in range(4))
        original_update = self.runtime.state.update_player
        async def paused_update(query, updates, **kwargs):
            if query.get("user_id") == "222" and "$addToSet" in updates:
                writer_paused.set()
                await release_writer.wait()
            return await original_update(query, updates, **kwargs)
        self.runtime.state.player_collection.update_one.side_effect = paused_update
        ctx = MagicMock(defer=AsyncMock(side_effect=lambda **_kwargs: delete_started.set()), send=AsyncMock())
        async def delete() -> None:
            await self.delete_record(ctx)
            delete_finished.set()
        # When
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.runtime.cog._finalize_match_results, "TOKEN", retry_fixture.RESULTS)
                await writer_paused.wait()
                tasks.start_soon(delete)
                await delete_started.wait()
                deletion_waited = not delete_finished.is_set()
                release_writer.set()
        # Then: deletion follows the completed write and removes both links and rating changes.
        self.assertTrue(deletion_waited)
        self.assertEqual(set(self.runtime.state.records), {"10"})
        self.assertEqual([player["elo"] for player in self.runtime.state.players.values()], [1000, 1000])

    async def test_already_open_modal_is_rejected_after_finalization_claims_the_result(self) -> None:
        # Given: a current result modal is submitted while finalization is awaiting its guild lookup.
        ctx = self.store_context()
        async def correct_during_guild_lookup(_guild):
            await self.runtime.cog.result_store_modal.callback.func(self.runtime.cog, ctx,
                result1="2.90", result2="3.100", result3="3.100")
            return self.runtime.state.bot.fetch_guild.return_value
        self.runtime.state.bot.fetch_guild.side_effect = correct_during_guild_lookup
        # When
        await self.runtime.cog._finalize_match_results("TOKEN", retry_fixture.RESULTS)
        # Then: the stale modal cannot claim its correction was saved and then have it overwritten.
        self.assertFalse(any(call.args[0].startswith(str(SUCCESS_EMOJI)) for call in ctx.send.await_args_list))
        self.assertEqual(self.runtime.match["participants"][0]["result"], ["3.100"] * 3)

    async def test_saved_correction_wins_if_it_commits_before_the_finalizer_claim(self) -> None:
        # Given: the result correction commits between the finalizer's read and operation claim.
        ctx = self.store_context()
        async def correct_before_claim(_query, updates) -> None:
            if "result_operation" in updates.get("$set", {}):
                self.runtime.before_match_write = AsyncMock()
                await self.runtime.cog.result_store_modal.callback.func(self.runtime.cog, ctx,
                    result1="2.90", result2="3.100", result3="3.100")
        self.runtime.before_match_write = correct_before_claim
        response = deepcopy(retry_fixture.RESULTS)
        # When
        finalized = await self.runtime.cog._finalize_match_results("TOKEN", response)
        # Then: finalization loses its stale claim and the acknowledged correction remains persisted.
        self.assertFalse(finalized)
        self.assertEqual(self.runtime.match["participants"][0]["result"], ["2.90", "3.100", "3.100"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_stored_modal_checks_only_player_after_persisting_their_results(self) -> None:
        ctx = self.store_context()
        self.runtime.match["participants"][0]["result"] = [None, None, None]
        self.runtime.match["participants"][1]["result"] = [None, None, None]
        observed_results = []

        async def check_after_write(_bot, user_ids):
            observed_results.append((user_ids, list(self.runtime.match["participants"][0]["result"])))

        self.runtime.state.invite.side_effect = check_after_write
        await self.runtime.cog.result_store_modal.callback.func(
            self.runtime.cog, ctx, result1="2.90", result2="3.100", result3="3.100",
        )
        self.assertEqual(observed_results, [(("111",), ["2.90", "3.100", "3.100"])])
        self.assertEqual(self.runtime.match["participants"][1]["result"], [None, None, None])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_result_button_explains_pending_recorded_and_deleted_operations_before_opening(self) -> None:
        # Given: the same current result button can outlive the operation's state transitions.
        match = await Match.find_one({"token": "TOKEN"})
        await begin_result_operation(match, retry_fixture.RESULTS)
        for completed, cancelled, expected in (
                (False, False, "being recorded"),
                (True, False, "/match resubmit"),
                (True, True, "deleted and will not be recreated"),
        ):
            with self.subTest(completed=completed, cancelled=cancelled):
                self.runtime.match["result_operation"].update(completed=completed, cancelled=cancelled)
                ctx = self.store_context()
                ctx.custom_id = "match_result_store|TOKEN|0"
                ctx.send_modal = AsyncMock()
                # When
                await self.runtime.cog.result_store_button.callback.func(self.runtime.cog, ctx)
                # Then
                ctx.send_modal.assert_not_awaited()
                self.assertIn(expected, ctx.send.await_args.args[0])

    async def claim_before_result_write(self, _query, updates) -> None:
        if any(field.startswith("participants.") for field in updates.get("$set", {})):
            self.runtime.before_match_write = AsyncMock()
            match = await Match.find_one({"token": "TOKEN"})
            await begin_result_operation(match, retry_fixture.RESULTS)

    async def test_slash_storage_loses_to_a_result_claim_at_the_write_boundary(self) -> None:
        # Given: the slash callback reads an editable result before finalization claims it.
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(), author=MagicMock(id=999), channel=MagicMock(id=321))
        self.runtime.before_match_write = self.claim_before_result_write
        # When
        await self.runtime.cog.match_result_store.callback.func(self.runtime.cog, ctx,
            player=0, district=retry_fixture.DISTRICTS[0], stars=2, destruction=90)
        # Then: the operation's input remains authoritative and no score-storage success is sent.
        self.assertEqual(self.runtime.match["participants"][0]["result"], ["3.100"] * 3)
        self.assertIn("being recorded", ctx.send.await_args.args[0])
        self.assertFalse(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_helper_storage_loses_to_a_result_claim_at_the_write_boundary(self) -> None:
        # Given: Helper returns complete scores while finalization claims the match before their write.
        self.runtime.match["is_war"] = True
        self.runtime.match["participants"][0]["result"] = [None, None, None]
        match = await Match.find_one({"token": "TOKEN"})
        self.runtime.before_match_write = self.claim_before_result_write
        outcome = HelperScoreStoreOutcome(results=(None, None, None), changed=False, skipped=(), job_id="job",
            status="succeeded", summary="complete", trusted=tuple(zip(retry_fixture.DISTRICTS, ["3.100"] * 3)))
        helper = self.enterContext(patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(
            return_value=outcome)))
        ctx = MagicMock(send=AsyncMock(), author=MagicMock(id=999))
        # When
        await result_store.store_match_result_automatically(self.runtime.cog, ctx,
            result_store.AutomaticResultStoreRequest(match=match, player=0))
        # Then: the completed Helper job cannot report a rejected database write as stored.
        helper.assert_awaited_once()
        self.assertEqual(self.runtime.match["participants"][0]["result"], ["3.100"] * 3)
        self.assertIsNone(self.runtime.match["submitters"][0])
        self.assertIn("being recorded", ctx.send.await_args.args[0])

    async def test_deleting_a_different_operation_record_does_not_cancel_the_current_result(self) -> None:
        # Given: a legacy record carries another operation key while this match has a pending result.
        self.runtime.state.records["10"].operation_key = "another-match:older-operation"
        await self.runtime.cog._finalize_match_results("TOKEN", retry_fixture.RESULTS)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        # When
        await self.data.data_delete_match.callback.func(self.data, ctx, "10")
        # Then: only the explicitly selected history record is deleted.
        self.assertEqual(set(self.runtime.state.records), {"11"})
        self.assertFalse(self.runtime.match["result_operation"]["cancelled"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_cancelled_completed_retry_explains_deletion_while_rename_is_pending(self) -> None:
        await self.runtime.cog._finalize_match_results("TOKEN", retry_fixture.RESULTS)
        await self.delete_record(MagicMock(defer=AsyncMock(), send=AsyncMock()))
        ctx = MagicMock(send=AsyncMock())
        self.assertFalse(await self.runtime.cog._finalize_match_results("TOKEN", retry_fixture.RESULTS, ctx))
        self.assertIn("deleted and will not be recreated", ctx.send.await_args.args[0])

    async def test_cancelled_resubmission_cannot_replay_its_previous_rollback(self) -> None:
        corrected = {"player1": retry_fixture.RESULTS["player2"], "player2": retry_fixture.RESULTS["player1"]}
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.runtime.state.bot.fetch_guild.return_value)
        def fail_second(query, updates) -> None:
            if query.get("user_id") == "222" and "$addToSet" in updates:
                self.runtime.state.before_player_update = lambda _query, _updates: None
                raise OperationFailure("injected pending correction failure")
        self.runtime.state.before_player_update = fail_second
        with self.assertRaises(OperationFailure):
            await self.runtime.cog.resubmit_modal.callback.func(self.runtime.cog, ctx, **corrected)
        await self.delete_record(MagicMock(defer=AsyncMock(), send=AsyncMock()))
        players = deepcopy(self.runtime.state.players)
        self.runtime.state.player_collection.update_one.reset_mock()
        await self.runtime.cog.resubmit_modal.callback.func(self.runtime.cog, ctx, **corrected)
        self.runtime.state.player_collection.update_one.assert_not_awaited()
        self.assertEqual(self.runtime.state.players, players)
        self.assertEqual(set(self.runtime.state.records), {"10"})
        self.assertIn("deleted and will not be recreated", ctx.send.await_args.args[0])

    async def test_missing_token_match_cannot_fall_back_to_standalone_recording(self) -> None:
        self.runtime.match["token"] = "OTHER"
        with self.assertRaises(ResultOperationConflict):
            await self.runtime.invoke_record()
        self.assertEqual(set(self.runtime.state.records), {"10"})
        self.runtime.state.player_collection.update_one.assert_not_awaited()
