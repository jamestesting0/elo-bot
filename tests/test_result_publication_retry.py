from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

import cogs.match_commands as match_commands
from common.discord import message_edits
from tests._result_finalization_fixture import ResultFinalizationFixture


class TestResultPublicationRetry(ResultFinalizationFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.edit_time = 0.0

        async def advance_edit_time(delay: float) -> None:
            self.edit_time += delay

        self.enterContext(patch.object(message_edits, "_now", side_effect=lambda: self.edit_time))
        self.enterContext(patch.object(message_edits, "_sleep", new=advance_edit_time))
        self.summary = MagicMock(id=901, edit=AsyncMock())
        self.public = MagicMock(id=902, edit=AsyncMock())
        self.thread.send.return_value = self.summary
        self.match_channel.send.return_value = self.public
        self.match_channel.fetch_message = AsyncMock(return_value=self.public)
        self.first = MagicMock(id=77, embeds=[ipy.Embed(title="**Match Started**", description="Updating…")])
        self.first.edit = AsyncMock()
        self.second = MagicMock(id=78, embeds=[ipy.Embed(title="**Match Started**", description="Updating…")])
        self.second.edit = AsyncMock()
        self.match.old_msgs = [77, 78]
        messages = {77: self.first, 78: self.second, 901: self.summary}
        self.thread.fetch_message.side_effect = lambda message_id: messages[message_id]
        self.ctx = MagicMock(message=None, send=AsyncMock())

    async def test_failed_war_retirement_stays_unpublished_and_retry_reuses_summary(self) -> None:
        self.second.edit.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        self.assertFalse(await self.cog._finalize_match_results_locked("TOKEN", ctx=self.ctx))
        self.assertTrue(self.match.result_operation.completed)
        self.assertFalse(self.match.result_operation.published)
        self.assertEqual(self.match.result_operation.summary_message_id, 901)
        self.assertEqual(self.match.msg, "902")
        self.assertIn("recorded", self.ctx.send.await_args.args[0])
        self.second.edit.side_effect = None
        with patch.object(match_commands, "completed_result_changes", new=AsyncMock(return_value=("+5", "-5"))):
            self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN"))
        self.assertTrue(self.match.result_operation.published)
        self.record.assert_awaited_once()
        self.record_bases.assert_awaited_once()
        self.award_helper.assert_awaited_once()
        self.thread.send.assert_awaited_once()
        self.match_channel.send.assert_awaited_once()
        self.summary.edit.assert_awaited()
        self.assertNotIn("components", self.summary.edit.await_args.kwargs)
        self.assertNotIn("components", self.public.edit.await_args.kwargs)
        self.assertNotIn("components", self.first.edit.await_args.kwargs)
        self.assertNotIn("components", self.second.edit.await_args.kwargs)
        self.assertEqual(self.second.edit.await_args.kwargs["embeds"][0]["description"], "")

    async def test_failed_live_retirement_does_not_claim_publication(self) -> None:
        self.match.is_war = False
        self.match.old_msgs = [77, None]
        self.first.edit.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        self.assertFalse(await self.cog._finalize_match_results_locked("TOKEN", ctx=self.ctx))
        self.assertFalse(self.match.result_operation.published)
        self.assertTrue(self.match.result_operation.completed)

    async def test_summary_binding_failure_does_not_mark_published(self) -> None:
        from pymongo.errors import PyMongoError

        original = self.operation_collection.update_one.return_value
        self.operation_collection.update_one.side_effect = [original, original, PyMongoError("write failed")]
        self.assertFalse(await self.cog._finalize_match_results_locked("TOKEN", ctx=self.ctx))
        self.assertTrue(self.match.result_operation.completed)
        self.assertFalse(self.match.result_operation.published)
        self.assertIsNone(self.match.result_operation.summary_message_id)
        self.match_channel.send.assert_not_awaited()
        self.assertEqual(self.summary.edit.await_args.kwargs["embed"].title, "**Result Publication Incomplete**")
        self.assertEqual(self.summary.edit.await_args.kwargs["components"], [])
        self.assertIn("incomplete", self.ctx.send.await_args.args[0])

    async def test_existing_match_announcement_is_not_reused(self) -> None:
        self.match.msg = "80"
        self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN"))
        self.match_channel.fetch_message.assert_not_awaited()
        self.match_channel.send.assert_awaited_once()
        self.public.edit.assert_not_awaited()
        self.assertEqual(self.match.result_operation.public_message_id, 902)
        self.assertEqual(self.match.msg, "902")

    async def test_public_edit_failure_retries_without_another_summary(self) -> None:
        self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN"))
        with anyio.fail_after(5):
            await self.thread_renamed.wait()
        self.cog._pending_match_thread_renames.pop("TOKEN", None)
        self.thread.send.reset_mock()
        self.match_channel.send.reset_mock()
        self.public.edit.reset_mock()
        self.match.result_operation.published = False
        self.public.edit.side_effect = [
            ipy.errors.HTTPException(response=MagicMock(status=500)), None,
        ]
        with patch.object(match_commands, "completed_result_changes", new=AsyncMock(return_value=("+5", "-5"))):
            self.assertFalse(await self.cog._finalize_match_results_locked("TOKEN"))
            self.assertEqual(self.public.edit.await_args.kwargs["embed"].title, "**Result Publication Incomplete**")
            self.public.edit.side_effect = None
            self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN"))
        self.thread.send.assert_not_awaited()
        self.match_channel.send.assert_not_awaited()
        self.record.assert_awaited_once()
        self.assertTrue(self.match.result_operation.published)

    async def test_deleted_initiation_message_does_not_block_publication(self) -> None:
        self.match.old_msgs = [77, None]
        self.thread.fetch_message.side_effect = ipy.errors.NotFound(response=MagicMock(status=404))
        self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN"))
        self.assertTrue(self.match.result_operation.published)

    async def test_stored_side_http_failure_keeps_cleanup_and_logs_recovery(self) -> None:
        self.first.edit.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        with patch.object(match_commands, "free_clan", new_callable=AsyncMock) as free_clan:
            with self.assertLogs(level="ERROR") as logs:
                await self.cog._finish_stored_result_side(self.match, 0)
        free_clan.assert_awaited_once()
        self.autoqueue.assert_awaited_once()
        self.assertIn("pending recovery", logs.output[0])

    async def test_stored_side_stale_message_does_not_edit_new_run(self) -> None:
        snapshot = self.match.model_copy(deep=True)
        self.match.old_msgs[0] = 99
        with patch.object(match_commands, "free_clan", new_callable=AsyncMock):
            await self.cog._finish_stored_result_side(snapshot, 0)
        self.thread.fetch_message.assert_not_awaited()
        self.first.edit.assert_not_awaited()
