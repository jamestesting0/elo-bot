import unittest
from unittest.mock import AsyncMock, MagicMock, call

import anyio
import interactions as ipy

from elobot.config.ids import MAIN_GUILD
from elobot.config.emojis import STAR_EMOJI
from elobot.db import HelperHistory, Match
from elobot.db.result_models import MatchResultOperation
from tests._result_finalization_fixture import (
    PLAYER2_RESULTS, VALID_RESULTS, RecordArgument, ResultFinalizationFixture,
    make_result_match, record_result, release_result_stores,
)
from tests._patch_sites import patch_sites


class TestResultFinalization(ResultFinalizationFixture):

    def _assert_published(self, match: Match) -> None:
        operation = match.result_operation
        assert operation is not None
        assert operation.record is not None
        self.assertEqual(operation.record.operation_key, f"{match.id}:{operation.key}")
        self.assertEqual(operation.response, {
            "player1": "\n".join(VALID_RESULTS), "player2": "\n".join(PLAYER2_RESULTS),
        })
        self.assertTrue(operation.completed)
        self.assertTrue(operation.published)
        self.assertEqual(match.recorded_match_id, "1")
        initial_operation = operation.model_copy(update={
            "record": None, "completed": False, "published": False,
            "summary_message_id": None, "public_message_id": None,
        })
        self.assertEqual(self.operation_collection.update_one.await_args_list, [
            call(
                {"_id": match.id, "result_operation.key": None,
                 "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
                 "participants.0.result": VALID_RESULTS, "participants.1.result": PLAYER2_RESULTS},
                {"$set": {"result_operation": initial_operation.model_dump(),
                          "participants.0.result": VALID_RESULTS, "participants.1.result": PLAYER2_RESULTS}},
            ),
            call(
                {
                    "_id": match.id, "result_operation.key": operation.key,
                    "result_operation.cancelled": {"$ne": True},
                    "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
                },
                {"$set": {"result_operation.completed": True, "recorded_match_id": "1"}},
            ),
            call(
                {"_id": match.id, "result_operation.key": operation.key, "result_operation.completed": True,
                 "result_operation.cancelled": {"$ne": True}, "result_operation.summary_message_id": None},
                {"$set": {"result_operation.summary_message_id": 901}},
            ),
            call(
                {"_id": match.id, "result_operation.key": operation.key, "result_operation.completed": True,
                 "result_operation.cancelled": {"$ne": True}, "msg": None},
                {"$set": {"msg": "902", "result_operation.public_message_id": 902}},
            ),
            call(
                {"_id": match.id, "result_operation.key": operation.key, "result_operation.completed": True,
                 "result_operation.cancelled": {"$ne": True},
                 "participants.0.base_store": None, "participants.1.base_store": None,
                 "participants.0.opponent_bases_stored": {"$ne": True},
                 "participants.1.opponent_bases_stored": {"$ne": True}},
                {"$set": {"result_operation.published": True}},
            ),
        ])
        self.clear_stores.assert_awaited_once_with(match, reset_stored_flags=True, bot=self.bot, reason="finished")
        self.assertEqual([item[0] for item in self.lifecycle.mock_calls], [
            "checkpoint", "award_helper", "record_bases", "record", "checkpoint",
            "clear_stores", "free_clans", "checkpoint", "checkpoint", "checkpoint",
        ])
        self.assertTrue(all(participant.base_store is None for participant in match.participants))
        self.save_changes.assert_not_awaited()
        self.free_clans.assert_awaited_once_with(match)
        self.record_bases.assert_awaited_once()
        self.record.assert_awaited_once()
        self.player_model.find_one.assert_not_called()
        self.award_helper.assert_awaited_once_with(
            "999", f"result:{match.id}:host:0", 0.5,
            HelperHistory(time=123, player1="111", player2=None),
        )
        self.autoqueue.assert_awaited_once_with(self.bot, "TOKEN", ("111", "222"))
        self.assertEqual(
            self.thread.send.await_args.kwargs["embed"].description,
            f"**[Player 111](<https://discordapp.com/users/111>)** (+5) vs. "
            f"**[Player 222](<https://discordapp.com/users/222>)** (-5)\n\n**Final Score**\n"
            f"6{STAR_EMOJI} 270% - 5{STAR_EMOJI} 250%",
        )

    async def test_manual_and_automatic_race_records_once(self) -> None:
        match = self.match
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "result_modal|TOKEN"
        ctx.message = None
        ctx.send = AsyncMock()
        response = {"player1": "\n".join(VALID_RESULTS), "player2": "\n".join(PLAYER2_RESULTS)}
        record_started = anyio.Event()
        release_record = anyio.Event()
        automatic_started = anyio.Event()

        async def record_once(*_args: RecordArgument) -> tuple[str, str]:
            record_started.set()
            await release_record.wait()
            return record_result(match)

        self.record.side_effect = record_once

        async def finalize_automatically() -> None:
            automatic_started.set()
            await self.cog._finalize_match_results("TOKEN")

        with anyio.fail_after(5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(self.cog._process_match_results, ctx, response)
                await record_started.wait()
                task_group.start_soon(finalize_automatically)
                await automatic_started.wait()
                release_record.set()
            await self.thread_renamed.wait()
        self._assert_published(match)
        self.thread.edit.assert_awaited_once()

    async def test_dm_context_uses_fetched_guild_and_match_thread(self) -> None:
        match = self.match
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.guild = None
        ctx.channel = MagicMock(name="dm-channel")
        ctx.message = None
        ctx.send = AsyncMock()

        finalized = await self.cog._finalize_match_results("TOKEN", ctx=ctx)

        self.assertTrue(finalized)
        self._assert_published(match)
        with anyio.fail_after(5):
            await self.thread_renamed.wait()
        self.bot.fetch_guild.assert_awaited_once_with(MAIN_GUILD)
        self.assertIs(self.record.await_args.args[0], self.guild)
        self.thread.send.assert_awaited_once()
        action_rows = self.thread.send.await_args.kwargs["components"]
        self.assertEqual(
            [(button.label, button.custom_id) for button in action_rows[0].components],
            [("Delete Thread", "delete_thread|TOKEN")],
        )
        self.thread.edit.assert_awaited_once()

    async def test_helper_submit_button_records_complete_results(self) -> None:
        match = self.match
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "result_modal|TOKEN"
        ctx.message = None
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        response = {"player1": "\n".join(VALID_RESULTS), "player2": "\n".join(PLAYER2_RESULTS)}

        async def send_success(message: str, *, ephemeral: bool) -> None:
            self._assert_published(match)
            self.assertIn("submitted and recorded", message)
            self.assertTrue(ephemeral)

        ctx.send.side_effect = send_success

        finalized = await self.cog._process_match_results(ctx, response)

        self.assertTrue(finalized)
        self._assert_published(match)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_automatic_submission_records_complete_stored_results(self) -> None:
        finalized = await self.cog._finalize_match_results("TOKEN")

        self.assertTrue(finalized)
        self._assert_published(self.match)

    async def test_bookkeeping_failure_preserves_both_stored_slots(self) -> None:
        for boundary in (self.award_helper, self.record_bases, self.record, self.operation_collection.update_one):
            with self.subTest(boundary=boundary._mock_name):
                self.lifecycle.reset_mock()
                self.match = make_result_match(stored_bases=True)
                stores = [participant.base_store.model_copy(deep=True) for participant in self.match.participants]
                previous = boundary.side_effect
                failure = RuntimeError("bookkeeping failed")
                boundary.side_effect = (
                    [boundary.return_value, failure] if boundary is self.operation_collection.update_one else failure
                )
                with self.assertRaisesRegex(RuntimeError, "bookkeeping failed"):
                    await self.cog._finalize_match_results("TOKEN")
                boundary.side_effect = previous
                self.assertEqual([participant.base_store for participant in self.match.participants], stores)
                self.clear_stores.assert_not_awaited()
                self.free_clans.assert_not_awaited()
                self.thread.send.assert_not_awaited()
                assert self.match.result_operation is not None
                self.assertFalse(self.match.result_operation.completed)
                self.assertFalse(self.match.result_operation.published)

    async def test_completed_retry_releases_slots_before_publishing_without_repeating_bookkeeping(self) -> None:
        self.match.result_operation = MatchResultOperation(
            key="completed", response={"player1": "\n".join(VALID_RESULTS), "player2": "\n".join(PLAYER2_RESULTS)},
            created_at=123, completed=True,
        )
        record_result(self.match)
        self.match.recorded_match_id = "1"
        self.clear_stores.side_effect = RuntimeError("release failed")
        with patch_sites(('elobot.extensions.match.results.completed_result_changes', 'elobot.features.results.recording.completed_result_changes'), new=AsyncMock(return_value=("+5", "-5"))):
            with self.assertRaisesRegex(RuntimeError, "release failed"):
                await self.cog._finalize_match_results("TOKEN")
            self.thread.send.assert_not_awaited()
            self.assertFalse(self.match.result_operation.published)
            self.assertTrue(all(participant.base_store is not None for participant in self.match.participants))
            self.clear_stores.side_effect = release_result_stores
            self.assertTrue(await self.cog._finalize_match_results("TOKEN"))
        self.assertEqual(self.clear_stores.await_args_list, [
            call(self.match, reset_stored_flags=True, bot=self.bot, reason="finished"),
            call(self.match, reset_stored_flags=True, bot=self.bot, reason="finished"),
        ])
        self.assertEqual([item[0] for item in self.lifecycle.mock_calls], [
            "clear_stores", "clear_stores", "free_clans", "checkpoint", "checkpoint", "checkpoint",
        ])
        self.award_helper.assert_not_awaited()
        self.record_bases.assert_not_awaited()
        self.record.assert_not_awaited()
        self.assertTrue(self.match.result_operation.published)
        self.assertTrue(all(participant.base_store is None for participant in self.match.participants))
        self.assertEqual(self.operation_collection.update_one.await_count, 3)
        self.operation_collection.update_one.assert_awaited_with(
            {"_id": self.match.id, "result_operation.key": "completed", "result_operation.completed": True,
             "result_operation.cancelled": {"$ne": True},
             "participants.0.base_store": None, "participants.1.base_store": None,
             "participants.0.opponent_bases_stored": {"$ne": True},
             "participants.1.opponent_bases_stored": {"$ne": True}},
            {"$set": {"result_operation.published": True}},
        )

    async def test_finalization_returns_before_thread_rename_finishes(self) -> None:
        # Given a completed match whose channel rename remains rate-limited
        match = self.match
        rename_started, release_rename, rename_finished = self._delay_rename()

        # When result finalization reaches the terminal channel rename
        with anyio.fail_after(1):
            finalized = await self.cog._finalize_match_results("TOKEN")

        # Then the command finishes while the rename continues in the background
        self.assertTrue(finalized)
        self._assert_published(match)
        with anyio.fail_after(1):
            await rename_started.wait()
        self.assertFalse(rename_finished.is_set())
        release_rename.set()
        with anyio.fail_after(1):
            await rename_finished.wait()

    async def test_duplicate_finalization_is_rejected_while_thread_rename_is_pending(self) -> None:
        # Given a completed match whose background channel rename is still pending
        match = self.match
        rename_started, release_rename, rename_finished = self._delay_rename()

        # When the same result is finalized again before that rename completes
        first_finalized = await self.cog._finalize_match_results("TOKEN")
        with anyio.fail_after(1):
            await rename_started.wait()
        second_finalized = await self.cog._finalize_match_results("TOKEN")
        rename_completed = anyio.Event()
        self.cog._pending_match_thread_renames["TOKEN"].add_done_callback(lambda _: rename_completed.set())
        release_rename.set()
        with anyio.fail_after(1):
            await rename_finished.wait()
            await rename_completed.wait()

        # Then the result is recorded only once
        self.assertTrue(first_finalized)
        self.assertFalse(second_finalized)
        self._assert_published(match)
        self.assertNotIn("TOKEN", self.cog._pending_match_thread_renames)

    async def test_incomplete_stored_result_blocks_all_side_effects(self) -> None:
        self.match = make_result_match(complete=False)
        finalized = await self.cog._finalize_match_results("TOKEN")

        self.assertFalse(finalized)
        self.save_changes.assert_not_awaited()
        self.free_clans.assert_not_awaited()
        self.record_bases.assert_not_awaited()
        self.record.assert_not_awaited()
        self.award_helper.assert_not_awaited()
        self.clear_stores.assert_not_awaited()
        self.operation_collection.update_one.assert_not_awaited()
        self.autoqueue.assert_not_awaited()

    async def test_missing_match_thread_is_rejected_before_channel_fetch(self) -> None:
        match = self.match
        match.thread = None
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.send = AsyncMock()
        finalized = await self.cog._finalize_match_results("TOKEN", ctx=ctx)

        self.assertFalse(finalized)
        self.bot.fetch_channel.assert_not_awaited()
        self.save_changes.assert_not_awaited()
        self.assertIn("match is invalid", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
